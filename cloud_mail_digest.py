from __future__ import annotations

import argparse
import csv
import re
from urllib.parse import quote
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from gmail_notify import DISCLAIMER, load_gmail_config, send_gmail
from note_mail_html import (
    build_copy_mails,
    build_copy_pack_html,
    build_mail_html,
    collect_note_parts,
)


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs"
JST = ZoneInfo("Asia/Tokyo")

NOTE_SECTIONS = (
    ("chatgpt", "300万円 ChatGPT"),
    ("claude", "300万円 Claude"),
    ("pullback", "25MA/押し目・200MA/240MA"),
    ("highs", "52週新高値"),
)

FIXED_ATTACHMENTS = (
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
    "note_copy_pack.html",
    "note_drafts_manifest.json",
    "note_cloud_artifact_manifest.json",
    "note_draft_url_cloud.txt",
    "note_draft_url_claude.txt",
    "note_draft_url_pullback.txt",
    "note_draft_url_highs.txt",
    "market_snapshot.json",
    "metron_kpi_report.md",
    "metron_kpi.json",
    "warren_summary.json",
    "decision_result.csv",
    "decision_report.md",
    "discipline_result.csv",
    "paper_portfolio_decision.csv",
    "screening_result.csv",
)

LATEST_ATTACHMENT_PATTERNS = (
    "screening_result_*.csv",
    "screening_pullback_*.csv",
    "screening_highs_*.csv",
    "screening_52w_retest_*.csv",
    "discipline_portfolio_*.csv",
    "s_rank_candidates_*.csv",
)


@dataclass(frozen=True)
class DigestMail:
    subject: str
    body: str
    attachments: list[Path]
    html_body: str = ""


def safari_note_url(note_url: str) -> str:
    """Return an indirect URL so an iPhone tap stays in Safari.

    Direct note/editor.note.com links are Universal Links and can be handed to
    the note iOS app.  The app currently crashes for some browser-created
    drafts, while the same draft remains readable in the web editor.  Starting
    on a web redirect keeps the navigation in the browser.
    """
    return f"https://www.google.com/url?q={quote(note_url, safe='')}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="クラウド生成済みのスクリーニング結果をGmailで送る")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--dry-run", action="store_true", help="送信せず本文だけ生成する")
    # T-P(2026-08-11): 休場日でも手動で1通だけ送れるようにする（手動再送・動作確認用）。
    #   既定はOFFなので、定時運用の休場日スキップはこれまでどおり効く。
    parser.add_argument(
        "--force-send",
        action="store_true",
        help="休場日でも送る（手動再送用。既定はOFF）",
    )
    # T-P(2026-08-13): note下書きは本編メールとは別に、1本ごとに
    #   「本文だけ」のプレーンメールでも送る。iPhoneのGmailは添付HTMLを
    #   開けず、file:// ではコピー機能も動かないため、本文を長押し→
    #   すべてを選択→コピー、で確実に貼り付けられる経路を用意する。
    parser.add_argument(
        "--no-copy-mails",
        action="store_true",
        help="note下書きのコピー用メールを送らない（既定は送る）",
    )
    return parser.parse_args()


def build_digest(output_dir: Path, now: datetime | None = None) -> DigestMail:
    now = now or datetime.now(JST)
    subject = f"【DUKEクラウド】25MA/200MA・本日のスクリーニング結果 {now.date().isoformat()}"

    # T-P(2026-08-11): note下書きをワンクリックでコピーできるページを先に書き出し、
    #   添付収集に間に合わせる（添付一覧は note_copy_pack.html を含む）。
    note_parts = collect_note_parts(output_dir)
    _write_copy_pack(output_dir, note_parts, now)

    attachments = collect_attachments(output_dir)

    lines: list[str] = [
        "DUKEクラウド 結果まとめ",
        f"作成: {now.strftime('%Y-%m-%d %H:%M JST')}",
        "",
        "25MA/押し目、新高値、300万円判断、メトロンKPIをまとめて送ります。",
        "Markdown/HTML/CSVの元ファイルは添付に入れています。",
        "",
    ]

    # T-P(2026-08-10): 本文が重い記事は複数の下書きに分割されるため、
    # note_draft_url_highs2.txt のような続きのURLも順番に拾う。
    note_urls = (
        ("統合版", "cloud"),
        ("300万円 Claude版", "claude"),
        ("押し目候補版", "pullback"),
        ("52週新高値版", "highs"),
    )
    # T-P(2026-08-11): 1本目の保存が失敗しても2本目以降のURLを落とさない。
    #   （run #72 では pullback/highs の1本目だけ保存に失敗し、
    #     旧実装では続きの7本ぶんのURLがメールから丸ごと消えていた）
    available_urls: list[tuple[str, str]] = []
    missing_parts: list[str] = []
    for label, key in note_urls:
        total = _count_note_parts(output_dir, key)
        found: list[tuple[int, str]] = []
        for index in range(1, max(total, 1) + 1):
            stem = key if index == 1 else f"{key}{index}"
            url = _read_optional(output_dir / f"note_draft_url_{stem}.txt")
            if url:
                found.append((index, url))
            elif total > 0:
                missing_parts.append(label if total == 1 else f"{label}（{index}/{total}）")
        for index, url in found:
            available_urls.append((label if total <= 1 else f"{label}（{index}/{total}）", url))
    if available_urls:
        lines.append("## Note下書きURL（iPhone Safari用・記事別）")
        lines.append("noteアプリが落ちる場合に備え、Safariを経由して開くリンクです。")
        lines.extend(f"- {label}: {safari_note_url(url)}" for label, url in available_urls)
        lines.append("")
    if missing_parts:
        lines.append("## note下書きの保存に失敗した本")
        lines.append("下記はnote側に保存できていません。添付 note_copy_pack.html からコピーして貼り付けてください。")
        lines.extend(f"- {label}" for label in missing_parts)
        lines.append("")
    lines.append("## note下書きのコピー")
    lines.append("添付の note_copy_pack.html をSafariで開くと、ボタン1つで本文をコピーできます。")
    lines.append("HTMLメールとして表示すると、noteでの見え方のプレビューも本文に出ます。")
    lines.append("")

    lines.extend(_ma_touch_summary(output_dir))
    lines.append("")

    lines.append("## 本文プレビュー")
    for key, label in NOTE_SECTIONS:
        title = _section_title(output_dir, key, label)
        preview = _preview_markdown(output_dir / f"note_{key}.md")
        lines.extend(["", f"### {label}", title, "", preview])

    metron = _preview_markdown(output_dir / "metron_kpi_report.md", max_lines=14, max_chars=1200)
    if metron:
        lines.extend(["", "### メトロンKPI", "", metron])

    lines.extend([
        "",
        "## 添付",
        *_attachment_lines(attachments),
        "",
        DISCLAIMER,
    ])
    body = "\n".join(lines)
    html_body = build_mail_html(subject, body, note_parts)
    return DigestMail(subject=subject, body=body, attachments=attachments, html_body=html_body)


def _count_note_parts(output_dir: Path, key: str) -> int:
    """note_{key}.md / note_draft_url_{key}.txt の連番から、その記事の本数を数える。

    1本目が欠けていても（保存失敗）2本目以降を数え漏らさない。
    """
    total = 0
    for index in range(1, 21):
        stem = key if index == 1 else f"{key}{index}"
        exists = (output_dir / f"note_{stem}.md").exists() or (
            output_dir / f"note_draft_url_{stem}.txt"
        ).exists()
        if exists:
            total = index
        elif index > 1:
            break
    return total


def _write_copy_pack(output_dir: Path, note_parts: list, now: datetime) -> Path | None:
    """ワンクリックでコピーできる単体HTMLを outputs/note_copy_pack.html に書く。"""
    path = output_dir / "note_copy_pack.html"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(build_copy_pack_html(note_parts, now), encoding="utf-8")
    except OSError as error:
        print(f"note_copy_pack=failed reason={error}")
        return None
    print(f"note_copy_pack={path} parts={len(note_parts)}")
    return path


def collect_attachments(output_dir: Path) -> list[Path]:
    attachments: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if not path.exists() or not path.is_file():
            return
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        attachments.append(path)

    for name in FIXED_ATTACHMENTS:
        add(output_dir / name)
    for pattern in LATEST_ATTACHMENT_PATTERNS:
        latest = _latest(output_dir, pattern)
        if latest is not None:
            add(latest)
    return attachments


def write_digest_artifacts(output_dir: Path, digest: DigestMail) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "cloud_digest_mail_subject.txt").write_text(digest.subject + "\n", encoding="utf-8")
    (output_dir / "cloud_digest_mail_body.md").write_text(digest.body + "\n", encoding="utf-8")
    if digest.html_body:
        (output_dir / "cloud_digest_mail_body.html").write_text(digest.html_body, encoding="utf-8")


def _section_title(output_dir: Path, key: str, fallback: str) -> str:
    text = _read_optional(output_dir / f"note_{key}_title.txt")
    return text or fallback


def _preview_markdown(path: Path, max_lines: int = 22, max_chars: int = 1800) -> str:
    text = _read_optional(path)
    if not text:
        return "取得できず"

    selected: list[str] = []
    for raw_line in text.splitlines():
        line = _clean_text(raw_line)
        if line.startswith("!["):
            continue
        if not line and not selected:
            continue
        selected.append(line)
        if len(selected) >= max_lines:
            break

    preview = "\n".join(selected).strip()
    if len(preview) > max_chars:
        preview = preview[:max_chars].rstrip() + "\n...（続きは添付ファイル）"
    return preview or "取得できず"


def _read_optional(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return _clean_text(path.read_text(encoding="utf-8")).strip()
    except UnicodeDecodeError:
        return _clean_text(path.read_text(encoding="utf-8-sig", errors="replace")).strip()


def _clean_text(value: object) -> str:
    text = str(value).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    if text.strip().lower() in {"nan", "none", "null"}:
        return ""
    return text


def _latest(output_dir: Path, pattern: str) -> Path | None:
    files = [path for path in output_dir.glob(pattern) if path.is_file()]
    if not files:
        return None
    return max(files, key=_latest_key)


def _latest_key(path: Path) -> tuple[str, float, str]:
    match = re.search(r"_(20\d{6})(?:_(\d{6}))?", path.stem)
    if match:
        return (match.group(1) + (match.group(2) or "000000"), path.stat().st_mtime, path.name)
    return ("", path.stat().st_mtime, path.name)


def _attachment_lines(attachments: list[Path]) -> list[str]:
    if not attachments:
        return ["- 添付なし（出力ファイルが見つかりません）"]
    return [f"- {path.name}" for path in attachments]


def _ma_touch_summary(output_dir: Path) -> list[str]:
    source = _latest(output_dir, "screening_pullback_*.csv")
    rows = _read_csv_dicts(source) if source else []
    lines = [
        "## 25MA/200MA候補（本文で確認）",
        "",
        f"元データ: {source.name if source else 'screening_pullback未取得'}",
        "",
    ]
    for flag, title, dist_col in (
        ("ma25_touch", "25MAタッチ", "dist_25ma_pct"),
        ("ma200_touch", "200MAタッチ", "dist_200ma_pct"),
    ):
        bucket = [row for row in rows if _truthy(row.get(flag))]
        lines.extend(_ma_touch_table(title, bucket, dist_col))
        lines.append("")
    return lines


def _read_csv_dicts(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))
    except Exception:
        return []


def _ma_touch_table(title: str, rows: list[dict[str, str]], dist_col: str, limit: int = 20) -> list[str]:
    lines = [f"### {title}（{len(rows)}件）", ""]
    if not rows:
        lines.append("- 該当なし")
        return lines

    lines.extend([
        "| コード | 銘柄 | 現在値 | MA25 | MA200 | MA240 | MA乖離% | 52週高値乖離% | 売買代金 | チャート |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ])
    for row in rows[:limit]:
        code = _normalize_code(_cell(row, "code"))
        lines.append(
            "| "
            + " | ".join(
                [
                    code,
                    _cell(row, "name"),
                    _cell(row, "current_price"),
                    _cell(row, "ma25"),
                    _cell(row, "ma200"),
                    _cell(row, "ma240"),
                    _cell(row, dist_col),
                    _cell(row, "dist_52w_high_pct"),
                    _cell(row, "turnover_20d"),
                    f"[開く]({_chart_url(code)})" if code != "-" else "-",
                ]
            )
            + " |"
        )
    if len(rows) > limit:
        lines.append(f"- ほか{len(rows) - limit}件は添付CSVに入っています。")
    return lines


def _truthy(value: object) -> bool:
    return _clean_text(value).strip().lower() in {"true", "1", "1.0", "yes", "y"}


def _cell(row: dict[str, str], key: str) -> str:
    text = _clean_text(row.get(key, "")).strip()
    if not text:
        return "-"
    return text.replace("|", "/").replace("\n", " ")


def _normalize_code(code: str) -> str:
    return code[:-2] if code.endswith(".0") else code


def _chart_url(code: str) -> str:
    return (
        f"https://finance.yahoo.co.jp/quote/{code}.T/chart"
        "?frm=dly&trm=6m&scl=stndrd&styl=cndl&evnts=volume"
        "&ovrIndctr=sma%2Cmma%2Clma&addIndctr=&compare="
    )


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    digest = build_digest(output_dir)
    write_digest_artifacts(output_dir, digest)

    if args.dry_run:
        print(digest.subject)
        print(digest.body)
        print(f"attachments={len(digest.attachments)}")
        print(f"html_body_bytes={len(digest.html_body.encode('utf-8'))}")
        for subject, text in build_copy_mails(collect_note_parts(output_dir)):
            print(f"copy_mail bytes={len(text.encode('utf-8'))} subject={subject}")
        return

    config = load_gmail_config()
    if config is None:
        raise RuntimeError("GMAIL_USER/GMAIL_APP_PASSWORD/MAIL_TO が未設定です")

    if not send_gmail(
        digest.subject,
        digest.body,
        config,
        attachments=digest.attachments,
        html_body=digest.html_body,
        allow_non_business_day=args.force_send,
    ):
        print("cloud_digest_mail=skipped reason=jpx_holiday")
        return
    print(
        f"cloud_digest_mail=sent attachments={len(digest.attachments)} "
        f"html_body_bytes={len(digest.html_body.encode('utf-8'))}"
    )

    copy_mails = build_copy_mails(collect_note_parts(output_dir))
    if args.no_copy_mails:
        print(f"note_copy_mails=skipped reason=disabled count={len(copy_mails)}")
        return
    sent = 0
    for subject, text in copy_mails:
        # ここに来た時点で本編は送れている＝休場日ゲートは通過済み。
        if send_gmail(subject, text, config, allow_non_business_day=True):
            sent += 1
    print(f"note_copy_mails=sent {sent}/{len(copy_mails)}")


if __name__ == "__main__":
    main()
