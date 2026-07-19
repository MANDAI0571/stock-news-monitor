#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配信下書き生成ひな型（フェーズ1・Claude/Fable）

52週新高値noteの下書き(.md)から「本命」銘柄を読み取り、
X（ツイート）とLINEの配信下書きを生成する。

原則:
- 実データのみ。note下書きに書かれた値だけを使い、数値を捏造しない。
- 断定・推奨・利益保証の表現を入れない（事実の状態のみ記述）。
- 登録導線リンクにはUTMを付与（規約は phase1/docs/phase1_collaboration.md）。
- プレースホルダ {{...}} は出力に残さない。必要な設定が無ければエラーで停止する。
- 生成後は phase1/tools/compliance_check.py に通すこと（本スクリプトは --check で自動実行）。

使い方:
  python3 generate_distribution_drafts.py \
      --note 本日のnote下書き_YYYYMMDD.md \
      --config distribution_config.json \
      --content morning \
      --out-dir out/ [--check]

config(JSON)の必須項目:
  signup_base_url : 登録導線のベースURL（UTMを付与する対象）
config(JSON)の任意項目:
  note_url        : 公開note記事のURL（あれば併記）
  campaign        : 例 phase1_202607（無ければ日付から自動生成）
"""

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ---- 禁止トークン（compliance_check.py と同じ考え方）----
FORBIDDEN_SUBSTR = ("未取得", "OpenWork", "残業", "有給")
FORBIDDEN_WORD = ("nan", "None", "null", "inf", "NaN", "NULL")


def find_forbidden(text):
    leaked = [t for t in FORBIDDEN_SUBSTR if t in text]
    for t in FORBIDDEN_WORD:
        if re.search(rf"(?<![A-Za-z]){re.escape(t)}(?![A-Za-z])", text):
            leaked.append(t)
    return leaked


def has_placeholder(text):
    return re.findall(r"\{\{[^}]*\}\}", text)


def width(s):
    """全角=2, 半角=1 でおおよその表示幅を返す（X字数の目安）。"""
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("F", "W", "A") else 1
    return w


# ---- note下書きの解析 ----
def parse_date(text):
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    return datetime(y, mo, d)


def parse_honmei(text):
    """『本命』セクションから銘柄を抽出。note下書きに書かれた値のみ使用。"""
    # 本命セクションを切り出す
    start = text.find("本命")
    sec = text[start:] if start >= 0 else text
    # 次の "## " 見出しで打ち切り
    nxt = re.search(r"\n##\s", sec[3:])
    if nxt:
        sec = sec[: nxt.start() + 3]

    picks = []
    # 例: ### 1. 吉野家ホールディングス（9861）｜Sランク
    blocks = re.split(r"\n###\s+\d+\.\s*", sec)
    for b in blocks[1:]:
        head = b.splitlines()[0]
        mh = re.match(r"(.+?)（(\d{3,4})）｜([SABＳＡＢ])ランク", head)
        if not mh:
            continue
        name, code, rank = mh.group(1).strip(), mh.group(2), mh.group(3)
        price = None
        mp = re.search(r"現在値\s*([0-9,]+)\s*円", b)
        if mp:
            price = mp.group(1)
        dist = None
        md = re.search(r"52週高値まで\s*([0-9.]+)\s*%", b)
        if md:
            dist = float(md.group(1))
        reached = "到達" in b and (dist == 0.0)
        vol = None
        mv = re.search(r"出来高倍率\s*([0-9.]+)\s*倍", b)
        if mv:
            vol = mv.group(1)
        picks.append({
            "name": name, "code": code, "rank": rank,
            "price": price, "dist": dist, "reached": reached, "vol": vol,
        })
    return picks


def dist_phrase(p):
    if p["dist"] is None:
        return None
    if p["dist"] == 0.0:
        return "高値に到達"
    return f"高値まで{p['dist']:.2f}%"


def pick_line(p, with_vol=True):
    """1銘柄の要点（事実のみ）。"""
    parts = [f"{p['name']}({p['code']})"]
    dp = dist_phrase(p)
    if dp:
        parts.append(dp)
    if with_vol and p["vol"]:
        parts.append(f"出来高{p['vol']}倍")
    return "・".join(parts)


DISCLAIMER_SHORT = "※特定銘柄の売買を推奨するものではありません"


def build_utm_link(base, campaign, content, source, medium):
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("signup_base_url は http(s) の有効なURLを指定してください。")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({
        "utm_source": source,
        "utm_medium": medium,
        "utm_campaign": campaign,
        "utm_content": content,
    })
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def build_x(dt, picks, link, note_url):
    ds = f"{dt.month}/{dt.day}"
    # note下書きの本命の並び（編集部ランク順）を尊重。X本文は簡潔に頭1銘柄＋総数。
    n = len(picks)
    head = picks[0]
    lead = f"【{ds} 52週新高値】本命Sランク中心に{n}銘柄。"
    body = f"注目は{pick_line(head)}。ほか{n-1}銘柄を記事に。" if n > 1 else f"注目は{pick_line(head)}。"
    cta = f"一覧と登録→{link}"
    tags = "#日本株 #52週新高値"
    lines = [lead + body, cta, tags, DISCLAIMER_SHORT]
    if note_url:
        lines.insert(2, f"記事：{note_url}")
    return "\n".join(lines)


def build_line(dt, picks, link, note_url):
    ds = f"{dt.year}年{dt.month}月{dt.day}日"
    top = picks[:3]
    head = f"[{ds}｜52週新高値] 本日の注目（本命）"
    bullets = "\n".join(f"・{pick_line(p)}" for p in top)
    cta = f"▼一覧・無料登録\n{link}"
    art = f"▼記事\n{note_url}\n" if note_url else ""
    return f"{head}\n{bullets}\n\n{art}{cta}\n\n{DISCLAIMER_SHORT}"


def guard(text, label):
    leaked = find_forbidden(text)
    ph = has_placeholder(text)
    problems = []
    if leaked:
        problems.append(f"禁止トークン {leaked}")
    if ph:
        problems.append(f"未置換プレースホルダ {ph}")
    if problems:
        sys.stderr.write(f"[中止] {label}: " + " / ".join(problems) + "\n")
        sys.exit(2)


def run_compliance(path):
    tool = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compliance_check.py")
    if not os.path.exists(tool):
        print(f"  （校閲ツール未検出: {tool}／手動で compliance_check を実行してください）")
        return None
    import subprocess
    r = subprocess.run([sys.executable, tool, "--input", path, "--quiet"])
    print(f"  校閲: {'✅合格 (exit 0)' if r.returncode == 0 else '⚠️要修正 (exit 1)'}  {os.path.basename(path)}")
    return r.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--note", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--content", default="morning",
                    help="utm_content: morning/ma25/ma200/close/chatgpt300/claude300")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--check", action="store_true", help="生成後にcompliance_check.pyを実行")
    args = ap.parse_args()

    with open(args.note, encoding="utf-8") as f:
        note = f.read()
    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)

    base = (cfg.get("signup_base_url") or "").strip()
    if not base:
        sys.stderr.write("[中止] config の signup_base_url が空です。登録導線URLを設定してください。\n")
        sys.exit(2)
    note_url = (cfg.get("note_url") or "").strip()

    dt = parse_date(note)
    if not dt:
        sys.stderr.write("[中止] note下書きから日付を読み取れませんでした。\n")
        sys.exit(2)
    campaign = (cfg.get("campaign") or f"phase1_{dt:%Y%m}").strip()

    picks = parse_honmei(note)
    if not picks:
        sys.stderr.write("[中止] 本命銘柄を抽出できませんでした。note下書きの形式を確認してください。\n")
        sys.exit(2)

    try:
        x_link = build_utm_link(base, campaign, args.content, "x", "social")
        line_link = build_utm_link(base, campaign, args.content, "line", "messaging")
    except ValueError as exc:
        sys.stderr.write(f"[中止] {exc}\n")
        sys.exit(2)

    x_text = build_x(dt, picks, x_link, note_url)
    line_text = build_line(dt, picks, line_link, note_url)

    guard(x_text, "X下書き")
    guard(line_text, "LINE下書き")

    os.makedirs(args.out_dir, exist_ok=True)
    x_path = os.path.join(args.out_dir, f"配信下書き_X_{dt:%Y%m%d}.md")
    line_path = os.path.join(args.out_dir, f"配信下書き_LINE_{dt:%Y%m%d}.md")

    with open(x_path, "w", encoding="utf-8") as f:
        f.write(x_text + "\n")
    with open(line_path, "w", encoding="utf-8") as f:
        f.write(line_text + "\n")

    print(f"本命 {len(picks)}銘柄を抽出。")
    print(f"X下書き   : {x_path}（約{width(x_text.splitlines()[0])}幅／全体{len(x_text)}文字）")
    print(f"LINE下書き: {line_path}")
    if args.check:
        results = (run_compliance(x_path), run_compliance(line_path))
        if any(result != 0 for result in results):
            sys.stderr.write("[中止] 校閲に合格しなかった下書きがあります。\n")
            sys.exit(1)
    else:
        print("※ 配信前に compliance_check.py（exit 0）と pre_publish_checklist.md の人の目チェックを必ず通してください。")


if __name__ == "__main__":
    main()
