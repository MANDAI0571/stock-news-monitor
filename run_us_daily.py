"""米株の1日ぶんをまとめて回す。ワークフローからはこの1本だけを呼ぶ。

高重さんの指示(2026-09-06)「米株を日本株と同じようにやる」。

ワークフローのYAMLはWeb画面からしか直せない。手順をYAMLに書くと、
直したくなるたびにWeb画面での編集が要る。だから手順はここ（Pythonのファイル）に書き、
YAMLからは `python run_us_daily.py` だけを呼ぶ。以後の変更は tmpapply でできる。

やる順番:
  1. 前回宣告した注文を、今回の米国営業日の寄り値で約定させる
  2. スクリーニング（S&P500 と NASDAQ大型）
  3. 52週新高値・押し目の記事を作る
  4. 規律ルールで次の営業日の注文を宣告する
  5. $20,000運用の記事を作る
  6. メールを送り、コピー用ページを公開する

米国市場が開いていない日は何もしない。値段が取れないものは見送る。推測では埋めない。
"""

from __future__ import annotations

import argparse
import subprocess
import traceback
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from us_calendar import is_us_business_day, prev_us_business_day

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs"
STAMP_PATH = PROJECT_ROOT / "outputs" / "us_daily_last_session.txt"
JST = ZoneInfo("Asia/Tokyo")


def target_session(now: datetime | None = None) -> date:
    """直前に終わった米国市場の営業日。

    この処理は日本時間の朝に動く。米国市場が閉まるのはその数時間前なので、
    「日本の今日」ではなく直前の米国営業日を対象にする。
    """
    today = (now or datetime.now(JST)).date()
    return prev_us_business_day(today)


def already_published(session: date) -> bool:
    """その営業日のコピー用ページが main にもう入っているか。

    GitHub Actions は起動が遅れることがあるので、予備の cron を並べてある。
    先に走った回が公開ずみなら、後続はここで止める（メールの二重送信を防ぐ）。
    outputs/ は毎回まっさらなので、判定は main の中身で行う。
    """
    name = f"docs/copy/us_{session.isoformat()}.html"
    if (PROJECT_ROOT / name).exists():
        return True
    try:
        subprocess.run(
            ["git", "fetch", "--quiet", "origin", "main"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60,
        )
        found = subprocess.run(
            ["git", "cat-file", "-e", f"origin/main:{name}"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        print(f"us_daily_guard=unknown reason={error}", flush=True)
        return False
    return found.returncode == 0


def _read_stamp() -> str:
    if not STAMP_PATH.exists():
        return ""
    try:
        return STAMP_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_stamp(session: date) -> None:
    try:
        STAMP_PATH.parent.mkdir(parents=True, exist_ok=True)
        STAMP_PATH.write_text(session.isoformat(), encoding="utf-8")
    except OSError as error:
        print(f"us_daily_stamp=failed {error}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="米株の1日ぶんをまとめて回す")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--limit", type=int, default=None, help="動作確認用。本番では指定しない")
    parser.add_argument("--no-mail", action="store_true", help="メールを送らない")
    parser.add_argument(
        "--force", action="store_true", help="同じ営業日でもやり直す（手動再実行用）"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    now = datetime.now(JST)
    session = target_session(now)

    if not is_us_business_day(session):
        print(f"us_daily=skipped reason=not_us_business_day session={session}", flush=True)
        return 0
    if not args.force and _read_stamp() == session.isoformat():
        print(f"us_daily=skipped reason=already_done session={session}", flush=True)
        return 0
    if not args.force and already_published(session):
        print(f"us_daily=skipped reason=already_published session={session}", flush=True)
        return 0

    print(f"us_daily=start session={session} now={now:%Y-%m-%d %H:%M} JST", flush=True)
    failures: list[str] = []

    # 1. 前回の注文を約定させる
    try:
        from us_portfolio import fill_us_orders

        fill_us_orders(session)
    except Exception:  # noqa: BLE001 - 1つ転んでも残りは進める
        failures.append("約定")
        traceback.print_exc()

    # 2. スクリーニング
    try:
        from run_screening_us import run_us_screening

        run_us_screening(limit=args.limit, output_dir=output_dir)
    except Exception:  # noqa: BLE001
        failures.append("スクリーニング")
        traceback.print_exc()

    # 3. 52週新高値・押し目の記事
    try:
        from note_draft_us import build_us_notes

        build_us_notes(output_dir=output_dir, target_date=session.isoformat())
    except Exception:  # noqa: BLE001
        failures.append("記事（新高値・押し目）")
        traceback.print_exc()

    # 4. 次の営業日の注文を宣告
    try:
        from us_portfolio import declare_us_orders

        declare_us_orders(session, output_dir=output_dir)
    except Exception:  # noqa: BLE001
        failures.append("宣告")
        traceback.print_exc()

    # 5. $20,000運用の記事
    try:
        from us_portfolio import save_us_portfolio_note

        save_us_portfolio_note(session.isoformat(), output_dir=output_dir)
    except Exception:  # noqa: BLE001
        failures.append("記事（$20,000運用）")
        traceback.print_exc()

    # 6. メールとコピー用ページ
    if args.no_mail:
        print("us_daily_mail=skipped reason=disabled", flush=True)
    else:
        try:
            import sys

            import us_mail_digest

            saved = sys.argv
            sys.argv = ["us_mail_digest.py", "--output-dir", str(output_dir)]
            try:
                us_mail_digest.main()
            finally:
                sys.argv = saved
        except Exception:  # noqa: BLE001
            failures.append("メール")
            traceback.print_exc()

    if failures:
        print(f"us_daily=failed steps={'/'.join(failures)} session={session}", flush=True)
        return 1

    _write_stamp(session)
    print(f"us_daily=done session={session}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
