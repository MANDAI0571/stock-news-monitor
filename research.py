#!/usr/bin/env python3
"""Fast parameter research from existing backtest checkpoints.

This script intentionally does not fetch prices. It reuses candidate trades
stored in a checkpoint and reconstructs a simple 3-slot portfolio for each
condition set.
"""

from __future__ import annotations

import argparse
import itertools
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "outputs"
DEFAULT_CHECKPOINT = PROJECT_ROOT / "cache/checkpoints/backtest_9580cc1ce435ae97.jsonl"
DEFAULT_META = PROJECT_ROOT / "cache/checkpoints/backtest_9580cc1ce435ae97.meta.json"
ELECTRIC_VOLUME_MIN = 1.1

INITIAL_CAPITAL = 3_000_000
MAX_POSITIONS = 3
ROUND_LOT = 100

VOLUME_RANGES = [(0.8, 1.5), (0.9, 1.5), (1.0, 1.5), (1.1, 1.5)]
MA25_GAP_RANGES = [(0.0, 7.0), (1.0, 7.0), (2.0, 7.0)]
DIST_52W_RANGES = [(1.0, 7.0), (1.0, 10.0), (3.0, 7.0)]
ALLOCATIONS = {
    "equal_100_100_100": (1_000_000, 1_000_000, 1_000_000),
    "rank_150_100_50": (1_500_000, 1_000_000, 500_000),
    "focus_200_100_0": (2_000_000, 1_000_000, 0),
}

RANKING_RULES = {
    "current": ("turnover_20d_avg", "ma25_gap_pct", "score"),
    "ma25_first": ("ma25_gap_pct", "score", "turnover_20d_avg"),
    "score_first": ("score", "ma25_gap_pct", "turnover_20d_avg"),
}


@dataclass(frozen=True)
class Condition:
    volume_min: float
    volume_max: float
    ma25_gap_min: float
    ma25_gap_max: float
    dist52w_min: float
    dist52w_max: float
    allocation_name: str
    allocations: tuple[int, int, int]


def main() -> None:
    args = _parse_args()
    checkpoint_path = Path(args.checkpoint).resolve()
    meta_path = Path(args.meta).resolve()

    meta = _load_meta(meta_path)
    candidates = _load_checkpoint(checkpoint_path)
    _validate_source(meta, candidates)

    filtered = _filter_electric_volume_only(candidates)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = f"research_{timestamp}"

    rows = []
    for ranking_rule in RANKING_RULES:
        trades = _simulate_portfolio(filtered, ranking_rule, ALLOCATIONS["equal_100_100_100"])
        rows.append(
            _build_result_row(
                run_id,
                checkpoint_path,
                ranking_rule,
                filtered,
                trades,
            )
        )

    results = pd.DataFrame(rows)
    results["robust_score"] = results.apply(_robust_score, axis=1)
    results = results.sort_values(
        ["robust_score", "top10_removed_pf", "max_dd_pct", "capital_utilization_pct", "pf"],
        ascending=[False, False, False, False, False],
    ).reset_index(drop=True)

    best = results.copy()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results_path = OUTPUT_DIR / f"research_results_{timestamp}.csv"
    best_path = OUTPUT_DIR / f"research_best_{timestamp}.csv"
    results.to_csv(results_path, index=False)
    best.to_csv(best_path, index=False)

    print(f"source_checkpoint={DEFAULT_CHECKPOINT}")
    print(f"results={results_path}")
    print(f"best={best_path}")
    print(f"conditions={len(results)}")
    print()
    print("TOP20")
    cols = [
        "ranking_rule",
        "robust_score",
        "candidate_count",
        "trade_count",
        "pf",
        "win_rate_pct",
        "expectancy_pct",
        "max_dd_pct",
        "ending_equity_yen",
        "capital_utilization_pct",
        "top10_removed_pf",
        "top20_removed_pf",
        "pf_2021_2022",
        "pf_2023_2024",
        "pf_2025_2026",
    ]
    print(results[cols].head(20).round(3).to_string(index=False))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fast parameter research from existing backtest checkpoints.")
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT, help="candidate checkpoint JSONL path")
    parser.add_argument("--meta", type=Path, default=DEFAULT_META, help="matching checkpoint meta JSON path")
    return parser.parse_args()


def _load_meta(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"checkpoint meta not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_checkpoint(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"checkpoint not found: {path}")

    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            record = json.loads(line)
            rows.extend(record.get("trades", []))

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError(f"checkpoint has no candidate trades: {path}")

    numeric_cols = [
        "entry_idx",
        "exit_idx",
        "entry_price",
        "exit_price",
        "pnl_pct",
        "hold_days",
        "dist_52w_high_pct",
        "ma25_gap_pct",
        "volume_ratio_5d_20d",
        "turnover_20d_avg",
        "score",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in ("entry_date", "exit_date"):
        df[col] = pd.to_datetime(df[col], errors="coerce")

    df["code"] = df["code"].astype(str)
    return df.dropna(subset=["entry_date", "exit_date", "entry_price", "pnl_pct"])


def _validate_source(meta: dict, candidates: pd.DataFrame) -> None:
    params = meta.get("params", {})
    if params.get("timeout_bdays") != 20:
        raise RuntimeError("research.py first version requires timeout_bdays=20 checkpoint")
    if meta.get("limit") != "ALL":
        raise RuntimeError("research.py first version requires ALL-universe checkpoint")
    required = [
        "entry_date",
        "exit_date",
        "entry_price",
        "pnl_pct",
        "ma25_gap_pct",
        "dist_52w_high_pct",
        "volume_ratio_5d_20d",
        "turnover_20d_avg",
        "score",
    ]
    missing = [col for col in required if col not in candidates.columns]
    if missing:
        raise RuntimeError(f"checkpoint missing required columns: {missing}")


def _filter_electric_volume_only(candidates: pd.DataFrame) -> pd.DataFrame:
    mask = (
        (candidates["turnover_20d_avg"] >= 1_000_000_000)
        & (
            (candidates["sector"] != "電気機器")
            | (candidates["volume_ratio_5d_20d"] >= ELECTRIC_VOLUME_MIN)
        )
    )
    return candidates.loc[mask].copy()


def _simulate_portfolio(candidates: pd.DataFrame, ranking_rule: str, allocations: tuple[int, int, int]) -> pd.DataFrame:
    if candidates.empty:
        return pd.DataFrame()

    sort_cols, ascending = _ranking_sort_spec(ranking_rule)
    ordered = candidates.sort_values(sort_cols, ascending=ascending)
    cash = float(INITIAL_CAPITAL)
    open_positions: list[dict] = []
    closed: list[dict] = []

    for entry_date, day_candidates in ordered.groupby("entry_date", sort=True):
        remaining_positions = []
        for pos in open_positions:
            if pos["exit_date"] <= entry_date:
                cash += pos["entry_amount"] + pos["pnl_yen"]
                closed.append(pos)
            else:
                remaining_positions.append(pos)
        open_positions = remaining_positions

        open_codes = {pos["code"] for pos in open_positions}
        for _, row in day_candidates.iterrows():
            if len(open_positions) >= MAX_POSITIONS:
                break
            code = str(row["code"])
            if code in open_codes:
                continue
            slot_index = len(open_positions)
            if slot_index >= len(allocations):
                break
            slot_capital = allocations[slot_index]
            if slot_capital <= 0:
                break
            shares = int(slot_capital // row["entry_price"] // ROUND_LOT * ROUND_LOT)
            if shares <= 0:
                continue
            entry_amount = float(shares * row["entry_price"])
            if entry_amount > cash:
                continue

            pnl_yen = entry_amount * float(row["pnl_pct"]) / 100.0
            position = row.to_dict()
            position.update(
                {
                    "research_rank": slot_index + 1,
                    "slot_capital": slot_capital,
                    "shares": shares,
                    "entry_amount": entry_amount,
                    "pnl_yen": pnl_yen,
                }
            )
            cash -= entry_amount
            open_positions.append(position)
            open_codes.add(code)

    closed.extend(open_positions)
    return pd.DataFrame(closed)


def _ranking_sort_spec(ranking_rule: str) -> tuple[list[str], list[bool]]:
    if ranking_rule not in RANKING_RULES:
        raise ValueError(f"unknown ranking_rule: {ranking_rule}")
    cols = ["entry_date", *RANKING_RULES[ranking_rule], "code"]
    ascending = [True]
    ascending.extend([False if col in {"turnover_20d_avg", "score"} else True for col in RANKING_RULES[ranking_rule]])
    ascending.append(True)
    return cols, ascending


def _build_result_row(run_id: str, checkpoint_path: Path, ranking_rule: str, filtered: pd.DataFrame, trades: pd.DataFrame) -> dict:
    metrics = _compute_metrics(trades)
    top10_metrics = _compute_metrics(_remove_top_profit(trades, 10))
    top20_metrics = _compute_metrics(_remove_top_profit(trades, 20))
    period_metrics = {
        period: _compute_metrics(_period_slice(trades, start_year, end_year))
        for period, (start_year, end_year) in {
            "2021_2022": (2021, 2022),
            "2023_2024": (2023, 2024),
            "2025_2026": (2025, 2026),
        }.items()
    }

    return {
        "run_id": run_id,
        "source_checkpoint": str(checkpoint_path.relative_to(PROJECT_ROOT)),
        "timeout_bdays": 20,
        "ranking_rule": ranking_rule,
        "candidate_count": int(len(filtered)),
        "trade_count": metrics["n"],
        "pf": metrics["pf"],
        "win_rate_pct": metrics["win_rate_pct"],
        "expectancy_pct": metrics["expectancy_pct"],
        "max_dd_pct": metrics["max_dd_pct"],
        "ending_equity_yen": metrics["ending_equity_yen"],
        "total_pnl_yen": metrics["total_pnl_yen"],
        "avg_holdings": metrics["avg_holdings"],
        "capital_utilization_pct": metrics["capital_utilization_pct"],
        "cash_ratio_pct": metrics["cash_ratio_pct"],
        "monthly_trades_avg": metrics["monthly_trades_avg"],
        "top10_removed_pf": top10_metrics["pf"],
        "top20_removed_pf": top20_metrics["pf"],
        "pf_2021_2022": period_metrics["2021_2022"]["pf"],
        "pf_2023_2024": period_metrics["2023_2024"]["pf"],
        "pf_2025_2026": period_metrics["2025_2026"]["pf"],
        "allocation_name": "equal_100_100_100",
        "slot1_yen": ALLOCATIONS["equal_100_100_100"][0],
        "slot2_yen": ALLOCATIONS["equal_100_100_100"][1],
        "slot3_yen": ALLOCATIONS["equal_100_100_100"][2],
        "stop7_rate_pct": metrics["stop7_rate_pct"],
        "timeout_avg_profit_pct": metrics["timeout_avg_profit_pct"],
    }


def _compute_metrics(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {
            "n": 0,
            "pf": np.nan,
            "win_rate_pct": np.nan,
            "expectancy_pct": np.nan,
            "max_dd_pct": np.nan,
            "ending_equity_yen": INITIAL_CAPITAL,
            "total_pnl_yen": 0.0,
            "avg_holdings": 0.0,
            "capital_utilization_pct": 0.0,
            "cash_ratio_pct": 100.0,
            "monthly_trades_avg": 0.0,
            "stop7_rate_pct": np.nan,
            "timeout_avg_profit_pct": np.nan,
        }

    trades = trades.copy()
    trades["pnl_pct"] = pd.to_numeric(trades["pnl_pct"], errors="coerce")
    trades["pnl_yen"] = pd.to_numeric(trades["pnl_yen"], errors="coerce")
    wins = trades[trades["pnl_yen"] > 0]
    losses = trades[trades["pnl_yen"] <= 0]
    gross_win = wins["pnl_yen"].sum()
    gross_loss = -losses["pnl_yen"].sum()
    pf = gross_win / gross_loss if gross_loss else np.nan

    realized = trades.sort_values(["exit_date", "entry_date"])["pnl_yen"].fillna(0)
    equity = np.array([INITIAL_CAPITAL] + list(INITIAL_CAPITAL + realized.cumsum()), dtype=float)
    peaks = np.maximum.accumulate(equity)
    max_dd_pct = float(((equity - peaks) / peaks * 100).min())

    avg_holdings, utilization = _capital_usage(trades)
    months = pd.period_range(trades["entry_date"].min().to_period("M"), trades["exit_date"].max().to_period("M"))
    monthly_trades_avg = len(trades) / len(months) if len(months) else 0.0
    timeout = trades[trades["exit_reason"] == "timeout"]

    return {
        "n": int(len(trades)),
        "pf": float(pf) if pd.notna(pf) else np.nan,
        "win_rate_pct": float((trades["pnl_yen"] > 0).mean() * 100),
        "expectancy_pct": float((trades["pnl_yen"].sum() / trades["entry_amount"].sum()) * 100),
        "max_dd_pct": max_dd_pct,
        "ending_equity_yen": float(INITIAL_CAPITAL + trades["pnl_yen"].sum()),
        "total_pnl_yen": float(trades["pnl_yen"].sum()),
        "avg_holdings": avg_holdings,
        "capital_utilization_pct": utilization,
        "cash_ratio_pct": 100.0 - utilization,
        "monthly_trades_avg": float(monthly_trades_avg),
        "stop7_rate_pct": float((trades["exit_reason"] == "stop7").mean() * 100),
        "timeout_avg_profit_pct": float(timeout["pnl_pct"].mean()) if len(timeout) else np.nan,
    }


def _capital_usage(trades: pd.DataFrame) -> tuple[float, float]:
    if trades.empty:
        return 0.0, 0.0
    start = trades["entry_date"].min()
    end = trades["exit_date"].max()
    business_days = pd.bdate_range(start, end)
    if len(business_days) == 0:
        return 0.0, 0.0
    holdings = []
    invested = []
    for day in business_days:
        open_positions = trades[(trades["entry_date"] <= day) & (trades["exit_date"] > day)]
        holdings.append(len(open_positions))
        invested.append(open_positions["entry_amount"].sum())
    return float(np.mean(holdings)), float(np.mean(invested) / INITIAL_CAPITAL * 100)


def _remove_top_profit(trades: pd.DataFrame, n: int) -> pd.DataFrame:
    if trades.empty or len(trades) <= n:
        return trades.iloc[0:0].copy()
    return trades.drop(trades.nlargest(n, "pnl_yen").index)


def _period_slice(trades: pd.DataFrame, start_year: int, end_year: int) -> pd.DataFrame:
    if trades.empty:
        return trades
    years = trades["entry_date"].dt.year
    return trades[(years >= start_year) & (years <= end_year)]


def _robust_score(row: pd.Series) -> float:
    period_pfs = [row["pf_2021_2022"], row["pf_2023_2024"], row["pf_2025_2026"]]
    period_pfs = [float(x) for x in period_pfs if pd.notna(x)]
    if period_pfs:
        min_pf = min(period_pfs)
        std_pf = float(np.std(period_pfs))
        floor_score = _clamp((min_pf - 1.0) / 0.8) * 70.0
        stability_score = _clamp(1.0 - std_pf / 0.6) * 30.0
        period_score = floor_score + stability_score
    else:
        period_score = 0.0

    top10_score = _clamp((float(row["top10_removed_pf"]) - 1.0) / 0.8) * 100.0
    dd_score = _clamp((float(row["max_dd_pct"]) + 25.0) / 20.0) * 100.0
    utilization_score = _clamp(float(row["capital_utilization_pct"]) / 70.0) * 100.0
    pf_score = _clamp((float(row["pf"]) - 1.0) / 1.0) * 100.0
    return round(
        period_score * 0.35
        + top10_score * 0.25
        + dd_score * 0.20
        + utilization_score * 0.10
        + pf_score * 0.10,
        3,
    )


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    if pd.isna(value):
        return lower
    return max(lower, min(upper, value))


def _select_best(results: pd.DataFrame) -> pd.DataFrame:
    eligible = results[
        (results["trade_count"] >= 100)
        & (results["pf"] >= 1.3)
        & (results["top10_removed_pf"] >= 1.1)
        & (results["max_dd_pct"] >= -15.0)
        & (results["capital_utilization_pct"] >= 50.0)
        & (results["pf_2021_2022"] >= 1.0)
        & (results["pf_2023_2024"] >= 1.0)
        & (results["pf_2025_2026"] >= 1.0)
    ].copy()
    if eligible.empty:
        eligible = results.head(20).copy()
    return eligible.sort_values("robust_score", ascending=False)


if __name__ == "__main__":
    main()
