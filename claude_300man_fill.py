from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from chatgpt_300man_note import fetch_open_price_yfinance, is_jpx_business_day


ROOT = Path(__file__).resolve().parent
ORDERS_PATH = ROOT / "data" / "claude_300man_orders.csv"
JOURNAL_PATH = ROOT / "data" / "claude_300man_journal.csv"
LEDGER_PATH = ROOT / "docs" / "claude_300man_ledger.md"
JST = ZoneInfo("Asia/Tokyo")

ORDER_COLUMNS = [
    "decision_date", "execution_date", "side", "code", "ticker",
    "name", "shares", "reason", "status",
]
JOURNAL_COLUMNS = [
    "entry_date", "fill_time_jst", "status", "code", "ticker",
    "name", "entry_price", "shares", "position_value", "source_order_date",
]


def _read(path: Path, columns: list[str]) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=columns)
    return pd.read_csv(path, dtype=str).reindex(columns=columns).fillna("")


def _write(df: pd.DataFrame, path: Path, columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.reindex(columns=columns).to_csv(path, index=False, encoding="utf-8-sig")


def _write_ledger(orders: pd.DataFrame, journal: pd.DataFrame) -> None:
    open_rows = journal[journal["status"].str.upper().eq("OPEN")] if not journal.empty else journal
    invested = pd.to_numeric(open_rows.get("position_value"), errors="coerce").fillna(0).sum()
    lines = [
        "# Claudeが300万円運用 - 運用台帳（正本）",
        "",
        "Claude運用専用のペーパー運用記録です。Codex運用の判断・注文・台帳とは分離しています。",
        "",
        f"- 初期資金: 3,000,000円",
        f"- 現金残: {3_000_000 - invested:,.0f}円",
        f"- 保有: {len(open_rows)}銘柄",
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
    lines.extend(["", "## 宣告ログ", "", "| 宣告日 | 執行日 | 売買 | コード | 銘柄 | 株数 | 状態 |", "|---|---|---|---|---|---:|---|"])
    for _, row in orders.iterrows():
        lines.append(
            f"| {row['decision_date']} | {row['execution_date']} | {row['side']} | "
            f"{row['code']} | {row['name']} | {int(float(row['shares']))}株 | {row['status']} |"
        )
    lines.extend(["", "始値はYahoo Financeをyfinance経由（auto_adjust=False）で取得します。カブタンは取得に使用しません。", ""])
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text("\n".join(lines), encoding="utf-8")


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
        duplicate = (
            journal["entry_date"].eq(target_date.isoformat())
            & journal["code"].eq(order["code"])
            & journal["source_order_date"].eq(order["decision_date"])
        ).any()
        if duplicate:
            orders.at[idx, "status"] = "FILLED"
            continue
        price = fetch_open_price_yfinance(order["ticker"], target_date)
        if price is None:
            print(f"claude_300man_fill=pending code={order['code']} reason=open_unavailable")
            continue
        value = round(price * shares)
        if pd.to_numeric(journal.get("position_value"), errors="coerce").fillna(0).sum() + value > 3_000_000:
            print(f"claude_300man_fill=skipped code={order['code']} reason=capital_limit")
            continue
        journal = pd.concat([journal, pd.DataFrame([{
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
        }])], ignore_index=True)
        orders.at[idx, "status"] = "FILLED"
        filled += 1
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
