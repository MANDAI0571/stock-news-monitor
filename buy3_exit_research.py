from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

import pandas as pd

from buy3_combo_research import Rule, filter_rule
from claude_shadow_exits import EXIT_VARIANTS, simulate_exit as simulate_shadow_exit


def parse_rule_id(rule_id: str) -> Optional[Rule]:
    if rule_id in {"baseline_buy", "phase2_plus_combo"}:
        return None
    parts = rule_id.split("__")
    if len(parts) != 6:
        raise ValueError(f"invalid rule_id: {rule_id}")
    family, decision_scope, rank_scope, score, volume, distance = parts
    return Rule(
        family=family,
        decision_scope=decision_scope,
        rank_scope=rank_scope,
        score_min=float(score.removeprefix("s")),
        volume_min=float(volume.removeprefix("v")),
        dist52_max=float(distance.removeprefix("d")),
    )


def load_histories(candidates: pd.DataFrame, cache_dir: Path) -> dict[str, pd.DataFrame]:
    histories: dict[str, pd.DataFrame] = {}
    for ticker in sorted(candidates["ticker"].dropna().astype(str).unique()):
        path = cache_dir / f"{ticker}.parquet"
        if not path.exists():
            continue
        history = pd.read_parquet(path).sort_index()
        history.index = pd.to_datetime(history.index).normalize()
        histories[ticker] = history
    return histories


def common_entry_cutoff(histories: dict[str, pd.DataFrame], timeout_days: int) -> pd.Timestamp:
    # entry + timeout判定日 + 翌朝の約定が必要。
    cutoffs = [history.index[-(timeout_days + 2)] for history in histories.values() if len(history) >= timeout_days + 2]
    if not cutoffs:
        raise ValueError("price histories are too short")
    return pd.Timestamp(min(cutoffs)).normalize()


def simulate_trade(
    row: pd.Series,
    history: pd.DataFrame,
    stop_loss_pct: float,
    take_profit_pct: float,
    timeout_days: int,
    slippage_roundtrip_pct: float,
    exit_variant_key: str = "current_close_5_tp12_t15",
) -> dict[str, object] | None:
    entry_date = pd.Timestamp(str(row["entry_date"])).normalize()
    if entry_date not in history.index:
        return None
    entry_pos = int(history.index.get_loc(entry_date))
    entry_open = float(row["entry_open"])
    if entry_open <= 0:
        return None

    variants = {variant.key: variant for variant in EXIT_VARIANTS}
    if exit_variant_key not in variants:
        raise ValueError(f"unknown exit variant: {exit_variant_key}")
    simulated = simulate_shadow_exit(
        history,
        entry_date=entry_date.date(),
        entry_price=entry_open,
        variant=variants[exit_variant_key],
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        timeout_days=timeout_days,
        roundtrip_cost_pct=slippage_roundtrip_pct,
    )
    if simulated.get("status") != "CLOSED":
        return None
    decision_stamp = pd.Timestamp(str(simulated["exit_signal_date"])).normalize()
    decision_pos = int(history.index.get_loc(decision_stamp))
    exit_stamp = pd.Timestamp(str(simulated["exit_date"])).normalize()
    exit_pos = int(history.index.get_loc(exit_stamp))
    exit_open = float(simulated["exit_price"])
    decision_close = float(history["Close"].iloc[decision_pos])
    decision_return = (decision_close / entry_open - 1) * 100
    fill_gap = (exit_open / decision_close - 1) * 100
    return {
        **row.to_dict(),
        "exit_variant": exit_variant_key,
        "exit_type_sim": simulated["exit_type"],
        "decision_date_sim": history.index[decision_pos].date().isoformat(),
        "exit_date_sim": history.index[exit_pos].date().isoformat(),
        "held_business_days_sim": simulated["holding_business_days"],
        "decision_return_pct_sim": round(decision_return, 4),
        "exit_open_sim": round(exit_open, 4),
        "fill_gap_pct_sim": round(fill_gap, 4),
        "gross_return_pct_sim": simulated["gross_return_pct"],
        "net_return_pct_sim": simulated["net_return_pct"],
        "mfe_pct_sim": simulated["mfe_pct"],
        "mae_pct_sim": simulated["mae_pct"],
    }


def simulate_portfolio(
    candidates: pd.DataFrame,
    histories: dict[str, pd.DataFrame],
    *,
    stop_loss_pct: float,
    take_profit_pct: float,
    timeout_days: int,
    slippage_roundtrip_pct: float,
    max_positions: int,
    cooldown_days: int,
    exit_variant_key: str = "current_close_5_tp12_t15",
) -> tuple[pd.DataFrame, str]:
    all_dates = sorted(candidates["asof_date"].astype(str).unique())
    date_position = {date: position for position, date in enumerate(all_dates)}
    cutoff = common_entry_cutoff(histories, timeout_days)
    candidates = candidates[pd.to_datetime(candidates["entry_date"]) <= cutoff].copy()
    active: list[dict[str, object]] = []
    last_entry_position: dict[str, int] = {}
    trades: list[dict[str, object]] = []

    for entry_date_text, day in candidates.groupby("entry_date", sort=True):
        entry_date = pd.Timestamp(str(entry_date_text)).normalize()
        active = [trade for trade in active if pd.Timestamp(str(trade["exit_date_sim"])) > entry_date]
        active_codes = {str(trade["code"]) for trade in active}
        active_sectors = {str(trade.get("sector", "")).strip() for trade in active if str(trade.get("sector", "")).strip()}
        available = max_positions - len(active)
        if available <= 0:
            continue
        sort_columns = ["score", "confidence", "dist_52w_high_pct"]
        sort_ascending = [False, False, True]
        if "_strategy_priority" in day.columns:
            sort_columns.insert(0, "_strategy_priority")
            sort_ascending.insert(0, True)
        day = day.sort_values(sort_columns, ascending=sort_ascending)
        for _, row in day.iterrows():
            if available <= 0:
                break
            ticker = str(row["ticker"])
            code = str(row["code"])
            sector = str(row.get("sector", "")).strip()
            if code in active_codes or ticker not in histories:
                continue
            if sector and sector in active_sectors:
                continue
            asof_position = date_position[str(row["asof_date"])]
            previous = last_entry_position.get(code)
            if previous is not None and asof_position - previous <= cooldown_days:
                continue
            trade = simulate_trade(
                row,
                histories[ticker],
                stop_loss_pct,
                take_profit_pct,
                timeout_days,
                slippage_roundtrip_pct,
                exit_variant_key,
            )
            if trade is None:
                continue
            trades.append(trade)
            active.append(trade)
            active_codes.add(code)
            if sector:
                active_sectors.add(sector)
            last_entry_position[code] = asof_position
            available -= 1
    return pd.DataFrame(trades), cutoff.date().isoformat()


def metrics(trades: pd.DataFrame) -> dict[str, object]:
    if trades.empty:
        return {"count": 0}
    returns = pd.to_numeric(trades["net_return_pct_sim"], errors="coerce").dropna()
    gains = returns[returns > 0].sum()
    losses = returns[returns < 0].sum()
    stops = trades[trades["exit_type_sim"].astype(str).str.startswith("STOP_LOSS")]
    return {
        "count": int(len(trades)),
        "unique_symbols": int(trades["code"].astype(str).nunique()),
        "win_rate_pct": round(float((returns > 0).mean() * 100), 2),
        "avg_return_net_pct": round(float(returns.mean()), 4),
        "median_return_net_pct": round(float(returns.median()), 4),
        "profit_factor": round(float(gains / abs(losses)), 4) if losses < 0 else "",
        "worst_return_net_pct": round(float(returns.min()), 4),
        "avg_mfe_pct": round(float(pd.to_numeric(trades["mfe_pct_sim"], errors="coerce").mean()), 4),
        "avg_mae_pct": round(float(pd.to_numeric(trades["mae_pct_sim"], errors="coerce").mean()), 4),
        "stop_count": int(len(stops)),
        "stop_minus_5_to_7_count": int(pd.to_numeric(stops["gross_return_pct_sim"], errors="coerce").between(-7, -5).sum()),
        "stop_below_minus_10_count": int((pd.to_numeric(stops["gross_return_pct_sim"], errors="coerce") < -10).sum()),
        "take_profit_count": int(trades["exit_type_sim"].eq("TAKE_PROFIT").sum()),
        "trailing_stop_count": int(trades["exit_type_sim"].eq("TRAILING_STOP").sum()),
        "timeout_count": int(trades["exit_type_sim"].eq("TIMEOUT").sum()),
    }


def write_report(
    output: Path,
    rule_id: str,
    source: Path,
    cutoff: str,
    exit_variant: str,
    summary: dict[str, object],
) -> None:
    lines = [
        "# Phase 2 exit-rule simulation",
        "",
        f"- rule: `{rule_id}`",
        f"- source: {source}",
        f"- exit variant: `{exit_variant}`",
        f"- unbiased entry cutoff: {cutoff}",
        "- execution: next-open entry; fixed free-daily-OHLC shadow variant; close signals exit next open",
        "- constraints: max 3 open positions, no duplicate open code, 5-business-day re-entry cooldown",
        "- cost assumption: round-trip 0.30% deducted",
        "",
        "| metric | value |",
        "|---|---:|",
    ]
    lines.extend(f"| {key} | {value} |" for key, value in summary.items())
    lines.append("")
    output.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2の実際の出口ルールと3枠制約で候補条件を再検証")
    parser.add_argument("--detail", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--rule-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--universe", type=Path)
    parser.add_argument("--stop-loss-pct", type=float, default=-5.0)
    parser.add_argument("--take-profit-pct", type=float, default=12.0)
    parser.add_argument("--timeout-days", type=int, default=15)
    parser.add_argument("--slippage-roundtrip-pct", type=float, default=0.30)
    parser.add_argument("--max-positions", type=int, default=3)
    parser.add_argument("--cooldown-days", type=int, default=5)
    parser.add_argument(
        "--exit-variant",
        choices=[variant.key for variant in EXIT_VARIANTS],
        default="current_close_5_tp12_t15",
    )
    args = parser.parse_args()

    detail = pd.read_csv(args.detail, dtype={"code": str, "ticker": str})
    if args.universe:
        universe = pd.read_csv(args.universe, dtype={"code": str, "ticker": str})
        sector_map = universe.drop_duplicates("ticker").set_index("ticker")["sector"]
        detail["sector"] = detail["ticker"].map(sector_map).fillna("")
    rule = parse_rule_id(args.rule_id)
    if args.rule_id == "baseline_buy":
        candidates = detail[detail["decision"].astype(str).str.upper().eq("BUY")].copy()
    elif args.rule_id == "phase2_plus_combo":
        combo_rule = parse_rule_id("tag_25ma_52w_pullback__watch__A__s80__v1.1__d7")
        combo = filter_rule(detail, combo_rule)
        combo["_strategy_priority"] = 0
        baseline = detail[
            detail["decision"].astype(str).str.upper().eq("BUY")
            & pd.to_numeric(detail["score"], errors="coerce").ge(105)
            & detail["buy_reason"].fillna("").astype(str).str.contains("売買代金10億円以上", regex=False)
            & pd.to_numeric(detail["volume_ratio_5d_20d"], errors="coerce").ge(1.15)
            & pd.to_numeric(detail["dist_25ma_pct"], errors="coerce").between(0, 8)
        ].copy()
        baseline["_strategy_priority"] = 1
        candidates = pd.concat([combo, baseline], ignore_index=True).drop_duplicates(
            ["asof_date", "code"], keep="first"
        )
    else:
        candidates = filter_rule(detail, rule)
    histories = load_histories(candidates, args.cache_dir)
    trades, cutoff = simulate_portfolio(
        candidates,
        histories,
        stop_loss_pct=args.stop_loss_pct,
        take_profit_pct=args.take_profit_pct,
        timeout_days=args.timeout_days,
        slippage_roundtrip_pct=args.slippage_roundtrip_pct,
        max_positions=args.max_positions,
        cooldown_days=args.cooldown_days,
        exit_variant_key=args.exit_variant,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    trades.to_csv(args.output, index=False, encoding="utf-8-sig")
    summary = metrics(trades)
    write_report(args.output, args.rule_id, args.detail, cutoff, args.exit_variant, summary)
    print(
        f"buy3_exit_research variant={args.exit_variant} count={summary.get('count', 0)} "
        f"cutoff={cutoff} output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
