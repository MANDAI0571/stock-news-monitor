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
  7. note の2本立てを作って貼り付け用のメールで届け、台帳を main に残す（2026-10-18 から。note は自動ログインできない）
     メールが出なかった日は、予備の回が 2・3・5・6・7 だけをやり直す（約定と宣告はやり直さない。2026-10-20）

米国市場が開いていない日は何もしない。値段が取れないものは見送る。推測では埋めない。
"""

from __future__ import annotations

import argparse
import os
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


NOTE_SENT = "note=sent"


def note_sent(session: date) -> bool:
    """その日の note 貼り付け用メールを送り終えたか（その日の印に NOTE_SENT の行があるか）。

    2026-10-20: 記事づくりが転んでメールが出なかったのに、その日の印（台帳を残した印）だけが
    main に入り、予備の回が「済み」と見て止まった。印に「メールを送った」を分けて書き、
    送れていない日は、予備の回が記事とメールだけをやり直す（約定と宣告はやり直さない）。
    """
    name = f"data/us_daily_done/{session.isoformat()}.txt"
    local = PROJECT_ROOT / name
    try:
        if local.exists() and NOTE_SENT in local.read_text(encoding="utf-8"):
            return True
        shown = subprocess.run(["git", "show", f"origin/main:{name}"], cwd=PROJECT_ROOT,
                               capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return shown.returncode == 0 and NOTE_SENT in shown.stdout


def _push_ledger(session: date, mailed: bool = False) -> bool:
    """米株の注文台帳と、その日を終えた印を main に push する。
    以前はコピー用ページ（us_mail_digest）と一緒に push していた。2026-10-16 にメールと
    コピー用ページをやめたので、台帳だけをここで残す。mailed なら印に NOTE_SENT の行を足す。"""
    def run(*args):
        return subprocess.run(list(args), cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=120)
    try:
        mark = PROJECT_ROOT / "data" / "us_daily_done" / f"{session.isoformat()}.txt"
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(f"{session.isoformat()}\n" + (f"{NOTE_SENT}\n" if mailed else ""), encoding="utf-8")
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


# 前営業日の日足が Yahoo に載ったかを見る銘柄（売買代金の大きい大型株）
READY_SAMPLE = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "JPM", "XOM", "JNJ", "PG",
                "KO", "WMT", "V", "MA", "HD", "CVX", "PEP", "COST", "ABBV", "MRK")
READY_SHARE = 0.8


def data_ready(session: date, fetch=None, prefetch=None) -> tuple[bool, str]:
    """直前の米国営業日（session）の日足が、スクリーニングと同じ取り方（18か月分の一括）で取れるか。

    2026-10-20〜22: 朝いちばんの回（日本時間 9:20 ごろ）は、前営業日の足がほとんどの銘柄で無く
    （10/21 は 0/516、10/22 は 1/516）、記事が作れずに失敗メールになっていた。1時間後の回では
    そろっていた（511/517）。足が無いまま約定・宣告・記事を作らないよう、最初に確かめる。"""
    if fetch is None or prefetch is None:
        from scanner.prices import fetch_price_history, prefetch_price_histories

        fetch = fetch or (lambda t: fetch_price_history(t, period="18mo"))
        prefetch = prefetch or (lambda ts: prefetch_price_histories(ts, period="18mo"))
    sample = list(READY_SAMPLE)
    try:
        prefetch(sample)
    except Exception as error:  # noqa: BLE001 - 一括が転んでも1銘柄ずつ見る
        print(f"us_ready_prefetch=failed reason={type(error).__name__}", flush=True)
    ok = 0
    for t in sample:
        try:
            closes = fetch(t)["Close"].dropna()
            days = [(d.date() if hasattr(d, "date") else d) for d in closes.index]
            days = [d for d in days if d <= session]
            ok += bool(days) and days[-1] == session
        except Exception:  # noqa: BLE001
            continue
    return ok >= len(sample) * READY_SHARE, f"{ok}/{len(sample)}"


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
    # 記事とメールだけをやり直す回（台帳はもう残してあるが、メールが出ていない日）
    note_only = False
    if not args.force and already_published(session):
        if note_sent(session):
            print(f"us_daily=skipped reason=already_published session={session}", flush=True)
            return 0
        note_only = True

    # 前営業日の日足が載るまで待つ（最後の回でなければ、失敗メールにせず次の回に回す）
    ready, detail = data_ready(session)
    if not ready:
        last_try = os.environ.get("US_LAST_TRY") == "1"
        print(f"::notice title=米株::{session} の日足がまだそろっていない（{detail}）。"
              + ("最後の回なので失敗にします" if last_try else "次の回でやり直します"), flush=True)
        print(f"us_daily=skipped reason=data_not_ready session={session} sample={detail}", flush=True)
        return 1 if last_try else 0

    print(f"us_daily=start session={session} now={now:%Y-%m-%d %H:%M} JST"
          + (" mode=note_only（約定と宣告はしない）" if note_only else ""), flush=True)
    failures: list[str] = []

    # 1. 前回の注文を約定させる（記事だけのやり直しではしない）
    try:
        from us_portfolio import fill_us_orders

        if not note_only:
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

    # 4. 次の営業日の注文を宣告（記事だけのやり直しではしない。二重に宣告しない）
    try:
        from us_portfolio import declare_us_orders

        if not note_only:
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
    mailed = False
    try:
        import note_two_us

        note_dir = note_two_us.build(session, output_dir)
        if args.no_mail:
            print("us_daily_note=skipped reason=disabled", flush=True)
        elif not note_two_us.save_drafts(note_dir, session):
            print("us_daily_note=mail_failed", flush=True)
            failures.append("note貼り付け用メール")
        else:
            mailed = True
    except Exception as error:  # noqa: BLE001
        failures.append("note下書き")
        traceback.print_exc()
        # 公開リポジトリなので、注記には例外の種類と場所だけを出す（銘柄は出さない）
        tb = traceback.extract_tb(error.__traceback__)
        where = f"{Path(tb[-1].filename).name}:{tb[-1].lineno}" if tb else "-"
        print(f"::warning title=米株 note::{type(error).__name__} at {where}", flush=True)
    print(f"::notice title=米株 note::session={session} mailed={mailed}"
          + (" mode=note_only" if note_only else ""), flush=True)
    if not _push_ledger(session, mailed=mailed):
        failures.append("台帳の保存")

    if failures:
        print(f"us_daily=failed steps={'/'.join(failures)} session={session}", flush=True)
        # 記事だけのやり直しで転んだときは、失敗メールを重ねて出さない（最初の回で出している）
        return 0 if note_only else 1

    _write_stamp(session)
    print(f"us_daily=done session={session}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
