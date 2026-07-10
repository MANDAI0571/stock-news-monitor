from __future__ import annotations

import argparse
import csv
from html import escape
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
    # 4本のNote下書き（52週高値 / 押し目MAタッチ / 300万ChatGPT / 300万Claude）は必須
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
    "note_body.md",
    "note_preview.html",
    "eyecatch.png",
    "market_status.png",
    "funnel.png",
    "watch.png",
    "warren_summary.json",
    "decision_result.csv",
    "discipline_result.csv",
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
    "warren_summary.json",
    "decision_result.csv",
    "decision_report.md",
    "discipline_result.csv",
    "paper_portfolio_decision.csv",
    "eyecatch.png",
    "market_status.png",
    "funnel.png",
    "watch.png",
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


def _first_text(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = str(row.get(name, "")).strip()
        if value and value.lower() not in {"nan", "none", "<na>"}:
            return value
    return ""


def _short(value: str, limit: int = 90) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _top_rows(rows: list[dict[str, str]], decision: str, limit: int = 5) -> list[dict[str, str]]:
    selected = [row for row in rows if str(row.get("decision", "")).upper() == decision]

    def key(row: dict[str, str]) -> tuple[float, float]:
        try:
            confidence = float(str(row.get("confidence", "0")).replace(",", ""))
        except ValueError:
            confidence = 0.0
        try:
            score = float(str(row.get("score", "0")).replace(",", ""))
        except ValueError:
            score = 0.0
        return (-confidence, -score)

    return sorted(selected, key=key)[:limit]


def _buy0_reasons(decisions: list[dict[str, str]], market: dict[str, str]) -> list[str]:
    regime = str(market.get("regime", "UNKNOWN")).upper()
    if regime in {"STOP", "RISK"}:
        return [f"地合いが{regime}のため、新規BUYを出さず現金待機にしています。"]
    skip_text = " / ".join(
        _first_text(row, "skip_reason")
        for row in decisions
        if str(row.get("decision", "")).upper() == "SKIP"
    )
    reasons: list[str] = []
    if "ランクがSでない" in skip_text:
        reasons.append("Sランク条件に届かない銘柄が多く、BUY条件を満たしませんでした。")
    if "52週高値から" in skip_text or "52週高値まで" in skip_text:
        reasons.append("52週高値からの距離が遠い銘柄があり、短期の勢い条件を満たしませんでした。")
    if "出来高" in skip_text:
        reasons.append("出来高の増加が弱い銘柄があり、買いの根拠を強められませんでした。")
    if "MA" in skip_text or "移動平均" in skip_text:
        reasons.append("移動平均線の上昇トレンド条件がそろわない銘柄がありました。")
    if "高すぎ" in skip_text or "資金20%" in skip_text:
        reasons.append("300万円運用の100株単位ルールで、1銘柄の金額が大きすぎる銘柄がありました。")
    if not reasons:
        reasons.append("Sランク・52週高値距離・出来高・移動平均・資金管理の条件が同時にそろう銘柄がありませんでした。")
    return reasons[:4]


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
        return {"regime": "UNKNOWN", "source": "missing", "note": "", "indicators": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"regime": "UNKNOWN", "source": "invalid", "note": "", "indicators": {}}


def _market_indicator_items(market: dict[str, object]) -> list[dict[str, object]]:
    indicators = market.get("indicators", {})
    if not isinstance(indicators, dict):
        indicators = {}
    order = [
        ("nikkei", "日経平均", "NIKKEI"),
        ("topix", "TOPIX", "TOPIX"),
        ("vix", "VIX", "VIX"),
        ("sox", "SOX", "SOX"),
        ("usdjpy", "ドル円", "USDJPY"),
    ]
    rows: list[dict[str, object]] = []
    for key, label, short_label in order:
        raw = indicators.get(key, {})
        item = raw if isinstance(raw, dict) else {}
        rows.append(
            {
                "key": key,
                "label": str(item.get("label") or label),
                "short_label": str(item.get("short_label") or short_label),
                "status": str(item.get("status") or "unavailable"),
                "display_value": str(item.get("display_value") or "未取得"),
                "display_change_pct": str(item.get("display_change_pct") or "未取得"),
                "as_of": str(item.get("as_of") or "未取得"),
                "source_note": str(item.get("source_note") or ""),
            }
        )
    return rows


def _market_body_lines(market: dict[str, object]) -> list[str]:
    lines: list[str] = []
    indicator_regime = str(market.get("indicator_regime") or "").upper()
    indicator_note = str(market.get("indicator_regime_note") or "").strip()
    if indicator_regime:
        note = f"（{indicator_note}）" if indicator_note else ""
        lines.append(f"- フクロウ補助判定: **{indicator_regime}**{note}")
    for item in _market_indicator_items(market):
        source_note = f" / {item['source_note']}" if item["source_note"] else ""
        lines.append(
            f"- {item['label']}: **{item['display_value']}**"
            f"（前日比 {item['display_change_pct']} / {item['as_of']}{source_note}）"
        )
    return lines


def _png_market_value(value: object) -> str:
    text = str(value or "").replace(",", "").replace("未取得", "N/A")
    return text if len(text) <= 12 else text[:12]


def _png_market_change(value: object) -> str:
    text = str(value or "").replace("未取得", "N/A")
    return text if len(text) <= 8 else text[:8]


def _latest_rows(output_dir: Path, fixed_name: str, pattern: str) -> list[dict[str, str]]:
    fixed = output_dir / fixed_name
    if fixed.exists():
        return _read_rows(fixed)
    paths = sorted(output_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return _read_rows(paths[0]) if paths else []


def _latest_path(output_dir: Path, fixed_name: str, pattern: str) -> Path | None:
    fixed = output_dir / fixed_name
    paths = [p for p in output_dir.glob(pattern) if p.exists() and p.stat().st_size > 0]
    if fixed.exists() and fixed.stat().st_size > 0:
        paths.append(fixed)
    paths = sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


def _copy_file(src: Path, dst: Path) -> Path:
    if src.resolve() == dst.resolve():
        return dst
    dst.write_bytes(src.read_bytes())
    return dst


def _ensure_warren_csvs(output_dir: Path) -> dict[str, str]:
    source_files: dict[str, str] = {}
    decision = output_dir / "decision_result.csv"
    if decision.exists() and decision.stat().st_size > 0:
        source_files["decision_result"] = str(decision.relative_to(PROJECT_ROOT))

    report = output_dir / "decision_report.md"
    if report.exists() and report.stat().st_size > 0:
        source_files["decision_report"] = str(report.relative_to(PROJECT_ROOT))

    discipline_src = _latest_path(output_dir, "discipline_result.csv", "discipline_portfolio_*.csv")
    if discipline_src and discipline_src.exists():
        discipline_fixed = _copy_file(discipline_src, output_dir / "discipline_result.csv")
        portfolio_fixed = _copy_file(discipline_fixed, output_dir / "paper_portfolio_decision.csv")
        source_files["discipline_result"] = str(discipline_fixed.relative_to(PROJECT_ROOT))
        source_files["paper_portfolio_decision"] = str(portfolio_fixed.relative_to(PROJECT_ROOT))

    screening = output_dir / "screening_result.csv"
    if screening.exists() and screening.stat().st_size > 0:
        source_files["screening_result"] = str(screening.relative_to(PROJECT_ROOT))

    market = output_dir / "market_snapshot.json"
    if market.exists() and market.stat().st_size > 0:
        source_files["market_snapshot"] = str(market.relative_to(PROJECT_ROOT))
    return source_files


def _safe_float(value: object) -> float | None:
    text = str(value or "").replace(",", "").replace("円", "").strip()
    if not text or text.lower() in {"nan", "none", "<na>"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _safe_int(value: object, default: int = 0) -> int:
    number = _safe_float(value)
    return int(number) if number is not None else default


def _format_yen(value: object) -> str:
    number = _safe_float(value)
    return "未取得" if number is None else f"{int(round(number)):,}円"


def _first_nonempty(rows: list[dict[str, str]], column: str) -> str:
    for row in rows:
        value = _first_text(row, column)
        if value:
            return value
    return ""


def _warren_selected_symbols(buy_rows: list[dict[str, str]]) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    for row in buy_rows[:3]:
        lot_value = _safe_int(_first_text(row, "lot_value_100"))
        position_size = _safe_int(_first_text(row, "position_size"), 100)
        if position_size <= 0:
            position_size = 100
        current_price = _safe_float(_first_text(row, "current_price"))
        position_value = int(round(current_price * position_size)) if current_price is not None else lot_value
        if position_value <= 0:
            position_value = lot_value
        selected.append(
            {
                "code": _first_text(row, "code"),
                "name": _first_text(row, "name"),
                "rank": _first_text(row, "rank"),
                "score": _first_text(row, "score"),
                "screen_type": _first_text(row, "screen_type"),
                "screen_tags": _first_text(row, "screen_tags"),
                "buy_reason": _first_text(row, "buy_reason", "entry_reason"),
                "shares": position_size,
                "lot_value_100": lot_value,
                "position_value": position_value,
                "allocation_pct": round(position_value / 3_000_000 * 100, 2) if position_value else 0.0,
            }
        )
    return selected


def _risk_control_reason(regime: str, cash_reason: str, buy_count: int) -> str:
    if regime in {"STOP", "RISK"}:
        return f"地合い{regime}のため新規BUYを止め、現金を優先します。"
    if regime == "CAUTION":
        return "地合いCAUTIONのためBUY枠を最大1銘柄に絞ります。"
    if buy_count == 0:
        return cash_reason or "300万円運用ルールで、条件がそろうまで現金待機します。"
    return "300万円・100株単位・最大3銘柄・1銘柄60万円目安で過大集中を避けます。"


def _build_warren_summary(output_dir: Path) -> dict[str, object]:
    source_files = _ensure_warren_csvs(output_dir)
    decisions = _latest_rows(output_dir, "decision_result.csv", "decision_result.csv")
    discipline = _latest_rows(output_dir, "discipline_result.csv", "discipline_portfolio_*.csv")
    market = _load_market(output_dir)
    regime = str(market.get("regime", "UNKNOWN")).upper()
    buy_rows = _top_rows(decisions, "BUY", 3)
    watch_rows = _top_rows(decisions, "WATCH", 5)
    buy_count = _count(decisions, "decision", "BUY")
    watch_count = _count(decisions, "decision", "WATCH")
    skip_count = _count(decisions, "decision", "SKIP")
    cash_count = sum(1 for row in discipline if str(row.get("action", "")).upper() == "CASH")
    if not discipline:
        cash_count = max(0, 3 - min(buy_count, 3))

    discipline_cash_reason = _first_nonempty(
        [row for row in discipline if str(row.get("action", "")).upper() == "CASH"],
        "cash_reason",
    )
    cash_reasons = _buy0_reasons(decisions, market) if buy_count == 0 else []
    cash_reason = discipline_cash_reason or " / ".join(cash_reasons)
    if buy_count == 0 and not cash_reason:
        cash_reason = "BUY条件を満たす銘柄がないため、無理に買わず現金待機します。"

    selected_symbols = _warren_selected_symbols(buy_rows)
    summary: dict[str, object] = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "capital": 3_000_000,
        "regime": regime,
        "buy_count": buy_count,
        "watch_count": watch_count,
        "skip_count": skip_count,
        "cash_count": cash_count,
        "selected_symbols": selected_symbols,
        "cash_reason": cash_reason,
        "risk_control_reason": _risk_control_reason(regime, cash_reason, buy_count),
        "source_files": source_files,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "watch_symbols": [
            {
                "code": _first_text(row, "code"),
                "name": _first_text(row, "name"),
                "rank": _first_text(row, "rank"),
                "score": _first_text(row, "score"),
                "screen_type": _first_text(row, "screen_type"),
                "skip_reason": _first_text(row, "skip_reason", "entry_reason"),
            }
            for row in watch_rows
        ],
    }
    path = output_dir / "warren_summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"warren_summary={path}")
    print(
        f"warren_decision buy={buy_count} watch={watch_count} skip={skip_count} "
        f"cash={cash_count} regime={regime}"
    )
    return summary


def _load_warren_summary(output_dir: Path) -> dict[str, object]:
    path = output_dir / "warren_summary.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _warren_note_lines(summary: dict[str, object]) -> list[str]:
    if not summary:
        return [
            "## 本日の300万円運用判断",
            "",
            "- ウォーレン判断: 未取得",
            "- 300万円運用判断ファイルが見つからないため、買い判断は保留します。",
            "",
        ]
    buy_count = _safe_int(summary.get("buy_count"))
    watch_count = _safe_int(summary.get("watch_count"))
    skip_count = _safe_int(summary.get("skip_count"))
    cash_count = _safe_int(summary.get("cash_count"))
    selected = summary.get("selected_symbols")
    selected_symbols = selected if isinstance(selected, list) else []
    decision_label = "BUY" if buy_count > 0 else "CASH"
    lines = [
        "## 本日の300万円運用判断",
        "",
        f"- 資金: **{_format_yen(summary.get('capital'))}**",
        f"- ウォーレン判断: **{decision_label}**",
        f"- 地合い: **{summary.get('regime', 'UNKNOWN')}**",
        f"- BUY件数: {buy_count}件",
        f"- WATCH件数: {watch_count}件",
        f"- SKIP件数: {skip_count}件",
        f"- CASH枠: {cash_count}枠",
        f"- 地合いによる制御理由: {summary.get('risk_control_reason', '未取得')}",
    ]
    cash_reason = str(summary.get("cash_reason") or "").strip()
    if cash_reason:
        lines.append(f"- CASH理由: {cash_reason}")
    lines.append("")
    if selected_symbols:
        lines.append("### BUY採用候補")
        lines.append("")
        for item in selected_symbols:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- {item.get('code', '')} {item.get('name', '')}: "
                f"rank {item.get('rank', '')} / score {item.get('score', '')} / "
                f"screen_type {item.get('screen_type', '')} / tags {item.get('screen_tags', '')}"
            )
            lines.append(f"  採用理由: {_short(str(item.get('buy_reason', '') or '未取得'))}")
            lines.append(
                f"  100株想定: {_format_yen(item.get('lot_value_100'))} / "
                f"想定株数: {item.get('shares', 100)}株 / "
                f"300万円内配分: {item.get('allocation_pct', 0)}%"
            )
    else:
        lines.extend(
            [
                "### 今日は無理に買わない",
                "",
                "BUY条件を満たす銘柄がないため、現金を守ります。",
                "WATCH候補があれば、出来高・地合い・高値距離の改善を待ちます。",
            ]
        )
    lines.append("")
    return lines


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
    _rect(img, width, height, 80, 185, 1040, 150, color)
    _text(img, width, height, 125, 235, f"REGIME {regime}", (255, 255, 255), 8)
    indicator_regime = str(market.get("indicator_regime", "UNKNOWN")).upper()
    _text(img, width, height, 95, 365, f"FUKUROU {indicator_regime}", (72, 84, 97), 4)
    for idx, item in enumerate(_market_indicator_items(market)):
        label = str(item["short_label"])
        value = _png_market_value(item["display_value"])
        change = _png_market_change(item["display_change_pct"])
        _text(img, width, height, 95, 430 + idx * 34, f"{label} {value} {change}", (42, 48, 56), 3)
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
    watch_rows = _top_rows(decisions, "WATCH", 3)
    img = _canvas(width, height, (250, 251, 253))
    _draw_header(img, width, height, "WATCH LIST", "NEXT CANDIDATES")
    _text(img, width, height, 90, 190, f"WATCH {watch_count}", (48, 105, 152), 8)
    if watch_rows:
        for idx, row in enumerate(watch_rows, start=1):
            code = "".join(ch for ch in str(row.get("code", "")) if ch.isalnum()) or "-"
            rank = _first_text(row, "rank") or "-"
            conf = _first_text(row, "confidence") or "-"
            _text(img, width, height, 95, 270 + (idx - 1) * 72, f"{idx} CODE {code} RANK {rank} CONF {conf}", (42, 48, 56), 4)
    else:
        _text(img, width, height, 95, 300, "NO WATCH TODAY", (72, 84, 97), 6)
    images.append(_save_png(output_dir / "watch.png", width, height, img))
    return images


def _image_md(filename: str, label: str) -> str:
    return f"![{label}]({filename})"


def _build_note_body(output_dir: Path) -> str:
    screening = _latest_rows(output_dir, "screening_result.csv", "screening_result_*.csv")
    decisions = _latest_rows(output_dir, "decision_result.csv", "decision_result.csv")
    market = _load_market(output_dir)
    warren_summary = _load_warren_summary(output_dir)
    regime = str(market.get("regime", "UNKNOWN")).upper()
    today = datetime.now().strftime("%Y-%m-%d")
    buy_rows = _top_rows(decisions, "BUY", 3)
    watch_rows = _top_rows(decisions, "WATCH", 5)
    buy_count = _count(decisions, "decision", "BUY")
    watch_count = _count(decisions, "decision", "WATCH")
    skip_count = _count(decisions, "decision", "SKIP")
    candidate_count = sum(1 for row in screening if str(row.get("rank", "")).upper() in {"S", "A", "B"})
    buy_images = sorted(p.name for p in output_dir.glob("buy_*.png"))

    lines: list[str] = [
        f"# 本日の日本株短期売買メモ {today}",
        "",
        _image_md("eyecatch.png", "アイキャッチ"),
        "",
        "## 市場状況",
        "",
        _image_md("market_status.png", "市場状況"),
        "",
        f"- 地合い: **{regime}**",
        *_market_body_lines(market),
        f"- 対象行数: {len(screening)}件",
        f"- 判定: BUY {buy_count}件 / WATCH {watch_count}件 / SKIP {skip_count}件",
        "",
        "## ファネル図",
        "",
        _image_md("funnel.png", "ファネル図"),
        "",
        f"- S/A/B候補: {candidate_count}件",
        "",
        *_warren_note_lines(warren_summary),
    ]

    if buy_rows:
        lines.extend(["## BUYカード", ""])
        for image in buy_images:
            lines.extend([_image_md(image, "BUYカード"), ""])
        for row in buy_rows:
            code = _first_text(row, "code")
            name = _first_text(row, "name")
            reason = _first_text(row, "entry_reason", "buy_reason")
            lines.append(f"- {code} {name}: {_short(reason)}")
    else:
        lines.extend([
            "## BUYカード または CASHカード",
            "",
            _image_md("buy_cash.png", "CASHカード"),
            "",
            "本日はBUY候補を出さず、現金待機とします。",
            "",
            "### なぜBUY0件なのか",
            "",
        ])
        for reason in _buy0_reasons(decisions, market):
            lines.append(f"- {reason}")
    lines.append("")

    lines.extend(["## WATCHカード", "", _image_md("watch.png", "WATCHカード"), ""])
    if watch_rows:
        lines.append("次に監視したい候補です。BUY条件には届いていないため、出来高・地合い・高値距離の改善待ちです。")
        lines.append("")
        for row in watch_rows:
            code = _first_text(row, "code")
            name = _first_text(row, "name")
            reason = _first_text(row, "skip_reason", "entry_reason")
            lines.append(f"- {code} {name}: {_short(reason)}")
    else:
        lines.append("本日はWATCH候補もありません。無理に候補を作らず、次のスクリーニングを待ちます。")
    lines.append("")

    lines.extend([
        "## まとめ",
        "",
        f"- 地合いは{regime}です。",
        f"- BUYは{buy_count}件、WATCHは{watch_count}件です。",
        "- 条件がそろわない日は現金を守ることも、300万円運用のルールの一部です。",
        "",
        "## 免責文",
        "",
        "この下書きは投資助言ではありません。売買判断は、最新の株価、出来高、決算予定、地合いを確認したうえで自己責任で行ってください。",
        "",
    ])
    return "\n".join(lines)


def _render_preview_html(markdown: str) -> str:
    body: list[str] = []
    in_list = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if in_list and not line.startswith("- "):
            body.append("</ul>")
            in_list = False
        if not line:
            continue
        if line.startswith("# "):
            body.append(f"<h1>{escape(line[2:])}</h1>")
        elif line.startswith("## "):
            body.append(f"<h2>{escape(line[3:])}</h2>")
        elif line.startswith("### "):
            body.append(f"<h3>{escape(line[4:])}</h3>")
        elif line.startswith("![") and "](" in line and line.endswith(")"):
            alt = line[2:].split("](", 1)[0]
            src = line.split("](", 1)[1][:-1]
            body.append(f'<figure><img src="{escape(src)}" alt="{escape(alt)}"><figcaption>{escape(alt)}</figcaption></figure>')
        elif line.startswith("- "):
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append(f"<li>{escape(line[2:])}</li>")
        else:
            body.append(f"<p>{escape(line)}</p>")
    if in_list:
        body.append("</ul>")
    return """<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Note下書きプレビュー</title>
  <style>
    body { margin: 0; background: #f4f6f8; color: #202833; font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Yu Gothic", sans-serif; line-height: 1.75; }
    main { max-width: 860px; margin: 0 auto; padding: 22px 16px 56px; background: #fff; }
    h1 { font-size: 26px; line-height: 1.35; margin: 10px 0 18px; }
    h2 { font-size: 21px; border-left: 5px solid #1f745f; padding-left: 10px; margin-top: 34px; }
    h3 { font-size: 17px; margin-top: 20px; }
    p, li { font-size: 15px; }
    figure { margin: 18px 0; }
    img { display: block; width: 100%; height: auto; border: 1px solid #d9e0e7; }
    figcaption { font-size: 12px; color: #697586; margin-top: 6px; }
    ul { padding-left: 22px; }
  </style>
</head>
<body>
<main>
""" + "\n".join(body) + """
</main>
</body>
</html>
"""


def _write_cloud_article(output_dir: Path) -> None:
    note_body = _build_note_body(output_dir)
    body_path = output_dir / "note_body.md"
    preview_path = output_dir / "note_preview.html"
    body_path.write_text(note_body, encoding="utf-8")
    preview_path.write_text(_render_preview_html(note_body), encoding="utf-8")
    print(f"note_body={body_path}")
    print(f"note_preview={preview_path}")


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
    image_paths = _build_cloud_images(output_dir)
    _build_warren_summary(output_dir)
    _write_cloud_article(output_dir)

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
    # 編集長: X告知文の自動生成（best-effort。失敗しても下書き生成は止めない）
    try:
        from sns_promo import build_sns_posts

        build_sns_posts(Path(args.output_dir))
    except Exception as exc:  # noqa: BLE001
        print(f"sns_promo_failed={exc}")


if __name__ == "__main__":
    main()
