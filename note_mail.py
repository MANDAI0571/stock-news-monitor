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
from urllib.parse import quote

# 2026-11-13 持ち主のメモ「x記事自動」: 記事のお知らせの X の文を、記事ごとにメールの最後へ自動で付ける。
# 📋 でコピー、𝕏 で投稿の画面が開く。投稿するのは持ち主（こちらから X へは投稿しない）。
# 有料の線より下（有料の部分）の見出しは使わない。売買をすすめる言葉は入れない。
PAID_LINE = "▼ここから有料▼"
X_SKIP = ("ご注意", "印について", "Xで", "これまでの決済", "直近の売買", "運用の状況", "くわしく")
X_HEAD_LIMIT = 3


def _x_head(head: str) -> str:
    """見出しを短くする。かっこの中は「3件」「1銘柄」だけ残し、「全3件のうち…」は「全3件」にする。"""
    def keep(m: re.Match) -> str:
        inner = m.group(1)
        if re.fullmatch(r"全?\d+(件|銘柄)", inner):
            return f"（{inner}）"
        total = re.search(r"全\d+件", inner)
        return f" {total.group(0)}" if total else ""

    short = re.sub(r"（([^（）]*)）", keep, head).replace(" TOP10", "").strip()
    return short if len(short) <= 36 else short[:35] + "…"


def x_post(title: str, md: str, limit: int = 280) -> str:
    """記事のお知らせの X の文（280字に収める。長いときは見出しを後ろから外し、それでも長ければタグも外す）。"""
    from intraday_high_alert import NOTE_URL, x_length

    free = md.split(PAID_LINE, 1)[0]
    heads: list[str] = []
    for line in free.splitlines():
        if line.startswith("## "):
            head = line[3:].replace("**", "").strip()
            if head and head != "ルール" and not any(word in head for word in X_SKIP):
                short = "・" + _x_head(head)
                if short not in heads:
                    heads.append(short)
    tag_lines = [x for x in md.strip().splitlines() if re.match(r"^#[^#\s]", x.strip())]
    tags = " ".join(tag_lines[-1].split()[:3]) if tag_lines else "#日本株 #株式投資"
    note = "※売買をすすめるものではありません"
    for tg in (tags, ""):
        for n_heads in range(min(X_HEAD_LIMIT, len(heads)), -1, -1):
            text = "\n".join(["📝 note を更新しました", title, *heads[:n_heads], NOTE_URL] + ([tg] if tg else []) + [note])
            if x_length(text) <= limit:
                return text
    return "\n".join(["📝 note を更新しました", title[:60], NOTE_URL, note])


def _x_html(i: int, text: str) -> str:
    from intraday_high_alert import clip_url

    btn = ("display:inline-block;margin:6px 6px 0 0;padding:8px 12px;border-radius:8px;color:#ffffff;"
           "text-decoration:none;font-weight:bold;font-size:13px;")
    return (f'<div style="margin:0 0 14px 0;">'
            f'<div style="font-size:12px;color:#888;">X 用の文{i}（記事{i}のお知らせ）</div>'
            f'<div style="white-space:pre-wrap;font-size:13px;line-height:1.6;padding:8px 10px;background:#f6f8fa;'
            f'border:1px dashed #c9d1d9;border-radius:6px;">{H.escape(text)}</div>'
            f'<a style="{btn}background:#1f745f;" href="{H.escape(clip_url(text))}">📋 この文をコピー</a>'
            f'<a style="{btn}background:#111111;" href="https://x.com/intent/post?text={quote(text, safe="")}">𝕏に投稿</a>'
            f'</div>')


def build_mail(note_dir: Path, subject: str) -> tuple[str, str, str]:
    """(件名, テキスト本文, HTML本文)。記事ごとに「タイトル」と「本文」を分けて、そのままコピーできる形にする。"""
    entries = json.loads((note_dir / "note_drafts_manifest.json").read_text(encoding="utf-8"))
    text_parts, html_parts, x_parts = [], [], []
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
        x_parts.append((i, x_post(title, md)))
    lead = ("note の自動保存はできないため（note のログインがロボット確認を求めるため）、"
            "記事をこのメールでお届けします。各記事のタイトルと本文を、note にコピーして貼り付けてください。")
    x_lead = "X に流すお知らせの文です（📋 でコピー、𝕏 で投稿の画面が開きます。投稿はご自身で。記事を公開してからどうぞ）。"
    text = (lead + "\n\n" + "\n".join(text_parts) + "\n━━━━ X に流す文（記事のお知らせ） ━━━━\n" + x_lead + "\n\n"
            + "\n\n".join(f"［X 用の文{i}］\n{t}" for i, t in x_parts) + "\n")
    x_html = (f'<div style="margin:0 0 28px 0;padding:14px 16px;border:1px solid #dcdee1;border-radius:10px;">'
              f'<div style="font-size:15px;font-weight:bold;margin:0 0 6px 0;">X に流す文（記事のお知らせ）</div>'
              f'<div style="font-size:12px;color:#555;margin:0 0 10px 0;">{H.escape(x_lead)}</div>'
              + "".join(_x_html(i, t) for i, t in x_parts) + "</div>") if x_parts else ""
    html_doc = ('<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;padding:12px;'
                'font-family:-apple-system,\'Hiragino Sans\',sans-serif;color:#1a1a1a;">'
                f'<p style="font-size:13px;color:#555;">{H.escape(lead)}</p>' + "".join(html_parts) + x_html
                + "</body></html>")
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
    # タグと見出し画像の別メール（【noteタグ・画像】）は 2026-10-22 夜にやめた。持ち主「メールはリアルタイムと
    # 出来高急増以外はない方向で」「タグ画像もコピペワンタッチで」。タグは記事の最後の1行に入れ
    # （note_two.write_outputs）、見出し画像は下書きを作るときに Mac 側で作って note に入れる
    # （note_assets.py --image-for-title。持ち主の了承あり）。このメール自体も Gmail のフィルタで受信トレイに入れない。
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
