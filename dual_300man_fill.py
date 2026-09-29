"""Codex/Claude 300万円運用の共通・翌寄り約定処理。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from dual_300man_config import CONFIG, JOURNAL_COLUMNS, ORDER_COLUMNS, ROOT, order_compatibility
from jpx_calendar import fetch_open_price_yfinance, is_jpx_business_day


JST = ZoneInfo("Asia/Tokyo")
DEFAULT_STRATEGIES = {"claude": "claude_momentum", "codex": "codex_quality_pullback"}


def account_paths(account: str) -> tuple[Path, Path, Path]:
    if account not in DEFAULT_STRATEGIES:
        raise ValueError(f"unknown account: {account}")
    return (
        ROOT / "data" / f"{account}_300man_orders.csv",
        ROOT / "data" / f"{account}_300man_journal.csv",
        ROOT / "docs" / f"{account}_300man_ledger.md",
    )


def read_csv(path: Path, columns: list[str]) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=columns)
    return pd.read_csv(path, dtype=str).reindex(columns=columns).fillna("")


def write_csv(df: pd.DataFrame, path: Path, columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.reindex(columns=columns).to_csv(path, index=False, encoding="utf-8-sig")


def numbers(series: pd.Series | None) -> pd.Series:
    if series is None:
        return pd.Series(dtype=float)
    return pd.to_numeric(series, errors="coerce").fillna(0)


def cash_balance(journal: pd.DataFrame) -> float:
    if journal.empty:
        return float(CONFIG.initial_cash)
    return float(
        CONFIG.initial_cash
        - numbers(journal.get("position_value")).sum()
        + numbers(journal.get("exit_value")).sum()
    )


def business_days_between(start: date, end: date) -> int:
    if end <= start:
        return 0
    count = 0
    cursor = start + timedelta(days=1)
    while cursor <= end:
        count += int(is_jpx_business_day(cursor))
        cursor += timedelta(days=1)
    return count


def _float(value: object) -> float | None:
    try:
        out = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return out if pd.notna(out) else None


def _cancel(orders: pd.DataFrame, idx: int, status: str, note: str) -> None:
    orders.at[idx, "status"] = status
    orders.at[idx, "status_note"] = note


def _record_order_fill(
    orders: pd.DataFrame,
    idx: int,
    execution_date: date,
    price: float,
    decision_price: float | None,
) -> None:
    orders.at[idx, "status"] = "FILLED"
    orders.at[idx, "fill_date"] = execution_date.isoformat()
    orders.at[idx, "fill_price"] = f"{price:.2f}"
    if decision_price and decision_price > 0:
        orders.at[idx, "fill_gap_pct"] = f"{(price / decision_price - 1) * 100:.2f}"


def _fill_buy(
    account: str,
    orders: pd.DataFrame,
    journal: pd.DataFrame,
    idx: int,
    order: pd.Series,
    execution_date: date,
    price: float,
    shares: int,
) -> tuple[pd.DataFrame, bool]:
    duplicate = (
        journal["entry_date"].eq(execution_date.isoformat())
        & journal["code"].eq(order["code"])
        & journal["source_order_date"].eq(order["decision_date"])
    ).any()
    decision_price = _float(order.get("decision_price"))
    if duplicate:
        _record_order_fill(orders, idx, execution_date, price, decision_price)
        return journal, False
    value = round(price * shares)
    if value > cash_balance(journal):
        _cancel(orders, idx, "CANCELLED_CAPITAL", "約定額が現金残を超過")
        print(f"{account}_300man_fill=cancelled code={order['code']} reason=capital_limit")
        return journal, False
    stop_risk = value * abs(CONFIG.stop_loss_pct) / 100
    row = {
        "phase_id": order.get("phase_id", ""),
        "strategy_version": order.get("strategy_version", ""),
        "rule_hash": order.get("rule_hash", ""),
        "entry_date": execution_date.isoformat(),
        "fill_time_jst": datetime.now(JST).isoformat(timespec="seconds"),
        "status": "OPEN", "code": order["code"], "ticker": order["ticker"],
        "name": order["name"], "sector": order.get("sector", ""),
        "entry_price": f"{price:.2f}", "shares": str(shares), "position_value": str(value),
        "position_pct_initial": f"{value / CONFIG.initial_cash * 100:.2f}",
        "planned_stop_risk_jpy": f"{stop_risk:.0f}",
        "planned_stop_risk_pct_initial": f"{stop_risk / CONFIG.initial_cash * 100:.2f}",
        "strategy": order.get("strategy") or DEFAULT_STRATEGIES[account],
        "source_order_date": order["decision_date"],
        "entry_decision_price": f"{decision_price:.2f}" if decision_price else "",
        "entry_gap_pct": f"{(price / decision_price - 1) * 100:.2f}" if decision_price else "",
        "last_mark_date": execution_date.isoformat(), "mark_price": f"{price:.2f}",
        "unrealized_pnl": "0", "unrealized_return_pct": "0.00",
        "peak_price": f"{price:.2f}", "peak_date": execution_date.isoformat(),
        "mfe_pct": "0.00", "mfe_peak_business_day": "0",
        "trough_price": f"{price:.2f}", "trough_date": execution_date.isoformat(), "mae_pct": "0.00",
    }
    journal = pd.concat([journal, pd.DataFrame([row])], ignore_index=True).fillna("")
    _record_order_fill(orders, idx, execution_date, price, decision_price)
    return journal, True


def _fill_sell(
    account: str,
    orders: pd.DataFrame,
    journal: pd.DataFrame,
    idx: int,
    order: pd.Series,
    execution_date: date,
    price: float,
    shares: int,
) -> tuple[pd.DataFrame, bool]:
    decision_price = _float(order.get("decision_price"))
    if (
        journal["exit_date"].eq(execution_date.isoformat())
        & journal["code"].eq(order["code"])
        & journal["exit_order_date"].eq(order["decision_date"])
    ).any():
        _record_order_fill(orders, idx, execution_date, price, decision_price)
        return journal, False
    open_mask = journal["status"].str.upper().eq("OPEN") & journal["code"].eq(order["code"])
    available = numbers(journal.loc[open_mask, "shares"]).sum()
    if available < shares:
        _cancel(orders, idx, "CANCELLED_POSITION", "売却可能株数が不足")
        print(f"{account}_300man_fill=cancelled code={order['code']} reason=insufficient_position")
        return journal, False
    remaining = shares
    for journal_idx in journal.index[open_mask]:
        if remaining <= 0:
            break
        lot_shares = int(float(journal.at[journal_idx, "shares"]))
        close_shares = min(remaining, lot_shares)
        entry_price = float(journal.at[journal_idx, "entry_price"])
        closed = journal.loc[journal_idx].copy()
        closed["status"] = "CLOSED"
        closed["shares"] = str(close_shares)
        closed["position_value"] = str(round(entry_price * close_shares))
        closed["exit_date"] = execution_date.isoformat()
        closed["exit_price"] = f"{price:.2f}"
        closed["exit_value"] = str(round(price * close_shares))
        closed["realized_pnl"] = str(round((price - entry_price) * close_shares))
        closed["exit_return_pct"] = f"{(price / entry_price - 1) * 100:.2f}"
        closed["exit_order_date"] = order["decision_date"]
        closed["exit_type"] = order.get("exit_type", "")
        closed["exit_reason"] = order.get("reason", "")
        closed["exit_decision_price"] = f"{decision_price:.2f}" if decision_price else ""
        decision_return = (decision_price / entry_price - 1) * 100 if decision_price else None
        closed["exit_decision_return_pct"] = f"{decision_return:.2f}" if decision_return is not None else ""
        closed["exit_gap_pct"] = f"{(price / decision_price - 1) * 100:.2f}" if decision_price else ""
        closed["holding_business_days"] = str(business_days_between(
            date.fromisoformat(str(closed["entry_date"])), execution_date
        ))
        closed["last_mark_date"] = execution_date.isoformat()
        closed["mark_price"] = f"{price:.2f}"
        closed["unrealized_pnl"] = "0"
        closed["unrealized_return_pct"] = "0.00"
        if close_shares == lot_shares:
            journal.loc[journal_idx] = closed
        else:
            journal.at[journal_idx, "shares"] = str(lot_shares - close_shares)
            journal.at[journal_idx, "position_value"] = str(round(entry_price * (lot_shares - close_shares)))
            journal = pd.concat([journal, pd.DataFrame([closed])], ignore_index=True).fillna("")
        remaining -= close_shares
    _record_order_fill(orders, idx, execution_date, price, decision_price)
    return journal, True


def write_ledger(account: str, orders: pd.DataFrame, journal: pd.DataFrame) -> None:
    _, _, ledger_path = account_paths(account)
    open_rows = journal[journal["status"].str.upper().eq("OPEN")] if not journal.empty else journal
    closed_rows = journal[journal["status"].str.upper().eq("CLOSED")] if not journal.empty else journal
    realized = numbers(closed_rows.get("realized_pnl")).sum()
    name = "Claude" if account == "claude" else "Codex"
    lines = [
        f"# {name}が300万円運用 - 運用台帳（正本）", "", f"{name}運用専用のペーパー運用記録です。", "",
        f"- 期: `{CONFIG.phase_id}`", f"- ルール版: `{CONFIG.strategy_version}` / 設定 `{CONFIG.rule_hash}`",
        f"- 再スタート日: {CONFIG.restart_date.isoformat()}", f"- 初期資金: {CONFIG.initial_cash:,}円",
        f"- 現金残: {cash_balance(journal):,.0f}円", f"- 保有: {len(open_rows)}銘柄",
        f"- 実現損益（累計）: {realized:,.0f}円", "", "## 保有一覧", "",
        "| 約定日 | コード | 銘柄 | 株数 | 取得始値 | 投資額 | 投入率 | MFE | MAE |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in open_rows.iterrows():
        lines.append(
            f"| {row['entry_date']} | {row['code']} | {row['name']} | {int(float(row['shares']))}株 | "
            f"{float(row['entry_price']):,.2f}円 | {float(row['position_value']):,.0f}円 | "
            f"{row.get('position_pct_initial') or '-'}% | {row.get('mfe_pct') or '-'}% | {row.get('mae_pct') or '-'}% |"
        )
    if open_rows.empty:
        lines.append("| - | - | なし | - | - | - | - | - | - |")
    lines.extend(["", "## 実現損益", "",
        "| 売却日 | コード | 銘柄 | 実現損益 | 判定時率 | 約定率 | 判定→約定 | 決済理由 | 保有営業日 |",
        "|---|---|---|---:|---:|---:|---:|---|---:|",
    ])
    for _, row in closed_rows.iterrows():
        lines.append(
            f"| {row['exit_date']} | {row['code']} | {row['name']} | {float(row['realized_pnl']):+,.0f}円 | "
            f"{row.get('exit_decision_return_pct') or '-'}% | {row.get('exit_return_pct') or '-'}% | "
            f"{row.get('exit_gap_pct') or '-'}% | {row.get('exit_type') or '-'} | {row.get('holding_business_days') or '-'} |"
        )
    if closed_rows.empty:
        lines.append("| - | - | なし | - | - | - | - | - | - |")
    lines.extend(["", "## 宣告ログ", "",
        "| 宣告日 | 執行日 | 売買 | コード | 銘柄 | 株数 | 状態 | 期/設定 |",
        "|---|---|---|---|---|---:|---|---|",
    ])
    for _, row in orders.iterrows():
        lines.append(
            f"| {row['decision_date']} | {row['execution_date']} | {row['side']} | {row['code']} | {row['name']} | "
            f"{int(float(row['shares']))}株 | {row['status']} | {row.get('phase_id') or '-'} / {row.get('rule_hash') or '-'} |"
        )
    lines.extend(["", "売買価格は宣告した次の東証営業日の始値をYahoo Financeから取得します。"
        "未実行日があった場合は同じ予定日の始値で自動補完し、取得不能が続く注文は期限切れにします。", ""])
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text("\n".join(lines), encoding="utf-8")


def run(account: str, target_date: date) -> int:
    orders_path, journal_path, _ = account_paths(account)
    orders = read_csv(orders_path, ORDER_COLUMNS)
    journal = read_csv(journal_path, JOURNAL_COLUMNS)
    declared = orders[orders["status"].str.upper().eq("DECLARED")]
    filled = 0
    for idx, order in declared.iterrows():
        try:
            execution_date = date.fromisoformat(str(order["execution_date"]))
        except ValueError:
            _cancel(orders, idx, "CANCELLED_INVALID_DATE", "執行日を読めない")
            continue
        if execution_date > target_date:
            continue
        compatible, incompatibility = order_compatibility(order)
        if not compatible:
            _cancel(orders, idx, f"CANCELLED_{incompatibility}", "現在の期・ルールと不一致")
            print(f"{account}_300man_fill=cancelled code={order['code']} reason={incompatibility}")
            continue
        if not is_jpx_business_day(execution_date):
            _cancel(orders, idx, "CANCELLED_NON_BUSINESS_DAY", "執行日が東証休場日")
            continue
        try:
            shares = int(float(order["shares"] or 0))
        except (TypeError, ValueError):
            shares = 0
        if shares <= 0 or shares % 100:
            _cancel(orders, idx, "CANCELLED_INVALID_SHARES", "株数が100株単位でない")
            continue
        side = str(order["side"]).strip().upper()
        if side not in {"BUY", "SELL"}:
            _cancel(orders, idx, "CANCELLED_INVALID_SIDE", "売買区分が不正")
            continue
        price = fetch_open_price_yfinance(str(order["ticker"]), execution_date)
        if price is None:
            age = business_days_between(execution_date, target_date)
            if age > CONFIG.pending_expiry_business_days:
                _cancel(orders, idx, "CANCELLED_OPEN_UNAVAILABLE", f"始値取得不能が{age}営業日継続")
            else:
                print(f"{account}_300man_fill=pending code={order['code']} reason=open_unavailable age={age}")
            continue
        decision_price = _float(order.get("decision_price"))
        if side == "BUY":
            if not decision_price or decision_price <= 0:
                _cancel(orders, idx, "CANCELLED_NO_DECISION_PRICE", "判定価格なし")
                continue
            gap_pct = (price / decision_price - 1) * 100
            if abs(gap_pct) > CONFIG.max_entry_gap_pct:
                _cancel(orders, idx, "CANCELLED_GAP", f"寄付ギャップ {gap_pct:+.2f}%")
                orders.at[idx, "fill_date"] = execution_date.isoformat()
                orders.at[idx, "fill_price"] = f"{price:.2f}"
                orders.at[idx, "fill_gap_pct"] = f"{gap_pct:.2f}"
                print(f"{account}_300man_fill=cancelled code={order['code']} gap={gap_pct:+.2f}%")
                continue
            journal, changed = _fill_buy(account, orders, journal, idx, order, execution_date, price, shares)
        else:
            journal, changed = _fill_sell(account, orders, journal, idx, order, execution_date, price, shares)
        filled += int(changed)
    write_csv(orders, orders_path, ORDER_COLUMNS)
    write_csv(journal, journal_path, JOURNAL_COLUMNS)
    write_ledger(account, orders, journal)
    print(f"{account}_300man_fill_date={target_date.isoformat()} filled={filled}")
    return filled
