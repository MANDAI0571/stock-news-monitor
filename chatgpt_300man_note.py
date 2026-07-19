from __future__ import annotations

import argparse
import json
import math
import os
import re
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from gmail_notify import load_gmail_config, send_gmail
from jptime import jst_today
from paper_portfolio_discipline import (
    MAX_POSITIONS,
    SLOT_CAPITAL,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    TIMEOUT_BUSINESS_DAYS,
)


PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "outputs"
DEFAULT_JOURNAL_PATH = PROJECT_ROOT / "data" / "chatgpt_300man_journal.csv"
JST = ZoneInfo("Asia/Tokyo")
CAPITAL = 3_000_000
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


def normalize_code(value: object) -> str:
    text = _text(value)
    if "." in text:
        text = text.split(".", 1)[0]
    return "".join(ch for ch in text if ch.isdigit())


def ticker_for(row: pd.Series | dict[str, object]) -> str:
    ticker = _text(row.get("ticker", ""))
    if ticker and ticker.lower() != "nan":
        return ticker
    code = normalize_code(row.get("code", ""))
    return f"{code}.T" if code else ""


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
            return _positive_float(values.iloc[0]) if not values.empty else None
        else:
            return None
    if "Open" not in frame.columns:
        return None
    values = pd.to_numeric(frame["Open"], errors="coerce").dropna()
    return _positive_float(values.iloc[0]) if not values.empty else None


def _positive_float(value: object) -> float | None:
    number = _num(value)
    if number is None or not math.isfinite(number) or number <= 0:
        return None
    return number


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
    if free_slots <= 0:
        return journal, [{"status": "SKIP", "reason": "already fully invested"}]

    buys = discipline[discipline["action"].astype(str).str.upper().eq("BUY")].copy()
    if buys.empty:
        return journal, [{"status": "SKIP", "reason": "no BUY rows"}]

    fills: list[dict[str, object]] = []
    new_rows: list[dict[str, object]] = []
    next_slot = len(open_positions) + 1
    fill_time_jst = fill_time_jst or datetime.now(JST).isoformat(timespec="seconds")
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
        shares = round_lot_shares(open_price)
        if shares <= 0:
            fills.append({"status": "SKIP", "code": code, "ticker": ticker, "reason": "round lot cannot fit slot capital"})
            continue
        position_value = int(round(open_price * shares))
        item = {
            "entry_date": trading_date.isoformat(),
            "fill_time_jst": fill_time_jst,
            "slot": next_slot,
            "status": "OPEN",
            "code": code,
            "ticker": ticker,
            "name": row.get("name", ""),
            "entry_price": round(open_price, 2),
            "shares": int(shares),
            "position_value": position_value,
            "current_price": round(open_price, 2),
            "market_value": position_value,
            "unrealized_pnl": 0,
            "unrealized_pnl_pct": 0,
            "stop_loss": round(open_price * (1 - STOP_LOSS_PCT), 2),
            "take_profit": round(open_price * (1 + TAKE_PROFIT_PCT), 2),
            "timeout_date": add_jpx_business_days(trading_date, TIMEOUT_BUSINESS_DAYS).isoformat(),
            "entry_rank": row.get("rank", ""),
            "entry_score": row.get("score", ""),
            "entry_regime": row.get("regime", ""),
            "rule": "ChatGPT 300万円運用 / 寄り付きペーパー約定",
            "source_decision_price": row.get("entry_price", row.get("current_price", "")),
            "exit_date": "",
            "exit_price": "",
            "exit_reason": "",
        }
        new_rows.append(item)
        fills.append({**item, "status": "FILLED"})
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
        price = _positive_float(row.get("current_price", row.get("close", "")))
        if code and price is not None:
            prices[code] = price
    if not prices:
        return journal
    out = journal.copy().astype(object)
    for idx, row in out.iterrows():
        if _text(row.get("status")).upper() != "OPEN":
            continue
        code = normalize_code(row.get("code", ""))
        current = prices.get(code)
        entry = _positive_float(row.get("entry_price"))
        shares = int(_positive_float(row.get("shares")) or 0)
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
                "fill_source": "chatgpt_300man_open_journal",
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
                "fill_source": "chatgpt_300man_open_journal",
            }
        )
    return pd.DataFrame(rows)


def _latest_path(output_dir: Path, fixed_name: str, pattern: str) -> Path | None:
    fixed = output_dir / fixed_name
    paths = [p for p in output_dir.glob(pattern) if p.exists() and p.stat().st_size > 0]
    if fixed.exists() and fixed.stat().st_size > 0:
        paths.append(fixed)
    paths = sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


def _read_csv(path: Path | None) -> pd.DataFrame:
    if path is None or not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _text(value: object) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def _num(value: object) -> float | None:
    try:
        out = float(str(value).replace(",", ""))
    except Exception:
        return None
    if pd.isna(out):
        return None
    return out


def _yen(value: object) -> str:
    num = _num(value)
    if num is None:
        return "-"
    return f"{num:,.0f}円"


def _pct(value: object) -> str:
    num = _num(value)
    if num is None:
        return "-"
    return f"{num:+.2f}%"


def _chart_url(code: object) -> str:
    clean = "".join(ch for ch in _text(code) if ch.isdigit())
    if not clean:
        return ""
    return (
        f"https://finance.yahoo.co.jp/quote/{clean}.T/chart"
        "?frm=dly&trm=6m&scl=stndrd&styl=cndl&evnts=volume"
        "&ovrIndctr=sma%2Cmma%2Clma&addIndctr=&compare="
    )


def _chart_link(code: object, label: object | None = None) -> str:
    url = _chart_url(code)
    text = _text(label if label is not None else code) or "-"
    return f"[{text}]({url})" if url else text


def _open_rows(portfolio: pd.DataFrame) -> pd.DataFrame:
    if portfolio.empty or "action" not in portfolio.columns:
        return pd.DataFrame()
    return portfolio[portfolio["action"].astype(str).str.upper().eq("BUY")].copy()


def _uses_open_journal(portfolio: pd.DataFrame) -> bool:
    return (
        not portfolio.empty
        and "fill_source" in portfolio.columns
        and portfolio["fill_source"].astype(str).eq("chatgpt_300man_open_journal").any()
    )


def write_open_report(fills: list[dict[str, object]], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    lines = ["# ChatGPT 300万円運用 寄り付き記録", ""]
    for item in fills:
        status = _text(item.get("status"))
        code = _text(item.get("code"))
        name = _text(item.get("name"))
        if status == "FILLED":
            lines.append(f"- FILLED {code} {name}: {_text(item.get('shares'))}株 @ {_yen(item.get('entry_price'))}")
        else:
            reason = _text(item.get("reason"))
            lines.append(f"- {status or 'SKIP'}: {code or '-'} {reason}")
    path = output_dir / "chatgpt_300man_open_fill_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def record_open_fill(output_dir: Path, journal_path: Path, trading_date: date | None = None) -> None:
    trading_date = trading_date or jst_today()
    discipline_path = _latest_path(output_dir, "discipline_result.csv", "discipline_portfolio_*.csv")
    screening_path = _latest_path(output_dir, "screening_result.csv", "screening_result_*.csv")
    discipline = _read_csv(discipline_path)
    screening = _read_csv(screening_path)
    journal = load_journal(journal_path)
    journal, fills = fill_open_entries(discipline, journal, trading_date)
    if not screening.empty:
        journal = mark_to_market(journal, screening)
    save_journal(journal, journal_path)
    portfolio = portfolio_view_for_note(discipline, screening, journal_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    portfolio.to_csv(output_dir / "chatgpt_300man_portfolio.csv", index=False, encoding="utf-8-sig")
    report_path = write_open_report(fills, output_dir)
    print(f"chatgpt_300man_open_date={trading_date.isoformat()}")
    print(f"chatgpt_300man_open_report={report_path}")
    print(f"chatgpt_300man_open_fills={sum(1 for item in fills if item.get('status') == 'FILLED')}")


def _portfolio_table(portfolio: pd.DataFrame) -> list[str]:
    if portfolio.empty:
        return ["- 300万円運用データなし"]
    uses_open = _uses_open_journal(portfolio)
    price_label = "寄り付き約定" if uses_open else "取得想定"
    lines = [
        f"| 枠 | 状態 | コード | 銘柄 | 株数 | {price_label} | 投資額 | 現在値 | 評価額 | 損益 |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in portfolio.iterrows():
        action = _text(row.get("action")).upper() or "-"
        code = _text(row.get("code"))
        name = _text(row.get("name")) or ("現金" if action == "CASH" else "-")
        lines.append(
            "| {slot} | {action} | {code} | {name} | {shares} | {entry} | {position} | {current} | {market} | {pnl} |".format(
                slot=_text(row.get("slot")) or "-",
                action=action,
                code=_chart_link(code) if code else "-",
                name=name,
                shares=_text(row.get("shares")) or "-",
                entry=_yen(row.get("entry_price")),
                position=_yen(row.get("position_value")),
                current=_yen(row.get("current_price")),
                market=_yen(row.get("market_value")),
                pnl=_yen(row.get("unrealized_pnl")),
            )
        )
    return lines


def _summary_lines(portfolio: pd.DataFrame) -> list[str]:
    if portfolio.empty:
        return ["- クラウド記録: なし", f"- 運用資金: {CAPITAL:,}円"]
    buys = _open_rows(portfolio)
    invested = pd.to_numeric(portfolio.get("position_value", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()
    market = pd.to_numeric(portfolio.get("market_value", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()
    pnl = pd.to_numeric(portfolio.get("unrealized_pnl", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()
    cash = CAPITAL - invested
    uses_open = _uses_open_journal(portfolio)
    lines = [
        f"- 運用資金: {CAPITAL:,}円",
        f"- 保有数: {len(buys)}銘柄",
        f"- 投資額: {invested:,.0f}円",
        f"- 現金: {cash:,.0f}円",
    ]
    if uses_open:
        total = cash + market
        pct = pnl / invested * 100 if invested else 0
        lines.extend([
            "- クラウド記録: 寄り付き約定を使用",
            f"- 保有評価額: {market:,.0f}円",
            f"- 運用総額: {total:,.0f}円",
            f"- 未実現損益: {pnl:+,.0f}円（{pct:+.2f}%）",
        ])
    else:
        lines.append("- クラウド記録: まだ寄り付き約定なし（候補ベース）")
    return lines


def _candidate_lines(screening: pd.DataFrame, limit: int = 10) -> list[str]:
    if screening.empty or "rank" not in screening.columns:
        return ["- 候補データなし"]
    out = screening.copy()
    out["rank"] = out["rank"].astype(str).str.upper()
    out = out[out["rank"].isin(["S", "A", "B"])]
    if out.empty:
        return ["- S/A/B候補なし"]
    rank_order = {"S": 0, "A": 1, "B": 2}
    out["_rank_order"] = out["rank"].map(rank_order).fillna(9)
    out["score"] = pd.to_numeric(out.get("score"), errors="coerce").fillna(0)
    out = out.sort_values(["_rank_order", "score"], ascending=[True, False]).head(limit)
    lines = ["| コード | 銘柄 | ランク | スコア | 現在値 | 理由 |", "|---|---|---:|---:|---:|---|"]
    for _, row in out.iterrows():
        code = _text(row.get("code"))
        lines.append(
            f"| {_chart_link(code)} | {_text(row.get('name')) or '-'} | {_text(row.get('rank')) or '-'} | "
            f"{_text(row.get('score')) or '-'} | {_yen(row.get('current_price'))} | {_text(row.get('reason')) or '-'} |"
        )
    return lines


def build_note(output_dir: Path, journal_path: Path) -> tuple[str, str, pd.DataFrame]:
    today = jst_today()
    screening_path = _latest_path(output_dir, "screening_result.csv", "screening_result_*.csv")
    discipline_path = _latest_path(output_dir, "discipline_result.csv", "discipline_portfolio_*.csv")
    screening = _read_csv(screening_path)
    discipline = _read_csv(discipline_path)
    portfolio = portfolio_view_for_note(discipline, screening, journal_path)
    title = f"ChatGPT 300万円運用｜本日の記録 {today.isoformat()}"
    lines: list[str] = [
        f"# {title}",
        "",
        "## 本日の状態",
        "",
        *_summary_lines(portfolio),
        "",
        "## 保有・候補一覧",
        "",
        *_portfolio_table(portfolio),
        "",
        "## 買い候補TOP10",
        "",
        *_candidate_lines(screening),
        "",
        "## 運用ルール",
        "",
        "- 最大3銘柄",
        "- 1銘柄あたり約100万円",
        "- 100株単位",
        "- 損切り -7%",
        "- 利確 +15%",
        "- 10営業日タイムアウト",
        "",
        "## 注意書き",
        "",
        "- これは投資助言ではありません。",
        "- この記録はChatGPT 300万円運用のクラウド上のペーパー運用記録です。",
        "- 実際の売買判断は、最新の株価、出来高、決算予定、地合いを確認して行ってください。",
        "",
        f"source_screening={screening_path.name if screening_path else '未取得'}",
        f"source_discipline={discipline_path.name if discipline_path else '未取得'}",
        f"source_journal={journal_path}",
    ]
    return title, "\n".join(lines) + "\n", portfolio


def _inline(text: str) -> str:
    escaped = escape(text)

    def repl(match: re.Match[str]) -> str:
        label = match.group(1)
        url = match.group(2)
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{label}</a>'

    escaped = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", repl, escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    return escaped


def render_html(title: str, markdown: str) -> str:
    lines = markdown.splitlines()
    html: list[str] = [
        "<!doctype html>",
        '<html lang="ja">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(title)}</title>",
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.65;max-width:960px;margin:24px auto;padding:0 16px;color:#111}table{border-collapse:collapse;width:100%;margin:12px 0}th,td{border:1px solid #ccc;padding:6px 8px;vertical-align:top;text-align:left}h1,h2{line-height:1.35}ul{padding-left:1.4em}</style>",
        "</head>",
        "<body>",
    ]
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith("# "):
            html.append(f"<h1>{_inline(line[2:])}</h1>")
            i += 1
            continue
        if line.startswith("## "):
            html.append(f"<h2>{_inline(line[3:])}</h2>")
            i += 1
            continue
        if line.startswith("|"):
            table_lines: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            rows = [row.strip("|").split("|") for row in table_lines]
            html.append("<table>")
            if rows:
                html.append("<thead><tr>" + "".join(f"<th>{_inline(cell.strip())}</th>" for cell in rows[0]) + "</tr></thead>")
                body_rows = rows[2:] if len(rows) > 1 and set(rows[1][0].strip()) <= {"-"} else rows[1:]
                html.append("<tbody>")
                for row in body_rows:
                    html.append("<tr>" + "".join(f"<td>{_inline(cell.strip())}</td>" for cell in row) + "</tr>")
                html.append("</tbody>")
            html.append("</table>")
            continue
        if line.startswith("- "):
            html.append("<ul>")
            while i < len(lines) and lines[i].strip().startswith("- "):
                html.append(f"<li>{_inline(lines[i].strip()[2:])}</li>")
                i += 1
            html.append("</ul>")
            continue
        html.append(f"<p>{_inline(line)}</p>")
        i += 1
    html.extend(["</body>", "</html>"])
    return "\n".join(html)


def write_outputs(output_dir: Path, title: str, body: str, portfolio: pd.DataFrame) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    md = output_dir / "note_chatgpt_300man.md"
    html = output_dir / "note_chatgpt_300man.html"
    title_file = output_dir / "note_chatgpt_300man_title.txt"
    portfolio_file = output_dir / "chatgpt_300man_portfolio.csv"
    subject_file = output_dir / "chatgpt_300man_mail_subject.txt"
    body_file = output_dir / "chatgpt_300man_mail_body.md"
    manifest_file = output_dir / "note_drafts_manifest.json"
    md.write_text(body, encoding="utf-8")
    html.write_text(render_html(title, body), encoding="utf-8")
    title_file.write_text(title + "\n", encoding="utf-8")
    body_file.write_text(body, encoding="utf-8")
    subject_file.write_text(f"【ChatGPT 300万円運用】本日の記録 {jst_today().isoformat()}\n", encoding="utf-8")
    portfolio.to_csv(portfolio_file, index=False, encoding="utf-8-sig")
    manifest = [
        {
            "key": "chatgpt_300man",
            "title": title,
            "md_file": md.name,
            "title_file": title_file.name,
            "html_file": html.name,
            "url_file": "note_draft_url_chatgpt_300man.txt",
        }
    ]
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def maybe_send_mail(subject: str, body: str, attachments: list[Path], enabled: bool) -> bool:
    if not enabled:
        print("chatgpt_300man_mail=skipped reason=disabled")
        return False
    config = load_gmail_config()
    if config is None:
        print("chatgpt_300man_mail=skipped reason=missing_secrets")
        return False
    send_gmail(subject, body, config, attachments=attachments)
    print(f"chatgpt_300man_mail=sent to={config.mail_to}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="ChatGPT 300万円運用専用のnote下書きとメールを作る")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    parser.add_argument("--journal", default=str(DEFAULT_JOURNAL_PATH))
    parser.add_argument("--record-open", action="store_true", help="朝の候補を寄り付き価格でペーパー約定して記録する")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD。省略時はJST今日")
    parser.add_argument("--send-mail", action="store_true")
    parser.add_argument("--allow-holiday", action="store_true")
    args = parser.parse_args()

    today = jst_today()
    target_date = date.fromisoformat(args.date) if args.date else today
    business_day = target_date if args.record_open else today
    if not args.allow_holiday and not is_jpx_business_day(business_day):
        print(f"chatgpt_300man_skipped=holiday date={business_day.isoformat()}")
        return

    output_dir = Path(args.output_dir)
    journal_path = Path(args.journal)
    if args.record_open:
        record_open_fill(output_dir, journal_path, target_date)
        return

    title, body, portfolio = build_note(output_dir, journal_path)
    write_outputs(output_dir, title, body, portfolio)
    maybe_send_mail(
        f"【ChatGPT 300万円運用】本日の記録 {today.isoformat()}",
        body,
        [output_dir / "chatgpt_300man_portfolio.csv"],
        enabled=args.send_mail and os.environ.get("SEND_CHATGPT_300MAN_MAIL", "true").lower() != "false",
    )
    print("chatgpt_300man_note=generated")
    print(f"chatgpt_300man_open_journal={_uses_open_journal(portfolio)}")


if __name__ == "__main__":
    main()
