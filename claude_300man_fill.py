from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from jpx_calendar import fetch_open_price_yfinance, is_jpx_business_day


ROOT = Path(__file__).resolve().parent
ORDERS_PATH = ROOT / "data" / "claude_300man_orders.csv"
JOURNAL_PATH = ROOT / "data" / "claude_300man_journal.csv"
LEDGER_PATH = ROOT / "docs" / "claude_300man_ledger.md"
JST = ZoneInfo("Asia/Tokyo")
INITIAL_CASH = 3_000_000

ORDER_COLUMNS = [
    "decision_date", "execution_date", "side", "code", "ticker",
    "name", "shares", "reason", "status",
]
JOURNAL_COLUMNS = [
    "entry_date", "fill_time_jst", "status", "code", "ticker",
    "name", "entry_price", "shares", "position_value", "source_order_date",
    "exit_date", "exit_price", "exit_value", "realized_pnl", "exit_order_date",
]


def _read(path: Path, columns: list[str]) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=columns)
    return pd.read_csv(path, dtype=str).reindex(columns=columns).fillna("")


def _write(df: pd.DataFrame, path: Path, columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.reindex(columns=columns).to_csv(path, index=False, encoding="utf-8-sig")


def _numbers(series: pd.Series | None) -> pd.Series:
    if series is None:
        return pd.Series(dtype=float)
    return pd.to_numeric(series, errors="coerce").fillna(0)


def _cash_balance(journal: pd.DataFrame) -> float:
    if journal.empty:
        return float(INITIAL_CASH)
    bought = _numbers(journal.get("position_value")).sum()
    sold = _numbers(journal.get("exit_value")).sum()
    return float(INITIAL_CASH - bought + sold)


def _write_ledger(orders: pd.DataFrame, journal: pd.DataFrame) -> None:
    open_rows = journal[journal["status"].str.upper().eq("OPEN")] if not journal.empty else journal
    closed_rows = journal[journal["status"].str.upper().eq("CLOSED")] if not journal.empty else journal
    realized = _numbers(closed_rows.get("realized_pnl")).sum()
    lines = [
        "# Claudeが300万円運用 - 運用台帳（正本）",
        "",
        "Claude運用専用のペーパー運用記録です。",
        "",
        f"- 初期資金: {INITIAL_CASH:,}円",
        f"- 現金残: {_cash_balance(journal):,.0f}円",
        f"- 保有: {len(open_rows)}銘柄",
        f"- 実現損益（累計）: {realized:,.0f}円",
        "",
        "## 保有一覧",
        "",
        "| 約定日 | コード | 銘柄 | 株数 | 取得始値 | 投資額 |",
        "|---|---|---|---:|---:|---:|",
    ]
    for _, row in open_rows.iterrows():
        lines.append(
            f"| {row['entry_date']} | {row['code']} | {row['name']} | "
            f"{int(float(row['shares']))}株 | {float(row['entry_price']):,.2f}円 | "
            f"{float(row['position_value']):,.0f}円 |"
        )
    if open_rows.empty:
        lines.append("| - | - | なし | - | - | - |")
    lines.extend([
        "",
        "## 実現損益",
        "",
        "| 売却日 | コード | 銘柄 | 株数 | 取得始値 | 売却始値 | 実現損益 |",
        "|---|---|---|---:|---:|---:|---:|",
    ])
    for _, row in closed_rows.iterrows():
        lines.append(
            f"| {row['exit_date']} | {row['code']} | {row['name']} | "
            f"{int(float(row['shares']))}株 | {float(row['entry_price']):,.2f}円 | "
            f"{float(row['exit_price']):,.2f}円 | {float(row['realized_pnl']):,.0f}円 |"
        )
    if closed_rows.empty:
        lines.append("| - | - | なし | - | - | - | - |")
    lines.extend([
        "",
        "## 宣告ログ",
        "",
        "| 宣告日 | 執行日 | 売買 | コード | 銘柄 | 株数 | 状態 |",
        "|---|---|---|---|---|---:|---|",
    ])
    for _, row in orders.iterrows():
        lines.append(
            f"| {row['decision_date']} | {row['execution_date']} | {row['side']} | "
            f"{row['code']} | {row['name']} | {int(float(row['shares']))}株 | {row['status']} |"
        )
    lines.extend([
        "",
        "売買価格は翌営業日の始値をYahoo Financeからyfinance経由（auto_adjust=False）で取得します。"
        "カブタンは取得に使用しません。",
        "",
    ])
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text("\n".join(lines), encoding="utf-8")


def _fill_buy(
    orders: pd.DataFrame,
    journal: pd.DataFrame,
    idx: int,
    order: pd.Series,
    target_date: date,
    price: float,
    shares: int,
) -> tuple[pd.DataFrame, bool]:
    duplicate = (
        journal["entry_date"].eq(target_date.isoformat())
        & journal["code"].eq(order["code"])
        & journal["source_order_date"].eq(order["decision_date"])
    ).any()
    if duplicate:
        orders.at[idx, "status"] = "FILLED"
        return journal, False
    value = round(price * shares)
    if value > _cash_balance(journal):
        print(f"claude_300man_fill=skipped code={order['code']} reason=capital_limit")
        return journal, False
    row = {
        "entry_date": target_date.isoformat(),
        "fill_time_jst": datetime.now(JST).isoformat(timespec="seconds"),
        "status": "OPEN",
        "code": order["code"],
        "ticker": order["ticker"],
        "name": order["name"],
        "entry_price": f"{price:.2f}",
        "shares": str(shares),
        "position_value": str(value),
        "source_order_date": order["decision_date"],
    }
    journal = pd.concat([journal, pd.DataFrame([row])], ignore_index=True).fillna("")
    orders.at[idx, "status"] = "FILLED"
    return journal, True


def _fill_sell(
    orders: pd.DataFrame,
    journal: pd.DataFrame,
    idx: int,
    order: pd.Series,
    target_date: date,
    price: float,
    shares: int,
) -> tuple[pd.DataFrame, bool]:
    if (
        journal["exit_date"].eq(target_date.isoformat())
        & journal["code"].eq(order["code"])
        & journal["exit_order_date"].eq(order["decision_date"])
    ).any():
        orders.at[idx, "status"] = "FILLED"
        return journal, False
    open_mask = (
        journal["status"].str.upper().eq("OPEN")
        & journal["code"].eq(order["code"])
    )
    available = _numbers(journal.loc[open_mask, "shares"]).sum()
    if available < shares:
        print(f"claude_300man_fill=skipped code={order['code']} reason=insufficient_position")
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
        closed["exit_date"] = target_date.isoformat()
        closed["exit_price"] = f"{price:.2f}"
        closed["exit_value"] = str(round(price * close_shares))
        closed["realized_pnl"] = str(round((price - entry_price) * close_shares))
        closed["exit_order_date"] = order["decision_date"]
        if close_shares == lot_shares:
            journal.loc[journal_idx] = closed
        else:
            journal.at[journal_idx, "shares"] = str(lot_shares - close_shares)
            journal.at[journal_idx, "position_value"] = str(round(entry_price * (lot_shares - close_shares)))
            journal = pd.concat([journal, pd.DataFrame([closed])], ignore_index=True).fillna("")
        remaining -= close_shares
    orders.at[idx, "status"] = "FILLED"
    return journal, True


def run(target_date: date) -> int:
    if not is_jpx_business_day(target_date):
        print(f"claude_300man_fill=skipped reason=jpx_holiday date={target_date}")
        return 0
    orders = _read(ORDERS_PATH, ORDER_COLUMNS)
    journal = _read(JOURNAL_PATH, JOURNAL_COLUMNS)
    due = orders[
        orders["execution_date"].eq(target_date.isoformat())
        & orders["status"].str.upper().eq("DECLARED")
    ]
    filled = 0
    for idx, order in due.iterrows():
        shares = int(float(order["shares"] or 0))
        if shares <= 0 or shares % 100:
            print(f"claude_300man_fill=skipped code={order['code']} reason=invalid_shares")
            continue
        side = order["side"].strip().upper()
        if side not in {"BUY", "SELL"}:
            print(f"claude_300man_fill=skipped code={order['code']} reason=invalid_side")
            continue
        price = fetch_open_price_yfinance(order["ticker"], target_date)
        if price is None:
            print(f"claude_300man_fill=pending code={order['code']} reason=open_unavailable")
            continue
        if side == "BUY":
            journal, changed = _fill_buy(orders, journal, idx, order, target_date, price, shares)
        else:
            journal, changed = _fill_sell(orders, journal, idx, order, target_date, price, shares)
        filled += int(changed)
    _write(orders, ORDERS_PATH, ORDER_COLUMNS)
    _write(journal, JOURNAL_PATH, JOURNAL_COLUMNS)
    _write_ledger(orders, journal)
    print(f"claude_300man_fill_date={target_date.isoformat()} filled={filled}")
    return filled


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now(JST).date().isoformat())
    args = parser.parse_args()
    run(date.fromisoformat(args.date))


if __name__ == "__main__":
    main()
