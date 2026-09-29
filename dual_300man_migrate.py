"""2026-09-28再スタート直後のCSVを第2期スキーマへ一度だけ移行する。"""

from __future__ import annotations

from datetime import date

import pandas as pd

from dual_300man_config import CONFIG, JOURNAL_COLUMNS, ORDER_COLUMNS, git_commit
from dual_300man_fill import account_paths, read_csv, write_csv, write_ledger


def _num(value: object) -> float | None:
    try:
        out = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return out if pd.notna(out) else None


def migrate(account: str) -> tuple[int, int]:
    orders_path, journal_path, _ = account_paths(account)
    raw_orders = pd.read_csv(orders_path, dtype=str).fillna("") if orders_path.exists() else pd.DataFrame()
    raw_journal = pd.read_csv(journal_path, dtype=str).fillna("") if journal_path.exists() else pd.DataFrame()
    orders = raw_orders.reindex(columns=ORDER_COLUMNS).fillna("")
    journal = raw_journal.reindex(columns=JOURNAL_COLUMNS).fillna("")
    migrated_orders = migrated_rows = 0
    for idx, row in orders.iterrows():
        try:
            execution_date = date.fromisoformat(str(row.get("execution_date", "")))
        except ValueError:
            continue
        if execution_date < CONFIG.restart_date:
            continue
        if not str(row.get("phase_id", "")).strip():
            orders.at[idx, "phase_id"] = CONFIG.phase_id
            orders.at[idx, "strategy_version"] = CONFIG.strategy_version
            orders.at[idx, "rule_hash"] = CONFIG.rule_hash
            orders.at[idx, "declaration_commit"] = f"migration-{git_commit()}"
            orders.at[idx, "status_note"] = "第2期開始コミットから移行"
            migrated_orders += 1
        if str(row.get("status", "")).upper() == "FILLED":
            matches = journal[
                journal["code"].eq(str(row.get("code", "")))
                & journal["source_order_date"].eq(str(row.get("decision_date", "")))
            ]
            if not matches.empty:
                filled = matches.iloc[0]
                price = _num(filled.get("entry_price"))
                decision = _num(row.get("decision_price"))
                orders.at[idx, "fill_date"] = str(filled.get("entry_date", ""))
                if price:
                    orders.at[idx, "fill_price"] = f"{price:.2f}"
                if price and decision:
                    orders.at[idx, "fill_gap_pct"] = f"{(price / decision - 1) * 100:.2f}"
    for idx, row in journal.iterrows():
        try:
            entry_date = date.fromisoformat(str(row.get("entry_date", "")))
        except ValueError:
            continue
        if entry_date < CONFIG.restart_date:
            continue
        buys = orders[
            orders["side"].str.upper().eq("BUY")
            & orders["code"].eq(str(row.get("code", "")))
            & orders["decision_date"].eq(str(row.get("source_order_date", "")))
        ]
        if buys.empty:
            continue
        order = buys.iloc[-1]
        for key in ("phase_id", "strategy_version", "rule_hash"):
            journal.at[idx, key] = str(order.get(key, ""))
        entry_price = _num(row.get("entry_price"))
        shares = _num(row.get("shares"))
        decision = _num(order.get("decision_price"))
        if entry_price and shares:
            value = entry_price * shares
            stop_risk = value * abs(CONFIG.stop_loss_pct) / 100
            journal.at[idx, "position_pct_initial"] = f"{value / CONFIG.initial_cash * 100:.2f}"
            journal.at[idx, "planned_stop_risk_jpy"] = f"{stop_risk:.0f}"
            journal.at[idx, "planned_stop_risk_pct_initial"] = f"{stop_risk / CONFIG.initial_cash * 100:.2f}"
            journal.at[idx, "last_mark_date"] = entry_date.isoformat()
            journal.at[idx, "mark_price"] = f"{entry_price:.2f}"
            journal.at[idx, "unrealized_pnl"] = "0"
            journal.at[idx, "unrealized_return_pct"] = "0.00"
            journal.at[idx, "peak_price"] = f"{entry_price:.2f}"
            journal.at[idx, "peak_date"] = entry_date.isoformat()
            journal.at[idx, "mfe_pct"] = "0.00"
            journal.at[idx, "mfe_peak_business_day"] = "0"
            journal.at[idx, "trough_price"] = f"{entry_price:.2f}"
            journal.at[idx, "trough_date"] = entry_date.isoformat()
            journal.at[idx, "mae_pct"] = "0.00"
        if decision:
            journal.at[idx, "entry_decision_price"] = f"{decision:.2f}"
        if entry_price and decision:
            journal.at[idx, "entry_gap_pct"] = f"{(entry_price / decision - 1) * 100:.2f}"
        migrated_rows += 1
    write_csv(orders, orders_path, ORDER_COLUMNS)
    write_csv(journal, journal_path, JOURNAL_COLUMNS)
    write_ledger(account, orders, journal)
    return migrated_orders, migrated_rows


def main() -> None:
    for account in ("codex", "claude"):
        order_count, row_count = migrate(account)
        print(f"dual_300man_migrate account={account} orders={order_count} journal={row_count}")


if __name__ == "__main__":
    main()
