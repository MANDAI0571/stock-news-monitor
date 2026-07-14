from __future__ import annotations

"""
jptime.py — 日本時間(JST)基準の日付・時刻ヘルパー

GitHub Actions のランナーは UTC で動くため、date.today() / datetime.now() を
そのまま使うと 07:30 JST の実行時に「前日の日付」で記録されてしまう。
業務的な意味を持つ日付（signal_date・学習ログのdate・メール件名など）は
必ずこのモジュール経由で取得すること。
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def jst_now() -> datetime:
    """日本時間の現在時刻（tz-aware）を返す。"""
    return datetime.now(JST)


def jst_today() -> date:
    """日本時間での今日の日付を返す。"""
    return jst_now().date()


# ============================================================================
# T-K修正(2026-07-12): JPX営業日カレンダー（日本の祝日・年末年始対応）
# - 外部通信なしで祝日を計算する（2022年以降を対象。五輪特例年は対象外）。
# - JPX休場 = 土日 + 国民の祝日（振替休日・国民の休日含む）+ 年末年始(12/31〜1/3)。
# - 記事の対象日決定は「スクリーニングデータの最終日」が最優先で、
#   このカレンダーはデータが無い場合のフォールバックに使う。
# ============================================================================


def _nth_monday(year: int, month: int, n: int) -> date:
    """その月の第n月曜日（ハッピーマンデー用）。"""
    d = date(year, month, 1)
    offset = (7 - d.weekday()) % 7  # 最初の月曜まで
    return d + timedelta(days=offset + 7 * (n - 1))


def _equinox_day(year: int, spring: bool) -> date:
    """春分/秋分の日の簡易計算（1980〜2099年で官報実績と一致する近似式）。"""
    base = 20.8431 if spring else 23.2488
    day = int(base + 0.242194 * (year - 1980) - (year - 1980) // 4)
    return date(year, 3 if spring else 9, day)


def jp_holidays(year: int) -> set[date]:
    """その年の国民の祝日（振替休日・国民の休日を含む）。2022年以降を想定。"""
    base = {
        date(year, 1, 1),                 # 元日
        _nth_monday(year, 1, 2),          # 成人の日
        date(year, 2, 11),                # 建国記念の日
        date(year, 2, 23),                # 天皇誕生日
        _equinox_day(year, spring=True),  # 春分の日
        date(year, 4, 29),                # 昭和の日
        date(year, 5, 3),                 # 憲法記念日
        date(year, 5, 4),                 # みどりの日
        date(year, 5, 5),                 # こどもの日
        _nth_monday(year, 7, 3),          # 海の日
        date(year, 8, 11),                # 山の日
        _nth_monday(year, 9, 3),          # 敬老の日
        _equinox_day(year, spring=False), # 秋分の日
        _nth_monday(year, 10, 2),         # スポーツの日
        date(year, 11, 3),                # 文化の日
        date(year, 11, 23),               # 勤労感謝の日
    }
    holidays = set(base)
    # 振替休日: 祝日が日曜なら、その後の最初の「祝日でない日」が休日
    for d in sorted(base):
        if d.weekday() == 6:
            sub = d + timedelta(days=1)
            while sub in holidays:
                sub += timedelta(days=1)
            holidays.add(sub)
    # 国民の休日: 前後を祝日に挟まれた平日（9月のシルバーウィーク等）
    for d in sorted(base):
        sandwiched = d + timedelta(days=2)
        middle = d + timedelta(days=1)
        if sandwiched in base and middle not in holidays and middle.weekday() != 6:
            holidays.add(middle)
    return holidays


def is_jpx_business_day(d: date) -> bool:
    """JPX（東証）の営業日か。土日・祝日・年末年始(12/31〜1/3)は休場。"""
    if d.weekday() >= 5:
        return False
    if (d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 3):
        return False
    return d not in jp_holidays(d.year)


def prev_jpx_business_day(d: date | None = None) -> date:
    """d（省略時はJST今日）以前で直近のJPX営業日を返す。"""
    current = d or jst_today()
    while not is_jpx_business_day(current):
        current -= timedelta(days=1)
    return current
