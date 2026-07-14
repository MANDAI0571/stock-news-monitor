from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import pandas as pd

from jptime import jst_today

from scanner.indicators import calculate_indicators, calculate_indicators_lenient, detect_ma_touches, passes_base_filters
from scanner.openwork import add_openwork_scores
from scanner.highs import classify_high_profile, detect_52w_high_retest, detect_duke_old_high_support, detect_previous_52w_high_line_retest, high_quality_flags
from scanner.patterns import detect_cup_with_handle
from scanner.prices import fetch_next_earnings_date, fetch_price_history, prefetch_price_histories, timestamped_csv_path
from scanner.scoring import assess_earnings_window, rejection_row, score_stock
from scanner.universe import UniverseConfig, load_jpx_listed


PROJECT_ROOT = Path(__file__).resolve().parent
CAPITAL = 3_000_000


# 結果CSV / コンソール表示で使う列。
DISPLAY_COLUMNS = [
    "code",
    "name",
    "market",
    "screen_type",
    "screen_tags",
    "current_price",
    "score",
    "rank",
    "buy_reason",
    "volume_ratio_5d_20d",
    "dist_52w_high_pct",
    "dist_25ma_pct",
    "dist_200ma_pct",
    "swing_high_price",
    "swing_high_date",
    "swing_high_break_pct",
    "swing_high_break",
    "swing_high_label",
    "duke_old_high_support",
    "old_52w_high",
    "old_52w_high_date",
    "dist_to_old_52w_high_pct",
    "recent_high_after_breakout",
    "pullback_from_recent_high_pct",
    "duke_support_score",
    "duke_support_signal",
    "duke_support_rank",
    "high_type",
    "high_label",
    "high_window_days",
    "high_price",
    "high_date",
    "dist_to_high_pct",
    "days_since_52w_high",
    "ma25_rising",
    "ma75_rising",
    "ma75_gap_pct",
    "ma200_gap_pct",
    "lot_value_100",
    "max_positions_3m",
    "reason",
]


# outputs/ に必ず残す固定名の結果CSV（GitHub Actions の Artifacts 用）。
FIXED_RESULT_NAME = "screening_result.csv"
# 進捗の経過時間ログを何銘柄ごとに出すか。
PROGRESS_EVERY = int(os.environ.get("PROGRESS_EVERY", "25"))


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def resolve_symbol_limit(explicit_limit: int | None) -> int | None:
    """処理する銘柄数の上限を決める。
    優先順位: 明示の --limit > QUICK_MODE(MAX_SYMBOLS) > 制限なし(本番)。
    QUICK_MODE=true のとき MAX_SYMBOLS(既定30)件だけ処理して短時間で完了させる。"""
    if explicit_limit is not None:
        return explicit_limit
    if _env_truthy("QUICK_MODE"):
        try:
            return max(1, int(os.environ.get("MAX_SYMBOLS", "30")))
        except ValueError:
            return 30
    return None


def _log_step(label: str, seconds: float, extra: str = "") -> None:
    suffix = f" {extra}" if extra else ""
    print(f"[timing] {label}: {seconds:.1f}s{suffix}", flush=True)


def write_result_csv(
    result: pd.DataFrame,
    output_dir: str | Path,
    allow_empty_overwrite: bool = True,
) -> Path:
    """outputs/screening_result.csv を保存する（GitHub Actions の Artifacts 用）。
    候補が0件・列無しでも、表示用ヘッダーだけの空CSVを残す（ファイルが無い事態を防ぐ）。
    ただし allow_empty_overwrite=False（例外発生時など）で結果が空のときは、
    既存の正常な結果CSVを空で上書きしない（前回の正常候補を保持する）。"""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / FIXED_RESULT_NAME
    is_empty = result is None or result.empty or len(result.columns) == 0
    if is_empty:
        if not allow_empty_overwrite and path.exists():
            print(
                f"WARNING: 例外/空結果のため固定CSVは上書きしません（前回の正常結果を保持）: {path}",
                flush=True,
            )
            return path
        pd.DataFrame(columns=DISPLAY_COLUMNS).to_csv(path, index=False, encoding="utf-8-sig")
    else:
        result.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def run_screening(
    markets: tuple[str, ...],
    limit: int | None,
    output_dir: str,
    include_rejected: bool,
    max_candidates: int | None = 20,
    strict: bool = False,
) -> pd.DataFrame:
    run_started = time.perf_counter()
    # QUICK_MODE / MAX_SYMBOLS の解決（明示の --limit が最優先）。
    limit = resolve_symbol_limit(limit)
    if _env_truthy("QUICK_MODE"):
        print(f"QUICK_MODE=ON max_symbols={limit} (テスト用の軽量実行)", flush=True)

    t0 = time.perf_counter()
    universe = _load_universe(markets, output_dir)
    if not universe.empty:
        universe = universe[~universe.apply(lambda row: _is_rank_excluded_security(str(row.get("name", "")), str(row.get("market", "")), str(row.get("sector", ""))), axis=1)].reset_index(drop=True)
    _log_step("universe_load", time.perf_counter() - t0, f"rows={len(universe)}")
    if limit:
        print(f"WARNING: run_screening limit={limit} is for tests only; production must use all symbols", flush=True)
        before_limit_count = len(universe)
        universe = universe.head(limit)
        print(f"WARNING: universe limited before data fetch: before={before_limit_count} after={len(universe)}", flush=True)

    # 価格データを事前に一括取得してキャッシュへ保存（銘柄ループは fetch_price_history の
    # キャッシュヒットで高速化される。プリフェッチ失敗銘柄はループ内で従来通り単発取得）。
    t0 = time.perf_counter()
    prefetch_stats = prefetch_price_histories([str(t) for t in universe["ticker"].tolist()]) if not universe.empty else {}
    _log_step("price_prefetch", time.perf_counter() - t0, f"stats={prefetch_stats}")

    rows: list[dict[str, object]] = []
    # T-D(2026-06-28): メインの300万/ブレイク候補とは独立に、押し目(タッチ/リテスト)と
    # 高値更新(52週新高値・接近)を専用収集する。メインゲート(current>MA25/75/200 等)を通らない
    # 押し目銘柄も拾うため、ベースフィルター前に収集する。
    pullback_rows: list[dict[str, object]] = []
    highs_rows: list[dict[str, object]] = []
    retest_rows: list[dict[str, object]] = []
    total = len(universe)
    today = jst_today()

    loop_started = time.perf_counter()
    for idx, stock in enumerate(universe.itertuples(index=False), start=1):
        print(f"[{idx}/{total}] {stock.ticker} {stock.name}", flush=True)
        if PROGRESS_EVERY > 0 and idx % PROGRESS_EVERY == 0:
            elapsed = time.perf_counter() - loop_started
            rate = elapsed / idx if idx else 0.0
            eta = rate * (total - idx)
            print(f"[timing] progress {idx}/{total} elapsed={elapsed:.0f}s eta={eta:.0f}s", flush=True)
        row_base = {
            "code": stock.code,
            "ticker": stock.ticker,
            "name": stock.name,
            "market": stock.market,
            "sector": stock.sector,
        }

        try:
            history = fetch_price_history(stock.ticker)
            indicators = calculate_indicators(history)
            high_info = classify_high_profile(history)
            if indicators is None:
                # 上場1年未満などlen<252でも、カブタン同様に52週(上場来)高値リストへは載せる。
                lenient = calculate_indicators_lenient(history)
                if lenient is not None:
                    highs_extra = _collect_highs_row(row_base, lenient, high_info, history)
                    if highs_extra is not None:
                        highs_extra["earnings_date"] = _earnings_date_text(stock.ticker)
                        _finalize_highs_row(highs_extra, stock.ticker)
                        highs_rows.append(highs_extra)
                if include_rejected:
                    rows.append(row_base | high_info | rejection_row(None, "価格データ不足"))
                continue

            duke_support = detect_duke_old_high_support(history, indicators)

            # T-D: 押し目(タッチ/リテスト)・高値更新の専用収集（メインゲートとは独立）。
            pullback_extra = _collect_pullback_row(row_base, indicators, high_info, history)
            if pullback_extra is not None:
                pullback_rows.append(pullback_extra)
            retest_extra = _collect_previous_52w_retest_row(row_base, indicators, history)
            if retest_extra is not None:
                retest_rows.append(retest_extra)
            highs_extra = _collect_highs_row(row_base, indicators, high_info, history)
            if highs_extra is not None:
                highs_rows.append(highs_extra)

            # 決算日: 専用リストに載った銘柄だけ取得（全銘柄への問い合わせは避ける）。
            if pullback_extra is not None or retest_extra is not None or highs_extra is not None:
                earnings_text = _earnings_date_text(stock.ticker)
                for extra in (pullback_extra, retest_extra, highs_extra):
                    if extra is not None:
                        extra["earnings_date"] = earnings_text
            if highs_extra is not None:
                # T-K: note1本目用のファンダ取得（ヒット銘柄のみ）＋異常値チェック
                _finalize_highs_row(highs_extra, stock.ticker)

            passed, reject_reasons = passes_base_filters(indicators)
            if not passed:
                if include_rejected:
                    rows.append(row_base | format_indicators(indicators) | high_info | duke_support | rejection_row(indicators, " / ".join(reject_reasons)))
                continue

            earnings_date = fetch_next_earnings_date(stock.ticker)
            earnings = assess_earnings_window(today, earnings_date)
            if earnings["exclude_for_earnings"]:
                if include_rejected:
                    rows.append(
                        row_base
                        | format_indicators(indicators)
                        | high_info
                        | duke_support
                        | earnings
                        | rejection_row(indicators, "決算14営業日前〜決算翌営業日のため除外")
                    )
                continue

            cwh = detect_cup_with_handle(history["Close"])
            scored = score_stock(indicators, cwh, earnings, capital=CAPITAL, name=stock.name, sector=stock.sector, strict=strict, duke_support=duke_support)
            rows.append(
                row_base
                | format_indicators(indicators)
                | high_info
                | duke_support
                | format_cwh(cwh)
                | earnings
                | scored
            )
        except Exception as exc:
            if include_rejected:
                rows.append(row_base | rejection_row(None, f"エラー: {exc}"))

    _log_step("scan_loop", time.perf_counter() - loop_started, f"symbols={total} rows={len(rows)}")
    result = pd.DataFrame(rows)
    # 専用CSVはメイン候補の有無に依存させない。該当0件なら書かない。
    _write_aux_csv(pullback_rows, output_dir, "screening_pullback")
    _write_aux_csv(highs_rows, output_dir, "screening_highs")
    _write_aux_csv(retest_rows, output_dir, "screening_52w_retest")
    if result.empty:
        _print_screening_summary(total, result, highs_rows, pullback_rows, retest_rows)
        _log_step("run_screening_total", time.perf_counter() - run_started, "candidates=0")
        return result

    if "dist_52w_high_pct" not in result.columns:
        result["dist_52w_high_pct"] = 999
    result["dist_52w_high_pct"] = pd.to_numeric(result["dist_52w_high_pct"], errors="coerce").fillna(999)
    if "high_type" not in result.columns:
        result["high_type"] = "OTHER"
    if "high_label" not in result.columns:
        result["high_label"] = "分類外"
    if "high_window_days" not in result.columns:
        result["high_window_days"] = 0
    if "high_price" not in result.columns:
        result["high_price"] = ""
    if "high_date" not in result.columns:
        result["high_date"] = ""
    if "dist_to_high_pct" not in result.columns:
        result["dist_to_high_pct"] = 999
    result["dist_to_high_pct"] = pd.to_numeric(result["dist_to_high_pct"], errors="coerce").fillna(999)

    for column, default in {
        "swing_high_price": "",
        "swing_high_date": "",
        "swing_high_break_pct": "",
        "swing_high_break": False,
        "swing_high_label": "",
        "duke_old_high_support": False,
        "old_52w_high": "",
        "old_52w_high_date": "",
        "dist_to_old_52w_high_pct": "",
        "recent_high_after_breakout": "",
        "pullback_from_recent_high_pct": "",
        "duke_support_score": 0,
        "duke_support_signal": False,
        "duke_support_rank": "見送り",
    }.items():
        if column not in result.columns:
            result[column] = default

    result = _normalize_screening_schema(result)

    rank_order = {"S": 0, "A": 1, "B": 2, "C": 3, "SKIP": 4}
    result["_rank_order"] = result["rank"].map(rank_order).fillna(9)
    result["_high_priority"] = result.apply(_high_priority, axis=1)
    result = result.sort_values(["_high_priority", "_rank_order", "score", "dist_52w_high_pct"], ascending=[True, True, False, True])
    result = result.drop(columns=["_rank_order", "_high_priority"]).reset_index(drop=True)

    # 毎日の買い候補（S/A/B）は最大 max_candidates 件に絞る（見送りは分析用に保持）。
    is_candidate = result["rank"].astype(str).str.upper().isin(["S", "A", "B"])
    candidates = result[is_candidate]
    if max_candidates is not None and len(candidates) > max_candidates:
        candidates = candidates.head(max_candidates)
    rejected = result[~is_candidate]
    _log_step("run_screening_total", time.perf_counter() - run_started, f"candidates={len(candidates)}")
    if include_rejected:
        final = add_openwork_scores(pd.concat([candidates, rejected], ignore_index=True))
    else:
        final = add_openwork_scores(candidates.reset_index(drop=True))
    _print_screening_summary(total, final, highs_rows, pullback_rows, retest_rows)
    return final


RANK_MAP = {
    "S": "S",
    "A": "A",
    "B": "B",
    "C": "C",
    "SKIP": "SKIP",
    "見送り": "SKIP",
    "": "SKIP",
    "NAN": "SKIP",
    "NONE": "SKIP",
    "<NA>": "SKIP",
}


def _normalize_rank(value: object) -> str:
    text = str(value).strip().upper()
    return RANK_MAP.get(text, "SKIP")


def _numeric_series(df: pd.DataFrame, column: str, default: float = 999.0) -> pd.Series:
    if column not in df.columns:
        return pd.Series(default, index=df.index, dtype="float64")
    return pd.to_numeric(df[column], errors="coerce").fillna(default)


SCREEN_TYPE_VALUES = [
    "MULTI",
    "52W_BREAKOUT",
    "52W_MOMENTUM",
    "52W_PULLBACK",
    "25MA_PULLBACK",
    "200MA_TOUCH",
    "WATCH",
    "SKIP",
]
SCREEN_TAG_VALUES = [
    "52W_BREAKOUT",
    "52W_MOMENTUM",
    "52W_PULLBACK",
    "25MA_PULLBACK",
    "200MA_TOUCH",
]
TRUE_VALUES = {"1", "1.0", "true", "yes", "y", "on"}


def _is_blank(value: object) -> bool:
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text == "" or text.lower() in {"nan", "none", "<na>", "nat"}


def _as_float(value: object, default: float | None = None) -> float | None:
    if _is_blank(value):
        return default
    text = str(value).replace(",", "").replace("%", "").strip()
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in TRUE_VALUES


def _append_tag(tags: list[str], tag: str) -> None:
    if tag not in tags:
        tags.append(tag)


def _screen_tags_for_row(row: pd.Series) -> list[str]:
    tags: list[str] = []
    high_type = str(row.get("high_type", "")).upper()
    dist_52w = _as_float(row.get("dist_52w_high_pct"), 999.0)
    days_since_52w = _as_float(row.get("days_since_52w_high"), 999.0)
    if high_type == "52W_NEW_HIGH" or (days_since_52w is not None and days_since_52w <= 0):
        _append_tag(tags, "52W_BREAKOUT")
    elif high_type == "52W_NEAR_HIGH" or (
        dist_52w is not None and dist_52w <= 3
    ) or (
        days_since_52w is not None and days_since_52w <= 14
    ):
        _append_tag(tags, "52W_MOMENTUM")

    has_52w_pullback = (
        _truthy(row.get("retest_52w"))
        or _truthy(row.get("prev_52w_retest"))
        or _truthy(row.get("duke_old_high_support"))
        or _truthy(row.get("duke_support_signal"))
        or not _is_blank(row.get("previous_52w_high_line"))
        or not _is_blank(row.get("line_deviation_pct"))
        or not _is_blank(row.get("old_52w_high"))
        or not _is_blank(row.get("dist_to_old_52w_high_pct"))
    )
    if has_52w_pullback:
        _append_tag(tags, "52W_PULLBACK")

    dist_25ma = _as_float(row.get("dist_25ma_pct"), 999.0)
    dist_200ma = _as_float(row.get("dist_200ma_pct"), 999.0)
    if _truthy(row.get("ma25_touch")) or (dist_25ma is not None and abs(dist_25ma) <= 3):
        _append_tag(tags, "25MA_PULLBACK")
    if _truthy(row.get("ma200_touch")) or (dist_200ma is not None and abs(dist_200ma) <= 3):
        _append_tag(tags, "200MA_TOUCH")
    return tags


def _screen_type_from_tags(tags: list[str], rank: object | None = None) -> str:
    tags = [tag for tag in dict.fromkeys(tags) if tag in SCREEN_TAG_VALUES]
    if rank is not None and _normalize_rank(rank) == "SKIP":
        return "SKIP"
    if len(tags) >= 2:
        return "MULTI"
    return tags[0] if tags else "WATCH"


def _screen_tags_text(tags: list[str], screen_type: str) -> str:
    tags = [tag for tag in dict.fromkeys(tags) if tag in SCREEN_TAG_VALUES]
    return ",".join(tags) if tags else screen_type


def _screen_type_for_row(row: pd.Series) -> str:
    return _screen_type_from_tags(_screen_tags_for_row(row), row.get("rank"))


def _normalize_screening_schema(result: pd.DataFrame) -> pd.DataFrame:
    """screening_result.csv の中核列を毎回同じ名前・値域で出す。"""
    out = result.copy()
    if "rank" not in out.columns:
        out["rank"] = "SKIP"
    out["rank"] = out["rank"].map(_normalize_rank)

    out["dist_25ma_pct"] = _numeric_series(out, "ma25_gap_pct").round(2)
    out["dist_200ma_pct"] = _numeric_series(out, "ma200_gap_pct").round(2)
    if "ma25_touch" not in out.columns:
        out["ma25_touch"] = out["dist_25ma_pct"].abs().le(3)
    if "ma200_touch" not in out.columns:
        out["ma200_touch"] = out["dist_200ma_pct"].abs().le(3)

    reason = out.get("reason", pd.Series("", index=out.index)).fillna("").astype(str)
    out["buy_reason"] = reason.where(out["rank"].isin(["S", "A", "B", "C"]), "")
    tags = out.apply(_screen_tags_for_row, axis=1)
    out["screen_type"] = [
        _screen_type_from_tags(row_tags, rank)
        for row_tags, rank in zip(tags, out["rank"])
    ]
    out["screen_tags"] = [
        _screen_tags_text(row_tags, screen_type)
        for row_tags, screen_type in zip(tags, out["screen_type"])
    ]
    return out


def _print_screening_summary(
    symbols: int,
    result: pd.DataFrame,
    highs_rows: list[dict[str, object]],
    pullback_rows: list[dict[str, object]],
    retest_rows: list[dict[str, object]],
) -> None:
    rank = result.get("rank", pd.Series(dtype=object)).astype(str).str.upper() if not result.empty else pd.Series(dtype=object)
    reason = result.get("reason", pd.Series(dtype=object)).astype(str) if not result.empty else pd.Series(dtype=object)
    high_new = sum(1 for row in highs_rows if str(row.get("high_type", "")) == "52W_NEW_HIGH")
    ma25 = sum(1 for row in pullback_rows if bool(row.get("ma25_touch")))
    ma200 = sum(1 for row in pullback_rows if bool(row.get("ma200_touch")))
    retest_buy = sum(1 for row in retest_rows if str(row.get("candidate_action", "")) == "BUY")
    error_rows = int(reason.str.contains("エラー|ERROR|Traceback|Exception", case=False, regex=True, na=False).sum())
    missing_price = int(reason.str.contains("価格データ不足", regex=False, na=False).sum())
    screen_type = result.get("screen_type", pd.Series(dtype=object)).astype(str) if not result.empty else pd.Series(dtype=object)
    screen_type_counts = " ".join(f"{value}={int(screen_type.eq(value).sum())}" for value in SCREEN_TYPE_VALUES)
    print(
        "screening_summary "
        f"symbols={symbols} result_rows={len(result)} "
        f"S={int(rank.eq('S').sum())} A={int(rank.eq('A').sum())} B={int(rank.eq('B').sum())} "
        f"C={int(rank.eq('C').sum())} SKIP={int(rank.eq('SKIP').sum())} "
        f"screen_types=[{screen_type_counts}] "
        f"buy_candidates={int(rank.isin(['S', 'A', 'B']).sum())} "
        f"new_52w={high_new} ma25_pullback={ma25} ma200_touch={ma200} "
        f"retest_52w_buy={retest_buy} error_rows={error_rows} missing_price_rows={missing_price}",
        flush=True,
    )


def _collect_pullback_row(
    row_base: dict[str, object],
    indicators: dict[str, float],
    high_info: dict[str, object],
    history: pd.DataFrame,
) -> dict[str, object] | None:
    """25/200/240MAタッチ または 52週新高値後リテストに該当する銘柄行を返す（非該当はNone）。
    流動性ゲート（20日平均売買代金1億円以上）のみ課す。捏造しない。"""
    if float(indicators.get("turnover_20d", 0)) < 100_000_000:
        return None
    touches = detect_ma_touches(indicators)
    retest = detect_52w_high_retest(history)
    if not (touches.get("ma_touch_any") or retest.get("retest_52w")):
        return None

    labels: list[str] = []
    if retest.get("retest_52w"):
        labels.append("52週新高値リテスト")
    if touches.get("ma_touch_labels"):
        labels.append(str(touches["ma_touch_labels"]))

    touch_types = []
    if retest.get("retest_52w"):
        touch_types.append("52W_PULLBACK")
    if touches.get("ma25_touch"):
        touch_types.append("25MA_PULLBACK")
    if touches.get("ma200_touch"):
        touch_types.append("200MA_TOUCH")
    screen_type = _screen_type_from_tags(touch_types)

    return {
        **row_base,
        "screen_type": screen_type,
        "screen_tags": _screen_tags_text(touch_types, screen_type),
        "current_price": round(float(indicators["current_price"]), 1),
        "high_52w": round(float(indicators["high_52w"]), 1),
        "dist_52w_high_pct": round(float(indicators["dist_52w_high_pct"]), 2),
        "ma25": round(float(indicators["ma25"]), 1),
        "ma75": round(float(indicators["ma75"]), 1),
        "ma200": round(float(indicators["ma200"]), 1),
        "ma240": round(float(indicators["ma240"]), 1),
        "ma25_rising": bool(indicators["ma25_rising"]),
        "ma200_rising": bool(indicators["ma200_rising"]),
        "ma240_rising": bool(indicators["ma240_rising"]),
        "ma25_touch": bool(touches.get("ma25_touch")),
        "ma200_touch": bool(touches.get("ma200_touch")),
        "ma240_touch": bool(touches.get("ma240_touch")),
        "ma25_touch_pct": round(float(indicators["ma25_touch_pct"]), 2),
        "ma200_touch_pct": round(float(indicators["ma200_touch_pct"]), 2),
        "ma240_touch_pct": round(float(indicators["ma240_touch_pct"]), 2),
        "dist_25ma_pct": round(float(indicators["ma25_gap_pct"]), 2),
        "dist_200ma_pct": round(float(indicators["ma200_gap_pct"]), 2),
        "retest_52w": bool(retest.get("retest_52w")),
        "retest_line_price": retest.get("retest_line_price", ""),
        "retest_breakout_date": retest.get("retest_breakout_date", ""),
        "retest_dist_pct": retest.get("retest_dist_pct", ""),
        "retest_post_high": retest.get("retest_post_high", ""),
        "turnover_20d": int(indicators["turnover_20d"]),
        "volume_ratio_5d_20d": round(float(indicators["volume_ratio_5d_20d"]), 2),
        "label": " / ".join(labels),
    }


def _collect_previous_52w_retest_row(
    row_base: dict[str, object],
    indicators: dict[str, float],
    history: pd.DataFrame,
) -> dict[str, object] | None:
    retest = detect_previous_52w_high_line_retest(history, indicators)
    if str(retest.get("prev_52w_retest_rank", "見送り")) == "見送り":
        return None

    return {
        **row_base,
        "screen_type": "52W_PULLBACK",
        "screen_tags": "52W_PULLBACK",
        "current_price": round(float(indicators["current_price"]), 1),
        "recent_52w_high": retest.get("recent_52w_high", ""),
        "recent_52w_high_date": retest.get("recent_52w_high_date", ""),
        "previous_52w_high_line": retest.get("previous_52w_high_line", ""),
        "previous_52w_high_date": retest.get("previous_52w_high_date", ""),
        "breakout_52w_date": retest.get("breakout_52w_date", ""),
        "line_deviation_pct": retest.get("line_deviation_pct", ""),
        "drawdown_from_recent_high_pct": retest.get("drawdown_from_recent_high_pct", ""),
        "dist_25ma_pct": round(float(indicators["ma25_gap_pct"]), 2),
        "dist_200ma_pct": round(float(indicators["ma200_gap_pct"]), 2),
        "ma25": round(float(indicators["ma25"]), 1),
        "ma50": round(float(indicators["ma50"]), 1),
        "ma75": round(float(indicators["ma75"]), 1),
        "ma25_rising": bool(indicators["ma25_rising"]),
        "ma50_rising": bool(indicators["ma50_rising"]),
        "volume_20d": int(indicators["volume_20d"]),
        "turnover_20d": int(indicators["turnover_20d"]),
        "volume_ratio_5d_20d": round(float(indicators["volume_ratio_5d_20d"]), 2),
        "rebound_sign": retest.get("prev_52w_retest_signs", ""),
        "score": int(retest.get("prev_52w_retest_score", 0)),
        "rank": retest.get("prev_52w_retest_rank", "見送り"),
        "candidate_action": retest.get("candidate_action", "CASH"),
        "reason": retest.get("prev_52w_retest_reason", ""),
    }


def _earnings_date_text(ticker: str) -> str:
    """次回決算日をISO文字列で返す（取得不能は空文字。捏造しない）。"""
    try:
        earnings = fetch_next_earnings_date(ticker)
    except Exception:
        return ""
    return earnings.isoformat() if earnings else ""


def _round_or_blank(value: object, ndigits: int = 1) -> object:
    """NaN/None/非数値は空文字にして丸める（上場1年未満のMA欠損に対応）。"""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if number != number:  # NaN
        return ""
    return round(number, ndigits)


def _note_flags_text(quality: dict[str, object]) -> str:
    """noteの従来表に出す注意フラグ文字列を組み立てる（事実のみ）。"""
    parts: list[str] = []
    if bool(quality.get("first_break_60d")):
        parts.append("初回ブレイク")
    breaks_20d = quality.get("breaks_20d")
    try:
        if int(breaks_20d) >= 5:  # type: ignore[arg-type]
            parts.append(f"連日更新{int(breaks_20d)}回/20日")  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pass
    text = str(quality.get("quality_flags", "")).strip()
    if text:
        parts.append(text)
    return " / ".join(parts)


def _daily_price_fields(history: pd.DataFrame) -> dict[str, object]:
    """当日データ（前日終値・前日比・本日高値・日中値幅・当日出来高倍率・対象取引日）。
    計算できない項目は入れない（捏造しない）。"""
    out: dict[str, object] = {}
    if history is None or history.empty or "Close" not in history.columns:
        return out
    close = history["Close"].astype(float)
    try:
        out["data_date"] = pd.Timestamp(history.index[-1]).date().isoformat()
    except Exception:
        pass
    if len(close) >= 2:
        current = float(close.iloc[-1])
        prev = float(close.iloc[-2])
        out["prev_close"] = round(prev, 1)
        if prev > 0:
            out["change_pct"] = round((current / prev - 1) * 100, 2)
    if "High" in history.columns and "Low" in history.columns:
        today_high = float(history["High"].astype(float).iloc[-1])
        today_low = float(history["Low"].astype(float).iloc[-1])
        out["today_high"] = round(today_high, 1)
        base = out.get("prev_close") or (float(close.iloc[-1]) or 0)
        try:
            base_f = float(base)
            if base_f > 0 and today_high >= today_low:
                out["intraday_range_pct"] = round((today_high - today_low) / base_f * 100, 2)
        except (TypeError, ValueError):
            pass
    if "Volume" in history.columns and len(history) >= 21:
        vol = history["Volume"].astype(float)
        avg20 = float(vol.iloc[-21:-1].mean())
        if avg20 > 0:
            out["volume_ratio_today"] = round(float(vol.iloc[-1]) / avg20, 2)
    return out


def _finalize_highs_row(extra: dict[str, object], ticker: str) -> None:
    """T-K: highs行にファンダ指標（検証済みのみ）と異常値判定を付与する（in-place）。
    取得失敗でもスクリーニングは止めない。"""
    try:
        from fundamentals import detect_price_anomalies, fetch_fundamentals

        current = extra.get("current_price")
        try:
            current_f = float(current)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            current_f = None
        extra.update(fetch_fundamentals(ticker, current_f))
        issues = detect_price_anomalies(extra)
        extra["data_anomaly"] = bool(issues)
        extra["anomaly_note"] = " / ".join(issues)
    except Exception:
        extra.setdefault("data_anomaly", False)
        extra.setdefault("anomaly_note", "")


def _collect_highs_row(
    row_base: dict[str, object],
    indicators: dict[str, float],
    high_info: dict[str, object],
    history: pd.DataFrame,
) -> dict[str, object] | None:
    """52週新高値(52W_NEW_HIGH) または 52週高値接近(52W_NEAR_HIGH) に該当する銘柄行を返す。
    流動性ゲート（20日平均売買代金1億円以上）のみ課す。捏造しない。
    鮮度（初回ブレイク/連日更新）とイナゴ・TOB疑いフラグ、決算日欄を付与する。"""
    high_type = str(high_info.get("high_type", ""))
    if high_type not in ("52W_NEW_HIGH", "52W_NEAR_HIGH"):
        return None
    if float(indicators.get("turnover_20d", 0)) < 100_000_000:
        return None
    quality = high_quality_flags(history)
    screen_tag = "52W_BREAKOUT" if high_type == "52W_NEW_HIGH" else "52W_MOMENTUM"
    return {
        **row_base,
        "screen_type": screen_tag,
        "screen_tags": screen_tag,
        "high_type": high_type,
        "high_label": high_info.get("high_label", ""),
        "high_price": high_info.get("high_price", ""),
        "high_date": high_info.get("high_date", ""),
        "dist_to_high_pct": high_info.get("dist_to_high_pct", ""),
        "high_window_days": high_info.get("high_window_days", ""),
        "current_price": _round_or_blank(indicators.get("current_price"), 1),
        "high_52w": _round_or_blank(indicators.get("high_52w"), 1),
        "dist_52w_high_pct": _round_or_blank(indicators.get("dist_52w_high_pct"), 2),
        "days_since_52w_high": int(indicators.get("days_since_52w_high", 0)),
        "ma25": _round_or_blank(indicators.get("ma25"), 1),
        "ma50": _round_or_blank(indicators.get("ma50"), 1),
        "ma200": _round_or_blank(indicators.get("ma200"), 1),
        "dist_25ma_pct": _round_or_blank(indicators.get("ma25_gap_pct"), 2),
        "dist_200ma_pct": _round_or_blank(indicators.get("ma200_gap_pct"), 2),
        "turnover_20d": int(indicators.get("turnover_20d", 0)),
        "volume_ratio_5d_20d": _round_or_blank(indicators.get("volume_ratio_5d_20d"), 2),
        # 鮮度・品質フラグ（scanner.highs.high_quality_flags）
        "breaks_20d": quality.get("breaks_20d", 0),
        "first_break_60d": bool(quality.get("first_break_60d", False)),
        "days_since_prev_break": quality.get("days_since_prev_break", ""),
        "surge_5d_pct": quality.get("surge_5d_pct", ""),
        "inago_suspect": bool(quality.get("inago_suspect", False)),
        "tob_suspect": bool(quality.get("tob_suspect", False)),
        "note_flags": _note_flags_text(quality),
        # 決算日（呼び出し側で該当行のみ取得して上書きする）
        "earnings_date": "",
        # T-K: 当日データ（前日比・本日高値・日中値幅・当日出来高倍率・対象取引日）
        **_daily_price_fields(history),
    }


AUX_COLUMNS = {
    "screening_pullback": [
        "code", "ticker", "name", "market", "sector", "screen_type", "screen_tags",
        "ma25_touch", "ma200_touch", "retest_52w",
    ],
    "screening_highs": [
        "code", "ticker", "name", "market", "sector", "screen_type", "screen_tags",
        "high_type", "high_label",
        "breaks_20d", "first_break_60d", "inago_suspect", "tob_suspect", "note_flags", "earnings_date",
        # T-K: note1本目（52週新高値 接近・到達）用の当日データ・ファンダ・異常値判定
        "data_date", "prev_close", "change_pct", "today_high", "intraday_range_pct", "volume_ratio_today",
        "per_actual", "per_forecast", "pbr", "dividend_yield_pct", "roe_pct", "op_margin_pct", "net_margin_pct",
        "sales_growth_pct", "profit_growth_pct", "market_cap_oku", "fundamentals_source",
        "data_anomaly", "anomaly_note",
    ],
    "screening_52w_retest": [
        "code", "ticker", "name", "market", "sector", "screen_type", "screen_tags",
        "rank", "score", "candidate_action", "reason",
    ],
}


def _write_aux_csv(rows: list[dict[str, object]], output_dir: str, prefix: str) -> None:
    path = timestamped_csv_path(output_dir, prefix=prefix)
    df = pd.DataFrame(rows)
    if df.empty:
        df = pd.DataFrame(columns=AUX_COLUMNS.get(prefix, []))
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"保存しました: {path}", flush=True)



def _is_rank_excluded_security(name: str, market: str = "", sector: str = "") -> bool:
    text = f"{name} {market} {sector}"
    excluded_words = (
        "ETF",
        "ETN",
        "REIT",
        "リート",
        "投信",
        "投資信託",
        "上場投信",
        "投資法人",
        "指数連動",
        "指数",
        "インデックス",
        "連動型",
    )
    return any(word in text for word in excluded_words)


def _high_priority(row: pd.Series) -> int:
    high_type = str(row.get("high_type", ""))
    if high_type == "SWING_HIGH_BREAK":
        return 0
    if high_type == "52W_NEW_HIGH":
        return 1
    return 2


def _load_universe(markets: tuple[str, ...], output_dir: str) -> pd.DataFrame:
    try:
        return load_jpx_listed(UniverseConfig(markets=markets))
    except Exception as exc:
        raise RuntimeError(
            "JPX銘柄一覧を取得できません。完全な銘柄一覧キャッシュがないため、"
            "不完全なscreening_result_*.csvへのフォールバックは行いません。"
        ) from exc


def format_indicators(indicators: dict[str, float]) -> dict[str, object]:
    return {
        "current_price": round(indicators["current_price"], 1),
        "high_52w": round(indicators["high_52w"], 1),
        "dist_52w_high_pct": round(indicators["dist_52w_high_pct"], 2),
        "days_since_52w_high": int(indicators["days_since_52w_high"]),
        "ma25": round(indicators["ma25"], 1),
        "ma50": round(indicators["ma50"], 1),
        "ma75": round(indicators["ma75"], 1),
        "ma200": round(indicators["ma200"], 1),
        "ma240": round(indicators["ma240"], 1),
        "ma25_slope": round(indicators["ma25_slope"], 3),
        "ma50_slope": round(indicators["ma50_slope"], 3),
        "ma75_slope": round(indicators["ma75_slope"], 3),
        "ma200_slope": round(indicators["ma200_slope"], 3),
        "ma240_slope": round(indicators["ma240_slope"], 3),
        "ma25_rising": bool(indicators["ma25_rising"]),
        "ma50_rising": bool(indicators["ma50_rising"]),
        "ma75_rising": bool(indicators["ma75_rising"]),
        "ma200_rising": bool(indicators["ma200_rising"]),
        "ma240_rising": bool(indicators["ma240_rising"]),
        "ma25_gap_pct": round(indicators["ma25_gap_pct"], 2),
        "ma50_gap_pct": round(indicators["ma50_gap_pct"], 2),
        "ma75_gap_pct": round(indicators["ma75_gap_pct"], 2),
        "ma200_gap_pct": round(indicators["ma200_gap_pct"], 2),
        "ma240_gap_pct": round(indicators["ma240_gap_pct"], 2),
        "dist_25ma_pct": round(indicators["ma25_gap_pct"], 2),
        "dist_200ma_pct": round(indicators["ma200_gap_pct"], 2),
        "ma25_touch_pct": round(indicators["ma25_touch_pct"], 2),
        "ma200_touch_pct": round(indicators["ma200_touch_pct"], 2),
        "ma240_touch_pct": round(indicators["ma240_touch_pct"], 2),
        "volume_ratio_5d_20d": round(indicators["volume_ratio_5d_20d"], 2),
        "turnover_20d": int(indicators["turnover_20d"]),
        "lot_value_100": int(indicators["lot_value_100"]),
    }


def format_cwh(cwh: dict[str, float] | None) -> dict[str, object]:
    if not cwh:
        return {
            "cwh_signal": False,
            "breakout_price": "",
            "pct_to_breakout": "",
            "cup_depth_pct": "",
            "handle_depth_pct": "",
        }
    return {
        "cwh_signal": True,
        "breakout_price": round(cwh["breakout_price"], 1),
        "pct_to_breakout": round(cwh["pct_to_breakout"], 2),
        "cup_depth_pct": round(cwh["cup_depth_pct"], 2),
        "handle_depth_pct": round(cwh["handle_depth_pct"], 2),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="300万円運用向け日本株スクリーナー")
    parser.add_argument(
        "--markets",
        nargs="+",
        choices=["prime", "standard", "growth"],
        default=["prime", "standard", "growth"],
        help="対象市場",
    )
    parser.add_argument("--limit", type=int, default=None, help="動作確認用に先頭N銘柄だけ処理")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "outputs"), help="CSV保存先")
    parser.add_argument("--include-rejected", action="store_true", help="見送り銘柄もCSVに含める")
    parser.add_argument("--max-candidates", type=int, default=20, help="毎日の買い候補(S/A/B)の最大件数。既定20")
    parser.add_argument("--strict", action="store_true", help="Sランクにstrictゲートを適用する")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    started = time.perf_counter()

    # QUICK_MODE のときは --limit 指定が無くても自動で MAX_SYMBOLS 件に絞る。
    # 上限の最終決定は run_screening 内（resolve_symbol_limit）で行うが、
    # ここでも例外時に空CSVを残せるように事前に解決しておく。
    effective_limit = resolve_symbol_limit(args.limit)
    if effective_limit is not None:
        print(f"QUICK_MODE/limit active: max_symbols={effective_limit}", flush=True)

    # 途中で止まっても screening_result.csv を必ず残す（GitHub Actions が安定して回るように）。
    result = pd.DataFrame()
    failed = False
    try:
        result = run_screening(
            markets=tuple(args.markets),
            limit=args.limit,
            output_dir=args.output_dir,
            include_rejected=args.include_rejected,
            max_candidates=args.max_candidates,
            strict=args.strict,
        )
    except Exception as exc:  # noqa: BLE001 - ワークフローを止めないため全例外を捕捉
        failed = True
        import traceback
        print(f"ERROR run_screening failed: {exc}", flush=True)
        traceback.print_exc()

    # 1) 固定名 outputs/screening_result.csv は必ず保存（正常な0件ならヘッダー付き空CSV）。
    #    ただし例外発生（failed）で空になった場合は、前回の正常な結果CSVを空で上書きしない。
    fixed_path = write_result_csv(result, args.output_dir, allow_empty_overwrite=not failed)
    print(f"保存しました（固定名）: {fixed_path}", flush=True)
    # 2) タイムスタンプ付きの履歴用CSVは、例外で空のときは作らない（空ファイルを増やさない）。
    if not (failed and result.empty):
        stamped_path = timestamped_csv_path(args.output_dir)
        result.to_csv(stamped_path, index=False, encoding="utf-8-sig")
        print(f"保存しました: {stamped_path}", flush=True)
    else:
        print("例外発生のため履歴用CSVは作成しません（前回の正常結果を保持）。", flush=True)

    if result.empty:
        if failed:
            print("例外により候補を取得できませんでした（前回の固定CSVは保持）。", flush=True)
        else:
            print("条件に合う銘柄はありませんでした（または取得失敗）。空のCSVを保存しました。", flush=True)
        _log_step("run_screening_main", time.perf_counter() - started, "candidates=0")
        return

    print("\n=== 300万円運用向け日本株スクリーニング ===\n")
    display = result.copy()
    for column in DISPLAY_COLUMNS:
        if column not in display.columns:
            display[column] = ""
    print(display[DISPLAY_COLUMNS].to_string(index=False))
    _log_step("run_screening_main", time.perf_counter() - started, f"candidates={len(result)}")


if __name__ == "__main__":
    main()
