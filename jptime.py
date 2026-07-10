from __future__ import annotations

"""
jptime.py — 日本時間(JST)基準の日付・時刻ヘルパー

GitHub Actions のランナーは UTC で動くため、date.today() / datetime.now() を
そのまま使うと 07:30 JST の実行時に「前日の日付」で記録されてしまう。
業務的な意味を持つ日付（signal_date・学習ログのdate・メール件名など）は
必ずこのモジュール経由で取得すること。
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def jst_now() -> datetime:
    """日本時間の現在時刻（tz-aware）を返す。"""
    return datetime.now(JST)


def jst_today() -> date:
    """日本時間での今日の日付を返す。"""
    return jst_now().date()
