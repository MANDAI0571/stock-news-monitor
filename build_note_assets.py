from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import note_draft


PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "outputs"
MANIFEST_NAME = "note_cloud_artifact_manifest.json"

REQUIRED_FILES = [
    "note_daily.md",
    "note_title.txt",
    "note_daily.html",
    "note_drafts_manifest.json",
]

ARTIFACT_FILES = [
    "note_daily.md",
    "note_title.txt",
    "note_daily.html",
    "note_chatgpt.md",
    "note_claude.md",
    "note_pullback.md",
    "note_highs.md",
    "note_chatgpt.html",
    "note_claude.html",
    "note_pullback.html",
    "note_highs.html",
    "note_chatgpt_title.txt",
    "note_claude_title.txt",
    "note_pullback_title.txt",
    "note_highs_title.txt",
    "note_drafts_manifest.json",
    "market_snapshot.json",
]


def _file_entry(path: Path) -> dict[str, object]:
    return {
        "path": str(path.relative_to(PROJECT_ROOT)),
        "size_bytes": path.stat().st_size,
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
    }


def build_note_assets(output_dir: str | Path = OUTPUT_DIR) -> Path:
    """既存のNote下書き生成を実行し、Artifact用manifestを作る。

    スクリーニングやBUY判定ロジックはここでは変更せず、note_draft.py の出力を集約する。
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    note_draft.main()

    missing = [name for name in REQUIRED_FILES if not (output_dir / name).exists()]
    if missing:
        raise FileNotFoundError(f"note draft output missing: {', '.join(missing)}")

    files = [
        _file_entry(path)
        for name in ARTIFACT_FILES
        for path in [output_dir / name]
        if path.exists() and path.stat().st_size > 0
    ]
    if not files:
        raise RuntimeError("note draft artifact files are empty")

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifact": "note-draft-cloud",
        "files": files,
    }
    manifest_path = output_dir / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"note_artifact_manifest={manifest_path}")
    print(f"note_artifact_files={len(files)}")
    for entry in files:
        print(f"note_asset={entry['path']} size={entry['size_bytes']}")
    return manifest_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Note draft files for GitHub Actions artifact upload.")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    args = parser.parse_args()
    build_note_assets(args.output_dir)


if __name__ == "__main__":
    main()
