from __future__ import annotations

import json
import os
import re
import signal
import shutil
import time
import queue as queue_mod
import multiprocessing as mp
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import yfinance as yf


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRICE_CACHE_ROOT = PROJECT_ROOT / "cache" / "prices"
PREFETCH_BATCH_SIZE = int(os.environ.get("PREFETCH_BATCH_SIZE", "200"))
YFINANCE_TIMEOUT = int(os.environ.get("YFINANCE_TIMEOUT", "20"))
YFINANCE_WALL_TIMEOUT = int(os.environ.get("YFINANCE_WALL_TIMEOUT", str(max(30, YFINANCE_TIMEOUT + 10))))
YFINANCE_THREADS = os.environ.get("YFINANCE_THREADS", "false").strip().lower() in {"1", "true", "yes", "on"}
CACHE_KEEP_DAYS = 3
# T-K修正(2026-08-02): Yahooのレート制限で空になった銘柄を、待ってから小さいバッチで取り直す。
PREFETCH_RETRY_ROUNDS = int(os.environ.get("PREFETCH_RETRY_ROUNDS", "2"))
PREFETCH_RETRY_WAIT = int(os.environ.get("PREFETCH_RETRY_WAIT", "20"))

# T-K修正(2026-08-02 その2): Yahooのレート制限そのものを避けるための自動減速。
# 2026-08-02のrun #153では3,544銘柄中2,146銘柄が
# YFRateLimitError('Too Many Requests')で空になった。原因は取得の出しすぎ。
# 何件/秒で怒られるかはYahoo側の非公開仕様で、こちらから測れない。
# そこで固定値を決め打ちせず、空振り率を見て自分で速度を落とす。
# PREFETCH_ADAPTIVE=0 で無効化（従来動作）。
PREFETCH_ADAPTIVE = os.environ.get("PREFETCH_ADAPTIVE", "1").strip().lower() not in {"0", "false", "no", "off"}
PREFETCH_BATCH_PAUSE = float(os.environ.get("PREFETCH_BATCH_PAUSE", "0"))
PREFETCH_MAX_PAUSE = float(os.environ.get("PREFETCH_MAX_PAUSE", "45"))
# このバッチの空振り率がこれ以上なら「出しすぎ」とみなして減速する。
PREFETCH_SLOW_RATIO = float(os.environ.get("PREFETCH_SLOW_RATIO", "0.5"))
# 何回続けて空振りゼロなら速度を戻すか。1にすると増減を繰り返して壁に当たり続ける。
PREFETCH_SPEEDUP_AFTER = int(os.environ.get("PREFETCH_SPEEDUP_AFTER", "3"))

# T-K修正(2026-08-02): 空データの記憶はプロセス内のみ。ディスクには残さない。
_EMPTY_THIS_PROCESS: set[tuple[str, str]] = set()

# T-K修正(2026-08-03): 学習した待ち時間をスキャン間で引き継ぐ。
# ザラ場中は60秒ごとに別プロセスで起動し直すため、そのままだと毎回0秒から
# 学習をやり直す。0->5->10->20->40と登り直す間のバッチはほぼ全部空振りになり、
# 時間を捨てるうえにYahooをさらに怒らせる。run #154では1スキャン24分かかった。
# 空ファイル名を渡せば無効化できる（PREFETCH_PAUSE_STATE=""）。
PREFETCH_PAUSE_STATE = os.environ.get("PREFETCH_PAUSE_STATE", "outputs/.prefetch_pause.json").strip()
# 古い学習値は捨てる。前場の混雑を後場まで引きずらないため。
PREFETCH_PAUSE_TTL = float(os.environ.get("PREFETCH_PAUSE_TTL", "1800"))


def _load_learned_pause() -> float:
    """前回スキャンが学習した待ち時間を読む。読めなければ既定値。"""
    if not PREFETCH_ADAPTIVE or not PREFETCH_PAUSE_STATE:
        return PREFETCH_BATCH_PAUSE
    try:
        path = Path(PREFETCH_PAUSE_STATE)
        if not path.exists():
            return PREFETCH_BATCH_PAUSE
        data = json.loads(path.read_text(encoding="utf-8"))
        # 保存時刻はファイルの中に持つ。アーティファクト経由で復元されると
        # 更新時刻が「復元した瞬間」になり、古い値が新しく見えてしまうため。
        written_at = float(data.get("ts", 0.0))
        if time.time() - written_at > PREFETCH_PAUSE_TTL:
            return PREFETCH_BATCH_PAUSE
        saved = float(data["sec"])
    except Exception:
        return PREFETCH_BATCH_PAUSE
    if not saved > PREFETCH_BATCH_PAUSE:
        return PREFETCH_BATCH_PAUSE
    return min(saved, PREFETCH_MAX_PAUSE)


def _save_learned_pause(sec: float) -> None:
    """次のスキャンのために待ち時間を残す。失敗しても本処理は止めない。"""
    if not PREFETCH_ADAPTIVE or not PREFETCH_PAUSE_STATE:
        return
    try:
        path = Path(PREFETCH_PAUSE_STATE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"sec": float(sec), "ts": time.time()}), encoding="utf-8"
        )
    except Exception:
        return


def _cache_enabled() -> bool:
    return os.environ.get("PRICE_CACHE_DISABLE", "").strip().lower() not in {"1", "true", "yes"}


def _cache_refresh_enabled() -> bool:
    """毎回キャッシュを取り直すか（ザラ場のリアルタイム監視用）。"""
    return os.environ.get("PRICE_CACHE_REFRESH", "").strip().lower() in {"1", "true", "yes", "on"}


@contextmanager
def _wall_timeout(seconds: int, label: str):
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def _raise_timeout(_signum, _frame):
        raise TimeoutError(f"{label} exceeded {seconds}s")

    old_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def _cache_dir(period: str, run_date: date | None = None) -> Path:
    run_date = run_date or date.today()
    safe_period = re.sub(r"[^A-Za-z0-9]+", "_", str(period))
    # T-K修正(2026-07-12): "__raw" = 未調整価格(auto_adjust=False)のキャッシュ。
    # 旧・配当調整済みキャッシュ（サフィックスなし）と混在しないよう名前で分離する。
    return PRICE_CACHE_ROOT / f"{run_date.isoformat()}__{safe_period}__raw"


def _cache_path(ticker: str, period: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(ticker))
    return _cache_dir(period) / f"{safe}.parquet"


def _empty_marker_path(cache_path: Path) -> Path:
    return cache_path.with_suffix(".empty")


def _save_price_cache(df: pd.DataFrame, cache_path: Path, save_empty_marker: bool = True) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if df is None or df.empty:
        if save_empty_marker:
            _empty_marker_path(cache_path).touch()
        return
    try:
        df.to_parquet(cache_path)
    except Exception:
        try:
            df.to_pickle(cache_path.with_suffix(".pkl"))
        except Exception:
            pass


def _read_price_cache(cache_path: Path) -> pd.DataFrame | None:
    """キャッシュ読込。ヒットなしはNone、空マーカーは空DataFrameを返す。"""
    if _empty_marker_path(cache_path).exists():
        return pd.DataFrame()
    if cache_path.exists():
        try:
            return pd.read_parquet(cache_path)
        except Exception:
            pass
    pkl_path = cache_path.with_suffix(".pkl")
    if pkl_path.exists():
        try:
            return pd.read_pickle(pkl_path)
        except Exception:
            pass
    return None


def cleanup_old_price_cache(keep_days: int = CACHE_KEEP_DAYS) -> None:
    """当日を含む直近keep_days日より古い日付ディレクトリを削除する。"""
    if not PRICE_CACHE_ROOT.exists():
        return
    today = date.today()
    for entry in PRICE_CACHE_ROOT.iterdir():
        if not entry.is_dir():
            continue
        date_part = entry.name.split("__", 1)[0]
        try:
            entry_date = date.fromisoformat(date_part)
        except ValueError:
            continue
        if (today - entry_date).days >= keep_days:
            shutil.rmtree(entry, ignore_errors=True)


def _split_batch_frame(batch: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if batch is None or batch.empty:
        return pd.DataFrame()
    if isinstance(batch.columns, pd.MultiIndex):
        top = batch.columns.get_level_values(0)
        if ticker in set(top):
            sub = batch[ticker]
        else:
            return pd.DataFrame()
    else:
        sub = batch
    sub = sub.dropna(how="all")
    return sub


def _download_worker(queue, target, kwargs) -> None:
    try:
        data = yf.download(target, **kwargs)
        queue.put(("ok", data))
    except Exception as exc:  # noqa: BLE001 - 子プロセスから親へ理由を返す
        queue.put(("error", repr(exc)))


def _download_with_process(target, *, label: str, wall_timeout: int, **kwargs) -> pd.DataFrame:
    """Run yfinance in a child process so libcurl hangs cannot stop the workflow."""
    if wall_timeout <= 0:
        return yf.download(target, **kwargs)

    ctx = mp.get_context("fork") if "fork" in mp.get_all_start_methods() else mp.get_context()
    queue = ctx.Queue(maxsize=1)
    proc = ctx.Process(target=_download_worker, args=(queue, target, kwargs), daemon=True)
    proc.start()
    # T-K修正(2026-08-02): 親は「先にqueueを読み出してから」子の終了を待つ。
    # 旧実装は proc.join(wall_timeout) → queue.get() の順だった。
    # 子がOSのパイプバッファ(Linuxで約64KB)を超えるDataFrameをput()すると、
    # 親が読み出すまで子は書き込み途中でブロックしたまま終了できない。
    # 親は join() で待ち続けるため、互いに相手待ちになりデッドロックする。
    # 結果、複数銘柄バッチ(数百KB)は通信状態と無関係に必ずTimeoutErrorになっていた。
    # 単一銘柄(約20KB)だけが偶然バッファに収まって成功していた。
    try:
        status, payload = queue.get(timeout=wall_timeout)
    except queue_mod.Empty:
        proc.terminate()
        proc.join(5)
        raise TimeoutError(f"{label} exceeded {wall_timeout}s")
    # ペイロードを受け取った後なら子は速やかに終了できる。
    proc.join(10)
    if proc.is_alive():
        proc.terminate()
        proc.join(5)
    if status == "ok":
        return payload
    raise RuntimeError(f"{label} failed: {payload}")


def prefetch_price_histories(
    tickers: list[str],
    period: str = "18mo",
    batch_size: int = PREFETCH_BATCH_SIZE,
) -> dict[str, int]:
    """複数銘柄をバッチ取得してキャッシュに保存する。

    既にキャッシュ済みの銘柄はスキップ。戻り値は集計 {"cached", "fetched", "empty"}。
    PRICE_CACHE_REFRESH=1 のときはキャッシュを無視して取り直し、上書きする。
    """
    stats = {"cached": 0, "fetched": 0, "empty": 0, "failed_batches": 0, "failed_tickers": 0}
    if not tickers or not _cache_enabled():
        return stats

    cleanup_old_price_cache()

    # T-K修正(2026-08-02): ザラ場監視ではキャッシュを日単位で使い回すと
    # 2回目以降のスキャンが1回目の価格を読み続け、その後の高値更新を
    # 一切検知できなくなる。PRICE_CACHE_REFRESH=1 のときは毎スキャン取り直す。
    # （バッチ取得のキャッシュ自体は1スキャン内の重複取得を防ぐため残す）
    refresh = _cache_refresh_enabled()
    pending: list[str] = []
    seen: set[str] = set()
    for ticker in tickers:
        ticker = str(ticker).strip()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        if not refresh and _read_price_cache(_cache_path(ticker, period)) is not None:
            stats["cached"] += 1
        else:
            pending.append(ticker)

    # T-K修正(2026-08-02 その2): バッチ間の待ち時間。空振りが多い間だけ自動で伸ばす。
    pause_state = {"sec": _load_learned_pause(), "slowdowns": 0, "clean": 0}
    if pause_state["sec"] > PREFETCH_BATCH_PAUSE:
        print(
            f"price_prefetch resume pause={pause_state['sec']:.0f}s (前回スキャンの学習値)",
            flush=True,
        )

    def _adapt(tag: str, start: int, chunk_len: int, empty_in_batch: int) -> None:
        """このバッチの空振り率を見て、次のバッチまでの待ちを増減する。"""
        if not PREFETCH_ADAPTIVE or chunk_len <= 0:
            return
        ratio = empty_in_batch / chunk_len
        if ratio >= PREFETCH_SLOW_RATIO:
            pause_state["clean"] = 0
            before = pause_state["sec"]
            pause_state["sec"] = min(max(before * 2, 5.0), PREFETCH_MAX_PAUSE)
            pause_state["slowdowns"] += 1
            if pause_state["sec"] != before:
                print(
                    f"price_prefetch{tag} slowdown start={start} empty={empty_in_batch}/{chunk_len} "
                    f"pause={before:.0f}s->{pause_state['sec']:.0f}s",
                    flush=True,
                )
            return
        if ratio > 0.0:
            # 少しでも空振りがあるうちは今の速度を維持する（戻すのは完全に通ったときだけ）。
            pause_state["clean"] = 0
            return
        pause_state["clean"] += 1
        if pause_state["clean"] < PREFETCH_SPEEDUP_AFTER:
            return
        pause_state["clean"] = 0
        if pause_state["sec"] > PREFETCH_BATCH_PAUSE:
            before = pause_state["sec"]
            pause_state["sec"] = max(before / 2, PREFETCH_BATCH_PAUSE)
            print(
                f"price_prefetch{tag} speedup start={start} pause={before:.0f}s->{pause_state['sec']:.0f}s",
                flush=True,
            )

    def _run_pass(targets: list[str], *, size: int, tag: str) -> list[str]:
        """1巡分のバッチ取得。値が取れなかった銘柄のリストを返す。"""
        missed: list[str] = []
        for start in range(0, len(targets), size):
            chunk = targets[start:start + size]
            # 直前のバッチで空振りが多かったぶんだけ間を空けてから次を投げる。
            if start > 0 and pause_state["sec"] > 0:
                time.sleep(pause_state["sec"])
            empty_in_batch = 0
            batch_failed = False
            try:
                batch = _download_with_process(
                    chunk,
                    label=f"price_prefetch{tag} start={start} size={len(chunk)}",
                    wall_timeout=YFINANCE_WALL_TIMEOUT,
                    period=period,
                    interval="1d",
                    # T-K修正(2026-07-12): カブタン整合のため未調整価格を使う。
                    # 配当調整済み設定（旧実装）は過去の高値を配当分だけ下方修正するため、
                    # 52週高値の「位置」と「距離%」がカブタン（分割調整のみ）とズレていた。
                    # False = 分割調整のみ・配当未調整 ＝ カブタンと同じ基準。
                    auto_adjust=False,
                    progress=False,
                    group_by="ticker",
                    threads=YFINANCE_THREADS,
                    timeout=YFINANCE_TIMEOUT,
                )
            except Exception as exc:
                stats["failed_batches"] += 1
                stats["failed_tickers"] += len(chunk)
                batch_failed = True
                print(
                    f"WARNING price_prefetch{tag} batch failed start={start} size={len(chunk)} timeout={YFINANCE_TIMEOUT}s error={exc}",
                    flush=True,
                )
                batch = None
            for ticker in chunk:
                if batch_failed:
                    missed.append(ticker)
                    continue
                raw = _split_batch_frame(batch, ticker) if batch is not None else pd.DataFrame()
                try:
                    normalized = normalize_price_history(raw)
                except ValueError:
                    normalized = pd.DataFrame()
                if normalized.empty:
                    missed.append(ticker)
                    empty_in_batch += 1
                    continue
                _save_price_cache(normalized, _cache_path(ticker, period), save_empty_marker=False)
                stats["fetched"] += 1
            _adapt(tag, start, len(chunk), len(chunk) if batch_failed else empty_in_batch)
        return missed

    remaining = _run_pass(pending, size=batch_size, tag="")

    # T-K修正(2026-08-02): 空になった銘柄を「待ってから小さいバッチで」取り直す。
    # 2026-08-02のrun #152では3,066銘柄取得後にYahooがレート制限を返し、
    # 8xxx〜9xxx番台の478銘柄(NTT 9432/ソフトバンクG 9984を含む)が丸ごと空になった。
    # 間を置いてバッチを小さくすると通ることが多いので、ここで取り戻す。
    for attempt in range(1, PREFETCH_RETRY_ROUNDS + 1):
        if not remaining:
            break
        wait = PREFETCH_RETRY_WAIT * attempt
        retry_size = max(20, batch_size // (2 ** attempt))
        print(
            f"price_prefetch retry#{attempt} tickers={len(remaining)} wait={wait}s batch={retry_size}",
            flush=True,
        )
        time.sleep(wait)
        before = len(remaining)
        remaining = _run_pass(remaining, size=retry_size, tag=f" retry#{attempt}")
        print(
            f"price_prefetch retry#{attempt} recovered={before - len(remaining)} still_empty={len(remaining)}",
            flush=True,
        )

    # T-K修正(2026-08-02): 再取得しても空だった銘柄は「このスキャン中は空」と記憶する。
    # これが無いと、後段のスキャンが1銘柄ずつ取り直しに行き、レート制限下では
    # 1銘柄あたりYFINANCE_WALL_TIMEOUT(120秒)まで待たされる。478銘柄なら最悪16時間で、
    # ザラ場中に1巡も終わらない。記憶はプロセス内だけなので、60秒後の次スキャン
    # （別プロセス）では改めて取りに行く。
    stats["empty"] = len(remaining)
    stats["slowdowns"] = pause_state["slowdowns"]
    _save_learned_pause(float(pause_state["sec"]))
    for ticker in remaining:
        _EMPTY_THIS_PROCESS.add((str(ticker), str(period)))
    return stats


def fetch_price_history(ticker: str, period: str = "18mo") -> pd.DataFrame:
    key = (str(ticker), str(period))
    if key in _EMPTY_THIS_PROCESS:
        return pd.DataFrame()
    if _cache_enabled():
        cache_path = _cache_path(ticker, period)
        cached = _read_price_cache(cache_path)
        if cached is not None:
            return cached if cached.empty else normalize_price_history(cached)

    df = _download_with_process(
        ticker,
        label=f"price_fetch {ticker}",
        wall_timeout=YFINANCE_WALL_TIMEOUT,
        period=period,
        interval="1d",
        # T-K修正(2026-07-12): カブタン整合（分割調整のみ・配当未調整）。上のprefetchと同一方針。
        auto_adjust=False,
        progress=False,
        timeout=YFINANCE_TIMEOUT,
    )
    result = normalize_price_history(df)
    # T-K修正(2026-08-02): 取得失敗による空データを .empty マーカーとして
    # ディスクに書かない。旧実装は一時的な失敗をその日いっぱい「データ無し」として
    # キャッシュし、同日中の再取得を全て封じていた（キャッシュ汚染）。
    # 空だった銘柄は同一プロセス内だけ記憶して重複取得を避ける。
    if result.empty:
        _EMPTY_THIS_PROCESS.add(key)
    elif _cache_enabled():
        _save_price_cache(result, _cache_path(ticker, period), save_empty_marker=False)
    return result


def normalize_price_history(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    if isinstance(df.columns, pd.MultiIndex):
        df = df.droplevel(-1, axis=1)

    needed = ["Open", "High", "Low", "Close", "Volume"]
    missing = [col for col in needed if col not in df.columns]
    if missing:
        raise ValueError(f"Price history is missing columns: {missing}")

    out = df[needed].copy()
    out.index = pd.to_datetime(out.index)
    out = out.dropna(subset=["Close", "Volume"])
    return out


def fetch_next_earnings_date(ticker: str) -> date | None:
    try:
        calendar = yf.Ticker(ticker).calendar
    except Exception:
        return None

    if calendar is None:
        return None

    value = None
    if isinstance(calendar, dict):
        value = calendar.get("Earnings Date") or calendar.get("EarningsDate")
    elif isinstance(calendar, pd.DataFrame):
        for key in ("Earnings Date", "EarningsDate"):
            if key in calendar.index:
                value = calendar.loc[key].dropna().iloc[0]
                break
            if key in calendar.columns:
                value = calendar[key].dropna().iloc[0]
                break

    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    if value is None:
        return None

    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    if isinstance(parsed, pd.DatetimeIndex):
        parsed = parsed[0]
    return parsed.date()


def ensure_output_dir(path: str | Path) -> Path:
    output_dir = Path(path)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def timestamped_csv_path(output_dir: str | Path, prefix: str = "screening_result") -> Path:
    directory = ensure_output_dir(output_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return directory / f"{prefix}_{stamp}.csv"
