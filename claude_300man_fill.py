from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from claude_300man_exits import settle as settle_standing_exits
from dual_300man_config import CONFIG
from dual_300man_fill import run as run_account
from jpx_calendar import is_jpx_business_day


JST = ZoneInfo("Asia/Tokyo")


def run(target_date: date) -> int:
    if CONFIG.standing_exit_orders:
        # 3.0.0: 前の営業日までに逆指値・指値・期限で売れたもの（夕方の判定が動かなかった日の分）を先に付ける
        previous = target_date - timedelta(days=1)
        while not is_jpx_business_day(previous):
            previous -= timedelta(days=1)
        try:
            settle_standing_exits(previous)
        except Exception as error:  # noqa: BLE001 - 売りの判定で買いの約定まで止めない（夕方にもう一度判定する）
            print(f"claude_300man_fill=exit_settle_failed reason={type(error).__name__}")
    return run_account("claude", target_date)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now(JST).date().isoformat())
    args = parser.parse_args()
    run(date.fromisoformat(args.date))


if __name__ == "__main__":
    main()
