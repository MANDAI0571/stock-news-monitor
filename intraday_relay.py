#!/usr/bin/env python3
"""ザラ場アラートを cron に頼らずに起こすための中継（intraday_relay.yml から呼ぶ）。

このリポジトリの定期実行（schedule）は数時間遅れて届く。Actions の履歴で実測:
  09:02〜10:02 JST の見張り役5本は、9/24 以降毎日 13:32〜15:59 に到着（約4.5〜5.5時間遅れ）
  07:30 JST の daily-discipline は、9/28 以降 10:01〜11:23 に到着
  06:10 / 07:10 JST の起動を足した後も、10/07 の前場は 11:40 まで巡回が1本も来なかった
一方 repository_dispatch は数秒で届く（見張り役の dispatch から巡回の起動まで8秒）。

そこで巡回が終わるたびにこの中継を起こし、次の立会の直前まで待ってから巡回を起こす。
1回の待ちはジョブの上限（6時間）の内側に収め、届かなければ中継を起こし直して待ちを継ぐ。

  python3 intraday_relay.py            待ってから、GITHUB_OUTPUT に event=... を書く
  python3 intraday_relay.py --dry-run  待たずに、何時に何を起こすかだけ表示する
"""

import argparse
import datetime as dt
import os
import sys
import time

JST = dt.timezone(dt.timedelta(hours=9))

# 起こす時刻と、送るイベント。巡回側は 06:00〜08:59 に起きたら 09:00 まで、
# 11:30〜12:29 に起きたら 12:30 まで待ってから巡回に入る。
# 14:05 は出来高急増スクリーナー（volume_screener.yml、2026-10-12 追加）。もとは
# Mac の定期タスクで14:10に動かしていた。cron は数時間遅れて届くので中継で起こす。
# 16:00 は note の2本立て（note_daily_drafts.yml、2026-10-16 追加）。
# 09:40 と 18:45 は Claude 300万円運用の約定と注文（claude_300man_fill.yml / claude_300man_declare.yml、
# 2026-10-30 追加）。cron では 18:45 の注文が夜中の 0:30〜2:20 に、09:40 の約定が 14:00〜16:00 に
# 動いていた（Actions の履歴で実測）。持ち主「300万運用でXでリアルタイム情報流します」のため時刻どおりに起こす。
# cron は予備として残す（どちらも同じ日・同じ注文は二重にならない）。
SLOTS = ((dt.time(8, 50), "intraday_tick"),
         (dt.time(9, 40), "claude_300man_fill"),
         (dt.time(12, 25), "intraday_tick"),
         (dt.time(14, 5), "volume_screener"),
         (dt.time(16, 0), "note_daily"),
         (dt.time(18, 45), "claude_300man_declare"))

# 1回の待ちの上限（分）。ジョブの timeout-minutes 355 と、GitHub の上限
# 360分の内側に、準備と送信のぶんを残す。
MAX_WAIT_MIN = 340


def _jpholiday():
    try:
        import jpholiday  # noqa: PLC0415
        return jpholiday
    except Exception:                                  # noqa: BLE001
        return None


def is_business_day(d, holiday_lib=None):
    """東証の立会日か。土日・年末年始・祝日（jpholiday が使えるときだけ）を除く。

    jpholiday が入らなかった日は祝日を見分けられないが、その場合も
    巡回側が祝日を判定して何もせず終わるので、害は中継が1回余分に動くことだけ。
    """
    if d.weekday() >= 5:
        return False
    if (d.month, d.day) in {(12, 31), (1, 1), (1, 2), (1, 3)}:
        return False
    if holiday_lib is not None and holiday_lib.is_holiday(d):
        return False
    return True


# 時刻を少し過ぎてから中継が起き直した場合（直前に別の巡回が終わって中継が
# 取り消され、起こし直された等）も、その立会を飛ばさないための猶予。
# 長くしすぎない。起こした巡回がすぐ失敗すると、その終わりにまた中継が起き、
# 猶予のあいだは巡回を起こし直し続ける（失敗メールが重なる）。巡回が失敗して
# 中継に戻るまで2〜3分かかるので、3分なら起こし直しは多くて1回。
GRACE = dt.timedelta(minutes=3)


def next_slot(now, business=is_business_day, after=None):
    """まだ間に合う、立会日の SLOTS の (時刻, イベント)（GRACE 以内に過ぎたものを含む）。

    after を渡すと、その時刻以前のものは選ばない。起こした直後に中継を起こし直す
    ときに使う（同じ時刻をもう一度起こさないため）。
    """
    d = now.date()
    for _ in range(20):
        if business(d):
            for s, event in SLOTS:
                t = dt.datetime.combine(d, s, JST)
                if t + GRACE > now and (after is None or t > after):
                    return t, event
        d += dt.timedelta(days=1)
    raise RuntimeError("20日先まで立会日が見つからない")


def plan(now, business=is_business_day, after=None):
    """(待つ秒数, 送るイベント, 狙っている時刻) を返す。

    次の時刻まで MAX_WAIT_MIN 以内なら、そこまで待ってそのイベントを送る。
    遠ければ MAX_WAIT_MIN だけ待って中継を起こし直す。
    """
    target, event = next_slot(now, business, after)
    wait = (target - now).total_seconds()
    if wait <= MAX_WAIT_MIN * 60:
        return max(0.0, wait), event, target
    return MAX_WAIT_MIN * 60.0, "intraday_relay", target


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    lib = _jpholiday()
    now = dt.datetime.now(JST)
    after = None
    raw = os.environ.get("RELAY_AFTER", "").strip()
    if raw:
        try:
            after = dt.datetime.fromisoformat(raw)
        except ValueError:
            print(f"[RELAY] ignore bad RELAY_AFTER={raw!r}", flush=True)
    wait, event, target = plan(now, lambda d: is_business_day(d, lib), after)
    print(f"[RELAY] now={now:%Y-%m-%d %H:%M} target={target:%Y-%m-%d %H:%M} "
          f"wait={wait / 60:.0f}min event={event} jpholiday={'yes' if lib else 'no'}",
          flush=True)
    if a.dry_run:
        return 0
    end = time.time() + wait
    while True:
        left = end - time.time()
        if left <= 0:
            break
        time.sleep(min(left, 300))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"event={event}\n")
            f.write(f"target={target.isoformat()}\n")
    print(f"[RELAY] fire {event} at {dt.datetime.now(JST):%H:%M:%S}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
