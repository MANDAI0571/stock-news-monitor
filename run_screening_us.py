"""米株スクリーナー。S&P500 ＋ NASDAQ大型株を回して候補CSVを出す。

日本株の run_screening.py と同じ流れにしてある。違うのは3つだけ。
  ・銘柄一覧が scanner.universe_us（S&P500 ＋ NASDAQ大型）
  ・採点が scanner.scoring_us（ドル建て・1株単位）
  ・流動性の下限をドルで指定する（日本株は1億円で固定だった）

押し目・52週新高値の拾い方は日本株とまったく同じ関数を呼んでいる。
あちらは価格データと指標だけを見る作りで、市場に依らないため。
（run_screening.py の _collect_pullback_row / _collect_highs_row をそのまま使う）

出力
  outputs/screening_us_YYYYMMDD_HHMMSS.csv           採点した候補
  outputs/screening_us_pullback_YYYYMMDD_HHMMSS.csv  押し目候補
  outputs/screening_us_highs_YYYYMMDD_HHMMSS.csv     52週新高値・接近

使い方
  python3 run_screening_us.py                 全銘柄
  python3 run_screening_us.py --limit 30      動作確認用
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from run_screening import (
    _collect_highs_row,
    _collect_pullback_row,
    _earnings_date_text,
    _finalize_highs_row,
)
from scanner.indicators import calculate_indicators, calculate_indicators_lenient
from scanner.highs import classify_high_profile
from scanner.prices import fetch_price_history, prefetch_price_histories
from scanner.scoring_us import US_MIN_TURNOVER, score_us_stock
from scanner.universe_us import UsUniverseConfig, load_us_universe
from us_calendar import is_us_business_day, prev_us_business_day

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"

# 日本株側の補助CSV（押し目・新高値）は1億円で足切りしている。
# 米株はドル建てなので、その関数に渡す前に自分で足切りする。
# S&P500の下位には日次売買代金が5,000万ドル前後の銘柄もあるので、
# 2,000万ドルを下限にした（scanner.scoring_us.US_MIN_TURNOVER と同じ値）。
AUX_MIN_TURNOVER_USD = US_MIN_TURNOVER


def us_target_date(now: datetime | None = None) -> str:
    """対象営業日。米国市場が開いていない日は直前の営業日を指す。"""
    today = (now or datetime.now()).date()
    if is_us_business_day(today):
        return today.isoformat()
    return prev_us_business_day(today).isoformat()


def _passes_us_liquidity(indicators: dict[str, float] | None) -> bool:
    if not indicators:
        return False
    return float(indicators.get("turnover_20d", 0.0) or 0.0) >= AUX_MIN_TURNOVER_USD


def run_us_screening(
    limit: int | None = None,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    nasdaq_top: int = 100,
) -> pd.DataFrame:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    universe = load_us_universe(UsUniverseConfig(nasdaq_top=nasdaq_top))
    print(f"us_universe_loaded={len(universe)}銘柄 ({time.perf_counter() - started:.0f}s)", flush=True)
    if limit:
        print(f"WARNING: limit={limit} は動作確認用。本番は全銘柄で実行してください。", flush=True)
        universe = universe.head(limit)

    tickers = [str(t) for t in universe["ticker"].tolist()]
    if len(tickers) > 50:
        stats = prefetch_price_histories(tickers)
        print(f"us_price_prefetch={stats}", flush=True)

    rows: list[dict[str, object]] = []
    pullback_rows: list[dict[str, object]] = []
    highs_rows: list[dict[str, object]] = []
    total = len(universe)

    for index, stock in enumerate(universe.itertuples(index=False), start=1):
        if index % 50 == 0:
            print(f"[{index}/{total}] scanning...", flush=True)
        row_base = {
            "code": stock.code,
            "ticker": stock.ticker,
            "name": stock.name,
            "market": stock.market,
            "sector": stock.sector,
        }
        try:
            history = fetch_price_history(stock.ticker)
            if history is None or history.empty:
                continue
            indicators = calculate_indicators(history)
            high_info = classify_high_profile(history)

            if indicators is None:
                # 上場1年未満などは緩い指標で新高値リストにだけ載せる（日本株と同じ扱い）
                lenient = calculate_indicators_lenient(history)
                if _passes_us_liquidity(lenient):
                    extra = _collect_highs_row(
                        row_base, lenient, high_info, history, AUX_MIN_TURNOVER_USD
                    )
                    if extra is not None:
                        extra["earnings_date"] = _earnings_date_text(stock.ticker)
                        _finalize_highs_row(extra, stock.ticker)
                        highs_rows.append(extra)
                continue

            if not _passes_us_liquidity(indicators):
                continue

            pullback_extra = _collect_pullback_row(
                row_base, indicators, high_info, history, AUX_MIN_TURNOVER_USD
            )
            if pullback_extra is not None:
                pullback_rows.append(pullback_extra)

            highs_extra = _collect_highs_row(
                row_base, indicators, high_info, history, AUX_MIN_TURNOVER_USD
            )
            if highs_extra is not None:
                highs_extra["earnings_date"] = _earnings_date_text(stock.ticker)
                _finalize_highs_row(highs_extra, stock.ticker)
                highs_rows.append(highs_extra)

            scored = score_us_stock(
                indicators,
                None,
                {"earnings_status": "確認済"},
                name=str(stock.name),
                sector=str(stock.sector),
            )
            rows.append(
                {
                    **row_base,
                    **high_info,
                    "current_price": round(float(indicators["current_price"]), 2),
                    "dist_52w_high_pct": round(float(indicators["dist_52w_high_pct"]), 2),
                    "turnover_20d": round(float(indicators["turnover_20d"]), 0),
                    "volume_ratio_5d_20d": round(float(indicators.get("volume_ratio_5d_20d", 0)), 2),
                    "score": scored["score"],
                    "rank": scored["rank"],
                    "reason": scored["reason"],
                }
            )
        except Exception as exc:  # 1銘柄の失敗で全体を止めない
            print(f"us_scan_error ticker={stock.ticker} {type(exc).__name__}: {exc}", flush=True)
            continue

    result = pd.DataFrame(rows)
    if not result.empty:
        result = result.sort_values(["score"], ascending=False).reset_index(drop=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _write(result, out_dir / f"screening_us_{stamp}.csv")
    _write(pd.DataFrame(pullback_rows), out_dir / f"screening_us_pullback_{stamp}.csv")
    _write(pd.DataFrame(highs_rows), out_dir / f"screening_us_highs_{stamp}.csv")

    print(
        f"us_screening_done target_date={us_target_date()} scanned={total} "
        f"scored={len(result)} pullback={len(pullback_rows)} highs={len(highs_rows)} "
        f"elapsed={time.perf_counter() - started:.0f}s",
        flush=True,
    )
    return result


def _write(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"saved={path} rows={len(df)}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="米株スクリーナー（S&P500＋NASDAQ大型）")
    parser.add_argument("--limit", type=int, default=None, help="動作確認用に銘柄数を絞る")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--nasdaq-top", type=int, default=100)
    args = parser.parse_args()
    run_us_screening(args.limit, args.output_dir, args.nasdaq_top)


if __name__ == "__main__":
    main()
