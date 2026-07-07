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
    "note_cloud_artifact_manifest.json",
]

MARKET_INDICATORS = [
    ("nikkei", "日経平均"),
    ("topix", "TOPIX"),
    ("vix", "VIX"),
    ("sox", "SOX"),
    ("usdjpy", "ドル円"),
]


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
    market_status: dict[str, str] = field(default_factory=dict)
    preview_images: list[str] = field(default_factory=list)

    def fail(self, item: str) -> None:
        self.valid = False
        self.missing_items.append(item)


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


def validate_artifact(artifact_dir: str | Path) -> ArtifactValidation:
    result = ArtifactValidation(Path(artifact_dir))
    _validate_files(result)

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

    return result


def _summary_lines(result: ArtifactValidation) -> list[str]:
    lines = [
        "## Note Artifact Quality Gate",
        "",
        f"- NOTE_ARTIFACT_VALID={'true' if result.valid else 'false'}",
        f"- Artifact dir: `{result.artifact_dir}`",
        f"- BUY/CASH判定: {result.buy_cash_judgement}",
        f"- WATCH件数: {result.watch_count if result.watch_count is not None else '未取得'}",
        f"- 欠けているファイル: {', '.join(result.missing_files) if result.missing_files else 'なし'}",
        f"- 欠けている項目: {', '.join(result.missing_items) if result.missing_items else 'なし'}",
        "",
        "### 市場指標の取得状況",
    ]
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
