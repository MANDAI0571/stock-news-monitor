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
  6. 記事を確かめる（台帳と食い違っていたらここで止める）
  7. note の2本立てを作って下書き保存し、台帳を main に残す（2026-10-16 にメールとコピー用ページから変更）

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
    # 2026-10-16 から、コピー用ページの代わりに data/us_daily_done/<日付>.txt を印にする
    #   （メールとコピー用ページをやめ、note の下書きにしたため）。古い印も見る。
    marks = (f"data/us_daily_done/{session.isoformat()}.txt", f"docs/copy/us_{session.isoformat()}.html")
    if any((PROJECT_ROOT / m).exists() for m in marks):
        return True
    name = marks[0]
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
    if found.returncode == 0:
        return True
    old = subprocess.run(["git", "cat-file", "-e", f"origin/main:{marks[1]}"],
                         cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60)
    return old.returncode == 0


def _push_ledger(session: date) -> bool:
    """米株の注文台帳と、その日を終えた印を main に push する。
    以前はコピー用ページ（us_mail_digest）と一緒に push していた。2026-10-16 にメールと
    コピー用ページをやめたので、台帳だけをここで残す。"""
    def run(*args):
        return subprocess.run(list(args), cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=120)
    try:
        mark = PROJECT_ROOT / "data" / "us_daily_done" / f"{session.isoformat()}.txt"
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(f"{session.isoformat()}\n", encoding="utf-8")
        run("git", "config", "user.name", "github-actions[bot]")
        run("git", "config", "user.email", "github-actions[bot]@users.noreply.github.com")
        targets = [str(mark.relative_to(PROJECT_ROOT))]
        for extra in ("data/claude_us20k_orders.csv", "data/claude_us20k_journal.csv"):
            if (PROJECT_ROOT / extra).exists():
                targets.append(extra)
        run("git", "add", *targets)
        if run("git", "diff", "--cached", "--quiet").returncode == 0:
            return True
        run("git", "commit", "-m", f"米株の台帳 {session.isoformat()} [skip ci]")
        for _ in range(3):
            if run("git", "pull", "--rebase", "origin", "main").returncode == 0 and \
                    run("git", "push", "origin", "HEAD:main").returncode == 0:
                return True
        print("us_daily_ledger=push_failed", flush=True)
        return False
    except (OSError, subprocess.SubprocessError) as error:
        print(f"us_daily_ledger=failed reason={error}", flush=True)
        return False


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
    parser.add_argument("--no-mail", "--no-note", dest="no_mail", action="store_true",
                        help="note の下書きを保存しない（記事は作る）")
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

    # 6. 送る前に確かめる。台帳と食い違う記事は絶対に送らない。
    #    fix60(2026-09-06): 日本株に validate_note_artifact.py があるのと同じ理由。
    #    2026-07-16 に、買っていない銘柄を保有として配信した事故がある。
    #    米株の $20,000運用の記事はまったく同じ形なので、同じ門番を置く。
    try:
        from validate_us_notes import summary_lines, validate_us_notes

        check = validate_us_notes(output_dir)
        for line in summary_lines(check):
            print(line, flush=True)
        if not check.ok:
            print(f"us_daily=failed steps=記事チェック session={session}", flush=True)
            return 1
    except Exception:  # noqa: BLE001
        failures.append("記事チェック")
        traceback.print_exc()
        print(f"us_daily=failed steps={'/'.join(failures)} session={session}", flush=True)
        return 1

    # 7. note の下書き（2026-10-16 持ち主の指示「米株も同じようにして」）。
    #    日本株と同じく2本立て: 一覧の記事（note_two_us）と $20,000運用の記事（5. で作ったもの）。
    #    メールとコピー用ページはやめた。台帳は _push_ledger で残す。
    try:
        import note_two_us

        note_dir = note_two_us.build(session, output_dir)
        if args.no_mail:
            print("us_daily_note=skipped reason=disabled", flush=True)
        elif not note_two_us.save_drafts(note_dir, session):
            # 2026-10-18 持ち主の指示: note のログインのことで持ち主を呼び出さない。失敗メールにしない
            print("us_daily_note=save_failed（失敗メールにはしない）", flush=True)
    except Exception:  # noqa: BLE001
        failures.append("note下書き")
        traceback.print_exc()
    if not _push_ledger(session):
        failures.append("台帳の保存")

    if failures:
        print(f"us_daily=failed steps={'/'.join(failures)} session={session}", flush=True)
        return 1

    _write_stamp(session)
    print(f"us_daily=done session={session}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
