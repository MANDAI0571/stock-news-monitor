from __future__ import annotations

import argparse
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from paper_portfolio_discipline import (
    MAX_POSITIONS,
    SLOT_CAPITAL,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    TIMEOUT_BUSINESS_DAYS,
)


PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "outputs"
DEFAULT_JOURNAL_PATH = PROJECT_ROOT / "data" / "paper_trade_journal.csv"
JST = ZoneInfo("Asia/Tokyo")

JOURNAL_COLUMNS = [
    "entry_date",
    "fill_time_jst",
    "slot",
    "status",
    "code",
    "ticker",
    "name",
    "entry_price",
    "shares",
    "position_value",
    "current_price",
    "market_value",
    "unrealized_pnl",
    "unrealized_pnl_pct",
    "stop_loss",
    "take_profit",
    "timeout_date",
    "entry_rank",
    "entry_score",
    "entry_regime",
    "rule",
    "source_decision_price",
    "exit_date",
    "exit_price",
    "exit_reason",
]


def today_jst() -> date:
    return datetime.now(JST).date()


def is_jpx_business_day(day: date) -> bool:
    if day.weekday() >= 5:
        return False
    if (day.month, day.day) in {(1, 1), (1, 2), (1, 3), (12, 31)}:
        return False
    try:
        import jpholiday

        if jpholiday.is_holiday(day):
            return False
    except Exception:
        pass
    return True


def next_jpx_business_day(day: date) -> date:
    current = day + timedelta(days=1)
    while not is_jpx_business_day(current):
        current += timedelta(days=1)
    return current


def add_jpx_business_days(day: date, days: int) -> date:
    current = day
    for _ in range(days):
        current = next_jpx_business_day(current)
    return current


def load_journal(path: str | Path = DEFAULT_JOURNAL_PATH) -> pd.DataFrame:
    journal_path = Path(path)
    if not journal_path.exists():
        return pd.DataFrame(columns=JOURNAL_COLUMNS)
    df = pd.read_csv(journal_path).astype(object)
    for column in JOURNAL_COLUMNS:
        if column not in df.columns:
            df[column] = ""
    return df.reindex(columns=JOURNAL_COLUMNS)


def save_journal(journal: pd.DataFrame, path: str | Path = DEFAULT_JOURNAL_PATH) -> Path:
    journal_path = Path(path)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal.reindex(columns=JOURNAL_COLUMNS).to_csv(journal_path, index=False, encoding="utf-8-sig")
    return journal_path


def latest_discipline_csv(output_dir: str | Path = OUTPUT_DIR) -> Path | None:
    out_dir = Path(output_dir)
    fixed = out_dir / "discipline_result.csv"
    if fixed.exists():
        return fixed
    paths = sorted(out_dir.glob("discipline_portfolio_*.csv"))
    return paths[-1] if paths else None


def latest_screening_csv(output_dir: str | Path = OUTPUT_DIR) -> Path | None:
    out_dir = Path(output_dir)
    fixed = out_dir / "screening_result.csv"
    if fixed.exists():
        return fixed
    paths = sorted(out_dir.glob("screening_result_*.csv"))
    return paths[-1] if paths else None


def normalize_code(value: object) -> str:
    text = str(value or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    if "." in text:
        text = text.split(".", 1)[0]
    return text


def ticker_for(row: pd.Series | dict[str, object]) -> str:
    ticker = str(row.get("ticker", "") or "").strip()
    if ticker and ticker.lower() != "nan":
        return ticker
    code = normalize_code(row.get("code", ""))
    return f"{code}.T" if code else ""


def _to_float(value: object) -> float | None:
    try:
        number = float(str(value).replace(",", ""))
    except Exception:
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def round_lot_shares(price: float) -> int:
    if price <= 0:
        return 0
    return int(SLOT_CAPITAL // price // 100 * 100)


def fetch_open_price_yfinance(ticker: str, trading_date: date) -> float | None:
    import yfinance as yf

    start = trading_date.isoformat()
    end = (trading_date + timedelta(days=1)).isoformat()
    for interval in ("1m", "5m", "1d"):
        try:
            data = yf.download(
                ticker,
                start=start,
                end=end,
                interval=interval,
                auto_adjust=False,
                progress=False,
                prepost=False,
                threads=False,
                timeout=20,
            )
        except Exception as exc:
            print(f"open_price_fetch_error[{ticker}][{interval}]={exc}", flush=True)
            continue
        price = _first_open(data, ticker)
        if price is not None:
            return round(price, 2)
    return None


def _first_open(data: pd.DataFrame, ticker: str) -> float | None:
    if data is None or data.empty:
        return None
    frame = data.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        levels0 = set(frame.columns.get_level_values(0))
        levelslast = set(frame.columns.get_level_values(-1))
        if ticker in levels0:
            frame = frame[ticker]
        elif "Open" in levels0:
            frame.columns = frame.columns.get_level_values(0)
        elif "Open" in levelslast:
            series = frame.xs("Open", axis=1, level=-1).iloc[:, 0]
            values = pd.to_numeric(series, errors="coerce").dropna()
            return _to_float(values.iloc[0]) if not values.empty else None
        else:
            return None
    if "Open" not in frame.columns:
        return None
    values = pd.to_numeric(frame["Open"], errors="coerce").dropna()
    return _to_float(values.iloc[0]) if not values.empty else None


def _open_positions(journal: pd.DataFrame) -> pd.DataFrame:
    if journal.empty:
        return journal
    status = journal.get("status", pd.Series(dtype=str)).astype(str).str.upper()
    exit_date = journal.get("exit_date", pd.Series([""] * len(journal))).astype(str).fillna("")
    return journal[status.eq("OPEN") & exit_date.isin(["", "nan", "None"])]


def fill_open_entries(
    discipline: pd.DataFrame,
    journal: pd.DataFrame,
    trading_date: date,
    price_fetcher=fetch_open_price_yfinance,
    fill_time_jst: str | None = None,
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    if not is_jpx_business_day(trading_date):
        return journal, [{"status": "SKIP", "reason": f"{trading_date.isoformat()} is not a JPX business day"}]
    if discipline.empty or "action" not in discipline.columns:
        return journal, [{"status": "SKIP", "reason": "discipline CSV has no BUY rows"}]

    open_positions = _open_positions(journal)
    open_codes = {normalize_code(code) for code in open_positions.get("code", pd.Series(dtype=str)).tolist()}
    free_slots = max(0, MAX_POSITIONS - len(open_positions))
    fills: list[dict[str, object]] = []
    if free_slots <= 0:
        return journal, [{"status": "SKIP", "reason": "already fully invested"}]

    buys = discipline[discipline["action"].astype(str).str.upper().eq("BUY")].copy()
    if buys.empty:
        return journal, [{"status": "SKIP", "reason": "no BUY rows"}]

    fill_time_jst = fill_time_jst or datetime.now(JST).isoformat(timespec="seconds")
    new_rows: list[dict[str, object]] = []
    next_slot = len(open_positions) + 1

    for _, row in buys.iterrows():
        if len(new_rows) >= free_slots:
            break
        code = normalize_code(row.get("code", ""))
        if not code or code in open_codes:
            continue
        ticker = ticker_for(row)
        open_price = price_fetcher(ticker, trading_date)
        if open_price is None:
            fills.append({"status": "SKIP", "code": code, "ticker": ticker, "reason": "open price unavailable"})
            continue
        shares = round_lot_shares(float(open_price))
        if shares <= 0:
            fills.append({"status": "SKIP", "code": code, "ticker": ticker, "reason": "round lot cannot fit slot capital"})
            continue
        position_value = round(float(open_price) * shares, 0)
        item = {
            "entry_date": trading_date.isoformat(),
            "fill_time_jst": fill_time_jst,
            "slot": next_slot,
            "status": "OPEN",
            "code": code,
            "ticker": ticker,
            "name": row.get("name", ""),
            "entry_price": round(float(open_price), 2),
            "shares": int(shares),
            "position_value": int(position_value),
            "current_price": round(float(open_price), 2),
            "market_value": int(position_value),
            "unrealized_pnl": 0,
            "unrealized_pnl_pct": 0,
            "stop_loss": round(float(open_price) * (1 - STOP_LOSS_PCT), 2),
            "take_profit": round(float(open_price) * (1 + TAKE_PROFIT_PCT), 2),
            "timeout_date": add_jpx_business_days(trading_date, TIMEOUT_BUSINESS_DAYS).isoformat(),
            "entry_rank": row.get("rank", ""),
            "entry_score": row.get("score", ""),
            "entry_regime": row.get("regime", ""),
            "rule": row.get("rule", ""),
            "source_decision_price": row.get("entry_price", row.get("current_price", "")),
            "exit_date": "",
            "exit_price": "",
            "exit_reason": "",
        }
        new_rows.append(item)
        fills.append({**item, "status": "FILLED", "journal_status": item["status"]})
        open_codes.add(code)
        next_slot += 1

    if new_rows:
        addition = pd.DataFrame(new_rows, columns=JOURNAL_COLUMNS)
        journal = addition if journal.empty else pd.concat([journal, addition], ignore_index=True)
    return journal.reindex(columns=JOURNAL_COLUMNS), fills or [{"status": "SKIP", "reason": "no new fills"}]


def mark_to_market(journal: pd.DataFrame, screening: pd.DataFrame) -> pd.DataFrame:
    if journal.empty or screening.empty or "code" not in screening.columns:
        return journal
    prices: dict[str, float] = {}
    for _, row in screening.iterrows():
        code = normalize_code(row.get("code", ""))
        price = _to_float(row.get("current_price", row.get("close", "")))
        if code and price is not None:
            prices[code] = price
    if not prices:
        return journal

    out = journal.copy().astype(object)
    for idx, row in out.iterrows():
        if str(row.get("status", "")).upper() != "OPEN":
            continue
        code = normalize_code(row.get("code", ""))
        current = prices.get(code)
        entry = _to_float(row.get("entry_price"))
        shares = int(_to_float(row.get("shares")) or 0)
        if current is None or entry is None or shares <= 0:
            continue
        market_value = current * shares
        pnl = (current - entry) * shares
        out.at[idx, "current_price"] = round(current, 2)
        out.at[idx, "market_value"] = int(round(market_value))
        out.at[idx, "unrealized_pnl"] = int(round(pnl))
        out.at[idx, "unrealized_pnl_pct"] = round((current - entry) / entry * 100, 2)
    return out.reindex(columns=JOURNAL_COLUMNS)


def portfolio_view_for_note(
    discipline: pd.DataFrame,
    screening: pd.DataFrame | None = None,
    journal_path: str | Path = DEFAULT_JOURNAL_PATH,
) -> pd.DataFrame:
    journal = load_journal(journal_path)
    if screening is not None and not screening.empty:
        journal = mark_to_market(journal, screening)
    open_positions = _open_positions(journal)
    if open_positions.empty:
        return discipline

    rows: list[dict[str, object]] = []
    for slot, (_, row) in enumerate(open_positions.head(MAX_POSITIONS).iterrows(), start=1):
        rows.append(
            {
                "slot": slot,
                "action": "BUY",
                "regime": row.get("entry_regime", ""),
                "code": normalize_code(row.get("code", "")),
                "ticker": row.get("ticker", ""),
                "name": row.get("name", ""),
                "rank": row.get("entry_rank", ""),
                "score": row.get("entry_score", ""),
                "entry_price": row.get("entry_price", ""),
                "shares": row.get("shares", 0),
                "position_value": row.get("position_value", 0),
                "current_price": row.get("current_price", ""),
                "market_value": row.get("market_value", ""),
                "unrealized_pnl": row.get("unrealized_pnl", ""),
                "unrealized_pnl_pct": row.get("unrealized_pnl_pct", ""),
                "stop_loss": row.get("stop_loss", ""),
                "take_profit": row.get("take_profit", ""),
                "timeout_date": row.get("timeout_date", ""),
                "entry_date": row.get("entry_date", ""),
                "rule": row.get("rule", ""),
                "cash_reason": "",
                "fill_source": "paper_open_journal",
            }
        )
    while len(rows) < MAX_POSITIONS:
        rows.append(
            {
                "slot": len(rows) + 1,
                "action": "CASH",
                "regime": rows[0].get("regime", "") if rows else "",
                "code": "",
                "ticker": "",
                "name": "現金",
                "rank": "",
                "score": "",
                "entry_price": "",
                "shares": 0,
                "position_value": 0,
                "current_price": "",
                "market_value": 0,
                "unrealized_pnl": 0,
                "unrealized_pnl_pct": 0,
                "stop_loss": "",
                "take_profit": "",
                "timeout_date": "",
                "entry_date": "",
                "rule": "最大3銘柄 / Sランクのみ",
                "cash_reason": "未使用枠",
                "fill_source": "paper_open_journal",
            }
        )
    return pd.DataFrame(rows)


def write_report(fills: list[dict[str, object]], output_dir: str | Path = OUTPUT_DIR) -> Path:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = ["# 寄り付き約定記録", ""]
    for item in fills:
        status = item.get("status", "")
        code = item.get("code", "")
        name = item.get("name", "")
        reason = item.get("reason", "")
        price = item.get("entry_price", "")
        shares = item.get("shares", "")
        if status == "FILLED":
            lines.append(f"- FILLED {code} {name}: {shares}株 @ {price}円")
        else:
            lines.append(f"- {status}: {code} {reason}")
    path = out_dir / "paper_open_fill_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="300万円運用を寄り付き価格でペーパー約定する")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    parser.add_argument("--journal", default=str(DEFAULT_JOURNAL_PATH))
    parser.add_argument("--date", default=None, help="YYYY-MM-DD。省略時はJST今日")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mark-only", action="store_true", help="新規約定は作らず、既存の寄り付き記録だけを評価更新する")
    args = parser.parse_args()

    trading_date = date.fromisoformat(args.date) if args.date else today_jst()
    discipline_path = latest_discipline_csv(args.output_dir)
    discipline = pd.read_csv(discipline_path) if discipline_path else pd.DataFrame()
    journal = load_journal(args.journal)
    if args.mark_only:
        fills = [{"status": "MARK", "reason": "existing paper open journal only"}]
    else:
        journal, fills = fill_open_entries(discipline, journal, trading_date)

    screening_path = latest_screening_csv(args.output_dir)
    screening = pd.read_csv(screening_path) if screening_path else pd.DataFrame()
    if not screening.empty:
        journal = mark_to_market(journal, screening)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.dry_run:
        save_journal(journal, args.journal)
    portfolio = portfolio_view_for_note(discipline, screening, args.journal)
    portfolio.to_csv(out_dir / "paper_open_portfolio.csv", index=False, encoding="utf-8-sig")
    report_path = write_report(fills, out_dir)
    print(f"paper_open_trading_date={trading_date.isoformat()}")
    print(f"paper_open_report={report_path}")
    print(f"paper_open_fills={sum(1 for item in fills if item.get('status') == 'FILLED')}")


if __name__ == "__main__":
    main()
