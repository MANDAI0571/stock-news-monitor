from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from gmail_notify import load_gmail_config, send_gmail
from jptime import jst_today

DETECTED_RE = re.compile(
    r"intraday_alerts_detected=(\d+) new=(\d+)(?: date=(\d{4}-\d{2}-\d{2}))?"
)
SCAN_RE = re.compile(r"(?:scan_count=|\[SESSION\] scan #)(\d+)")
FAIL_RE = re.compile(r"\[SESSION\] scan #\d+ failed")


def _detections_for_day(text: str, day: str) -> list[re.Match[str]]:
    """当日分の検出行だけを返す。

    T-K修正(2026-08-03): セッションログは前の実行のアーティファクトから復元されるため、
    前日の検出行が混ざりうる。実際 7/28と7/29、7/30と7/31 のサマリーメールは
    検出件数・新規件数が完全に一致していた（スキャン回数だけが違う）。
    日付つきの行が1つでもあれば、当日の日付と一致する行だけを数える。
    日付なしの古い行しか無い場合は従来どおり全部数える（後方互換）。
    """
    matches = list(DETECTED_RE.finditer(text))
    if any(m.group(3) for m in matches):
        return [m for m in matches if m.group(3) == day]
    return matches


def summarize_log(path: Path) -> dict[str, int | str]:
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    day = jst_today().isoformat()
    matches = _detections_for_day(text, day)
    detected = sum(int(m.group(1)) for m in matches)
    new = sum(int(m.group(2)) for m in matches)
    failures = len(FAIL_RE.findall(text))
    scan_matches = [int(value) for value in SCAN_RE.findall(text)]
    scan_count = max(scan_matches) if scan_matches else len(matches)
    return {
        "date": day,
        "state": "normal" if scan_count > 0 else "abnormal",
        "scan_count": scan_count,
        "detected_count": detected,
        "new_count": new,
        "failure_count": failures,
    }


def body_from_summary(summary: dict[str, int | str]) -> str:
    state = str(summary["state"])
    scan_count = int(summary["scan_count"])
    detected_count = int(summary["detected_count"])
    new_count = int(summary["new_count"])
    failure_count = int(summary["failure_count"])
    status = "正常（監視を実行）" if state == "normal" else "異常（監視0回）"
    lines = [
        "DUKEクラウド ザラ場監視サマリー",
        f"対象日: {summary['date']}",
        f"状態: {status}",
        f"スキャン回数: {scan_count}",
        f"検出件数（重複含む）: {detected_count}",
        f"新規アラート: {new_count}",
        f"失敗回数: {failure_count}",
    ]
    if state == "normal" and new_count == 0:
        lines.append("新規アラート0件ですが、スキャンは実行済みです。")
    elif state != "normal":
        lines.append("スキャンが0回のため、該当なしとは判定しません。")
    else:
        lines.append("新規アラートがありました。詳細は各アラートメールを確認してください。")
    return "\n".join(lines)


def html_from_summary(summary: dict[str, int | str]) -> str:
    """サマリーの色つきHTML版。

    T-P(2026-08-18): 高重さんの指示「今のままで色を色々変えて表示して」。
    数字も文言も今までと同じ。見た目だけ色を付けて一目で分かるようにする。
    """
    state = str(summary["state"])
    scan_count = int(summary["scan_count"])
    detected_count = int(summary["detected_count"])
    new_count = int(summary["new_count"])
    failure_count = int(summary["failure_count"])
    normal = state == "normal"
    state_color = "#1f745f" if normal else "#c0392b"
    status = "\u6B63\u5E38\uFF08\u76E3\u8996\u3092\u5B9F\u884C\uFF09" if normal else "\u7570\u5E38\uFF08\u76E3\u8996 0 \u56DE\uFF09"
    fail_color = "#c0392b" if failure_count else "#6b7280"
    new_color = "#1d4ed8" if new_count else "#6b7280"

    def card(label: str, value: str, color: str) -> str:
        return (
            '<td style="padding:10px 8px;text-align:center;background:#f6f7f8;'
            'border-radius:8px;">'
            f'<div style="font-size:12px;color:#6b7280;">{label}</div>'
            f'<div style="font-size:24px;font-weight:700;color:{color};">{value}</div>'
            "</td>"
        )

    if normal and new_count == 0:
        note = "\u65B0\u898F\u30A2\u30E9\u30FC\u30C8 0 \u4EF6\u3067\u3059\u304C\u3001\u30B9\u30AD\u30E3\u30F3\u306F\u5B9F\u884C\u6E08\u307F\u3067\u3059\u3002"
    elif not normal:
        note = "\u30B9\u30AD\u30E3\u30F3\u304C 0 \u56DE\u306E\u305F\u3081\u3001\u8A72\u5F53\u306A\u3057\u3068\u306F\u5224\u5B9A\u3057\u307E\u305B\u3093\u3002"
    else:
        note = "\u65B0\u898F\u30A2\u30E9\u30FC\u30C8\u304C\u3042\u308A\u307E\u3057\u305F\u3002\u8A73\u7D30\u306F\u5404\u30A2\u30E9\u30FC\u30C8\u30E1\u30FC\u30EB\u3092\u78BA\u8A8D\u3057\u3066\u304F\u3060\u3055\u3044\u3002"

    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "</head><body style=\"margin:0;padding:16px;background:#ffffff;color:#111111;"
        "font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans','Yu Gothic',sans-serif;\">"
        f'<div style="background:{state_color};color:#ffffff;padding:14px 16px;'
        'border-radius:10px;">'
        '<div style="font-size:13px;opacity:.85;">DUKE\u30AF\u30E9\u30A6\u30C9 \u30B6\u30E9\u5834\u76E3\u8996\u30B5\u30DE\u30EA\u30FC</div>'
        f'<div style="font-size:20px;font-weight:700;">{summary["date"]}\u3000{status}</div>'
        "</div>"
        '<table style="width:100%;border-collapse:separate;border-spacing:6px;'
        'margin-top:12px;"><tr>'
        + card("\u30B9\u30AD\u30E3\u30F3\u56DE\u6570", f"{scan_count:,}", "#111827")
        + card("\u691C\u51FA\uFF08\u91CD\u8907\u542B\u3080\uFF09", f"{detected_count:,}", "#374151")
        + "</tr><tr>"
        + card("\u65B0\u898F\u30A2\u30E9\u30FC\u30C8", f"{new_count:,}", new_color)
        + card("\u5931\u6557\u56DE\u6570", f"{failure_count:,}", fail_color)
        + "</tr></table>"
        f'<p style="margin:14px 0 0 0;font-size:14px;line-height:1.6;color:#333333;">{note}</p>'
        "</body></html>"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path)
    parser.add_argument("--write-json", type=Path)
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()

    if args.summary_json and args.summary_json.exists():
        summary = json.loads(args.summary_json.read_text(encoding="utf-8"))
    else:
        if not args.log:
            raise SystemExit("--log or --summary-json is required")
        summary = summarize_log(args.log)

    if args.write_json:
        args.write_json.parent.mkdir(parents=True, exist_ok=True)
        args.write_json.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    print(
        "intraday_summary "
        + " ".join(
            f"{key}={summary[key]}"
            for key in ("state", "scan_count", "detected_count", "new_count", "failure_count")
        ),
        flush=True,
    )

    if not args.send:
        return

    config = load_gmail_config()
    if config is None:
        print("intraday_summary_mail=skipped reason=missing_secrets", flush=True)
        return

    subject = f"【DUKEクラウド】ザラ場監視サマリー {summary['date']}"
    send_gmail(
        subject,
        body_from_summary(summary),
        config,
        html_body=html_from_summary(summary),
    )
    print(f"intraday_summary_mail=sent to={config.mail_to}", flush=True)


if __name__ == "__main__":
    main()
