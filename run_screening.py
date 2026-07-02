from __future__ import annotations

import argparse
import os
import time
from datetime import date
from pathlib import Path

import pandas as pd

from scanner.indicators import calculate_indicators, detect_ma_touches, passes_base_filters
from scanner.openwork import add_openwork_scores
from scanner.highs import classify_high_profile, detect_52w_high_retest, detect_duke_old_high_support, detect_previous_52w_high_line_retest
from scanner.patterns import detect_cup_with_handle
from scanner.prices import fetch_next_earnings_date, fetch_price_history, timestamped_csv_path
from scanner.scoring import assess_earnings_window, rejection_row, score_stock
from scanner.universe import UniverseConfig, load_jpx_listed


PROJECT_ROOT = Path(__file__).resolve().parent
CAPITAL = 3_000_000


def _quick_limit_from_env() -> int | None:
    quick = os.environ.get("QUICK_MODE", "").lower() in {"1", "true", "yes", "on"}
    max_symbols = os.environ.get("MAX_SYMBOLS", "").strip()
    if max_symbols:
        try:
            return max(1, int(max_symbols))
        except ValueError:
            print(f"WARNING invalid MAX_SYMBOLS={max_symbols}; ignored", flush=True)
    return 30 if quick else None


def _write_latest_screening_copy(result: pd.DataFrame, output_dir: str | Path) -> Path:
    path = Path(output_dir) / "screening_result.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(path, index=False, encoding="utf-8-sig")
    return path

# 結果CSV / コンソール表示で使う列。
DISPLAY_COLUMNS = [
    "code",
    "name",
    "market",
    "current_price",
    "score",
    "rank",
    "volume_ratio_5d_20d",
    "dist_52w_high_pct",
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

    rows: list[dict[str, object]] = []
    # T-D(2026-06-28): メインの300万/ブレイク候補とは独立に、押し目(タッチ/リテスト)と
    # 高値更新(52週新高値・接近)を専用収集する。メインゲート(current>MA25/75/200 等)を通らない
    # 押し目銘柄も拾うため、ベースフィルター前に収集する。
    pullback_rows: list[dict[str, object]] = []
    highs_rows: list[dict[str, object]] = []
    retest_rows: list[dict[str, object]] = []
    total = len(universe)
    today = date.today()

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
            highs_extra = _collect_highs_row(row_base, indicators, high_info)
            if highs_extra is not None:
                highs_rows.append(highs_extra)

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

    rank_order = {"S": 0, "A": 1, "B": 2, "見送り": 3}
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
        return add_openwork_scores(pd.concat([candidates, rejected], ignore_index=True))
    return add_openwork_scores(candidates.reset_index(drop=True))


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

    return {
        **row_base,
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
        "current_price": round(float(indicators["current_price"]), 1),
        "recent_52w_high": retest.get("recent_52w_high", ""),
        "recent_52w_high_date": retest.get("recent_52w_high_date", ""),
        "previous_52w_high_line": retest.get("previous_52w_high_line", ""),
        "previous_52w_high_date": retest.get("previous_52w_high_date", ""),
        "breakout_52w_date": retest.get("breakout_52w_date", ""),
        "line_deviation_pct": retest.get("line_deviation_pct", ""),
        "drawdown_from_recent_high_pct": retest.get("drawdown_from_recent_high_pct", ""),
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


def _collect_highs_row(
    row_base: dict[str, object],
    indicators: dict[str, float],
    high_info: dict[str, object],
) -> dict[str, object] | None:
    """52週新高値(52W_NEW_HIGH) または 52週高値接近(52W_NEAR_HIGH) に該当する銘柄行を返す。
    流動性ゲート（20日平均売買代金1億円以上）のみ課す。捏造しない。"""
    high_type = str(high_info.get("high_type", ""))
    if high_type not in ("52W_NEW_HIGH", "52W_NEAR_HIGH"):
        return None
    if float(indicators.get("turnover_20d", 0)) < 100_000_000:
        return None
    return {
        **row_base,
        "high_type": high_type,
        "high_label": high_info.get("high_label", ""),
        "high_price": high_info.get("high_price", ""),
        "high_date": high_info.get("high_date", ""),
        "dist_to_high_pct": high_info.get("dist_to_high_pct", ""),
        "current_price": round(float(indicators["current_price"]), 1),
        "high_52w": round(float(indicators["high_52w"]), 1),
        "dist_52w_high_pct": round(float(indicators["dist_52w_high_pct"]), 2),
        "days_since_52w_high": int(indicators["days_since_52w_high"]),
        "ma25": round(float(indicators["ma25"]), 1),
        "ma50": round(float(indicators["ma50"]), 1),
        "ma200": round(float(indicators["ma200"]), 1),
        "turnover_20d": int(indicators["turnover_20d"]),
        "volume_ratio_5d_20d": round(float(indicators["volume_ratio_5d_20d"]), 2),
    }


def _write_aux_csv(rows: list[dict[str, object]], output_dir: str, prefix: str) -> None:
    if not rows:
        return
    path = timestamped_csv_path(output_dir, prefix=prefix)
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
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
