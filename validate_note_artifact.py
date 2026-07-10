from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
from typing import Any


REQUIRED_FILES = [
    "note_body.md",
    "note_preview.html",
    "market_snapshot.json",
    "eyecatch.png",
    "market_status.png",
    "funnel.png",
    "watch.png",
    "warren_summary.json",
    "decision_result.csv",
    "discipline_result.csv",
    "paper_portfolio_decision.csv",
    "note_cloud_artifact_manifest.json",
    "note_drafts_manifest.json",
    "note_chatgpt.md",
    "note_claude.md",
    "note_pullback.md",
    "note_highs.md",
]

# 正しい4本（この定義は変更しない）
NOTE4_KEYS = ("highs", "pullback", "chatgpt", "claude")
NOTE4_LABELS = {
    "highs": "52週新高値到達・接近",
    "pullback": "新高値後の押し目（25MA・200MAタッチ）",
    "chatgpt": "300万円運用 ChatGPT",
    "claude": "300万円運用 Claude",
}
VALID_REGIMES = ("NORMAL", "CAUTION", "RISK", "STOP")

# 4本それぞれの「最低限の記事内容」チェック（存在するだけではPASSにしない）
# 300万円運用（chatgpt/claude）に必須のセクション見出し
NOTE4_PORTFOLIO_SECTIONS = (
    "## 保有銘柄・CASH判断",
    "## 売買理由",
    "## 評価額・現金比率",
    "## 損益（未実現損益）",
    "## 次営業日の方針",
)
# highs/pullback: 候補がある場合に必須の「理由」マーカー（従来表の根拠列）
NOTE4_REASON_MARKERS = {
    "highs": ("高値乖離%",),
    "pullback": ("新高値ライン", "MA25", "MA200", "MA240"),
}

MARKET_INDICATORS = [
    ("nikkei", "日経平均"),
    ("topix", "TOPIX"),
    ("vix", "VIX"),
    ("sox", "SOX"),
    ("usdjpy", "ドル円"),
]

WARREN_FILES = {
    "warren_summary.json",
    "decision_result.csv",
    "discipline_result.csv",
    "paper_portfolio_decision.csv",
}


class PreviewParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.image_sources: list[str] = []
        self.text_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "img":
            attr_map = dict(attrs)
            src = attr_map.get("src")
            if src:
                self.image_sources.append(src)

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.text_chunks.append(text)


@dataclass
class ArtifactValidation:
    artifact_dir: Path
    valid: bool = True
    missing_files: list[str] = field(default_factory=list)
    missing_items: list[str] = field(default_factory=list)
    buy_cash_judgement: str = "不明"
    watch_count: int | None = None
    warren_valid: bool = True
    warren_missing_items: list[str] = field(default_factory=list)
    capital: int | None = None
    regime: str = "未取得"
    cash_reason: str = ""
    selected_symbols: list[str] = field(default_factory=list)
    market_status: dict[str, str] = field(default_factory=dict)
    preview_images: list[str] = field(default_factory=list)
    note4_status: dict[str, str] = field(default_factory=dict)

    def fail(self, item: str) -> None:
        self.valid = False
        self.missing_items.append(item)

    def fail_warren(self, item: str) -> None:
        self.valid = False
        self.warren_valid = False
        self.warren_missing_items.append(item)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8-sig")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(_read_text(path))


def _has_text(text: str, needle: str) -> bool:
    return needle in text


def _parse_counts(note_body: str) -> tuple[int | None, int | None, int | None]:
    match = re.search(r"BUY\s*(\d+)件\s*/\s*WATCH\s*(\d+)件\s*/\s*SKIP\s*(\d+)件", note_body)
    if not match:
        return None, None, None
    return tuple(int(value) for value in match.groups())  # type: ignore[return-value]


def _png_is_present(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 1000 and path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def _validate_files(result: ArtifactValidation) -> None:
    for name in REQUIRED_FILES:
        path = result.artifact_dir / name
        if not path.exists() or path.stat().st_size == 0:
            result.valid = False
            result.missing_files.append(name)
            if name in WARREN_FILES:
                result.fail_warren(f"{name} がありません")

    buy_images = sorted(
        path.name
        for path in result.artifact_dir.glob("buy_*.png")
        if _png_is_present(path)
    )
    if "buy_cash.png" not in buy_images and not [name for name in buy_images if name != "buy_cash.png"]:
        result.valid = False
        result.missing_files.append("buy_cash.png または buy_*.png")

    for name in ["eyecatch.png", "market_status.png", "funnel.png", "watch.png"]:
        path = result.artifact_dir / name
        if path.exists() and not _png_is_present(path):
            result.fail(f"{name} がPNG実体として読めない、または小さすぎます")


def _validate_note_body(result: ArtifactValidation, note_body: str) -> None:
    required_text = [
        ("タイトル", "# "),
        ("市場状況", "## 市場状況"),
        ("地合い", "地合い"),
        ("日経平均", "日経平均"),
        ("TOPIX", "TOPIX"),
        ("VIX", "VIX"),
        ("SOX", "SOX"),
        ("ドル円", "ドル円"),
        ("WATCH", "WATCH"),
        ("本日の300万円運用判断", "## 本日の300万円運用判断"),
        ("300万円", "300万円"),
        ("BUY件数", "BUY件数"),
        ("WATCH件数", "WATCH件数"),
        ("免責文", "## 免責文"),
        ("投資助言ではありません", "投資助言ではありません"),
    ]
    for label, needle in required_text:
        if not _has_text(note_body, needle):
            result.fail(f"note_body.md: {label} がありません")

    buy_count, watch_count, _skip_count = _parse_counts(note_body)
    result.watch_count = watch_count
    if buy_count is None:
        result.fail("note_body.md: BUY/WATCH/SKIP件数が読めません")
    elif buy_count == 0:
        result.buy_cash_judgement = "CASH"
        if "CASHカード" not in note_body and "現金待機" not in note_body:
            result.fail("note_body.md: BUY0件時のCASH判断がありません")
        if "なぜBUY0件なのか" not in note_body:
            result.fail("note_body.md: BUY0件時の見送り理由見出しがありません")
    else:
        result.buy_cash_judgement = "BUY"
        if "BUYカード" not in note_body:
            result.fail("note_body.md: BUY判断があるのにBUYカードがありません")


def _validate_preview(result: ArtifactValidation, preview_html: str) -> None:
    if len(preview_html.strip()) < 200 or "<html" not in preview_html.lower():
        result.fail("note_preview.html: HTMLとして空、または短すぎます")

    parser = PreviewParser()
    parser.feed(preview_html)
    visible_text = "\n".join(parser.text_chunks)
    result.preview_images = parser.image_sources
    if not parser.image_sources:
        result.fail("note_preview.html: 画像参照がありません")

    required_refs = ["market_status.png", "funnel.png", "watch.png"]
    for name in required_refs:
        if name not in parser.image_sources:
            result.fail(f"note_preview.html: {name} 参照がありません")

    buy_refs = [src for src in parser.image_sources if Path(src).name.startswith("buy_")]
    if not buy_refs:
        result.fail("note_preview.html: buy_cash.png または buy_*.png 参照がありません")
    if "本日の300万円運用判断" not in visible_text:
        result.fail("note_preview.html: 本日の300万円運用判断 がありません")
    if "300万円" not in visible_text:
        result.fail("note_preview.html: 300万円運用の表示がありません")
    for src in parser.image_sources:
        local_path = result.artifact_dir / Path(src).name
        if not local_path.exists() or local_path.stat().st_size == 0:
            result.fail(f"note_preview.html: 参照画像 {src} の実体がありません")


def _validate_market_snapshot(result: ArtifactValidation, market: dict[str, Any]) -> None:
    if not market.get("regime"):
        result.fail("market_snapshot.json: regime がありません")
    if not market.get("indicator_regime"):
        result.fail("market_snapshot.json: indicator_regime がありません")

    indicators = market.get("indicators")
    if not isinstance(indicators, dict):
        result.fail("market_snapshot.json: indicators がありません")
        indicators = {}

    for key, label in MARKET_INDICATORS:
        item = indicators.get(key)
        if not isinstance(item, dict):
            result.fail(f"market_snapshot.json: {label} がありません")
            result.market_status[label] = "missing"
            continue
        display_value = str(item.get("display_value") or "未取得")
        status = str(item.get("status") or "unavailable")
        change = str(item.get("display_change_pct") or "未取得")
        result.market_status[label] = f"{display_value} / {change} / {status}"


def _validate_warren_summary(result: ArtifactValidation, summary: dict[str, Any]) -> None:
    required = [
        "date",
        "capital",
        "regime",
        "buy_count",
        "watch_count",
        "skip_count",
        "cash_count",
        "selected_symbols",
        "cash_reason",
        "risk_control_reason",
        "source_files",
        "generated_at",
    ]
    for key in required:
        if key not in summary:
            result.fail_warren(f"warren_summary.json: {key} がありません")

    try:
        result.capital = int(summary.get("capital", 0))
    except (TypeError, ValueError):
        result.capital = None
    if result.capital != 3_000_000:
        result.fail_warren("warren_summary.json: capital が3000000ではありません")

    result.regime = str(summary.get("regime") or "未取得")
    if result.regime == "未取得":
        result.fail_warren("warren_summary.json: regime がありません")

    buy_count = _int_value(summary.get("buy_count"))
    watch_count = _int_value(summary.get("watch_count"))
    cash_count = _int_value(summary.get("cash_count"))
    if buy_count is None:
        result.fail_warren("warren_summary.json: buy_count が読めません")
        buy_count = 0
    if watch_count is None:
        result.fail_warren("warren_summary.json: watch_count が読めません")
    if cash_count is None:
        result.fail_warren("warren_summary.json: cash_count が読めません")
        cash_count = 0

    result.cash_reason = str(summary.get("cash_reason") or "").strip()
    selected = summary.get("selected_symbols")
    if not isinstance(selected, list):
        result.fail_warren("warren_summary.json: selected_symbols が配列ではありません")
        selected = []
    result.selected_symbols = [
        str(item.get("code") or item.get("symbol") or "")
        for item in selected
        if isinstance(item, dict) and str(item.get("code") or item.get("symbol") or "").strip()
    ]

    if buy_count > 0 and not result.selected_symbols:
        result.fail_warren("warren_summary.json: BUYがあるのにselected_symbolsが空です")
    if (buy_count == 0 or cash_count > 0) and not result.cash_reason:
        result.fail_warren("warren_summary.json: CASH判断なのにcash_reasonがありません")
    if not str(summary.get("risk_control_reason") or "").strip():
        result.fail_warren("warren_summary.json: risk_control_reason がありません")

    source_files = summary.get("source_files")
    if not isinstance(source_files, dict):
        result.fail_warren("warren_summary.json: source_files がありません")
        source_files = {}
    if "decision_result" not in source_files and not (result.artifact_dir / "decision_result.csv").exists():
        result.fail_warren("warren_summary.json: decision_result系CSVが参照されていません")
    for name in ["decision_result.csv", "discipline_result.csv"]:
        if not (result.artifact_dir / name).exists():
            result.fail_warren(f"{name} がArtifactにありません")


def _md_section_body(text: str, header: str) -> str:
    """header見出しから次の「## 」見出しまでの本文を返す（見出し行は含まない）。"""
    body: list[str] = []
    in_section = False
    for line in text.splitlines():
        if line.strip() == header:
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section:
            body.append(line)
    return "\n".join(body)


def _note4_content_issue(key: str, text: str) -> str | None:
    """4本それぞれの最低限の記事内容を確認。問題があれば理由文字列を返す（None=OK）。"""
    if key in ("chatgpt", "claude"):
        # 300万円運用: 保有/CASH判断・売買理由・評価額/現金比率・損益・次営業日方針の5点
        for header in NOTE4_PORTFOLIO_SECTIONS:
            if header not in text:
                return f"必須セクション欠落: {header[3:]}"
            if not _md_section_body(text, header).strip():
                return f"セクション空欄: {header[3:]}"
        return None
    # highs / pullback: 候補銘柄一覧＋理由、無い場合は「該当なし」「データ不足」の明記が必須
    # 全候補がイナゴ/TOB疑いでカードが無い日でも、従来表があれば「候補あり」として扱う
    has_candidates = "### カード型候補" in text or "### 従来表" in text
    if has_candidates:
        markers = NOTE4_REASON_MARKERS[key]
        if not any(m in text for m in markers):
            return "候補はあるが理由列（" + "/".join(markers) + "）がありません"
        table_rows = sum(
            1 for line in text.splitlines()
            if line.startswith("|") and "---" not in line and "コード" not in line
        )
        if table_rows == 0:
            return "候補カードはあるが候補銘柄一覧（表）が空です"
        return None
    if "該当なし" not in text and "データ不足" not in text:
        return "候補銘柄一覧が無く、「該当なし」「データ不足」の明記もありません"
    return None


def _validate_note4(result: ArtifactValidation) -> None:
    """正しい4本（highs/pullback/chatgpt/claude）が生成され、各冒頭に市場ステータスが入っているか。"""
    manifest_path = result.artifact_dir / "note_drafts_manifest.json"
    manifest_keys: set[str] = set()
    if manifest_path.exists():
        try:
            entries = json.loads(_read_text(manifest_path))
            manifest_keys = {str(e.get("key")) for e in entries if isinstance(e, dict)}
        except (json.JSONDecodeError, TypeError):
            result.fail("note_drafts_manifest.json: JSONとして読めません")
    for key in NOTE4_KEYS:
        label = NOTE4_LABELS[key]
        if manifest_path.exists() and key not in manifest_keys:
            result.fail(f"note_drafts_manifest.json: {label}（{key}）がありません")
        md_path = result.artifact_dir / f"note_{key}.md"
        if not md_path.exists() or md_path.stat().st_size == 0:
            result.fail(f"note_{key}.md: {label} の下書きがありません（4本必須）")
            result.note4_status[label] = "未生成"
            continue
        text = _read_text(md_path)
        if "## 市場ステータス" not in text:
            result.fail(f"note_{key}.md: 冒頭の市場ステータスがありません")
            result.note4_status[label] = "市場ステータス欠落"
        elif not any(f"**{v}**" in text for v in VALID_REGIMES):
            result.fail(f"note_{key}.md: 市場ステータスが空欄です（NORMAL/CAUTION/RISK/STOPのいずれも無し）")
            result.note4_status[label] = "市場ステータス空欄"
        else:
            issue = _note4_content_issue(key, text)
            if issue:
                result.fail(f"note_{key}.md: {issue}")
                result.note4_status[label] = f"内容不足（{issue}）"
            else:
                result.note4_status[label] = "OK"


def _int_value(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def validate_artifact(artifact_dir: str | Path) -> ArtifactValidation:
    result = ArtifactValidation(Path(artifact_dir))
    _validate_files(result)
    _validate_note4(result)

    body_path = result.artifact_dir / "note_body.md"
    if body_path.exists():
        _validate_note_body(result, _read_text(body_path))

    preview_path = result.artifact_dir / "note_preview.html"
    if preview_path.exists():
        _validate_preview(result, _read_text(preview_path))

    market_path = result.artifact_dir / "market_snapshot.json"
    if market_path.exists():
        try:
            _validate_market_snapshot(result, _load_json(market_path))
        except json.JSONDecodeError as exc:
            result.fail(f"market_snapshot.json: JSONとして読めません: {exc}")

    warren_path = result.artifact_dir / "warren_summary.json"
    if warren_path.exists():
        try:
            _validate_warren_summary(result, _load_json(warren_path))
        except json.JSONDecodeError as exc:
            result.fail_warren(f"warren_summary.json: JSONとして読めません: {exc}")

    return result


def _summary_lines(result: ArtifactValidation) -> list[str]:
    lines = [
        "## Note Artifact Quality Gate",
        "",
        f"- NOTE_ARTIFACT_VALID={'true' if result.valid else 'false'}",
        f"- WARREN_VALID={'true' if result.warren_valid else 'false'}",
        f"- Artifact dir: `{result.artifact_dir}`",
        f"- capital: {result.capital if result.capital is not None else '未取得'}",
        f"- regime: {result.regime}",
        f"- BUY/CASH判定: {result.buy_cash_judgement}",
        f"- WATCH件数: {result.watch_count if result.watch_count is not None else '未取得'}",
        f"- CASH理由: {result.cash_reason or 'なし'}",
        f"- selected_symbols: {', '.join(result.selected_symbols) if result.selected_symbols else 'なし'}",
        f"- 欠けているファイル: {', '.join(result.missing_files) if result.missing_files else 'なし'}",
        f"- 欠けている項目: {', '.join(result.missing_items) if result.missing_items else 'なし'}",
        f"- ウォーレン欠け項目: {', '.join(result.warren_missing_items) if result.warren_missing_items else 'なし'}",
        "",
        "### 4本下書きの状態",
    ]
    if result.note4_status:
        for label, status in result.note4_status.items():
            lines.append(f"- {label}: {status}")
    else:
        lines.append("- 未検査")
    lines.extend(["", "### 市場指標の取得状況"])
    if result.market_status:
        for label, status in result.market_status.items():
            lines.append(f"- {label}: {status}")
    else:
        lines.append("- 未取得")
    lines.extend(["", "### HTML画像参照"])
    if result.preview_images:
        for src in result.preview_images:
            lines.append(f"- {src}")
    else:
        lines.append("- なし")
    return lines


def write_summary(result: ArtifactValidation, summary_path: str | Path | None = None) -> None:
    path_text = str(summary_path or os.environ.get("GITHUB_STEP_SUMMARY", "")).strip()
    if not path_text:
        return
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(_summary_lines(result)) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate real Note draft artifact files.")
    parser.add_argument("--artifact-dir", default="outputs")
    parser.add_argument("--summary-path", default=None)
    args = parser.parse_args()

    result = validate_artifact(args.artifact_dir)
    write_summary(result, args.summary_path)
    print("\n".join(_summary_lines(result)))
    return 0 if result.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
