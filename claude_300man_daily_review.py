"""Claude 300万円ペーパー運用を毎日監視し、改善候補を安全に評価する。"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from claude_300man_shadow import SHADOW_COLUMNS, SHADOW_PATH
from claude_shadow_exits import EXIT_VARIANTS
from dual_300man_config import CONFIG, JOURNAL_COLUMNS, ORDER_COLUMNS, ROOT
from dual_300man_fill import business_days_between, cash_balance, read_csv


JST = ZoneInfo("Asia/Tokyo")
HISTORY_PATH = ROOT / "data" / "claude_300man_daily_monitor.csv"
CANDIDATES_PATH = ROOT / "data" / "claude_300man_improvement_candidates.csv"
REPORT_PATH = ROOT / "docs" / "claude_300man_daily_monitor.md"
BASELINE_VARIANT = "current_close_5_tp12_t15"
MIN_PROMOTION_CLOSED = 40
MIN_AVG_EDGE_PCT = 0.50
BOOTSTRAP_SAMPLES = 4_000

HISTORY_COLUMNS = [
    "review_date", "phase_id", "strategy_version", "rule_hash", "health",
    "critical_count", "watch_count", "open_positions", "closed_trades",
    "declared_due", "overdue_declared", "cash_jpy", "total_assets_jpy",
    "realized_pnl_jpy", "unrealized_pnl_jpy", "win_rate_pct", "avg_win_pct",
    "avg_loss_pct", "payoff_ratio", "profit_factor", "expectancy_pct",
    "consecutive_losses", "stop_count", "stop_in_minus_5_to_7_count",
    "stop_below_minus_10_count", "take_profit_count", "trailing_count",
    "timeout_count", "timeout_share_pct", "stale_mark_count",
    "shadow_review_ready_count", "alerts", "updated_at_jst",
]

CANDIDATE_COLUMNS = [
    "review_date", "variant", "variant_label", "closed", "paired_with_current",
    "avg_net_0_30_pct", "avg_net_0_60_pct", "avg_net_1_00_pct",
    "profit_factor_0_30", "worst_net_0_30_pct", "below_minus_10_count",
    "avg_edge_vs_current_pct", "bootstrap_95_low_pct", "bootstrap_95_high_pct",
    "decision", "reason",
]


def _numbers(frame: pd.DataFrame, column: str) -> pd.Series:
    if frame.empty or column not in frame.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(frame[column], errors="coerce").dropna()


def _metric(value: float | int | None, digits: int = 4) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value):.{digits}f}"


def _profit_factor(returns: pd.Series) -> float | None:
    gains = float(returns[returns > 0].sum())
    losses = float(returns[returns < 0].sum())
    if losses < 0:
        return gains / abs(losses)
    return None


def _entry_key(frame: pd.DataFrame) -> pd.Series:
    columns = ["phase_id", "source_order_date", "entry_date", "code"]
    values = [frame.get(column, pd.Series("", index=frame.index)).astype(str) for column in columns]
    key = values[0]
    for value in values[1:]:
        key = key + "|" + value
    return key


def _bootstrap_interval(differences: pd.Series) -> tuple[float | None, float | None]:
    values = pd.to_numeric(differences, errors="coerce").dropna().to_numpy(dtype=float)
    if len(values) < 2:
        return None, None
    rng = np.random.default_rng(300)
    samples = rng.choice(values, size=(BOOTSTRAP_SAMPLES, len(values)), replace=True).mean(axis=1)
    low, high = np.quantile(samples, [0.025, 0.975])
    return float(low), float(high)


def evaluate_improvements(shadow: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """40組の同一エントリーが揃うまで改善候補を昇格させない。"""
    labels = {variant.key: variant.label for variant in EXIT_VARIANTS}
    closed = shadow[shadow.get("status", pd.Series(dtype=str)).eq("CLOSED")].copy()
    if not closed.empty:
        closed["_entry_key"] = _entry_key(closed)
        closed["_net"] = pd.to_numeric(closed["net_return_pct"], errors="coerce")
        closed["_gross"] = pd.to_numeric(closed["gross_return_pct"], errors="coerce")

    by_variant: dict[str, pd.DataFrame] = {
        key: closed[closed["variant"].eq(key)].dropna(subset=["_net", "_gross"])
        if not closed.empty else pd.DataFrame()
        for key in labels
    }
    baseline = by_variant[BASELINE_VARIANT]
    baseline_returns = _numbers(baseline, "_net")
    baseline_pf = _profit_factor(baseline_returns)
    baseline_worst = float(baseline_returns.min()) if len(baseline_returns) else None
    baseline_below_ten = int((_numbers(baseline, "_gross") < -10).sum())
    baseline_keyed = (
        baseline.drop_duplicates("_entry_key", keep="last").set_index("_entry_key")["_net"]
        if not baseline.empty else pd.Series(dtype=float)
    )

    rows: list[dict[str, object]] = []
    for variant in EXIT_VARIANTS:
        part = by_variant[variant.key]
        returns = _numbers(part, "_net")
        gross = _numbers(part, "_gross")
        average = float(returns.mean()) if len(returns) else None
        pf = _profit_factor(returns)
        worst = float(returns.min()) if len(returns) else None
        below_ten = int((gross < -10).sum())
        paired = pd.DataFrame()
        if variant.key == BASELINE_VARIANT:
            paired_count = len(baseline_keyed)
            edge = 0.0 if paired_count else None
            ci_low = ci_high = 0.0 if paired_count else None
            decision = "BASELINE"
            reason = "現行ルール。比較基準として固定"
        else:
            candidate_keyed = (
                part.drop_duplicates("_entry_key", keep="last").set_index("_entry_key")["_net"]
                if not part.empty else pd.Series(dtype=float)
            )
            paired = pd.concat(
                [baseline_keyed.rename("current"), candidate_keyed.rename("candidate")], axis=1,
            ).dropna()
            differences = paired["candidate"] - paired["current"] if not paired.empty else pd.Series(dtype=float)
            paired_count = len(paired)
            edge = float(differences.mean()) if paired_count else None
            ci_low, ci_high = _bootstrap_interval(differences)
            sample_failures: list[str] = []
            if len(returns) < MIN_PROMOTION_CLOSED or len(baseline_returns) < MIN_PROMOTION_CLOSED:
                sample_failures.append(f"決済{MIN_PROMOTION_CLOSED}件未満")
            if paired_count < MIN_PROMOTION_CLOSED:
                sample_failures.append(f"同一入口比較{MIN_PROMOTION_CLOSED}組未満")
            if sample_failures:
                decision = "COLLECTING"
                reason = " / ".join(sample_failures)
            else:
                metric_failures: list[str] = []
                if average is None or average - 0.70 <= 0:
                    metric_failures.append("往復1.00%コストで平均利益非正")
                if edge is None or edge < MIN_AVG_EDGE_PCT:
                    metric_failures.append(f"現行比+{MIN_AVG_EDGE_PCT:.2f}%未満")
                if pf is None or baseline_pf is None or pf < baseline_pf:
                    metric_failures.append("PFが現行以下")
                if worst is None or baseline_worst is None or worst < baseline_worst:
                    metric_failures.append("最悪損失が悪化")
                if below_ten > baseline_below_ten:
                    metric_failures.append("-10%超が増加")
                if ci_low is None or ci_low <= 0:
                    metric_failures.append("差の95%下限が0以下")
                if metric_failures:
                    decision = "HOLD"
                    reason = " / ".join(metric_failures)
                else:
                    decision = "REVIEW_READY"
                    reason = "全昇格条件を通過。人が確認するまで正本は変更しない"
        rows.append({
            "review_date": as_of.isoformat(),
            "variant": variant.key,
            "variant_label": variant.label,
            "closed": len(returns),
            "paired_with_current": paired_count,
            "avg_net_0_30_pct": _metric(average),
            "avg_net_0_60_pct": _metric(average - 0.30 if average is not None else None),
            "avg_net_1_00_pct": _metric(average - 0.70 if average is not None else None),
            "profit_factor_0_30": _metric(pf),
            "worst_net_0_30_pct": _metric(worst),
            "below_minus_10_count": below_ten,
            "avg_edge_vs_current_pct": _metric(edge),
            "bootstrap_95_low_pct": _metric(ci_low),
            "bootstrap_95_high_pct": _metric(ci_high),
            "decision": decision,
            "reason": reason,
        })
    return pd.DataFrame(rows).reindex(columns=CANDIDATE_COLUMNS).fillna("")


def _trailing_loss_count(returns: pd.Series) -> int:
    count = 0
    for value in reversed(returns.tolist()):
        if value < 0:
            count += 1
        else:
            break
    return count


def build_review(
    orders: pd.DataFrame,
    journal: pd.DataFrame,
    shadow: pd.DataFrame,
    *,
    as_of: date,
) -> tuple[dict[str, object], pd.DataFrame, list[tuple[str, str, str]]]:
    current_orders = orders[orders["phase_id"].eq(CONFIG.phase_id)].copy()
    current_journal = journal[journal["phase_id"].eq(CONFIG.phase_id)].copy()
    entry_dates = pd.to_datetime(current_journal.get("entry_date"), errors="coerce")
    current_journal = current_journal[entry_dates.le(pd.Timestamp(as_of))].copy()
    decision_dates = pd.to_datetime(current_orders.get("decision_date"), errors="coerce")
    current_orders = current_orders[decision_dates.le(pd.Timestamp(as_of))].copy()

    statuses = current_journal.get("status", pd.Series(dtype=str)).str.upper()
    open_rows = current_journal[statuses.eq("OPEN")].copy()
    closed = current_journal[statuses.eq("CLOSED")].copy()
    closed = closed.sort_values(["exit_date", "entry_date", "code"])
    returns = _numbers(closed, "exit_return_pct")
    wins = returns[returns > 0]
    losses = returns[returns <= 0]
    avg_win = float(wins.mean()) if len(wins) else None
    avg_loss = float(losses.mean()) if len(losses) else None
    payoff = avg_win / abs(avg_loss) if avg_win is not None and avg_loss not in (None, 0) else None
    expectancy = float(returns.mean()) if len(returns) else None
    pf = _profit_factor(returns)

    order_status = current_orders.get("status", pd.Series(dtype=str)).str.upper()
    pending = current_orders[order_status.eq("DECLARED")].copy()
    execution_dates = pd.to_datetime(pending.get("execution_date"), errors="coerce")
    due = pending[execution_dates.dt.date == as_of] if not pending.empty else pending
    overdue = pending[execution_dates.dt.date <= as_of] if not pending.empty else pending

    exits = closed.get("exit_type", pd.Series(dtype=str)).astype(str)
    stops = closed[exits.str.startswith("STOP_LOSS")]
    stop_returns = _numbers(stops, "exit_return_pct")
    stop_in_band = int(stop_returns.between(-7, -5).sum())
    stop_below_ten = int((stop_returns < -10).sum())
    take_profit = int(exits.eq("TAKE_PROFIT").sum())
    trailing = int(exits.eq("TRAILING_STOP").sum())
    timeout = int(exits.eq("TIMEOUT").sum())
    timeout_share = timeout / len(closed) * 100 if len(closed) else None

    marked_prices = pd.to_numeric(open_rows.get("mark_price"), errors="coerce")
    entry_prices = pd.to_numeric(open_rows.get("entry_price"), errors="coerce")
    shares = pd.to_numeric(open_rows.get("shares"), errors="coerce").fillna(0)
    marks = marked_prices.fillna(entry_prices)
    marked_value = float((marks * shares).sum()) if len(open_rows) else 0.0
    cash = cash_balance(current_journal)
    realized = float(_numbers(closed, "realized_pnl").sum())
    unrealized = float(_numbers(open_rows, "unrealized_pnl").sum())

    stale_mark_count = 0
    for _, row in open_rows.iterrows():
        try:
            mark_day = date.fromisoformat(str(row.get("last_mark_date", "")))
        except ValueError:
            stale_mark_count += 1
            continue
        if business_days_between(mark_day, as_of) > 1:
            stale_mark_count += 1

    candidates = evaluate_improvements(shadow, as_of)
    review_ready = int(candidates["decision"].eq("REVIEW_READY").sum()) if not candidates.empty else 0
    alerts: list[tuple[str, str, str]] = []
    if len(overdue):
        codes = ",".join(overdue["code"].astype(str).tolist())
        alerts.append(("CRITICAL", "OVERDUE_DECLARED", f"執行日を迎えたDECLARED注文 {len(overdue)}件: {codes}"))
    if len(open_rows) > CONFIG.max_positions:
        alerts.append(("CRITICAL", "POSITION_LIMIT", f"保有{len(open_rows)}銘柄 > 上限{CONFIG.max_positions}"))
    if open_rows.get("code", pd.Series(dtype=str)).duplicated().any():
        alerts.append(("CRITICAL", "DUPLICATE_OPEN", "同一銘柄の重複保有を検出"))
    incompatible = pending[
        ~pending["strategy_version"].eq(CONFIG.strategy_version)
        | ~pending["rule_hash"].eq(CONFIG.rule_hash)
    ] if not pending.empty else pending
    if len(incompatible):
        alerts.append(("CRITICAL", "RULE_MISMATCH", f"現行ルールと不一致のDECLARED注文 {len(incompatible)}件"))
    if stop_below_ten:
        alerts.append(("CRITICAL", "STOP_BELOW_MINUS_10", f"-10%超の損切り約定 {stop_below_ten}件"))
    if stale_mark_count:
        alerts.append(("WATCH", "STALE_MARK", f"2営業日以上更新されていない保有価格 {stale_mark_count}件"))
    if len(stops) >= 3 and stop_in_band / len(stops) < 0.80:
        alerts.append(("WATCH", "STOP_CONTAINMENT", f"損切り-5〜-7%収容率 {stop_in_band}/{len(stops)}"))
    if len(closed) >= 5 and timeout_share is not None and timeout_share > 70:
        alerts.append(("WATCH", "TIMEOUT_HEAVY", f"タイムアウト比率 {timeout_share:.1f}%"))
    consecutive_losses = _trailing_loss_count(returns)
    if consecutive_losses >= 3:
        alerts.append(("WATCH", "LOSS_STREAK", f"連続負け {consecutive_losses}回"))
    if len(closed) >= 10 and take_profit + trailing == 0:
        alerts.append(("WATCH", "NO_PROFIT_EXIT", "10決済以上で利確・トレーリングが0回"))
    if review_ready:
        names = ",".join(candidates.loc[candidates["decision"].eq("REVIEW_READY"), "variant"].tolist())
        alerts.append(("WATCH", "IMPROVEMENT_READY", f"人の確認待ちの改善候補: {names}"))

    critical_count = sum(level == "CRITICAL" for level, _, _ in alerts)
    watch_count = sum(level == "WATCH" for level, _, _ in alerts)
    health = "CRITICAL" if critical_count else "WATCH" if watch_count else "HEALTHY"
    snapshot: dict[str, object] = {
        "review_date": as_of.isoformat(),
        "phase_id": CONFIG.phase_id,
        "strategy_version": CONFIG.strategy_version,
        "rule_hash": CONFIG.rule_hash,
        "health": health,
        "critical_count": critical_count,
        "watch_count": watch_count,
        "open_positions": len(open_rows),
        "closed_trades": len(closed),
        "declared_due": len(due),
        "overdue_declared": len(overdue),
        "cash_jpy": _metric(cash, 0),
        "total_assets_jpy": _metric(cash + marked_value, 0),
        "realized_pnl_jpy": _metric(realized, 0),
        "unrealized_pnl_jpy": _metric(unrealized, 0),
        "win_rate_pct": _metric(len(wins) / len(returns) * 100 if len(returns) else None, 2),
        "avg_win_pct": _metric(avg_win),
        "avg_loss_pct": _metric(avg_loss),
        "payoff_ratio": _metric(payoff),
        "profit_factor": _metric(pf),
        "expectancy_pct": _metric(expectancy),
        "consecutive_losses": consecutive_losses,
        "stop_count": len(stops),
        "stop_in_minus_5_to_7_count": stop_in_band,
        "stop_below_minus_10_count": stop_below_ten,
        "take_profit_count": take_profit,
        "trailing_count": trailing,
        "timeout_count": timeout,
        "timeout_share_pct": _metric(timeout_share, 2),
        "stale_mark_count": stale_mark_count,
        "shadow_review_ready_count": review_ready,
        "alerts": " | ".join(f"{level}:{code}:{message}" for level, code, message in alerts),
        "updated_at_jst": f"{as_of.isoformat()}T23:59:59+09:00",
    }
    return snapshot, candidates, alerts


def _display(value: object, suffix: str = "") -> str:
    text = str(value).strip()
    return f"{text}{suffix}" if text else "データ待ち"


def write_report(
    snapshot: dict[str, object],
    candidates: pd.DataFrame,
    alerts: list[tuple[str, str, str]],
) -> None:
    lines = [
        "# Claude 300万円運用 毎日監視・改善レポート", "",
        f"- 基準日: {snapshot['review_date']}",
        f"- 状態: **{snapshot['health']}**（CRITICAL {snapshot['critical_count']} / WATCH {snapshot['watch_count']}）",
        f"- 期: `{snapshot['phase_id']}` / 正本ルール: `{snapshot['strategy_version']}` / `{snapshot['rule_hash']}`",
        "- 安全境界: 監視・記録・workflow補完は自動。売買ルールと資金配分の変更は人の確認なしに行わない", "",
        "## 本日の警報", "",
        "| 重要度 | コード | 内容 |", "|---|---|---|",
    ]
    if alerts:
        lines.extend(f"| {level} | `{code}` | {message} |" for level, code, message in alerts)
    else:
        lines.append("| - | `NONE` | 対応が必要な異常なし |")
    lines.extend([
        "", "## 正本運用", "",
        f"- 総資産: {_display(snapshot['total_assets_jpy'], '円')} / 現金 {_display(snapshot['cash_jpy'], '円')}",
        f"- 保有: {snapshot['open_positions']}銘柄 / 決済: {snapshot['closed_trades']}回 / 未約定期限超過: {snapshot['overdue_declared']}件",
        f"- 実現損益: {_display(snapshot['realized_pnl_jpy'], '円')} / 含み損益: {_display(snapshot['unrealized_pnl_jpy'], '円')}",
        f"- 勝率: {_display(snapshot['win_rate_pct'], '%')} / 平均勝ち {_display(snapshot['avg_win_pct'], '%')} / 平均負け {_display(snapshot['avg_loss_pct'], '%')}",
        f"- 損益比: {_display(snapshot['payoff_ratio'])} / PF {_display(snapshot['profit_factor'])} / 1取引期待値 {_display(snapshot['expectancy_pct'], '%')}",
        f"- 損切り: {snapshot['stop_count']}回、-5〜-7% {snapshot['stop_in_minus_5_to_7_count']}回、-10%超 {snapshot['stop_below_minus_10_count']}回",
        f"- 出口: 利確 {snapshot['take_profit_count']} / 追随 {snapshot['trailing_count']} / 時間切れ {snapshot['timeout_count']}",
        "", "## 改善候補ゲート", "",
        f"40決済・同一入口40組・現行比+{MIN_AVG_EDGE_PCT:.2f}%・PF改善・最悪損失非悪化・-10%超非増加・"
        "往復1.00%コストで平均プラス・差のブートストラップ95%下限>0をすべて必須とします。", "",
        "| 方式 | 決済 | 同一入口 | 平均0.30% | 平均1.00% | PF | 最悪 | 現行差 | 95%CI | 判定 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ])
    for _, row in candidates.iterrows():
        ci = f"{_display(row['bootstrap_95_low_pct'], '%')}〜{_display(row['bootstrap_95_high_pct'], '%')}"
        lines.append(
            f"| {row['variant_label']} | {row['closed']} | {row['paired_with_current']} | "
            f"{_display(row['avg_net_0_30_pct'], '%')} | {_display(row['avg_net_1_00_pct'], '%')} | "
            f"{_display(row['profit_factor_0_30'])} | {_display(row['worst_net_0_30_pct'], '%')} | "
            f"{_display(row['avg_edge_vs_current_pct'], '%')} | {ci} | **{row['decision']}** |"
        )
    lines.extend(["", "### 判定理由", ""])
    lines.extend(f"- `{row['variant']}`: {row['reason']}" for _, row in candidates.iterrows())
    lines.extend([
        "", "## 次の自動動作", "",
        "- CRITICAL: 安全なworkflow補完とユーザー通知の対象",
        "- WATCH: 記録を続け、正本ルールは変更しない",
        "- REVIEW_READY: 改善案を報告するが、承認なしでは反映しない", "",
    ])
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def run(as_of: date) -> dict[str, object]:
    orders = read_csv(ROOT / "data" / "claude_300man_orders.csv", ORDER_COLUMNS)
    journal = read_csv(ROOT / "data" / "claude_300man_journal.csv", JOURNAL_COLUMNS)
    shadow = read_csv(SHADOW_PATH, SHADOW_COLUMNS)
    snapshot, candidates, alerts = build_review(orders, journal, shadow, as_of=as_of)

    history = read_csv(HISTORY_PATH, HISTORY_COLUMNS)
    keep = ~(
        history["review_date"].eq(as_of.isoformat())
        & history["phase_id"].eq(CONFIG.phase_id)
    )
    history = pd.concat([history[keep], pd.DataFrame([snapshot])], ignore_index=True)
    history = history.where(history.notna(), "")
    history.reindex(columns=HISTORY_COLUMNS).to_csv(HISTORY_PATH, index=False, encoding="utf-8-sig")
    candidates.to_csv(CANDIDATES_PATH, index=False, encoding="utf-8-sig")
    write_report(snapshot, candidates, alerts)
    print(
        f"claude_daily_monitor date={as_of} health={snapshot['health']} "
        f"critical={snapshot['critical_count']} watch={snapshot['watch_count']}"
    )
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now(JST).date().isoformat())
    parser.add_argument("--fail-on-critical", action="store_true")
    args = parser.parse_args()
    snapshot = run(date.fromisoformat(args.date))
    return 2 if args.fail_on_critical and snapshot["health"] == "CRITICAL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
