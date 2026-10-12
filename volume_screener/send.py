#!/usr/bin/env python3
"""出来高急増スクリーナーの結果を送るかどうかを決めて送る（GitHub Actions 用）。2026-10-12 追加。

Mac の定期タスク（stock-volume-screener-daily の SKILL.md）と同じ判断をする。
  status.txt が skip で、件名に今日の日付がある → 送らない（休場日など）
  status.txt が send で、件名に今日の日付がある → subject / body.txt / body.html をそのまま送る
  それ以外（ファイルが無い・日付が違う） → 【スクリーニング失敗】を送る。理由は error.txt
    （日付が違うときは前回の残りの可能性があるので使わず、実行時の標準エラーの末尾）
VS_SEND=1 のときだけ実際に送る。VS_TAG があれば件名の頭に付ける（Mac と並べて比べる試験期間）。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jptime import jst_today  # noqa: E402


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def decide(out: Path, today: str, stderr_tail: str = ""):
    """('skip'|'send'|'fail', 件名, 本文, HTML)"""
    status, subject = _read(out / "status.txt"), _read(out / "subject.txt")
    dated = today in subject
    if status == "skip" and dated:
        return "skip", subject, "", ""
    if status == "send" and dated:
        return "send", subject, _read(out / "body.txt"), _read(out / "body.html")
    err = "" if (status in ("send", "skip") and not dated) else _read(out / "error.txt")
    why = err or stderr_tail or "（エラーの記録がありません）"
    body = ("本日のスクリーニングは完了できませんでした。\n"
            f"status.txt: {status or '(なし)'} / subject.txt: {subject or '(なし)'}\n\n"
            f"理由:\n{why}\n\n（GitHub Actions の出来高急増スクリーナーから）")
    return "fail", f"【スクリーニング失敗】{today}", body, ""


def main() -> int:
    out = Path(os.environ.get("SCREENER_OUT", ROOT / "volume_screener" / "out"))
    log = Path(os.environ.get("VS_LOG", out / "stderr.log"))
    tail = "\n".join(_read(log).splitlines()[-15:])
    kind, subject, body, html = decide(out, jst_today().isoformat(), tail)
    print(f"volume_screener_decision={kind}", flush=True)     # 公開ログなので件名は出さない
    if kind == "skip":
        return 0
    # 試験期間は件名の頭に印を付け、Mac 側のメールと見分けられるようにする
    subject = os.environ.get("VS_TAG", "") + subject
    if os.environ.get("VS_SEND") != "1":
        print("volume_screener_send=dry_run（VS_SEND が 1 でないので送らない）", flush=True)
        return 0
    from gmail_notify import load_gmail_config, send_gmail
    config = load_gmail_config()
    if config is None:
        print("volume_screener_send=failed reason=no_gmail_config", flush=True)
        return 1
    send_gmail(subject, body, config, allow_non_business_day=True, html_body=html or None)
    print("volume_screener_send=sent", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
