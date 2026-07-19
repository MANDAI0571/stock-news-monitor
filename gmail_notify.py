from __future__ import annotations

import os
import html
import mimetypes
import re
import smtplib
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import pandas as pd

from jptime import jst_today
from scanner.highs import build_high_sections_markdown
from scanner.openwork import format_openwork_score


DISCLAIMER = "※これは投資助言ではなく、スクリーニング結果です。売買判断は自己責任で行ってください。"


@dataclass(frozen=True)
class GmailConfig:
    user: str
    app_password: str
    mail_to: str


def load_gmail_config() -> GmailConfig | None:
    user = os.environ.get("GMAIL_USER", "").strip()
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    mail_to = os.environ.get("MAIL_TO", "").strip()
    if not user or not app_password or not mail_to:
        return None
    return GmailConfig(user=user, app_password=app_password, mail_to=mail_to)


def build_subject(today: date | None = None) -> str:
    today = today or jst_today()
    return f"【DUKEシステム】本日のS/A/B候補 {today.isoformat()}"


def build_candidate_body(
    screening: pd.DataFrame,
    regime: str,
    max_rows: int = 30,
    rank_limits: dict[str, int] | None = None,
) -> str:
    rank_limits = rank_limits or {"S": 5, "A": 10, "B": 10}
    lines: list[str] = [
        "DUKEシステム 日次スクリーニング",
        f"地合い: {regime}",
        "",
    ]

    if screening.empty or "rank" not in screening.columns:
        lines.extend(["S/A/B候補: 0件", "", DISCLAIMER])
        return "\n".join(lines)

    candidates = screening[screening["rank"].astype(str).isin(["S", "A", "B"])].copy()
    if candidates.empty:
        lines.extend(["S/A/B候補: 0件", "", DISCLAIMER])
        return "\n".join(lines)

    rank_order = {"S": 0, "A": 1, "B": 2}
    candidates["_rank_order"] = candidates["rank"].map(rank_order).fillna(9)
    candidates["score"] = pd.to_numeric(candidates.get("score", 0), errors="coerce").fillna(0)
    candidates = candidates.sort_values(["_rank_order", "score"], ascending=[True, False])

    high_lines = build_high_sections_markdown(candidates, max_rows=5)
    if high_lines:
        lines.extend(high_lines)

    total = len(candidates)
    rank_counts = {rank: int(candidates["rank"].eq(rank).sum()) for rank in ["S", "A", "B"]}
    shown_limit = min(max_rows, sum(rank_limits.values()))
    lines.append(
        f"S/A/B候補: {total}件"
        f"（S:{rank_counts['S']} / A:{rank_counts['A']} / B:{rank_counts['B']}、表示最大{shown_limit}件）"
    )
    lines.append("")
    if rank_counts["S"] == 0:
        lines.append("本日はSランクなし")
        lines.append("")

    shown = 0
    for rank in ["S", "A", "B"]:
        group = candidates[candidates["rank"].eq(rank)]
        if group.empty:
            continue
        limit = min(rank_limits.get(rank, 0), max_rows - shown)
        if limit <= 0:
            break
        lines.append(f"■ {rank}ランク（{len(group)}件中 最大{limit}件表示）")
        for _, row in group.head(limit).iterrows():
            if shown >= max_rows:
                break
            lines.extend(_format_candidate(row))
            shown += 1
        lines.append("")
        if shown >= max_rows:
            break

    if total > shown:
        lines.append(f"ほか {total - shown}件はCSVを確認してください。")
        lines.append("")

    lines.append(DISCLAIMER)
    return "\n".join(lines)


def send_gmail(
    subject: str,
    body: str,
    config: GmailConfig,
    attachments: list[Path] | None = None,
) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"DUKEクラウド通知 <{config.user}>"
    message["To"] = config.mail_to
    message["Importance"] = "high"
    message["X-Priority"] = "1"
    message.set_content(body)
    message.add_alternative(_body_to_html(body), subtype="html")

    for attachment in _expand_attachments(attachments):
        path = Path(attachment)
        if not path.exists():
            continue
        mimetype, _ = mimetypes.guess_type(path.name)
        if mimetype:
            maintype, subtype = mimetype.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"
        message.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(config.user, config.app_password)
        smtp.send_message(message)


def maybe_send_gmail(
    screening: pd.DataFrame,
    regime: str,
    enabled: bool,
    max_rows: int = 30,
    attachments: list[Path] | None = None,
) -> bool:
    if not enabled:
        print("gmail_notification=skipped reason=disabled")
        return False

    config = load_gmail_config()
    if config is None:
        print("gmail_notification=skipped reason=missing_secrets required=GMAIL_USER,GMAIL_APP_PASSWORD,MAIL_TO")
        return False

    subject = build_subject()
    body = build_candidate_body(screening, regime, max_rows=max_rows)
    expanded_attachments = _expand_attachments(attachments)
    if expanded_attachments:
        body = "\n".join(
            [
                body,
                "",
                "---",
                "Note投稿用ファイルを添付しています。",
                "添付:",
                "\n".join(path.name for path in expanded_attachments),
            ]
        )
    send_gmail(subject, body, config, attachments=expanded_attachments)
    print(f"gmail_notification=sent to={config.mail_to} subject={subject}")
    return True


def _expand_attachments(attachments: list[Path] | None) -> list[Path]:
    if not attachments:
        return []

    expanded: list[Path] = []
    seen: set[Path] = set()

    def add_path(path: Path) -> None:
        resolved = Path(path)
        if resolved in seen:
            return
        seen.add(resolved)
        expanded.append(resolved)

    for attachment in attachments:
        path = Path(attachment)
        if path.name == "note_daily.md":
            for name in ("note_title.txt", "note_daily.md", "note_daily.html"):
                sibling = path.with_name(name)
                if sibling.exists():
                    add_path(sibling)
        else:
            add_path(path)

    return [path for path in expanded if path.exists()]


def _format_candidate(row: pd.Series) -> list[str]:
    code = _text(row, "code")
    name = _text(row, "name")
    price = _text(row, "current_price")
    score = _text(row, "score")
    dist = _text(row, "dist_52w_high_pct")
    vol = _text(row, "volume_ratio_5d_20d")
    reason = _text(row, "reason")
    lot = _text(row, "lot_value_100")
    openwork = format_openwork_score(row.get("openwork_score"))
    return [
        f"[{code} {name}]({_chart_url(code)})",
        f"  株価:{price}円 / 点数:{score} / 100株:{lot}円 / OpenWork: {openwork}",
        f"  52週高値差:{dist}% / 出来高比:{vol}",
        f"  📈 チャート:{_chart_url(code)}",
        f"  理由:{reason}",
        "",
    ]


def _text(row: pd.Series, key: str) -> str:
    value = row.get(key, "")
    if pd.isna(value):
        return ""
    text = str(value)
    return text[:-2] if key == "code" and text.endswith(".0") else text


def _chart_url(code: str) -> str:
    return (
        f"https://finance.yahoo.co.jp/quote/{code}.T/chart"
        "?frm=dly&trm=6m&scl=stndrd&styl=cndl&evnts=volume"
        "&ovrIndctr=sma%2Cmma%2Clma&addIndctr=&compare="
    )


def _body_to_html(body: str) -> str:
    escaped = html.escape(body)

    def markdown_link(match: re.Match[str]) -> str:
        label = match.group(1)
        url = match.group(2)
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{label}</a>'

    linked = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", markdown_link, escaped)
    linked = re.sub(
        r"(?<![\"'=])\bhttps?://[^\s<>()]+",
        lambda match: f'<a href="{match.group(0)}" target="_blank" rel="noopener noreferrer">{match.group(0)}</a>',
        linked,
    )
    return (
        "<html><body>"
        "<pre style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "white-space:pre-wrap;line-height:1.5\">"
        f"{linked}"
        "</pre></body></html>"
    )
