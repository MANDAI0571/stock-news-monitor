from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd

from scanner.highs import detect_swing_high_break, classify_high_profile
from scanner.indicators import calculate_indicators
from scanner.patterns import detect_cup_with_handle
from scanner.prices import fetch_next_earnings_date, fetch_price_history, timestamped_csv_path
from scanner.scoring import assess_earnings_window, score_stock
from scanner.universe import UniverseConfig, load_jpx_listed

PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "outputs"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="直近スイング高値ブレイク検証レポート")
    p.add_argument("--codes", nargs="+", default=["9256", "6266", "3441", "5803"])
    p.add_argument("--lookback-days", type=int, default=30)
    p.add_argument("--forward-days", type=int, default=5)
    p.add_argument("--output-dir", default=str(OUTPUT_DIR))
    return p.parse_args()


def main() -> None:
    args = parse_args()
    codes = [c.upper().removesuffix(".T") for c in args.codes]
    lookup = _universe_lookup()
    latest_rows: list[dict[str, object]] = []
    bt_rows: list[dict[str, object]] = []

    for code in codes:
        ticker = f"{code}.T"
        meta = lookup.get(code, {"name": "", "sector": ""})
        history = fetch_price_history(ticker)
        if history.empty:
            latest_rows.append({"code": code, "ticker": ticker, "name": meta.get("name", ""), "error": "価格データなし"})
            continue
        latest_rows.append(_latest_row(code, ticker, str(meta.get("name", "")), str(meta.get("sector", "")), history))
        bt_rows.extend(_backtest_rows(code, ticker, str(meta.get("name", "")), history, args.lookback_days, args.forward_days))

    latest = pd.DataFrame(latest_rows)
    backtest = pd.DataFrame(bt_rows)
    compare = _ranking_compare(latest)
    summary = _backtest_summary(backtest)

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    latest_path = out / f"swing_break_latest_{stamp}.csv"
    backtest_path = out / f"swing_break_backtest_{stamp}.csv"
    report_path = out / f"swing_break_report_{stamp}.md"
    latest.to_csv(latest_path, index=False, encoding="utf-8-sig")
    backtest.to_csv(backtest_path, index=False, encoding="utf-8-sig")
    report_path.write_text(_render_report(latest, compare, summary, backtest, latest_path, backtest_path), encoding="utf-8")
    print(report_path)
    print(compare.to_string(index=False))
    if not summary.empty:
        print(summary.to_string(index=False))


def _universe_lookup() -> dict[str, dict[str, object]]:
    try:
        universe = load_jpx_listed(UniverseConfig())
    except Exception:
        return {}
    return {str(r.code): {"name": r.name, "sector": r.sector} for r in universe.itertuples(index=False)}


def _latest_row(code: str, ticker: str, name: str, sector: str, history: pd.DataFrame) -> dict[str, object]:
    indicators = calculate_indicators(history)
    high = classify_high_profile(history)
    row: dict[str, object] = {
        "code": code,
        "ticker": ticker,
        "name": name,
        "sector": sector,
        "current_price": round(float(history["Close"].iloc[-1]), 1),
        "today_high": round(float(history["High"].iloc[-1]), 1),
        **high,
    }
    if indicators:
        earnings = assess_earnings_window(pd.Timestamp.today().date(), fetch_next_earnings_date(ticker))
        scored = score_stock(indicators, detect_cup_with_handle(history["Close"]), earnings, name=name, sector=sector)
        row |= {
            "score": scored["score"],
            "rank": scored["rank"],
            "reason": scored["reason"],
            "dist_52w_high_pct": round(indicators["dist_52w_high_pct"], 2),
            "volume_ratio_5d_20d": round(indicators["volume_ratio_5d_20d"], 2),
            "ma25": round(indicators["ma25"], 1),
            "turnover_20d": int(indicators["turnover_20d"]),
        }
    return row


def _backtest_rows(code: str, ticker: str, name: str, history: pd.DataFrame, lookback_days: int, forward_days: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    start = max(0, len(history) - lookback_days - forward_days)
    end = len(history) - forward_days
    for idx in range(start, end):
        window = history.iloc[: idx + 1]
        signal = detect_swing_high_break(window)
        if not signal.get("swing_high_break"):
            continue
        entry = float(history["Close"].iloc[idx])
        exit_ = float(history["Close"].iloc[idx + forward_days])
        ret = (exit_ / entry - 1.0) * 100 if entry > 0 else 0.0
        rows.append(
            {
                "code": code,
                "ticker": ticker,
                "name": name,
                "signal_date": pd.Timestamp(history.index[idx]).date().isoformat(),
                "entry_close": round(entry, 1),
                "exit_date": pd.Timestamp(history.index[idx + forward_days]).date().isoformat(),
                "exit_close": round(exit_, 1),
                "forward_days": forward_days,
                "return_5d_pct": round(ret, 2),
                **signal,
            }
        )
    return rows


def _ranking_compare(latest: pd.DataFrame) -> pd.DataFrame:
    if latest.empty:
        return latest
    df = latest.copy()
    for col, default in {"rank": "見送り", "score": 0, "dist_52w_high_pct": 999, "high_type": "OTHER"}.items():
        if col not in df.columns:
            df[col] = default
    rank_order = {"S": 0, "A": 1, "B": 2, "見送り": 3}
    df["_rank_order"] = df["rank"].map(rank_order).fillna(9)
    df["score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0)
    df["dist_52w_high_pct"] = pd.to_numeric(df["dist_52w_high_pct"], errors="coerce").fillna(999)
    current = df.sort_values(["_rank_order", "score", "dist_52w_high_pct"], ascending=[True, False, True]).reset_index(drop=True)
    current["current_order"] = range(1, len(current) + 1)
    improved = df.assign(_high_priority=df["high_type"].apply(lambda v: 0 if str(v) == "SWING_HIGH_BREAK" else 1 if str(v) == "52W_NEW_HIGH" else 2))
    improved = improved.sort_values(["_high_priority", "_rank_order", "score", "dist_52w_high_pct"], ascending=[True, True, False, True]).reset_index(drop=True)
    improved["improved_order"] = range(1, len(improved) + 1)
    merged = current[["code", "current_order"]].merge(improved[["code", "improved_order"]], on="code", how="outer")
    cols = ["code", "name", "rank", "score", "high_type", "swing_high_price", "swing_high_date", "swing_high_break", "volume_ratio_5d_20d", "turnover_20d"]
    return merged.merge(df[[c for c in cols if c in df.columns]], on="code", how="left").sort_values("improved_order")


def _backtest_summary(backtest: pd.DataFrame) -> pd.DataFrame:
    if backtest.empty:
        return pd.DataFrame(columns=["code", "signals", "avg_return_5d_pct", "win_rate_pct"])
    grouped = backtest.groupby(["code", "name"], dropna=False)
    return grouped.agg(
        signals=("return_5d_pct", "count"),
        avg_return_5d_pct=("return_5d_pct", "mean"),
        median_return_5d_pct=("return_5d_pct", "median"),
        win_rate_pct=("return_5d_pct", lambda s: (s > 0).mean() * 100),
    ).round(2).reset_index()


def _markdown_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "該当なし"
    text_df = df.copy().fillna("")
    cols = [str(c) for c in text_df.columns]
    rows = [[str(v) for v in row] for row in text_df.to_numpy().tolist()]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(cell.replace("\n", " ") for cell in row) + " |")
    return "\n".join(lines)


def _render_report(latest: pd.DataFrame, compare: pd.DataFrame, summary: pd.DataFrame, backtest: pd.DataFrame, latest_path: Path, backtest_path: Path) -> str:
    lines = [
        "# 直近高値ブレイク評価レポート",
        "",
        "## 現在ランキング vs 改善後ランキング",
        "",
        _markdown_table(compare) if not compare.empty else "該当なし",
        "",
        "## 過去30日: 直近高値ブレイク後5営業日騰落率",
        "",
        _markdown_table(summary) if not summary.empty else "該当シグナルなし",
        "",
        "## シグナル明細",
        "",
        _markdown_table(backtest) if not backtest.empty else "該当シグナルなし",
        "",
        "## 出力CSV",
        f"- latest: {latest_path}",
        f"- backtest: {backtest_path}",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
