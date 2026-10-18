#!/usr/bin/env python3
"""note に貼り付けるだけの形で、記事を持ち主にメールで送る（2026-10-18）。

note の下書きへの自動保存はできない: note のログイン画面が reCAPTCHA（ロボットでないかの確認）を
求め、GitHub からの自動ログインを受け付けない（2026-10-18 に確認。突破はしない）。6月からの
ログインの控え（NOTE_STORAGE_STATE）は期限が切れるたびに持ち主のログインが要り、月に何度も
手間をかけていた。そこで、記事は毎日メールで届け、持ち主は note に貼り付けるだけにする。

manifest（note_two.write_outputs が書く note_drafts_manifest.json）の記事を1通にまとめる。
宛先は MAIL_TO の1つ目（持ち主）だけ。

  python3 note_mail.py --dir outputs/note_two --subject "【note貼り付け用】..."
"""
from __future__ import annotations

import argparse
import html as H
import json
import re
from pathlib import Path


def build_mail(note_dir: Path, subject: str) -> tuple[str, str, str]:
    """(件名, テキスト本文, HTML本文)。記事ごとに「タイトル」と「本文」を分けて、そのままコピーできる形にする。"""
    entries = json.loads((note_dir / "note_drafts_manifest.json").read_text(encoding="utf-8"))
    text_parts, html_parts = [], []
    for i, e in enumerate(entries, 1):
        title = (note_dir / e["title_file"]).read_text(encoding="utf-8").strip()
        md_path = note_dir / e["html_file"].replace(".html", ".md")
        md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
        body_md = "\n".join(x for x in md.splitlines() if not x.startswith("# ")).strip()
        page = (note_dir / e["html_file"]).read_text(encoding="utf-8")
        m = re.search(r"<body[^>]*>(.*)</body>", page, flags=re.S | re.I)
        body_html = m.group(1).strip() if m else page
        text_parts.append(f"━━━━ 記事{i} ━━━━\n■ タイトル（コピーして note のタイトルへ）\n{title}\n\n"
                          f"■ 本文（ここから下をコピーして note の本文へ）\n{body_md}\n")
        html_parts.append(
            f'<div style="margin:0 0 28px 0;padding:14px 16px;border:1px solid #dcdee1;border-radius:10px;">'
            f'<div style="font-size:12px;color:#888;">記事{i}／タイトル（コピーして note のタイトルへ）</div>'
            f'<div style="font-size:17px;font-weight:bold;margin:4px 0 14px 0;">{H.escape(title)}</div>'
            f'<div style="font-size:12px;color:#888;border-top:1px solid #eee;padding-top:8px;">'
            f'本文（ここから下をコピーして note の本文へ）</div>'
            f'<div style="font-size:14px;line-height:1.8;">{body_html}</div></div>')
    lead = ("note の自動保存はできないため（note のログインがロボット確認を求めるため）、"
            "記事をこのメールでお届けします。各記事のタイトルと本文を、note にコピーして貼り付けてください。")
    text = lead + "\n\n" + "\n".join(text_parts)
    html_doc = ('<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;padding:12px;'
                'font-family:-apple-system,\'Hiragino Sans\',sans-serif;color:#1a1a1a;">'
                f'<p style="font-size:13px;color:#555;">{H.escape(lead)}</p>' + "".join(html_parts) + "</body></html>")
    return subject, text, html_doc


def send(note_dir: Path, subject: str) -> bool:
    from gmail_notify import GmailConfig, load_gmail_config, send_gmail

    config = load_gmail_config()
    if config is None:
        print("note_mail=failed reason=no_gmail_config", flush=True)
        return False
    # 宛先は持ち主（MAIL_TO の1つ目）だけ
    config = GmailConfig(user=config.user, app_password=config.app_password,
                         mail_to=config.mail_to.split(",")[0].strip())
    subj, text, html_doc = build_mail(Path(note_dir), subject)
    send_gmail(subj, text, config, allow_non_business_day=True, html_body=html_doc)
    print("note_mail=sent", flush=True)
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="note に貼り付ける記事をメールで送る")
    ap.add_argument("--dir", required=True)
    ap.add_argument("--subject", required=True)
    a = ap.parse_args(argv)
    if not (Path(a.dir) / "note_drafts_manifest.json").exists():
        print("note_mail=skipped reason=no_manifest", flush=True)
        return 0
    return 0 if send(Path(a.dir), a.subject) else 1


if __name__ == "__main__":
    raise SystemExit(main())
