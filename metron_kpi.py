from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


EXPECTED_NOTE_KEYS = ("chatgpt", "claude", "pullback", "highs")


def _now_jst() -> datetime:
    return datetime.now(ZoneInfo("Asia/Tokyo"))


def _safe_text(value: Any, missing: str = "取得できず") -> str:
    if value is None:
        return missing
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return missing
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "inf", "-inf"}:
        return missing
    return text


def _safe_int(value: Any, missing: str = "取得できず") -> str:
    try:
        if value is None or (isinstance(value, float) and not math.isfinite(value)):
            return missing
        return f"{int(float(value)):,}"
    except Exception:
        return missing


def _safe_pct(value: Any, missing: str = "取得できず") -> str:
    try:
        if value is None or (isinstance(value, float) and not math.isfinite(value)):
            return missing
        return f"{float(value):+.2f}%"
    except Exception:
        return missing


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            return [dict(row) for row in csv.DictReader(fh)]
    except Exception:
        return []


def _read_json(path: Path) -> Any:
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _latest_file(output_dir: Path, pattern: str) -> Path | None:
    files = [p for p in output_dir.glob(pattern) if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: (p.stat().st_mtime, p.name))


def _file_status(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"status": "missing", "path": "取得できず", "bytes": 0}
    return {"status": "ok", "path": str(path), "bytes": path.stat().st_size}


def _count_by(rows: list[dict[str, str]], key: str) -> dict[str, int]:
    counts = Counter(_safe_text(row.get(key), missing="未分類") for row in rows)
    return dict(sorted(counts.items()))


def _to_float(value: Any) -> float | None:
    try:
        num = float(value)
    except Exception:
        return None
    return num if math.isfinite(num) else None


def _top_candidates(rows: list[dict[str, str]], limit: int = 5) -> list[dict[str, str]]:
    def sort_key(row: dict[str, str]) -> tuple[float, float]:
        score = _to_float(row.get("score")) or 0.0
        dist = _to_float(row.get("dist_52w_high_pct"))
        return (score, -(dist if dist is not None else 999.0))

    ranked = sorted(rows, key=sort_key, reverse=True)
    top: list[dict[str, str]] = []
    for row in ranked[:limit]:
        top.append(
            {
                "code": _safe_text(row.get("code")),
                "name": _safe_text(row.get("name")),
                "rank": _safe_text(row.get("rank")),
                "score": _safe_int(row.get("score")),
                "dist_52w_high_pct": _safe_pct(row.get("dist_52w_high_pct")),
                "reason": _safe_text(row.get("reason") or row.get("skip_reason")),
            }
        )
    return top


def _note_summary(output_dir: Path) -> dict[str, Any]:
    manifest_path = output_dir / "note_drafts_manifest.json"
    manifest = _read_json(manifest_path)
    items = manifest if isinstance(manifest, list) else []
    by_key = {str(item.get("key")): item for item in items if isinstance(item, dict)}
    notes = []
    for key in EXPECTED_NOTE_KEYS:
        item = by_key.get(key, {})
        md_file = output_dir / str(item.get("md_file", ""))
        title_file = output_dir / str(item.get("title_file", ""))
        html_file = output_dir / str(item.get("html_file", ""))
        url_file_name = item.get("url_file")
        url_file = output_dir / str(url_file_name) if url_file_name else None
        notes.append(
            {
                "key": key,
                "title": _safe_text(item.get("title")),
                "md": md_file.exists(),
                "title_file": title_file.exists(),
                "html": html_file.exists(),
                "url": bool(url_file and url_file.exists()),
            }
        )
    missing = [note["key"] for note in notes if not (note["md"] and note["title_file"] and note["html"])]
    return {
        "expected": len(EXPECTED_NOTE_KEYS),
        "ready": len(EXPECTED_NOTE_KEYS) - len(missing),
        "missing": missing,
        "notes": notes,
        "source": _file_status(manifest_path),
    }


def build_kpi(output_dir: Path = Path("outputs"), data_dir: Path = Path("data"), now: datetime | None = None) -> dict[str, Any]:
    now = now or _now_jst()
    output_dir = Path(output_dir)
    data_dir = Path(data_dir)

    screening_path = output_dir / "screening_result.csv"
    decision_path = output_dir / "decision_result.csv"
    discipline_path = output_dir / "discipline_result.csv"
    warren_path = output_dir / "warren_summary.json"
    performance_path = output_dir / "performance_report.md"
    highs_record_path = data_dir / "highs_track_record.csv"
    latest_intraday_path = _latest_file(output_dir, "intraday_high_alerts_*.csv")

    screening_rows = _read_csv_rows(screening_path)
    decision_rows = _read_csv_rows(decision_path)
    discipline_rows = _read_csv_rows(discipline_path)
    warren = _read_json(warren_path)
    warren = warren if isinstance(warren, dict) else {}
    highs_record_rows = _read_csv_rows(highs_record_path)
    intraday_rows = _read_csv_rows(latest_intraday_path) if latest_intraday_path else []
    note = _note_summary(output_dir)

    data_health = {
        "screening_result": _file_status(screening_path),
        "decision_result": _file_status(decision_path),
        "discipline_result": _file_status(discipline_path),
        "warren_summary": _file_status(warren_path),
        "note_drafts_manifest": note["source"],
        "performance_report": _file_status(performance_path),
        "highs_track_record": _file_status(highs_record_path),
        "latest_intraday_alerts": _file_status(latest_intraday_path),
    }

    alerts: list[str] = []
    if not screening_rows:
        alerts.append("スクリーニング結果を取得できず")
    if note["ready"] < note["expected"]:
        alerts.append(f"note4本のうち不足: {', '.join(note['missing'])}")
    if not warren:
        alerts.append("ウォーレン要約を取得できず")
    if not highs_record_rows:
        alerts.append("実績トラックレコードを取得できず")

    decision_counts = _count_by(decision_rows, "decision")
    rank_counts = _count_by(screening_rows, "rank")
    action_counts = _count_by(discipline_rows, "action")
    overall = "OK" if not alerts else "CAUTION"

    latest_signal_date = "取得できず"
    if highs_record_rows:
        dates = sorted({_safe_text(row.get("date")) for row in highs_record_rows if _safe_text(row.get("date")) != "取得できず"})
        latest_signal_date = dates[-1] if dates else "取得できず"

    return {
        "employee": "メトロン",
        "role": "経営ダッシュボード管理官",
        "mission": "全KPIを毎日自動集計し役員に提示",
        "generated_at": now.isoformat(timespec="seconds"),
        "target_date": now.strftime("%Y-%m-%d"),
        "overall_status": overall,
        "alerts": alerts,
        "data_health": data_health,
        "research": {
            "screening_rows": len(screening_rows),
            "rank_counts": rank_counts,
            "s_rank_count": rank_counts.get("S", 0),
            "a_rank_count": rank_counts.get("A", 0),
            "top_candidates": _top_candidates(screening_rows),
        },
        "operations": {
            "capital": warren.get("capital"),
            "regime": _safe_text(warren.get("regime")),
            "buy_count": warren.get("buy_count"),
            "watch_count": warren.get("watch_count"),
            "skip_count": warren.get("skip_count"),
            "cash_count": warren.get("cash_count"),
            "selected_symbols": warren.get("selected_symbols") or [],
            "cash_reason": _safe_text(warren.get("cash_reason") or warren.get("risk_control_reason")),
            "discipline_actions": action_counts,
            "decision_counts": decision_counts,
        },
        "editorial": note,
        "track_record": {
            "records_total": len(highs_record_rows),
            "latest_signal_date": latest_signal_date,
            "performance_report_present": performance_path.exists(),
        },
        "intraday": {
            "latest_alert_file": str(latest_intraday_path) if latest_intraday_path else "取得できず",
            "latest_alert_rows": len(intraday_rows),
        },
        "source_files": {name: info["path"] for name, info in data_health.items()},
    }


def render_markdown(kpi: dict[str, Any]) -> str:
    research = kpi["research"]
    operations = kpi["operations"]
    editorial = kpi["editorial"]
    track_record = kpi["track_record"]
    intraday = kpi["intraday"]

    lines = [
        f"# メトロン日次KPIレポート（{_safe_text(kpi.get('target_date'))}）",
        "",
        f"- AI社員: **{_safe_text(kpi.get('employee'))} / {_safe_text(kpi.get('role'))}**",
        f"- 使命: {_safe_text(kpi.get('mission'))}",
        f"- 生成時刻: {_safe_text(kpi.get('generated_at'))}",
        f"- 総合判定: **{_safe_text(kpi.get('overall_status'))}**",
        "",
        "## 役員向けサマリー",
    ]
    alerts = kpi.get("alerts") or []
    if alerts:
        lines.extend([f"- 注意: {_safe_text(item)}" for item in alerts])
    else:
        lines.append("- 主要データは取得済み。通常運転。")
    if int(research.get("s_rank_count") or 0) == 0 and int(operations.get("buy_count") or 0) == 0:
        lines.append("- 本日はSランク/BUYが無いため、無理に攻めず品質確認と実績蓄積を優先。")

    lines.extend(
        [
            "",
            "## 今日のKPI",
            "",
            "| 領域 | KPI | 値 |",
            "|---|---|---|",
            f"| リサーチ | スクリーニング行数 | {_safe_int(research.get('screening_rows'))} |",
            f"| リサーチ | Sランク / Aランク | {_safe_int(research.get('s_rank_count'))} / {_safe_int(research.get('a_rank_count'))} |",
            f"| 運用 | 地合い | {_safe_text(operations.get('regime'))} |",
            f"| 運用 | BUY / WATCH / SKIP | {_safe_int(operations.get('buy_count'))} / {_safe_int(operations.get('watch_count'))} / {_safe_int(operations.get('skip_count'))} |",
            f"| 運用 | 現金枠 | {_safe_int(operations.get('cash_count'))} |",
            f"| 編集 | note準備済み | {_safe_int(editorial.get('ready'))} / {_safe_int(editorial.get('expected'))} |",
            f"| 実績 | 記録済みシグナル | {_safe_int(track_record.get('records_total'))} |",
            f"| 日中監視 | 直近日中アラート行数 | {_safe_int(intraday.get('latest_alert_rows'))} |",
            "",
            "## note4本チェック",
            "",
            "| 記事 | タイトル | MD | HTML | タイトルファイル | 下書きURL |",
            "|---|---|---|---|---|---|",
        ]
    )
    for note in editorial.get("notes") or []:
        lines.append(
            "| {key} | {title} | {md} | {html} | {title_file} | {url} |".format(
                key=_safe_text(note.get("key")),
                title=_safe_text(note.get("title")),
                md="OK" if note.get("md") else "取得できず",
                html="OK" if note.get("html") else "取得できず",
                title_file="OK" if note.get("title_file") else "取得できず",
                url="OK" if note.get("url") else "取得できず",
            )
        )

    lines.extend(
        [
            "",
            "## 候補上位",
            "",
            "| コード | 銘柄 | ランク | スコア | 52週高値距離 | 理由 |",
            "|---|---|---|---|---|---|",
        ]
    )
    candidates = research.get("top_candidates") or []
    if candidates:
        for item in candidates:
            lines.append(
                f"| {_safe_text(item.get('code'))} | {_safe_text(item.get('name'))} | {_safe_text(item.get('rank'))} | "
                f"{_safe_text(item.get('score'))} | {_safe_text(item.get('dist_52w_high_pct'))} | {_safe_text(item.get('reason'))} |"
            )
    else:
        lines.append("| 取得できず | 取得できず | 取得できず | 取得できず | 取得できず | 取得できず |")

    lines.extend(
        [
            "",
            "## データ取得状況",
            "",
            "| データ | 状態 | ファイル |",
            "|---|---|---|",
        ]
    )
    for name, info in kpi.get("data_health", {}).items():
        status = "OK" if info.get("status") == "ok" else "取得できず"
        lines.append(f"| {_safe_text(name)} | {status} | `{_safe_text(info.get('path'))}` |")

    return "\n".join(lines).replace("nan", "取得できず").replace("None", "取得できず").replace("null", "取得できず")


def write_outputs(kpi: dict[str, Any], output_dir: Path = Path("outputs")) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "metron_kpi.json"
    md_path = output_dir / "metron_kpi_report.md"
    json_path.write_text(json.dumps(kpi, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    md_path.write_text(render_markdown(kpi), encoding="utf-8")
    return md_path, json_path


def main() -> None:
    parser = argparse.ArgumentParser(description="メトロン: 会社OSの日次KPIレポートを生成")
    parser.add_argument("--output-dir", default="outputs")
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()
    kpi = build_kpi(output_dir=Path(args.output_dir), data_dir=Path(args.data_dir))
    md_path, json_path = write_outputs(kpi, output_dir=Path(args.output_dir))
    print(f"metron: wrote {md_path} and {json_path}")
    print(f"metron: status={kpi['overall_status']} alerts={len(kpi['alerts'])}")


if __name__ == "__main__":
    main()
