"""米株 $20,000 ペーパー運用：台帳・翌営業日の宣告・約定・記事。

高重さんの指示(2026-09-06)「米株を日本株と同じようにやる」「ドルで記録（$20,000運用）」。
日本株の 300万円運用（claude_300man_declare.py / claude_300man_fill.py）と
同じ規律をドル建てに置き換えたもの。円換算はしない（為替は記事の注意書きに1行だけ）。

規律（日本株と同じ数字）:
  - 新規は売買代金の大きい順。最大3銘柄。1枠およそ $6,600
  - すでに持っている銘柄は買い増ししない。現金の範囲内でしか買わない
  - 手仕舞い: 損切 -7% / 利確 +15% / 10営業日タイムアウト

台帳（data/claude_us20k_orders.csv・claude_us20k_journal.csv）に
実際に約定した記録だけを書く。値段が取れないものは見送る。推測では埋めない。
これは架空資金の記録であり、投資助言ではない。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from us_calendar import (
    is_us_business_day,
    next_us_business_day,
    us_business_days_between,
)

ROOT = Path(__file__).resolve().parent
ORDERS_PATH = ROOT / "data" / "claude_us20k_orders.csv"
JOURNAL_PATH = ROOT / "data" / "claude_us20k_journal.csv"
DEFAULT_OUTPUT_DIR = ROOT / "outputs"

INITIAL_CASH_USD = 20_000.0
SLOT_USD = 6_600.0
MAX_POSITIONS = 3
STOP_LOSS_PCT = -7.0
TAKE_PROFIT_PCT = 15.0
TIMEOUT_DAYS = 10

RULE_LINE = (
    "ルール：1枠およそ$6,600・最大3銘柄 ／ 損切 -7% ／ 利確 +15% ／ 10営業日で手じまい"
)

ORDER_COLUMNS = [
    "decision_date", "execution_date", "side", "ticker", "name",
    "shares", "reason", "status",
]
JOURNAL_COLUMNS = [
    "entry_date", "fill_time_utc", "status", "ticker", "name",
    "entry_price", "shares", "position_value", "source_order_date",
    "exit_date", "exit_price", "exit_value", "realized_pnl", "exit_order_date",
]


# ---------------------------------------------------------------- 台帳の読み書き

def read_ledger(path: Path, columns: list[str]) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=columns)
    return pd.read_csv(path, dtype=str).reindex(columns=columns).fillna("")


def write_ledger(df: pd.DataFrame, path: Path, columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.reindex(columns=columns).to_csv(path, index=False, encoding="utf-8-sig")


def _nums(series: pd.Series | None) -> pd.Series:
    if series is None:
        return pd.Series(dtype=float)
    return pd.to_numeric(series, errors="coerce").fillna(0)


def _num(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _text(row: pd.Series, key: str) -> str:
    value = row.get(key)
    text = "" if value is None else str(value).strip()
    return "" if text.lower() in ("nan", "none", "null") else text


def status_rows(journal: pd.DataFrame, status: str) -> pd.DataFrame:
    if journal is None or journal.empty or "status" not in journal.columns:
        return pd.DataFrame(columns=JOURNAL_COLUMNS)
    return journal[journal["status"].astype(str).str.upper().eq(status)]


def cash_balance(journal: pd.DataFrame) -> float:
    """現金 = 元手 - 買った金額の合計 + 売った金額の合計。台帳だけで出す。"""
    if journal is None or journal.empty:
        return INITIAL_CASH_USD
    bought = _nums(journal.get("position_value")).sum()
    sold = _nums(journal.get("exit_value")).sum()
    return float(INITIAL_CASH_USD - bought + sold)


# ---------------------------------------------------------------- 値段を取りに行く

def last_close(ticker: str) -> float | None:
    """直近の終値。取れなければ None（推測では埋めない）。"""
    try:
        import yfinance as yf
    except ModuleNotFoundError:
        return None
    try:
        data = yf.download(
            ticker, period="5d", interval="1d",
            auto_adjust=False, progress=False, threads=False, timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"us_last_close_error[{ticker}]={exc}", flush=True)
        return None
    return _pick(data, ticker, "Close")


def open_price(ticker: str, trading_date: date) -> float | None:
    """指定日の寄り値。取れなければ None。"""
    try:
        import yfinance as yf
    except ModuleNotFoundError:
        return None
    start = trading_date.isoformat()
    end = (trading_date + timedelta(days=1)).isoformat()
    try:
        data = yf.download(
            ticker, start=start, end=end, interval="1d",
            auto_adjust=False, progress=False, threads=False, timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"us_open_price_error[{ticker}]={exc}", flush=True)
        return None
    return _pick(data, ticker, "Open")


def _pick(data: pd.DataFrame, ticker: str, column: str) -> float | None:
    if data is None or getattr(data, "empty", True):
        return None
    frame = data.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        if ticker in set(frame.columns.get_level_values(0)):
            frame = frame[ticker]
        elif ticker in set(frame.columns.get_level_values(-1)):
            frame = frame.xs(ticker, axis=1, level=-1)
        else:
            frame.columns = frame.columns.get_level_values(0)
    if column not in frame.columns:
        return None
    series = pd.to_numeric(frame[column], errors="coerce").dropna()
    if series.empty:
        return None
    return round(float(series.iloc[-1]), 2)


def holding_prices(open_rows: pd.DataFrame) -> dict[str, float]:
    prices: dict[str, float] = {}
    if open_rows is None or open_rows.empty or "ticker" not in open_rows.columns:
        return prices
    for ticker in open_rows["ticker"].astype(str).str.strip().unique():
        if not ticker:
            continue
        price = last_close(ticker)
        if price is not None:
            prices[ticker] = price
    print(f"us_holding_prices={len(prices)}/{len(open_rows)}", flush=True)
    return prices


# ---------------------------------------------------------------- 手仕舞いの判定

def exit_reason(entry_price: float, current_price: float, held_days: int) -> str:
    """ルールに当てはまるなら手仕舞いの理由を返す。当てはまらなければ空。"""
    if entry_price <= 0:
        return ""
    change = (current_price / entry_price - 1) * 100
    if change <= STOP_LOSS_PCT:
        return f"損切 {STOP_LOSS_PCT:.0f}%（{change:+.1f}%）"
    if change >= TAKE_PROFIT_PCT:
        return f"利確 +{TAKE_PROFIT_PCT:.0f}%（{change:+.1f}%）"
    if held_days >= TIMEOUT_DAYS:
        return f"タイムアウト {TIMEOUT_DAYS}営業日（{held_days}営業日）"
    return ""


def _held_days(entry_date: str, today: date) -> int:
    try:
        start = date.fromisoformat(str(entry_date).strip()[:10])
    except ValueError:
        return 0
    return us_business_days_between(start, today)


# ---------------------------------------------------------------- 翌営業日の宣告

def _buy_reason(row: pd.Series) -> str:
    """買った理由を、スクリーニングCSVにある値だけで作る。無い項目は書かない。"""
    parts: list[str] = []
    kind = _text(row, "high_type")
    if kind == "52W_NEW_HIGH":
        parts.append("52週新高値に到達")
    elif kind == "52W_NEAR_HIGH":
        dist = _num(row.get("dist_to_high_pct"))
        parts.append(f"52週新高値まで{dist:.2f}%" if dist is not None else "52週新高値に接近")
    turnover = _num(row.get("turnover_20d"))
    if turnover and turnover > 0:
        if turnover >= 1_000_000_000:
            parts.append(f"売買代金 ${turnover / 1_000_000_000:,.1f}B")
        else:
            parts.append(f"売買代金 ${turnover / 1_000_000:,.0f}M")
    ratio = _num(row.get("volume_ratio_5d_20d"))
    if ratio is not None:
        parts.append(f"出来高比 {ratio:.2f}倍")
    sector = _text(row, "sector")
    if sector and sector != "-":
        parts.append(sector)
    body = " ／ ".join(parts)
    return f"{body}（売買代金の大きい順に採用）" if body else "米株ペーパー運用の前日宣告"


def _latest_csv(output_dir: Path, prefix: str) -> Path | None:
    files = sorted(Path(output_dir).glob(f"{prefix}_*.csv"))
    return files[-1] if files else None


def declare_us_orders(
    today: date,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    orders_path: Path | str = ORDERS_PATH,
    journal_path: Path | str = JOURNAL_PATH,
    prices: dict[str, float] | None = None,
) -> pd.DataFrame:
    """翌営業日に出す注文（買い・売り）を台帳に足す。すでに宣告ずみなら足さない。"""
    orders_path = Path(orders_path)
    journal_path = Path(journal_path)
    orders = read_ledger(orders_path, ORDER_COLUMNS)
    journal = read_ledger(journal_path, JOURNAL_COLUMNS)
    open_rows = status_rows(journal, "OPEN")
    execution_date = next_us_business_day(today).isoformat()
    decision_date = today.isoformat()

    pending = orders[orders["status"].astype(str).str.upper().eq("PENDING")] if not orders.empty else orders
    already = set(pending.get("ticker", pd.Series(dtype=str)).astype(str).str.strip()) if not pending.empty else set()

    live = dict(prices) if prices is not None else holding_prices(open_rows)
    new_rows: list[dict[str, str]] = []

    # ① 手仕舞い（持っている銘柄をルールで落とす）
    for _, row in open_rows.iterrows():
        ticker = _text(row, "ticker")
        entry = _num(row.get("entry_price"))
        shares = _num(row.get("shares"))
        price = live.get(ticker)
        if not ticker or entry is None or shares is None or price is None:
            continue
        if ticker in already:
            continue
        reason = exit_reason(entry, price, _held_days(_text(row, "entry_date"), today))
        if not reason:
            continue
        new_rows.append({
            "decision_date": decision_date, "execution_date": execution_date,
            "side": "SELL", "ticker": ticker, "name": _text(row, "name"),
            "shares": f"{int(shares)}", "reason": reason, "status": "PENDING",
        })
        already.add(ticker)

    # ② 新規（枠が空いているぶんだけ。売り予定の銘柄は枠が空くまで数えたまま）
    held = set(open_rows.get("ticker", pd.Series(dtype=str)).astype(str).str.strip()) if not open_rows.empty else set()
    selling = {r["ticker"] for r in new_rows if r["side"] == "SELL"}
    slots = MAX_POSITIONS - (len(held) - len(selling))
    cash = cash_balance(journal)
    csv_path = _latest_csv(Path(output_dir), "screening_us_highs")
    if slots > 0 and csv_path is not None:
        table = pd.read_csv(csv_path, dtype=str).fillna("")
        if "turnover_20d" in table.columns:
            table = table.assign(_t=pd.to_numeric(table["turnover_20d"], errors="coerce").fillna(0))
            table = table.sort_values("_t", ascending=False)
        for _, row in table.iterrows():
            if slots <= 0:
                break
            ticker = _text(row, "ticker")
            price = _num(row.get("current_price"))
            if not ticker or price is None or price <= 0:
                continue
            if ticker in held or ticker in already:
                continue
            shares = int(SLOT_USD // price)
            if shares < 1:
                continue  # 1株も買えない値段の銘柄は見送る（推測で丸めない）
            cost = shares * price
            if cost > cash:
                continue
            new_rows.append({
                "decision_date": decision_date, "execution_date": execution_date,
                "side": "BUY", "ticker": ticker, "name": _text(row, "name"),
                "shares": f"{shares}", "reason": _buy_reason(row), "status": "PENDING",
            })
            already.add(ticker)
            cash -= cost
            slots -= 1

    if not new_rows:
        print("us_declare: 出す注文はありません", flush=True)
        return orders

    merged = pd.concat([orders, pd.DataFrame(new_rows)], ignore_index=True)
    write_ledger(merged, orders_path, ORDER_COLUMNS)
    print(f"us_declare: {len(new_rows)}件を {execution_date} 執行で宣告", flush=True)
    return merged


# ---------------------------------------------------------------- 約定

def fill_us_orders(
    trading_day: date,
    orders_path: Path | str = ORDERS_PATH,
    journal_path: Path | str = JOURNAL_PATH,
    prices: dict[str, float] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """執行日の寄り値で約定させる。値段が取れない注文は PENDING のまま残す。"""
    orders_path = Path(orders_path)
    journal_path = Path(journal_path)
    orders = read_ledger(orders_path, ORDER_COLUMNS)
    journal = read_ledger(journal_path, JOURNAL_COLUMNS)
    if orders.empty:
        return orders, journal
    if not is_us_business_day(trading_day):
        print(f"us_fill: {trading_day} は米国市場の休みです", flush=True)
        return orders, journal

    day = trading_day.isoformat()
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    filled = 0
    for index, row in orders.iterrows():
        if str(row.get("status", "")).upper() != "PENDING":
            continue
        if str(row.get("execution_date", "")).strip()[:10] != day:
            continue
        ticker = _text(row, "ticker")
        shares = _num(row.get("shares"))
        price = (prices or {}).get(ticker)
        if price is None:
            price = open_price(ticker, trading_day)
        if not ticker or shares is None or price is None:
            print(f"us_fill: {ticker} は寄り値が取れないので見送り", flush=True)
            continue

        if str(row.get("side", "")).upper() == "BUY":
            journal = pd.concat([journal, pd.DataFrame([{
                "entry_date": day, "fill_time_utc": stamp, "status": "OPEN",
                "ticker": ticker, "name": _text(row, "name"),
                "entry_price": f"{price:.2f}", "shares": f"{int(shares)}",
                "position_value": f"{price * shares:.2f}",
                "source_order_date": _text(row, "decision_date"),
                "exit_date": "", "exit_price": "", "exit_value": "",
                "realized_pnl": "", "exit_order_date": "",
            }])], ignore_index=True)
        else:
            hit = journal[
                journal["status"].astype(str).str.upper().eq("OPEN")
                & journal["ticker"].astype(str).str.strip().eq(ticker)
            ]
            if hit.empty:
                print(f"us_fill: {ticker} の保有が台帳に無いので売れません", flush=True)
                continue
            target = hit.index[0]
            entry = _num(journal.at[target, "entry_price"]) or 0.0
            held_shares = _num(journal.at[target, "shares"]) or 0.0
            journal.at[target, "status"] = "CLOSED"
            journal.at[target, "exit_date"] = day
            journal.at[target, "exit_price"] = f"{price:.2f}"
            journal.at[target, "exit_value"] = f"{price * held_shares:.2f}"
            journal.at[target, "realized_pnl"] = f"{(price - entry) * held_shares:.2f}"
            journal.at[target, "exit_order_date"] = _text(row, "decision_date")

        orders.at[index, "status"] = "FILLED"
        filled += 1

    # 執行日を過ぎても約定しなかった注文は EXPIRED にする。
    # そうしないと古い日付の注文が記事の「次の営業日にやること」に居座る。
    expired = 0
    for index, row in orders.iterrows():
        if str(row.get("status", "")).upper() != "PENDING":
            continue
        when = str(row.get("execution_date", "")).strip()[:10]
        if when and when < day:
            orders.at[index, "status"] = "EXPIRED"
            expired += 1

    if filled or expired:
        write_ledger(orders, orders_path, ORDER_COLUMNS)
        write_ledger(journal, journal_path, JOURNAL_COLUMNS)
    print(f"us_fill: {filled}件 約定・{expired}件 期限切れ（{day}）", flush=True)
    return orders, journal


# ---------------------------------------------------------------- 記事

US_PORTFOLIO_TITLE = "米国株 $20,000運用｜Claudeの実験記録"

SECTION_HOLDINGS = "## 保有銘柄"
SECTION_VALUATION = "## 評価額・現金比率"
SECTION_PNL = "## 損益"
SECTION_RECORD = "## 確定トレードの成績"
SECTION_REASONS = "## なぜ買ったか"
SECTION_NEXT = "## 次の営業日にやること"

DISCLAIMER_LINES = [
    "## 注意書き",
    "",
    "- これは架空資金による記録です。実際の売買ではありません。",
    "- 数値は台帳（実際に約定した記録）と取得済みの株価だけから出しています。取れなかった項目は載せていません。",
    "- ドル建ての記録です。円換算はしていません。円で見た損益は為替の分だけずれます。",
    "- 本記事は投資助言ではありません。売買判断はご自身の責任でお願いします。",
]


def _usd(value: float, signed: bool = False) -> str:
    """ドル表記。signed=True なら符号を先に出す（+$120.00 / -$45.00）。"""
    if not signed:
        return f"${value:,.2f}"
    sign = "-" if value < 0 else "+"
    return f"{sign}${abs(value):,.2f}"


def _holding_lines(open_rows: pd.DataFrame, prices: dict[str, float]) -> list[str]:
    """保有1件を2行で書く。1行目=銘柄 損益 購入日、2行目=株数と値段。"""
    out: list[str] = []
    for _, row in open_rows.iterrows():
        ticker = _text(row, "ticker")
        name = _text(row, "name")
        shares = _num(row.get("shares"))
        entry = _num(row.get("entry_price"))
        price = prices.get(ticker)
        head = f"{ticker} {name}".strip()
        if price is not None and shares and entry:
            pnl = (price - entry) * shares
            pct = (price / entry - 1) * 100
            head += f"　{_usd(pnl, signed=True)}（{pct:+.1f}%）"
        else:
            head += "　損益は現在値が取れないため未表示"
        head += f"　購入日 {_text(row, 'entry_date')}"
        out.append(head)
        second = f"　{int(shares) if shares else 0}株 ／ 取得 {_usd(entry or 0)}"
        if price is not None:
            second += f" → 現在 {_usd(price)}"
        out.append(second)
    return out


def _closed_record_lines(closed: pd.DataFrame) -> list[str]:
    lines = [SECTION_RECORD, ""]
    if closed is None or closed.empty:
        lines += ["- 確定したトレードはまだありません。", ""]
        return lines
    pnl = _nums(closed.get("realized_pnl"))
    wins = int((pnl > 0).sum())
    losses = int((pnl < 0).sum())
    total = len(pnl)
    lines.append(f"- 確定トレード：{total}件（勝ち {wins}件／負け {losses}件）")
    if total:
        lines.append(f"- 勝率：{wins / total * 100:.0f}%")
        lines.append(f"- 合計：{_usd(float(pnl.sum()), signed=True)}")
        lines.append(f"- 1件あたり平均：{_usd(float(pnl.mean()), signed=True)}")
    for _, row in closed.tail(5).iterrows():
        amount = _num(row.get("realized_pnl"))
        text = _usd(amount, signed=True) if amount is not None else "損益不明"
        lines.append(
            f"　{_text(row, 'ticker')} {_text(row, 'name')}"
            f"　{_text(row, 'entry_date')} → {_text(row, 'exit_date')}　{text}"
        )
    lines.append("")
    return lines


def _reason_lines(open_rows: pd.DataFrame, orders: pd.DataFrame) -> list[str]:
    if open_rows is None or open_rows.empty:
        return ["- 保有していないので、買った理由はありません。"]
    buys = orders[orders["side"].astype(str).str.upper().eq("BUY")] if not orders.empty else orders
    out: list[str] = []
    for _, row in open_rows.iterrows():
        ticker = _text(row, "ticker")
        reason = ""
        if not buys.empty:
            hit = buys[buys["ticker"].astype(str).str.strip().eq(ticker)]
            if not hit.empty:
                reason = _text(hit.iloc[-1], "reason")
        out.append(f"{ticker} {_text(row, 'name')}")
        out.append(f"　{reason or '注文台帳に理由の記録がありません（未確認）'}")
        out.append(f"　📈 チャート: https://finance.yahoo.com/quote/{ticker.upper()}/chart")
    return out


def _pending_lines(orders: pd.DataFrame) -> list[str]:
    if orders is None or orders.empty:
        return ["- 出す注文はありません。"]
    pending = orders[orders["status"].astype(str).str.upper().eq("PENDING")]
    if pending.empty:
        return ["- 出す注文はありません。"]
    out: list[str] = []
    for _, row in pending.iterrows():
        side = "買い" if str(row.get("side", "")).upper() == "BUY" else "売り"
        out.append(
            f"- {_text(row, 'execution_date')} {side}　{_text(row, 'ticker')} "
            f"{_text(row, 'name')}　{_text(row, 'shares')}株　{_text(row, 'reason')}"
        )
    return out


def build_us_portfolio_note(
    target_date: str,
    orders_path: Path | str = ORDERS_PATH,
    journal_path: Path | str = JOURNAL_PATH,
    prices: dict[str, float] | None = None,
) -> str:
    """$20,000運用の記事。台帳にある記録だけから書く。"""
    orders = read_ledger(Path(orders_path), ORDER_COLUMNS)
    journal = read_ledger(Path(journal_path), JOURNAL_COLUMNS)
    open_rows = status_rows(journal, "OPEN")
    closed = status_rows(journal, "CLOSED")

    live = dict(prices) if prices is not None else holding_prices(open_rows)
    invested = float(_nums(open_rows.get("position_value")).sum())
    realized = float(_nums(closed.get("realized_pnl")).sum())
    cash = cash_balance(journal)

    market_value = 0.0
    unrealized = 0.0
    priced = 0
    for _, row in open_rows.iterrows():
        price = live.get(_text(row, "ticker"))
        shares = _num(row.get("shares"))
        entry = _num(row.get("entry_price"))
        if price is None or shares is None or entry is None:
            continue
        priced += 1
        market_value += price * shares
        unrealized += (price - entry) * shares
    all_priced = priced > 0 and priced == len(open_rows)

    lines = [f"# {US_PORTFOLIO_TITLE} {target_date}", ""]
    lines += [SECTION_HOLDINGS, "", RULE_LINE, ""]
    if journal.empty:
        lines.append(f"- 約定はまだありません → CASH（現金 {_usd(INITIAL_CASH_USD)}）")
    elif open_rows.empty:
        lines.append(f"- 保有なし → CASH（現金 {_usd(cash)}）")
    else:
        lines += _holding_lines(open_rows, live)
        lines.append(f"現金 {_usd(cash)}")
    lines.append(f"　出所 {Path(journal_path).name}（実際に約定した記録だけ）")
    lines.append("")

    lines += [SECTION_VALUATION, ""]
    lines.append(f"- 運用資金: {_usd(INITIAL_CASH_USD)}")
    lines.append(f"- 投資額（取得原価・保有中）: {_usd(invested)}")
    lines.append(f"- 現金: {_usd(cash)}（現金比率 {cash / INITIAL_CASH_USD * 100:.1f}%）")
    if all_priced:
        lines.append(f"- 評価額（現値ベース・保有中）: {_usd(market_value)}")
        lines.append(f"- 総資産（評価額＋現金）: {_usd(market_value + cash)}")
    elif priced > 0:
        lines.append(f"- 評価額（現値が取れた{priced}銘柄ぶん）: {_usd(market_value)}")
        lines.append(f"- データ不足：{len(open_rows) - priced}銘柄は現値を取得できませんでした。")
    elif not open_rows.empty:
        lines.append("- データ不足：現値を取得できなかったため、時価評価は出していません（取得原価ベースです）。")
    lines.append("")

    lines += [SECTION_PNL, ""]
    lines.append(f"- 実現損益（累計）: {_usd(realized, signed=True)}")
    if open_rows.empty:
        lines.append("- 未実現損益: $0.00（保有なし）")
    elif priced > 0:
        pct = f"（{unrealized / invested * 100:+.1f}%）" if all_priced and invested else ""
        lines.append(f"- 未実現損益: {_usd(unrealized, signed=True)}{pct}")
        if not all_priced:
            lines.append(f"- データ不足：現値を取得できた{priced}銘柄ぶんの合計です。")
        lines.append(f"- 合計損益（実現＋未実現）: {_usd(realized + unrealized, signed=True)}")
    else:
        lines.append("- データ不足：現値が未取得のため、未実現損益は算出していません（推測では書きません）。")
    lines.append("")

    lines += _closed_record_lines(closed)
    lines += [SECTION_REASONS, ""]
    lines += _reason_lines(open_rows, orders)
    lines.append("")
    lines += [SECTION_NEXT, ""]
    lines += _pending_lines(orders)
    lines.append("")
    lines += DISCLAIMER_LINES
    return "\n".join(lines) + "\n"


def save_us_portfolio_note(
    target_date: str,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    **kwargs: object,
) -> Path:
    text = build_us_portfolio_note(target_date, **kwargs)  # type: ignore[arg-type]
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "note_us_portfolio.md"
    path.write_text(text, encoding="utf-8")
    (out / "note_us_portfolio_title.txt").write_text(
        f"{US_PORTFOLIO_TITLE} {target_date}", encoding="utf-8"
    )
    print(f"saved={path} chars={len(text):,}", flush=True)
    return path
