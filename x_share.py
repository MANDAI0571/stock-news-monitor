"""X（旧Twitter）に流す文の小さな道具（字数の数え方・note の URL・コピー用のページのリンク）。

2026-11-13: 記事メールの X の文（note_mail.py）が intraday_high_alert から読み込んでいたため、
yfinance を入れていない出来高急増のワークフローで有料記事のメールが止まった。標準ライブラリだけで動くようにここへ移した。
"""

from __future__ import annotations

import base64
import re

NOTE_URL = "https://note.com/breakout_scan"

# 1銘柄ずつコピーするページ（docs/clip.html。GitHub Pages）。文はリンクの「#」より後ろで運ぶので、サーバーにも
# リポジトリにも銘柄は残らない（2026-10-26 持ち主「一銘柄一つずつコピペできるように」）
CLIP_PAGE_URL = "https://mandai0571.github.io/stock-news-monitor/clip.html"


def x_length(text: str) -> int:
    """X（旧Twitter）の数え方での長さ。漢字・かなは2、英数字・記号は1、URL は23。"""
    n = 0
    for part in re.split(r"(https?://\S+)", text):
        if part.startswith("http"):
            n += 23
            continue
        for ch in part:
            o = ord(ch)
            n += 1 if (o <= 0x10FF or 0x2000 <= o <= 0x200D or 0x2010 <= o <= 0x201F or 0x2032 <= o <= 0x2037) else 2
    return n


def clip_url(text: str, chart: str = "") -> str:
    """コピー用のページのリンク。文は UTF-8 の base64url（# の後ろの b=。%XX にするより半分ほど短い）、
    チャートは chart_payload の文字（c=）。どちらも # の後ろなのでサーバーには送られない。"""
    b = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")
    return CLIP_PAGE_URL + "#b=" + b + (f"&c={chart}" if chart else "")
