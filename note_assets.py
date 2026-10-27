#!/usr/bin/env python3
"""note の記事ごとの「タグ」と「見出し画像」をメールで届ける（2026-10-22 持ち主の指示
「メールでnoteのタグと画像だけそれぞれの記事のぶんを送って」）。

記事の本文は「note貼り付け用」のメール（note_mail.py）で届け、持ち主の Chrome で下書きにしている。
タグと見出し画像は note の公開の設定で入れるもので、下書きの本文には入らないため、別のメールにする。
件名は「【noteタグ・画像】…」（「note貼り付け用」を含めない。下書きを作る仕組みが拾わないように）。

- タグ: 記事の種類ごとに決めた語（売買をすすめる語は使わない）。
- 見出し画像: 1280×670（note の推奨の大きさ）。タイトルと日付だけを描く。銘柄名は描かない
  （有料記事の中身を見出しで出さないため。公開リポジトリの成果物にもしない）。
- 宛先は MAIL_TO の1つ目（持ち主）だけ。

  python3 note_assets.py --dir outputs/note_two --subject "【noteタグ・画像】..."
  python3 note_assets.py --items '[{"key": "ranking", "title": "..."}]' --subject "..."   # 作り直し用
"""
from __future__ import annotations

import argparse
import html as H
import json
import os
import re
import tempfile
from pathlib import Path

WIDTH, HEIGHT = 1280, 670
ASSETS_PREFIX = "【noteタグ・画像】"

# 記事の種類ごとのタグ（note のハッシュタグ）。売買をすすめる語（おすすめ・買い時など）は入れない
TAGS: dict[str, list[str]] = {
    # 2026-10-26 持ち主「ICT手法の記事織り込んでタグも織り込んで」: ICT・オーダーブロックを足す（どれも10個以内に収める）
    "ranking": ["日本株", "株式投資", "52週高値", "ブレイクアウト", "押し目買い", "ICT", "オーダーブロック",
                "セリングクライマックス", "スクリーニング", "東証プライム"],
    "300man": ["日本株", "株式投資", "AI投資", "Claude", "紙上運用", "300万円運用", "ICT", "オーダーブロック",
               "資産運用", "投資記録"],
    # 2026-10-27 持ち主「出来高急増のところに52週新高値を織り込んで」: 52週新高値・押し目買いを足す
    "volume": ["日本株", "株式投資", "52週新高値", "押し目買い", "出来高急増", "下降トレンド", "セリングクライマックス",
               "逆張り", "スクリーニング", "東証プライム"],
    "us_ranking": ["米国株", "株式投資", "52週高値", "押し目買い", "ICT", "オーダーブロック", "セリングクライマックス",
                   "SP500", "NASDAQ", "スクリーニング"],
    "us_20k": ["米国株", "米株", "株式投資", "AI投資", "Claude", "紙上運用", "ICT", "オーダーブロック", "資産運用", "投資記録"],
}
DEFAULT_TAGS = ["株式投資", "スクリーニング"]

# 見出し画像の色（背景の上・下、差し色）と上の小さな見出し
STYLE: dict[str, tuple[str, str, str, str]] = {
    "ranking": ("#0b1f3a", "#17497f", "#f2c14e", "日本株｜毎営業日の機械スクリーニング"),
    "300man": ("#0c2a23", "#1f5e4f", "#7ee0b5", "日本株｜AIの紙上運用の記録"),
    "volume": ("#2a0f1c", "#5c1d3a", "#ff8a65", "日本株｜東証プライム全銘柄から機械的に抽出"),
    "us_ranking": ("#101a33", "#283c73", "#7cc4ff", "米国株｜毎営業日の機械スクリーニング"),
    "us_20k": ("#0f2a33", "#1f5566", "#9be7ff", "米国株｜AIの紙上運用の記録"),
}
DEFAULT_STYLE = ("#1b1f24", "#3a414b", "#e0e0e0", "毎営業日の機械スクリーニング")
FOOTER = "※ 投資助言ではありません。過去の成績は将来の結果を保証しません。"


# タイトルの書き出し → 記事の種類（下書きを作る側が、タイトルだけから見出し画像を作るため）
TITLE_KEYS = (("高値更新・押し目・セリングクライマックス ランキング", "ranking"), ("Claudeが300万円運用", "300man"),
              ("出来高急増×下降トレンド", "volume"), ("52週新高値と出来高急増", "volume"),
              ("米株 高値更新", "us_ranking"), ("米国株 $20,000運用", "us_20k"))


def key_for_title(title: str) -> str:
    for head, key in TITLE_KEYS:
        if title.strip().startswith(head):
            return key
    return ""


def tags_for(key: str) -> list[str]:
    return list(TAGS.get(key, DEFAULT_TAGS))


def tag_line(key: str) -> str:
    return " ".join(f"#{t}" for t in tags_for(key))


def find_font(bold: bool = True) -> str | None:
    """日本語の書ける字体。GitHub では japanize-matplotlib の IPAex ゴシック（pip で入れる）、
    Mac ではヒラギノを使う。見つからなければ None（画像は作らずタグだけ送る）。"""
    env = os.environ.get("NOTE_FONT", "").strip()
    cands = [env] if env else []
    try:
        # 読み込むと matplotlib を求めるので（--no-deps で入れている）、場所だけを調べる
        import importlib.util

        spec = importlib.util.find_spec("japanize_matplotlib")
        for loc in (spec.submodule_search_locations or []) if spec else []:
            cands.append(str(Path(loc) / "fonts" / "ipaexg.ttf"))
    except Exception:  # noqa: BLE001 - 入っていなければ次の候補
        pass
    weight = "W6" if bold else "W3"
    cands += [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        f"/System/Library/Fonts/ヒラギノ角ゴシック {weight}.ttc",
    ]
    for c in cands:
        if c and Path(c).exists():
            return c
    return None


def split_title(title: str) -> tuple[str, str]:
    """(大きく描く部分, 日付から後ろ)。「…｜2026-10-22 13:50時点 2件」→（…, 2026-10-22 13:50時点 2件）"""
    m = re.search(r"\d{4}-\d{2}-\d{2}", title)
    if not m:
        return title.strip(), ""
    return title[:m.start()].rstrip(" ｜|").strip(), title[m.start():].strip()


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _wrap(text: str, font, width: int, max_lines: int) -> list[str]:
    """幅に収まるように折り返す。区切り（・｜ 空白 ×）の後ろで折れるときはそこで折る。"""
    lines, cur = [], ""
    for ch in text:
        if font.getlength(cur + ch) <= width:
            cur += ch
            continue
        cut = max(cur.rfind(d) for d in ("・", "｜", " ", "×"))
        if cut >= len(cur) // 2:
            # 「・」「×」は行末に残し、「｜」と空白は行末に残さない（見た目が切れて見えるため）
            lines.append(cur[:cut + (1 if cur[cut] in "・×" else 0)].strip())
            cur = cur[cut + 1:] + ch
        else:
            lines.append(cur)
            cur = ch
    if cur.strip():
        lines.append(cur.strip())
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:-1] + "…"
    return lines


def make_image(key: str, title: str, out_path: Path) -> Path | None:
    """見出し画像（PNG）を作る。字体か Pillow が無ければ None。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    bold, regular = find_font(True), find_font(False)
    if not bold:
        return None
    regular = regular or bold
    top, bottom, accent, label = STYLE.get(key, DEFAULT_STYLE)
    t, b, a = _hex(top), _hex(bottom), _hex(accent)
    img = Image.new("RGB", (WIDTH, HEIGHT), t)
    d = ImageDraw.Draw(img)
    for y in range(HEIGHT):                                   # 上から下へのグラデーション
        r = y / (HEIGHT - 1)
        d.line([(0, y), (WIDTH, y)], fill=tuple(int(t[i] + (b[i] - t[i]) * r) for i in range(3)))
    # 飾り（右下の薄い円と左の差し色の帯）。数字やグラフの形は描かない（実データと誤解されないように）
    for i, rad in enumerate((420, 300, 190)):
        d.ellipse([WIDTH - 260 - rad, HEIGHT - 120 - rad, WIDTH - 260 + rad, HEIGHT - 120 + rad],
                  outline=tuple(min(255, c + 18 + 10 * i) for c in b), width=3)
    d.rectangle([64, 96, 76, 560], fill=a)
    f_label = ImageFont.truetype(regular, 30)
    f_main = ImageFont.truetype(bold, 66)
    f_date = ImageFont.truetype(bold, 48)
    f_foot = ImageFont.truetype(regular, 22)
    d.text((104, 92), label, font=f_label, fill=a)
    main, tail = split_title(title)
    y = 160
    for line in _wrap(main, f_main, WIDTH - 104 - 90, 3):
        d.text((104, y), line, font=f_main, fill=(255, 255, 255), stroke_width=1, stroke_fill=(255, 255, 255))
        y += 86
    if tail:
        d.text((104, y + 26), tail, font=f_date, fill=a, stroke_width=1, stroke_fill=a)
    d.text((104, HEIGHT - 62), FOOTER, font=f_foot, fill=(210, 214, 220))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG", optimize=True)
    return out_path


def _slug(title: str) -> str:
    m = re.search(r"\d{4}-\d{2}-\d{2}", title)
    return m.group(0) if m else "undated"


def items_from_dir(note_dir: Path) -> list[dict]:
    """note_drafts_manifest.json の記事（種類とタイトル）。"""
    entries = json.loads((note_dir / "note_drafts_manifest.json").read_text(encoding="utf-8"))
    return [{"key": e["key"], "title": (note_dir / e["title_file"]).read_text(encoding="utf-8").strip()}
            for e in entries]


def build(items: list[dict], out_dir: Path) -> list[dict]:
    """記事ごとに {key, title, tags, image(Path|None)} を返す。"""
    built = []
    for i, it in enumerate(items, 1):
        path = out_dir / f"note_{i:02d}_{it['key']}_{_slug(it['title'])}.png"
        try:
            image = make_image(it["key"], it["title"], path)
        except Exception as error:  # noqa: BLE001 - 画像が作れなくてもタグは送る
            print(f"note_assets_image=failed key={it['key']} reason={type(error).__name__}", flush=True)
            image = None
        built.append({**it, "tags": tag_line(it["key"]), "image": image})
    return built


def build_mail(built: list[dict], subject: str) -> tuple[str, str, str]:
    lead = ("note の記事ごとの「タグ」と「見出し画像」です。タグは note のハッシュタグの欄へ、"
            "画像（添付・1280×670）は見出し画像に設定してください。")
    text, parts = [lead, ""], []
    for i, b in enumerate(built, 1):
        img = b["image"].name if b["image"] else "（作れませんでした）"
        text += [f"━━━━ 記事{i} ━━━━", f"■ タイトル：{b['title']}",
                 "■ タグ（コピーして note のハッシュタグへ）", b["tags"], f"■ 見出し画像：{img}", ""]
        parts.append(
            '<div style="margin:0 0 22px 0;padding:12px 14px;border:1px solid #dcdee1;border-radius:10px;">'
            f'<div style="font-size:12px;color:#888;">記事{i}</div>'
            f'<div style="font-size:16px;font-weight:bold;margin:2px 0 10px 0;">{H.escape(b["title"])}</div>'
            '<div style="font-size:12px;color:#888;">タグ（コピーして note のハッシュタグへ）</div>'
            f'<div style="font-size:15px;margin:2px 0 10px 0;">{H.escape(b["tags"])}</div>'
            f'<div style="font-size:12px;color:#888;">見出し画像：{H.escape(img)}（添付）</div></div>')
    html_doc = ('<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;padding:12px;'
                "font-family:-apple-system,'Hiragino Sans',sans-serif;color:#1a1a1a;\">"
                f'<p style="font-size:13px;color:#555;">{H.escape(lead)}</p>' + "".join(parts) + "</body></html>")
    return subject, "\n".join(text), html_doc


def assets_subject(paste_subject: str) -> str:
    """貼り付け用の件名から作る（「note貼り付け用」を含めない）。"""
    s = paste_subject.replace("【note貼り付け用】", "").strip()
    return ASSETS_PREFIX + s


def send(items: list[dict], subject: str, out_dir: Path | None = None) -> bool:
    from gmail_notify import GmailConfig, load_gmail_config, send_gmail

    config = load_gmail_config()
    if config is None:
        print("note_assets=failed reason=no_gmail_config", flush=True)
        return False
    config = GmailConfig(user=config.user, app_password=config.app_password,
                         mail_to=config.mail_to.split(",")[0].strip())
    out_dir = out_dir or Path(tempfile.mkdtemp(prefix="note_assets_"))
    built = build(items, out_dir)
    subj, text, html_doc = build_mail(built, subject)
    send_gmail(subj, text, config, attachments=[b["image"] for b in built if b["image"]],
               allow_non_business_day=True, html_body=html_doc)
    print(f"note_assets=sent items={len(built)} images={sum(1 for b in built if b['image'])}", flush=True)
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="note のタグと見出し画像をメールで送る")
    ap.add_argument("--dir", default="")
    ap.add_argument("--items", default="", help='[{"key": ..., "title": ...}] の JSON（作り直し用）')
    ap.add_argument("--subject", default="")
    ap.add_argument("--out", default="", help="画像を書く場所（既定は一時フォルダ）")
    ap.add_argument("--image-for-title", default="", help="このタイトルの見出し画像だけを --out（ファイル名）に作る")
    a = ap.parse_args(argv)
    if a.image_for_title:
        # 下書きを作る Mac 側から使う（2026-10-22）。メールは送らない
        key = key_for_title(a.image_for_title)
        path = make_image(key, a.image_for_title, Path(a.out))
        print(f"note_image={'made' if path else 'failed'} key={key or '-'} path={path or '-'}", flush=True)
        return 0 if path else 1
    if a.items:
        items = json.loads(a.items)
    elif a.dir and (Path(a.dir) / "note_drafts_manifest.json").exists():
        items = items_from_dir(Path(a.dir))
    else:
        print("note_assets=skipped reason=no_items", flush=True)
        return 0
    return 0 if send(items, a.subject, Path(a.out) if a.out else None) else 1


if __name__ == "__main__":
    raise SystemExit(main())
