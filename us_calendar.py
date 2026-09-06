"""米国株式市場（NYSE / NASDAQ）の営業日カレンダー。通信しない。

日本株の jpx_calendar.py に対応するもの。祝日は規則から計算するので、
毎年リストを更新する必要がない（＝更新し忘れで静かに壊れることがない）。

NYSEの休場日（規則）
  1/1  元日
  1月の第3月曜  キング牧師の日
  2月の第3月曜  大統領の日
  聖金曜日      復活祭の2日前
  5月の最終月曜 戦没者追悼の日
  6/19 ジューンティーンス（2022年から）
  7/4  独立記念日
  9月の第1月曜  レイバーデー
  11月の第4木曜 感謝祭
  12/25 クリスマス

振替の決まり
  土曜にあたる日は前日の金曜が休場。ただし元日だけは前年の大晦日を休場にしない。
  日曜にあたる日は翌日の月曜が休場。
"""

from __future__ import annotations

from datetime import date, timedelta

JUNETEENTH_FROM_YEAR = 2022


def easter_sunday(year: int) -> date:
    """グレゴリオ暦の復活祭（Anonymous Gregorian algorithm）。"""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    """その月の第nth 曜日（weekday: 月=0 … 日=6）。"""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (nth - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        last = date(year, 12, 31)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date, *, shift_saturday: bool = True) -> date:
    """土曜は前日の金曜、日曜は翌日の月曜に振り替える。"""
    if day.weekday() == 5 and shift_saturday:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def nyse_holidays(year: int) -> set[date]:
    """その年のNYSE休場日。計算だけで出す（通信しない）。"""
    days: set[date] = set()
    # 元日。土曜のときは前年の大晦日を休場にしない（NYSEの決まり）。
    days.add(_observed(date(year, 1, 1), shift_saturday=False))
    days.add(_nth_weekday(year, 1, 0, 3))            # キング牧師の日
    days.add(_nth_weekday(year, 2, 0, 3))            # 大統領の日
    days.add(easter_sunday(year) - timedelta(days=2))  # 聖金曜日
    days.add(_last_weekday(year, 5, 0))              # 戦没者追悼の日
    if year >= JUNETEENTH_FROM_YEAR:
        days.add(_observed(date(year, 6, 19)))       # ジューンティーンス
    days.add(_observed(date(year, 7, 4)))            # 独立記念日
    days.add(_nth_weekday(year, 9, 0, 1))            # レイバーデー
    days.add(_nth_weekday(year, 11, 3, 4))           # 感謝祭
    days.add(_observed(date(year, 12, 25)))          # クリスマス
    return {d for d in days if d.year == year}


def is_us_business_day(day: date) -> bool:
    """その日が米国株式市場の営業日か。土日と休場日を除く。"""
    if day.weekday() >= 5:
        return False
    return day not in nyse_holidays(day.year)


def next_us_business_day(day: date) -> date:
    nxt = day + timedelta(days=1)
    while not is_us_business_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def prev_us_business_day(day: date) -> date:
    prv = day - timedelta(days=1)
    while not is_us_business_day(prv):
        prv -= timedelta(days=1)
    return prv


def add_us_business_days(day: date, n: int) -> date:
    step = next_us_business_day if n >= 0 else prev_us_business_day
    for _ in range(abs(int(n))):
        day = step(day)
    return day


def us_business_days_between(start: date, end: date) -> int:
    """start から end までの営業日数（start は数えない）。"""
    if end <= start:
        return 0
    count, cur = 0, start
    while cur < end:
        cur += timedelta(days=1)
        if is_us_business_day(cur):
            count += 1
    return count
