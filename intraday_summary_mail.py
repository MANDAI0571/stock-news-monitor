from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from gmail_notify import load_gmail_config, send_gmail
from jptime import jst_today

DETECTED_RE = re.compile(r"intraday_alerts_detected=(\d+) new=(\d+)")
SCAN_RE = re.compile(r"(?:scan_count=|\[SESSION\] scan #)(\d+)")
FAIL_RE = re.compile(r"\[SESSION\] scan #\d+ failed")


def summarize_log(path: Path) -> dict[str, int | str]:
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    detected = sum(int(m.group(1)) for m in DETECTED_RE.finditer(text))
    new = sum(int(m.group(2)) for m in DETECTED_RE.finditer(text))
    failures = len(FAIL_RE.findall(text))
    scan_matches = [int(value) for value in SCAN_RE.findall(text)]
    scan_count = max(scan_matches) if scan_matches else len(DETECTED_RE.findall(text))
    return {
        "date": jst_today().isoformat(),
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
    send_gmail(subject, body_from_summary(summary), config)
    print(f"intraday_summary_mail=sent to={config.mail_to}", flush=True)


if __name__ == "__main__":
    main()
