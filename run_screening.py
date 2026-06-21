from __future__ import annotations

import argparse
from datetime import date

import pandas as pd

from scanner.indicators import calculate_indicators, passes_base_filters
from scanner.patterns import detect_cup_with_handle
from scanner.prices import fetch_next_earnings_date, fetch_price_history, timestamped_csv_path
from scanner.scoring import assess_earnings_window, rejection_row, score_stock
from scanner.universe import UniverseConfig, load_jpx_listed


CAPITAL = 3_000_000


def run_screening(
    markets: tuple[str, ...],
    limit: int | None,
    output_dir: str,
    include_rejected: bool,
) -> pd.DataFrame:
    universe = load_jpx_listed(UniverseConfig(markets=markets))
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
            if indicators is None:
                if include_rejected:
                    rows.append(row_base | rejection_row(None, "価格データ不足"))
                continue

            passed, reject_reasons = passes_base_filters(indicators)
            if not passed:
                if include_rejected:
                    rows.append(row_base | format_indicators(indicators) | rejection_row(indicators, " / ".join(reject_reasons)))
                continue

            earnings_date = fetch_next_earnings_date(stock.ticker)
            earnings = assess_earnings_window(today, earnings_date)
            if earnings["exclude_for_earnings"]:
                if include_rejected:
                    rows.append(
                        row_base
                        | format_indicators(indicators)
                        | earnings
                        | rejection_row(indicators, "決算14営業日前〜決算翌営業日のため除外")
                    )
                continue

            cwh = detect_cup_with_handle(history["Close"])
            scored = score_stock(indicators, cwh, earnings, capital=CAPITAL)
            rows.append(
                row_base
                | format_indicators(indicators)
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

    rank_order = {"S": 0, "A": 1, "B": 2, "見送り": 3}
    result["_rank_order"] = result["rank"].map(rank_order).fillna(9)
    result = result.sort_values(["_rank_order", "score", "dist_52w_high_pct"], ascending=[True, False, True])
    return result.drop(columns=["_rank_order"]).reset_index(drop=True)


def format_indicators(indicators: dict[str, float]) -> dict[str, object]:
    return {
        "current_price": round(indicators["current_price"], 1),
        "high_52w": round(indicators["high_52w"], 1),
        "dist_52w_high_pct": round(indicators["dist_52w_high_pct"], 2),
        "ma25": round(indicators["ma25"], 1),
        "ma75": round(indicators["ma75"], 1),
        "ma200": round(indicators["ma200"], 1),
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
    parser.add_argument("--output-dir", default="outputs", help="CSV保存先")
    parser.add_argument("--include-rejected", action="store_true", help="見送り銘柄もCSVに含める")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_screening(
        markets=tuple(args.markets),
        limit=args.limit,
        output_dir=args.output_dir,
        include_rejected=args.include_rejected,
    )

    if result.empty:
        print("条件に合う銘柄はありませんでした。")
        return

    path = timestamped_csv_path(args.output_dir)
    result.to_csv(path, index=False, encoding="utf-8-sig")
    print("\n=== 300万円運用向け日本株スクリーニング ===\n")
    print(result[["code", "name", "market", "current_price", "score", "rank", "lot_value_100", "max_positions_3m", "reason"]].to_string(index=False))
    print(f"\n保存しました: {path}")


if __name__ == "__main__":
    main()
