from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pandas as pd

from scanner.indicators import calculate_indicators, passes_base_filters
from scanner.highs import classify_high_profile
from scanner.patterns import detect_cup_with_handle
from scanner.prices import fetch_next_earnings_date, fetch_price_history, timestamped_csv_path
from scanner.scoring import assess_earnings_window, rejection_row, score_stock
from scanner.universe import UniverseConfig, load_jpx_listed


PROJECT_ROOT = Path(__file__).resolve().parent
CAPITAL = 3_000_000


def run_screening(
    markets: tuple[str, ...],
    limit: int | None,
    output_dir: str,
    include_rejected: bool,
    max_candidates: int | None = 20,
    strict: bool = False,
) -> pd.DataFrame:
    universe = _load_universe(markets, output_dir)
    if limit:
        universe = universe.head(limit)

    rows: list[dict[str, object]] = []
    total = len(universe)
    today = date.today()

    for idx, stock in enumerate(universe.itertuples(index=False), start=1):
        print(f"[{idx}/{total}] {stock.ticker} {stock.name}", flush=True)
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

            passed, reject_reasons = passes_base_filters(indicators)
            if not passed:
                if include_rejected:
                    rows.append(row_base | format_indicators(indicators) | high_info | rejection_row(indicators, " / ".join(reject_reasons)))
                continue

            earnings_date = fetch_next_earnings_date(stock.ticker)
            earnings = assess_earnings_window(today, earnings_date)
            if earnings["exclude_for_earnings"]:
                if include_rejected:
                    rows.append(
                        row_base
                        | format_indicators(indicators)
                        | high_info
                        | earnings
                        | rejection_row(indicators, "決算14営業日前〜決算翌営業日のため除外")
                    )
                continue

            cwh = detect_cup_with_handle(history["Close"])
            scored = score_stock(indicators, cwh, earnings, capital=CAPITAL, name=stock.name, sector=stock.sector, strict=strict)
            rows.append(
                row_base
                | format_indicators(indicators)
                | high_info
                | format_cwh(cwh)
                | earnings
                | scored
            )
        except Exception as exc:
            if include_rejected:
                rows.append(row_base | rejection_row(None, f"エラー: {exc}"))

    result = pd.DataFrame(rows)
    if result.empty:
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
    }.items():
        if column not in result.columns:
            result[column] = default

    rank_order = {"S": 0, "A": 1, "B": 2, "見送り": 3}
    result["_rank_order"] = result["rank"].map(rank_order).fillna(9)
    result = result.sort_values(["_rank_order", "score", "dist_52w_high_pct"], ascending=[True, False, True])
    result = result.drop(columns=["_rank_order"]).reset_index(drop=True)

    # 毎日の買い候補（S/A/B）は最大 max_candidates 件に絞る（見送りは分析用に保持）。
    is_candidate = result["rank"].astype(str).str.upper().isin(["S", "A", "B"])
    candidates = result[is_candidate]
    if max_candidates is not None and len(candidates) > max_candidates:
        candidates = candidates.head(max_candidates)
    rejected = result[~is_candidate]
    if include_rejected:
        return pd.concat([candidates, rejected], ignore_index=True)
    return candidates.reset_index(drop=True)


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
        "ma75": round(indicators["ma75"], 1),
        "ma200": round(indicators["ma200"], 1),
        "ma25_slope": round(indicators["ma25_slope"], 3),
        "ma75_slope": round(indicators["ma75_slope"], 3),
        "ma25_rising": bool(indicators["ma25_rising"]),
        "ma75_rising": bool(indicators["ma75_rising"]),
        "ma25_gap_pct": round(indicators["ma25_gap_pct"], 2),
        "ma75_gap_pct": round(indicators["ma75_gap_pct"], 2),
        "ma200_gap_pct": round(indicators["ma200_gap_pct"], 2),
        "ma200_touch_pct": round(indicators["ma200_touch_pct"], 2),
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
    result = run_screening(
        markets=tuple(args.markets),
        limit=args.limit,
        output_dir=args.output_dir,
        include_rejected=args.include_rejected,
        max_candidates=args.max_candidates,
        strict=args.strict,
    )

    if result.empty:
        print("条件に合う銘柄はありませんでした。")
        return

    path = timestamped_csv_path(args.output_dir)
    result.to_csv(path, index=False, encoding="utf-8-sig")
    print("\n=== 300万円運用向け日本株スクリーニング ===\n")
    display_columns = [
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
    for column in display_columns:
        if column not in result.columns:
            result[column] = ""
    print(result[display_columns].to_string(index=False))
    print(f"\n保存しました: {path}")


if __name__ == "__main__":
    main()
