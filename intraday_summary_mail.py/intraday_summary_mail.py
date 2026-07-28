from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

from gmail_notify import load_gmail_config, send_gmail
from jptime import jst_today


DETECTED_RE = re.compile(r"intraday_alerts_detected=(\d+) new=(\d+)")
SCAN_RE = re.compile(r"(?:scan_count=|\[SESSION\] scan #)(\d+)")
FAIL_RE = re.compile(r"\[SESSION\] scan #\d+ failed")


def summarize_log(path: Path) -> dict[str, int | str]:
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    detected = 0
    new = 0
    scan_count = 0
    failures = len(FAIL_RE.findall(text))
    for match in DETECTED_RE.finditer(text):
        detected += int(match.group(1))
        new += int(match.group(2))
    scan_matches = [int(value) for value in SCAN_RE.findall(text)]
    if scan_matches:
        scan_count = max(scan_matches)
    if scan_count == 0:
        scan_count = len(list(DETECTED_RE.finditer(text)))
    state = "normal" if scan_count > 0 else "abnormal"
    return {
        "date": jst_today().isoformat(),
        "state": state,
        "scan_count": scan_count,
        "detected_count": detected,
        "new_count": new,
        "failure_count": failures,
    }


def body_from_summary(summary: dict[str, int | str]) -> str:
    scan_count = int(summary.get("scan_count", 0))
    detected = int(summary.get("detected_count", 0))
    new = int(summary.get("new_count", 0))
    failures = int(summary.get("failure_count", 0))
    state = str(summary.get("state", "abnormal"))
    status = "正常（監視を実行）" if state == "normal" else "異常（監視0回）"
    lines = [
        "DUKEクラウド ザラ場監視サマリー",
        f"対象日: {summary.get('date', jst_today().isoformat())}",
        f"状態: {status}",
        f"スキャン回数: {scan_count}回",
        f"検出件数（重複含む）: {detected}件",
        f"新規アラート: {new}件",
        f"失敗回数: {failures}回",
        "",
    ]
    if state == "normal" and new == 0:
        lines.append("新規アラート0件ですが、スキャンは実行済みです。")
    elif state != "normal":
        lines.append("スキャンが0回のため、該当なしとは判定しません。")
    else:
        lines.append("新規アラートがありました。詳細は各アラートメールを確認してください。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path)
    parser.add_argument("--write-json", type=Path)
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()

    if args.summary_json:
        summary = json.loads(args.summary_json.read_text(encoding="utf-8"))
    elif args.log:
        summary = summarize_log(args.log)
    else:
        raise SystemExit("--log or --summary-json is required")

    if args.write_json:
        args.write_json.parent.mkdir(parents=True, exist_ok=True)
        args.write_json.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    print(
        "intraday_summary "
        f"state={summary.get('state')} "
        f"scans={summary.get('scan_count', 0)} "
        f"detected={summary.get('detected_count', 0)} "
        f"new={summary.get('new_count', 0)} "
        f"failures={summary.get('failure_count', 0)}"
    )

    if not args.send:
        return 0

    config = load_gmail_config()
    if config is None:
        print("intraday_summary_mail=skipped reason=missing_secrets")
        return 0

    subject = f"【DUKEクラウド】ザラ場監視サマリー {summary.get('date', jst_today().isoformat())}"
    send_gmail(subject, body_from_summary(summary), config)
    print(f"intraday_summary_mail=sent to={config.mail_to}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
