"""sns_promo.py — 編集長: note下書き4本からX（旧Twitter）告知文を自動生成する。

- 入力: outputs/note_drafts_manifest.json / note_<key>.md / market_snapshot.json
- 出力: outputs/sns_posts.md（コピペ用）と outputs/sns_posts.json（将来のAPI自動投稿用）
- 事実データ（件数・地合い）だけで組み立てる。相場観の捏造はしない。
- X の文字数制限（日本語 約140字）に収まるように生成する。
- 投稿URLは note 公開後に決まるため {URL} プレースホルダにする。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"

# 4本の定義（note_draft.NOTE4_TITLES と対応。定義は変更しない）
PROMO_KEYS = ("highs", "pullback", "chatgpt", "claude")

HASHTAGS = {
    "highs": "#日本株 #株式投資 #52週新高値",
    "pullback": "#日本株 #株式投資 #押し目買い",
    "chatgpt": "#日本株 #AI投資 #ChatGPT",
    "claude": "#日本株 #AI投資 #Claude",
}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _count_from_lead(text: str, pattern: str) -> str:
    match = re.search(pattern, text)
    return match.group(1) if match else ""


def _regime(output_dir: Path) -> str:
    try:
        snapshot = json.loads(_read(output_dir / "market_snapshot.json") or "{}")
        value = str(snapshot.get("regime") or "").upper()
        return value if value in ("NORMAL", "CAUTION", "RISK", "STOP") else ""
    except (json.JSONDecodeError, TypeError):
        return ""


def build_post(key: str, note_text: str, regime: str, today: str) -> str:
    """1本分の告知文（約140字以内）。事実（件数・地合い）だけで作る。"""
    regime_part = f"地合い:{regime}" if regime else ""
    if key == "highs":
        new_cnt = _count_from_lead(note_text, r"更新した銘柄は\*\*(\d+)銘柄\*\*")
        near_cnt = _count_from_lead(note_text, r"迫った銘柄は\*\*(\d+)銘柄\*\*")
        counts = f"新高値{new_cnt}銘柄・接近{near_cnt}銘柄" if new_cnt or near_cnt else "本日の抽出結果"
        body = f"【{today} 52週新高値】{counts}。1年分の売りをこなした最強銘柄を毎日同じ基準で機械抽出。{regime_part}"
    elif key == "pullback":
        rt_cnt = _count_from_lead(note_text, r"リテスト」候補が\*\*(\d+)銘柄\*\*")
        ma_cnt = _count_from_lead(note_text, r"押し目候補が\*\*(\d+)銘柄\*\*")
        counts = f"リテスト{rt_cnt}・MAタッチ{ma_cnt}銘柄" if rt_cnt or ma_cnt else "本日の抽出結果"
        body = f"【{today} 押し目】強い銘柄が休んだ場所だけ狙う。{counts}。上向きMAタッチのみ、落ちるナイフは除外。{regime_part}"
    elif key == "chatgpt":
        body = f"【{today} 300万円運用×ChatGPT】本日の売買判断と保有・現金比率を公開。前日判断→翌寄付執行、信用なしの規律運用。{regime_part}"
    else:  # claude
        body = f"【{today} 300万円運用×Claude】本日の売買判断と保有・現金比率を公開。同じルールでAI2人の判断を毎日比較できます。{regime_part}"
    return f"{body}\n{{URL}}\n{HASHTAGS[key]}"


def build_sns_posts(output_dir: Path = OUTPUT_DIR) -> Path | None:
    """4本分の告知文を生成。manifestが無ければ何もしない（best-effort）。"""
    manifest_path = output_dir / "note_drafts_manifest.json"
    if not manifest_path.exists():
        print("sns_promo=skip（note_drafts_manifest.json なし）")
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    regime = _regime(output_dir)
    posts: list[dict[str, str]] = []
    md_lines = [f"# X告知文（{today}）", "", "note公開後、{URL} を記事URLに置き換えて投稿してください。", ""]
    for key in PROMO_KEYS:
        note_text = _read(output_dir / f"note_{key}.md")
        post = build_post(key, note_text, regime, today)
        posts.append({"key": key, "text": post})
        md_lines.extend([f"## {key}", "", "```", post, "```", ""])
    (output_dir / "sns_posts.json").write_text(
        json.dumps(posts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path = output_dir / "sns_posts.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"sns_posts={md_path}（{len(posts)}本）")
    return md_path


if __name__ == "__main__":
    build_sns_posts()
