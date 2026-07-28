from __future__ import annotations

import os

from gmail_notify import load_gmail_config, send_gmail
from jptime import jst_now


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
