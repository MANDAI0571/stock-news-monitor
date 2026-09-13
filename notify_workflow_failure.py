from __future__ import annotations

import os
from pathlib import Path

from gmail_notify import load_gmail_config, send_gmail
from jptime import jst_now


# fix63(2026-09-13): メールに「どこまで進んだか」と「最後のログ」を載せる。
#   実行ログURLだけでは、ログを開けない場所から原因を追えなかった。

# 進み具合を示すファイル。順番は処理の流れどおり。
_PROGRESS_FILES = (
    ("自己テスト", "outputs/self_test_last.log"),
    ("スクリーニング", "outputs/screening_result.csv"),
    ("判定(BUY/WATCH)", "outputs/decision_result.csv"),
    ("成績レポート", "outputs/performance_report.md"),
    ("note記事(claude)", "outputs/note_claude.md"),
    ("note記事(米株)", "outputs/note_us_highs.md"),
)

_LOG_TAIL_LINES = 30


def _diagnosis_lines() -> list[str]:
    """何が起きたかをメールに書ける形にする。読めないものは「未確認」と書く。"""
    from datetime import datetime, timedelta, timezone

    jst = timezone(timedelta(hours=9))
    root = Path(__file__).resolve().parent
    lines = ["--- どこまで進んだか（いつ作られたファイルか）---"]
    for label, rel in _PROGRESS_FILES:
        path = root / rel
        try:
            stat = path.stat() if path.exists() else None
            if stat and stat.st_size > 0:
                stamp = datetime.fromtimestamp(stat.st_mtime, jst).strftime("%m-%d %H:%M")
                lines.append(f"{label}: できている（{stat.st_size:,}バイト / {stamp} JST）")
            else:
                lines.append(f"{label}: できていない")
        except OSError:
            lines.append(f"{label}: 未確認（読めませんでした）")

    log_path = root / "outputs" / "self_test_last.log"
    lines.append("")
    lines.append("--- 自己テスト ---")
    try:
        if not log_path.exists():
            lines.append(
                "ログがありません。自己テストまで到達していない可能性があります"
                "（チェックアウト・Python準備・pip install のどれか）。"
            )
            return lines
        text = log_path.read_text(encoding="utf-8", errors="replace")
        rows = [row for row in text.split("\n") if row.strip()]
        if rows and "成功" in rows[0]:
            lines.append("自己テストは通っています → その先のステップで落ちています。")
            warnings = [row for row in rows if row.startswith("WARNING")]
            if warnings:
                lines.append("（自己テスト中の警告）")
                lines.extend(warnings[-5:])
            return lines
        lines.append("自己テストが落ちています。最後の行:")
        lines.extend(rows[-_LOG_TAIL_LINES:] if rows else ["（ログが空でした）"])
    except OSError as error:
        lines.append(f"未確認（ログを読めませんでした: {error}）")
    return lines


def main() -> int:
    """失敗通知を試みる。通知自体の失敗で元のActions失敗を隠さない。"""
    try:
        config = load_gmail_config()
        if config is None:
            print("workflow_failure_mail=skipped reason=gmail_secret_missing")
            return 0

        now = jst_now()
        workflow = os.environ.get("GITHUB_WORKFLOW", "unknown workflow")
        job = os.environ.get("GITHUB_JOB", "unknown job")
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        run_id = os.environ.get("GITHUB_RUN_ID", "unknown")
        run_url = f"{server}/{repository}/actions/runs/{run_id}"
        occurred_at = now.isoformat(timespec="seconds")
        subject = f"【障害】{workflow} が失敗しました {occurred_at}"
        body = "\n".join(
            [
                "GitHub Actionsのワークフローが失敗しました。",
                f"ワークフロー名: {workflow}",
                f"ジョブ名: {job}",
                f"発生時刻(JST): {occurred_at}",
                f"実行ログURL: {run_url}",
                "",
                *_diagnosis_lines(),
                "",
                "失敗した場合、note下書き・メール配信は生成されていない可能性があります。",
                "※本メールは自動送信です。投資助言ではありません。",
            ]
        )
        sent = send_gmail(
            subject,
            body,
            config,
            allow_non_business_day=True,
        )
        if sent:
            print(f"workflow_failure_mail=sent to={config.mail_to}")
        else:
            print("workflow_failure_mail=failed reason=send_gmail_returned_false")
    except Exception as exc:  # noqa: BLE001 - 元の失敗を隠さない
        print(f"workflow_failure_mail=failed reason={type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
