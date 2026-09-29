from __future__ import annotations

import argparse
from datetime import date, datetime
from zoneinfo import ZoneInfo

from dual_300man_fill import run as run_account


JST = ZoneInfo("Asia/Tokyo")


def run(target_date: date) -> int:
    return run_account("claude", target_date)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now(JST).date().isoformat())
    args = parser.parse_args()
    run(date.fromisoformat(args.date))


if __name__ == "__main__":
    main()
