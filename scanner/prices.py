from __future__ import annotations

import os
import re
import signal
import shutil
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


def _cache_enabled() -> bool:
    return os.environ.get("PRICE_CACHE_DISABLE", "").strip().lower() not in {"1", "true", "yes"}


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
    proc.join(wall_timeout)
    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        raise TimeoutError(f"{label} exceeded {wall_timeout}s")
    if queue.empty():
        raise TimeoutError(f"{label} exited without data")
    status, payload = queue.get()
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
    """
    stats = {"cached": 0, "fetched": 0, "empty": 0, "failed_batches": 0, "failed_tickers": 0}
    if not tickers or not _cache_enabled():
        return stats

    cleanup_old_price_cache()

    pending: list[str] = []
    seen: set[str] = set()
    for ticker in tickers:
        ticker = str(ticker).strip()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        if _read_price_cache(_cache_path(ticker, period)) is not None:
            stats["cached"] += 1
        else:
            pending.append(ticker)

    for start in range(0, len(pending), batch_size):
        chunk = pending[start:start + batch_size]
        batch_failed = False
        try:
            batch = _download_with_process(
                chunk,
                label=f"price_prefetch start={start} size={len(chunk)}",
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
                f"WARNING price_prefetch batch failed start={start} size={len(chunk)} timeout={YFINANCE_TIMEOUT}s error={exc}",
                flush=True,
            )
            batch = None
        for ticker in chunk:
            if batch_failed:
                continue
            raw = _split_batch_frame(batch, ticker) if batch is not None else pd.DataFrame()
            try:
                normalized = normalize_price_history(raw)
            except ValueError:
                normalized = pd.DataFrame()
            _save_price_cache(normalized, _cache_path(ticker, period), save_empty_marker=False)
            if normalized.empty:
                stats["empty"] += 1
            else:
                stats["fetched"] += 1
    return stats


def fetch_price_history(ticker: str, period: str = "18mo") -> pd.DataFrame:
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
    if _cache_enabled():
        _save_price_cache(result, _cache_path(ticker, period))
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
