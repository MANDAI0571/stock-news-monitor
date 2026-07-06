from __future__ import annotations

import argparse
import csv
import shutil
import struct
import json
import zlib
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
    "note_body.md",
    "note_preview.html",
    "eyecatch.png",
    "market_status.png",
    "funnel.png",
    "note_drafts_manifest.json",
]

ARTIFACT_FILES = [
    "note_daily.md",
    "note_body.md",
    "note_title.txt",
    "note_daily.html",
    "note_preview.html",
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
    "eyecatch.png",
    "market_status.png",
    "funnel.png",
]


FONT_5X7 = {
    " ": ["00000", "00000", "00000", "00000", "00000", "00000", "00000"],
    "-": ["00000", "00000", "00000", "11110", "00000", "00000", "00000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    "/": ["00001", "00010", "00100", "01000", "10000", "00000", "00000"],
    ":": ["00000", "01100", "01100", "00000", "01100", "01100", "00000"],
    "(": ["00010", "00100", "01000", "01000", "01000", "00100", "00010"],
    ")": ["01000", "00100", "00010", "00010", "00010", "00100", "01000"],
    "+": ["00000", "00100", "00100", "11111", "00100", "00100", "00000"],
    "%": ["11001", "11010", "00100", "01000", "10110", "00110", "00000"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01111", "10000", "10000", "10011", "10001", "10001", "01111"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "J": ["00111", "00010", "00010", "00010", "00010", "10010", "01100"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "10101", "01010"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
}


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _count(rows: list[dict[str, str]], column: str, value: str) -> int:
    return sum(1 for row in rows if str(row.get(column, "")).upper() == value)


def _copy_required_aliases(output_dir: Path) -> None:
    aliases = {
        "note_daily.md": "note_body.md",
        "note_daily.html": "note_preview.html",
    }
    for src_name, dst_name in aliases.items():
        src = output_dir / src_name
        if not src.exists() or src.stat().st_size == 0:
            raise FileNotFoundError(f"note draft source missing: {src_name}")
        shutil.copyfile(src, output_dir / dst_name)
        print(f"note_alias={dst_name} source={src_name}")


def _canvas(width: int, height: int, color: tuple[int, int, int]) -> bytearray:
    r, g, b = color
    return bytearray([r, g, b] * width * height)


def _rect(img: bytearray, width: int, height: int, x: int, y: int, w: int, h: int, color: tuple[int, int, int]) -> None:
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(width, x + w)
    y1 = min(height, y + h)
    r, g, b = color
    for yy in range(y0, y1):
        start = (yy * width + x0) * 3
        for xx in range(x0, x1):
            idx = start + (xx - x0) * 3
            img[idx:idx + 3] = bytes((r, g, b))


def _text(img: bytearray, width: int, height: int, x: int, y: int, text: str, color: tuple[int, int, int], scale: int = 4) -> None:
    cx = x
    for ch in text.upper():
        pattern = FONT_5X7.get(ch, FONT_5X7[" "])
        for row_i, row in enumerate(pattern):
            for col_i, bit in enumerate(row):
                if bit == "1":
                    _rect(img, width, height, cx + col_i * scale, y + row_i * scale, scale, scale, color)
        cx += 6 * scale


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    import binascii

    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)


def _save_png(path: Path, width: int, height: int, img: bytearray) -> Path:
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)
        raw.extend(img[y * stride:(y + 1) * stride])
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), level=6))
        + _png_chunk(b"IEND", b"")
    )
    path.write_bytes(data)
    if path.stat().st_size < 1000:
        raise RuntimeError(f"png too small: {path}")
    print(f"note_image={path} size={path.stat().st_size}")
    return path


def _load_market(output_dir: Path) -> dict[str, str]:
    path = output_dir / "market_snapshot.json"
    if not path.exists():
        return {"regime": "UNKNOWN", "source": "missing", "note": ""}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"regime": "UNKNOWN", "source": "invalid", "note": ""}


def _latest_rows(output_dir: Path, fixed_name: str, pattern: str) -> list[dict[str, str]]:
    fixed = output_dir / fixed_name
    if fixed.exists():
        return _read_rows(fixed)
    paths = sorted(output_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return _read_rows(paths[0]) if paths else []


def _draw_header(img: bytearray, width: int, height: int, title: str, subtitle: str) -> None:
    _rect(img, width, height, 0, 0, width, 96, (22, 28, 36))
    _rect(img, width, height, 0, 96, width, 8, (31, 116, 95))
    _text(img, width, height, 52, 30, title, (255, 255, 255), 5)
    _text(img, width, height, 54, 114, subtitle, (68, 77, 88), 3)


def _bar(img: bytearray, width: int, height: int, x: int, y: int, w: int, h: int, value: int, max_value: int, color: tuple[int, int, int], label: str) -> None:
    _text(img, width, height, x, y - 34, label, (42, 48, 56), 3)
    _rect(img, width, height, x, y, w, h, (224, 229, 235))
    fill = int(w * (value / max(max_value, 1)))
    _rect(img, width, height, x, y, fill, h, color)
    _text(img, width, height, x + w + 28, y + 6, str(value), (42, 48, 56), 4)


def _build_cloud_images(output_dir: Path) -> list[Path]:
    screening = _latest_rows(output_dir, "screening_result.csv", "screening_result_*.csv")
    decisions = _latest_rows(output_dir, "decision_result.csv", "decision_result.csv")
    market = _load_market(output_dir)
    regime = str(market.get("regime", "UNKNOWN")).upper()
    buy_count = _count(decisions, "decision", "BUY")
    watch_count = _count(decisions, "decision", "WATCH")
    skip_count = _count(decisions, "decision", "SKIP")
    candidate_count = sum(1 for row in screening if str(row.get("rank", "")).upper() in {"S", "A", "B"})
    generated = datetime.now().strftime("%Y-%m-%d")
    images: list[Path] = []

    width, height = 1200, 630
    img = _canvas(width, height, (247, 249, 252))
    _draw_header(img, width, height, "JP SCREENING", "NOTE DRAFT CLOUD ARTIFACT")
    _text(img, width, height, 70, 210, f"RUN DATE {generated}", (34, 40, 49), 5)
    _text(img, width, height, 70, 300, f"BUY {buy_count}  WATCH {watch_count}  SKIP {skip_count}", (31, 116, 95), 5)
    _text(img, width, height, 70, 390, f"CANDIDATES {candidate_count}  SCREENED {len(screening)}", (72, 84, 97), 4)
    _rect(img, width, height, 70, 500, 1060, 8, (31, 116, 95))
    images.append(_save_png(output_dir / "eyecatch.png", width, height, img))

    color = {"NORMAL": (31, 116, 95), "CAUTION": (201, 139, 33), "RISK": (188, 78, 42), "STOP": (166, 46, 46)}.get(regime, (86, 99, 115))
    img = _canvas(width, height, (250, 251, 253))
    _draw_header(img, width, height, "MARKET STATUS", "REGIME SNAPSHOT")
    _rect(img, width, height, 80, 220, 1040, 220, color)
    _text(img, width, height, 140, 292, regime, (255, 255, 255), 12)
    _text(img, width, height, 90, 500, "SOURCE MARKET REGIME", (72, 84, 97), 4)
    images.append(_save_png(output_dir / "market_status.png", width, height, img))

    img = _canvas(width, height, (247, 249, 252))
    _draw_header(img, width, height, "SCREENING FUNNEL", "SCREENED TO NOTE DRAFT")
    max_value = max(len(screening), candidate_count, buy_count, watch_count, 1)
    _bar(img, width, height, 120, 210, 760, 54, len(screening), max_value, (72, 84, 97), "SCREENED")
    _bar(img, width, height, 120, 330, 760, 54, candidate_count, max_value, (31, 116, 95), "S/A/B")
    _bar(img, width, height, 120, 450, 760, 54, buy_count, max_value, (48, 105, 152), "BUY")
    images.append(_save_png(output_dir / "funnel.png", width, height, img))

    buy_rows = [row for row in decisions if str(row.get("decision", "")).upper() == "BUY"]
    if not buy_rows:
        img = _canvas(width, height, (250, 251, 253))
        _draw_header(img, width, height, "BUY CANDIDATE", "CASH MODE")
        _text(img, width, height, 110, 250, "NO BUY TODAY", (166, 46, 46), 8)
        _text(img, width, height, 112, 380, f"REGIME {regime}", (72, 84, 97), 5)
        images.append(_save_png(output_dir / "buy_cash.png", width, height, img))
    else:
        for idx, row in enumerate(buy_rows[:3], start=1):
            code = "".join(ch for ch in str(row.get("code", f"{idx}")) if ch.isalnum()) or str(idx)
            img = _canvas(width, height, (250, 251, 253))
            _draw_header(img, width, height, "BUY CANDIDATE", f"SLOT {idx}")
            _text(img, width, height, 90, 210, f"CODE {code}", (34, 40, 49), 7)
            _text(img, width, height, 90, 310, f"RANK {row.get('rank', '')}  SCORE {row.get('score', '')}", (31, 116, 95), 5)
            _text(img, width, height, 90, 390, f"CONF {row.get('confidence', '')}  PRICE {row.get('current_price', '')}", (72, 84, 97), 4)
            images.append(_save_png(output_dir / f"buy_{code}.png", width, height, img))
    return images


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
    _copy_required_aliases(output_dir)
    image_paths = _build_cloud_images(output_dir)

    missing = [name for name in REQUIRED_FILES if not (output_dir / name).exists()]
    if missing:
        raise FileNotFoundError(f"note draft output missing: {', '.join(missing)}")

    artifact_names = [*ARTIFACT_FILES, *[p.name for p in sorted(output_dir.glob("buy_*.png"))]]
    files = [
        _file_entry(path)
        for name in artifact_names
        for path in [output_dir / name]
        if path.exists() and path.stat().st_size > 0
    ]
    for path in image_paths:
        if path.name not in {entry["path"].split("/")[-1] for entry in files}:
            files.append(_file_entry(path))
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
