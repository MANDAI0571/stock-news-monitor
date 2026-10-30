"""Claude 300万円運用の「判定を済ませた日」の控え。

2026-10-30: 18:45 の判定（中継）で注文を1件出したあと、数時間遅れて届いた予備の cron（01:13）が
同じ 10/30 の判定をやり直し、そのあいだに候補に入った別の銘柄を空き枠に足した（記事と X の文の後）。
予備の cron は判定が抜けた日を補うためのものなので、判定が最後まで済んだ日はここに書き、
予備（schedule）と中継（repository_dispatch）はその日をもう一度判定しない。
手動の補完（workflow_dispatch）は持ち主が決めてするものなので止めない。
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DECLARED_DATES_PATH = Path(__file__).resolve().parent / "data" / "claude_300man_declared_dates.csv"
COLUMNS = ["decision_date", "run_id", "event", "declared_at_jst"]


def already_declared(day: str, path: Path = DECLARED_DATES_PATH) -> bool:
    """その判定日の判定が、もう最後まで済んでいるか。"""
    if not path.exists():
        return False
    with path.open(encoding="utf-8", newline="") as handle:
        return any(row.get("decision_date", "").strip() == day for row in csv.DictReader(handle))


def mark(day: str, run_id: str, event: str, path: Path = DECLARED_DATES_PATH) -> None:
    """判定を済ませた日を1行足す（同じ日が2回書かれても already_declared の答えは同じ）。"""
    new_file = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerow({
            "decision_date": day,
            "run_id": run_id,
            "event": event,
            "declared_at_jst": datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%dT%H:%M:%S%z"),
        })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mark", required=True, help="判定日 (YYYY-MM-DD)")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--event", default="")
    args = parser.parse_args()
    mark(args.mark, args.run_id, args.event)
    print(f"claude_300man_declared_dates=marked date={args.mark} event={args.event}")


if __name__ == "__main__":
    main()
