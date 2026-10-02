"""JPX（東証）の営業日判定と、寄り付き価格の取得。

2026-09-28再スタート後は claude_300man_fill.py と codex_300man_fill.py が
同じ東証営業日・始値取得処理を使う。
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd


FALLBACK_MARKET_HOLIDAYS = {
    # JPX published market holidays for 2026/2027. Used only if jpholiday is unavailable.
    date(2026, 1, 1),
    date(2026, 1, 2),
    date(2026, 1, 3),
    date(2026, 1, 12),
    date(2026, 2, 11),
    date(2026, 2, 23),
    date(2026, 3, 20),
    date(2026, 4, 29),
    date(2026, 5, 3),
    date(2026, 5, 4),
    date(2026, 5, 5),
    date(2026, 5, 6),
    date(2026, 7, 20),
    date(2026, 8, 11),
    date(2026, 9, 21),
    date(2026, 9, 22),
    date(2026, 9, 23),
    date(2026, 10, 12),
    date(2026, 11, 3),
    date(2026, 11, 23),
    date(2026, 12, 31),
    date(2027, 1, 1),
    date(2027, 1, 2),
    date(2027, 1, 3),
    date(2027, 1, 11),
    date(2027, 2, 11),
    date(2027, 2, 23),
    date(2027, 3, 21),
    date(2027, 3, 22),
    date(2027, 4, 29),
    date(2027, 5, 3),
    date(2027, 5, 4),
    date(2027, 5, 5),
    date(2027, 7, 19),
    date(2027, 8, 11),
    date(2027, 9, 20),
    date(2027, 9, 23),
    date(2027, 10, 11),
    date(2027, 11, 3),
    date(2027, 11, 23),
    date(2027, 12, 31),
}


def is_jpx_business_day(day: date) -> bool:
    if day.weekday() >= 5:
        return False
    if day in FALLBACK_MARKET_HOLIDAYS or (day.month, day.day) in {(1, 1), (1, 2), (1, 3), (12, 31)}:
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


def fetch_close_price_yfinance(ticker: str, trading_date: date) -> float | None:
    """保有銘柄が候補CSVから消えても、当日の損切り判定に使う終値を取る。

    当日ぶんが取れないときは直近の営業日まで遡る（下の _close_fallback）。
    価格が取れないと手仕舞いの判定が丸ごと飛び、損切りが静かに効かなくなるため。
    2026-10-01 の 9508 九州電力で実際に発生した（前日9/30は取得できた）。
    """
    price = _close_on(ticker, trading_date)
    if price is not None:
        return price
    return _close_fallback(ticker, trading_date)


# さかのぼる上限。営業日で数える。
_FALLBACK_BUSINESS_DAYS = 3
# 営業日を数えるあいだに見る暦日の上限（年末年始の長い休みでも前営業日に届く）。
_FALLBACK_CALENDAR_LIMIT = 15


def _close_fallback(ticker: str, trading_date: date) -> float | None:
    """当日が取れないとき、直前の営業日を最大3営業日さかのぼって探す。

    暦日で5日さかのぼる書き方では、3連休や年末年始の前営業日に届かなかった。
    JPXカレンダーで営業日だけを見るので、休みの長さに左右されない。

    返した値が何営業日前の終値かをログに出す。黙って古い値を使うと、
    実際より良い（または悪い）価格で損切り・利確を判定してしまうため。
    """
    tried = 0
    for back in range(1, _FALLBACK_CALENDAR_LIMIT + 1):
        day = trading_date - timedelta(days=back)
        if not is_jpx_business_day(day):
            continue
        tried += 1
        price = _close_on(ticker, day)
        if price is not None:
            print(
                f"close_price_fallback[{ticker}] {trading_date}->{day} "
                f"business_days_back={tried} price={price}",
                flush=True,
            )
            return price
        if tried >= _FALLBACK_BUSINESS_DAYS:
            break
    print(
        f"close_price_unavailable[{ticker}] {trading_date} business_days_tried={tried}",
        flush=True,
    )
    return None


def _traded(data: "pd.DataFrame", ticker: str) -> bool:
    """その足が本当に売買のあった日か。出来高0の足は値段として使わない。

    Yahooは配信が遅れている日に、前営業日の終値をそのまま持った
    出来高0の足を返すことがある。2026-09-29と09-30の9508(九州電力)が実例で、
    どちらも終値2,098円・出来高0のまま、実際の10-01終値は1,993.5円だった。
    これを使うと本物より5%高い値段で損切りを判定してしまう。
    出来高が読めないときは判断せず True を返す（憶測で捨てない）。
    """
    frame = data.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        levels0 = set(frame.columns.get_level_values(0))
        if ticker in levels0:
            frame = frame[ticker]
        elif "Volume" in levels0:
            frame.columns = frame.columns.get_level_values(0)
        else:
            try:
                series = frame.xs("Volume", axis=1, level=-1).iloc[:, 0]
            except (KeyError, IndexError):
                return True
            values = pd.to_numeric(series, errors="coerce").dropna()
            return bool(values.empty or values.iloc[-1] > 0)
    if "Volume" not in frame.columns:
        return True
    values = pd.to_numeric(frame["Volume"], errors="coerce").dropna()
    return bool(values.empty or values.iloc[-1] > 0)


def _close_on(ticker: str, trading_date: date) -> float | None:
    import yfinance as yf

    start = trading_date.isoformat()
    end = (trading_date + timedelta(days=1)).isoformat()
    try:
        data = yf.download(
            ticker,
            start=start,
            end=end,
            interval="1d",
            auto_adjust=False,
            progress=False,
            prepost=False,
            threads=False,
            timeout=20,
        )
    except Exception as exc:
        print(f"close_price_fetch_error[{ticker}]={exc}", flush=True)
        return None
    if data is None or data.empty:
        return None
    if not _traded(data, ticker):
        print(f"close_price_zero_volume[{ticker}] {trading_date}", flush=True)
        return None
    frame = data.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        levels0 = set(frame.columns.get_level_values(0))
        if ticker in levels0:
            frame = frame[ticker]
        elif "Close" in levels0:
            frame.columns = frame.columns.get_level_values(0)
        else:
            try:
                series = frame.xs("Close", axis=1, level=-1).iloc[:, 0]
                values = pd.to_numeric(series, errors="coerce").dropna()
                return _positive_float(values.iloc[-1]) if not values.empty else None
            except (KeyError, IndexError):
                return None
    if "Close" not in frame.columns:
        return None
    values = pd.to_numeric(frame["Close"], errors="coerce").dropna()
    price = _positive_float(values.iloc[-1]) if not values.empty else None
    return round(price, 2) if price is not None else None


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


def _num(value: object) -> float | None:
    try:
        out = float(str(value).replace(",", ""))
    except Exception:
        return None
    if pd.isna(out):
        return None
    return out
