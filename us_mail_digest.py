"""米株の記事をメールで送り、コピー用ページを公開する。

高重さんの指示(2026-09-06)「米株を日本株と同じようにやる」「メールでのコピペはやめない」。

日本株の cloud_mail_digest.py とは別ファイルにする。あちらは本番で毎日動いていて、
300万円運用・25MAの表・メトロンKPI・添付の組み立てが全体に絡んでいる。
米株のために手を入れると、日本株のメールを壊す危険がある。
組み立ての部品（note_mail_html.py）は共有し、送る中身だけ別にする。

送るもの:
  1. 本編メール（3本ぶんの見出し＋コピー用ページのリンク）
  2. 記事1本ごとの【コピー用】メール（iPhoneで長押し→すべて選択→コピー）
  3. docs/copy/us_latest.html（Safariで開いてボタン1つでコピー）

米国市場の営業日で判定する。日本の休みは関係ない（JPXの休場ゲートは使わない）。
"""

from __future__ import annotations

import argparse
import subprocess
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from gmail_notify import load_gmail_config, send_gmail
from note_mail_html import (
    US_COPY_PAGE_URL,
    US_NOTE_ARTICLES,
    build_copy_mail_html,
    build_copy_mail_items,
    build_copy_pack_html,
    collect_note_parts,
    note_body_text,
    render_note_html,
    wrap_mail_html,
)
from us_calendar import is_us_business_day, prev_us_business_day

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs"
DOCS_COPY_DIR = PROJECT_ROOT / "docs" / "copy"
JST = ZoneInfo("Asia/Tokyo")

DISCLAIMER = "本記事は投資助言ではありません。売買判断はご自身の責任でお願いします。"


def us_session_date(now: datetime | None = None) -> date:
    """直前に終わった米国市場の営業日。

    米国市場が閉まるのは日本時間の早朝なので、この処理が動く時点の
    「日本の今日」ではなく、直前の米国営業日を対象にする。
    """
    today = (now or datetime.now(JST)).date()
    return prev_us_business_day(today)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="米株の記事をメールで送る")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--dry-run", action="store_true", help="送らずに本文だけ出す")
    parser.add_argument(
        "--no-copy-mails", action="store_true", help="【コピー用】メールを送らない（既定は送る）"
    )
    return parser.parse_args()


# ---------------------------------------------------------------- コピー用ページ

def publish_us_copy_page(parts: list, now: datetime, output_dir: Path) -> str | None:
    """docs/copy/us_latest.html を書いて push する。

    セルフテストは一時ディレクトリを渡すので、本番の outputs/ 以外では公開しない。
    """
    if output_dir.resolve() != DEFAULT_OUTPUT_DIR.resolve():
        print(f"us_copy_page=skipped reason=not_production_output_dir dir={output_dir}")
        return None
    if not parts:
        print("us_copy_page=skipped reason=no_note_parts")
        return None
    try:
        DOCS_COPY_DIR.mkdir(parents=True, exist_ok=True)
        html = build_copy_pack_html(parts, now)
        (DOCS_COPY_DIR / "us_latest.html").write_text(html, encoding="utf-8")
        (DOCS_COPY_DIR / f"us_{us_session_date(now).isoformat()}.html").write_text(
            html, encoding="utf-8"
        )
        _prune_us_pages()
    except Exception as error:  # noqa: BLE001 - 公開に失敗してもメールは送る
        print(f"us_copy_page=failed reason={error}")
        return None
    print(f"us_copy_page=written parts={len(parts)}")
    if _push_us_copy_page(now):
        print(f"us_copy_page=published url={US_COPY_PAGE_URL}")
    else:
        print("us_copy_page=not_pushed（メールのリンクは前回公開分を指します）")
    return US_COPY_PAGE_URL


def _prune_us_pages(keep: int = 14) -> None:
    dated = sorted(DOCS_COPY_DIR.glob("us_20*.html"), key=lambda p: p.name, reverse=True)
    for path in dated[keep:]:
        try:
            path.unlink()
            print(f"us_copy_page=pruned {path.name}")
        except OSError:
            pass


def _push_us_copy_page(now: datetime) -> bool:
    """docs/copy/ と米株の注文台帳を main に push する。失敗してもメールは送る。"""
    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=PROJECT_ROOT, capture_output=True, text=True)

    try:
        run("git", "config", "user.name", "github-actions[bot]")
        run("git", "config", "user.email", "github-actions[bot]@users.noreply.github.com")
        targets = ["docs/copy"]
        for extra in ("data/claude_us20k_orders.csv", "data/claude_us20k_journal.csv"):
            if (PROJECT_ROOT / extra).exists():
                targets.append(extra)
        run("git", "add", *targets)
        stamp = now.strftime("%Y-%m-%d %H:%M JST")
        commit = run("git", "commit", "-m", f"米株コピー用ページ更新 {stamp} [skip ci]")
        if commit.returncode != 0 and "nothing to commit" in (commit.stdout + commit.stderr):
            print("us_copy_page=no_change")
            return True
        push = run("git", "push", "origin", "HEAD:main")
        if push.returncode != 0:
            print(f"us_copy_page_push=failed {push.stderr.strip()[:200]}")
            return False
        return True
    except OSError as error:
        print(f"us_copy_page_push=failed {error}")
        return False


# ---------------------------------------------------------------- 本編メール

def build_us_digest(output_dir: Path, now: datetime | None = None) -> tuple[str, str, str]:
    """(件名, プレーン本文, HTML本文) を返す。"""
    now = now or datetime.now(JST)
    session = us_session_date(now)
    subject = f"【米株】52週新高値・押し目・$20,000運用 {session.isoformat()}"

    parts = collect_note_parts(output_dir, articles=US_NOTE_ARTICLES)
    page_url = publish_us_copy_page(parts, now, output_dir)

    lines: list[str] = [
        "米株まとめ",
        f"作成: {now.strftime('%Y-%m-%d %H:%M JST')}",
        f"対象の米国営業日: {session.isoformat()}",
        "",
    ]
    if not parts:
        lines += [
            "データ不足：米株の記事が1本もできていません。",
            "スクリーニングが失敗したか、記事の生成前にこのメールが動いた可能性があります（未確認）。",
            "",
            DISCLAIMER,
        ]
        body = "\n".join(lines)
        return subject, body, wrap_mail_html(subject, [render_note_html(body)])

    lines += [
        "米株の記事が3本できています。noteに貼るときは下のどちらかを使ってください。",
        "",
        "## コピー用ページ（iPhoneはここから）",
        "下のURLをSafariで開くと、ボタン1つで本文をコピーできます。",
        page_url or US_COPY_PAGE_URL,
        "",
        "## 【コピー用】メール",
        "このメールの後に、記事1本ごとの【コピー用】メールが届きます。",
        "本文を長押し →「すべてを選択」→ コピー で、そのままnoteに貼れます。",
        "",
        "## 記事一覧",
    ]
    for part in parts:
        lines.append(f"- {part.part_label}：{part.title}（{len(part.markdown):,}文字）")
    lines.append("")

    lines.append("## 本文プレビュー")
    for part in parts:
        # 記事の1行目はタイトルそのものなので落とす（すぐ上に同じものを出している）。
        plain = note_body_text(part.markdown).split("\n")
        if plain and plain[0].strip() == part.title.strip():
            plain = plain[1:]
        preview = "\n".join(plain[:24]).strip()
        lines += ["", f"### {part.part_label}", part.title, "", preview]

    lines += ["", DISCLAIMER]
    body = "\n".join(lines)
    html_body = wrap_mail_html(subject, [render_note_html(body)])
    return subject, body, html_body


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    now = datetime.now(JST)
    subject, body, html_body = build_us_digest(output_dir, now)
    parts = collect_note_parts(output_dir, articles=US_NOTE_ARTICLES)
    copy_mails = build_copy_mail_items(parts)

    if args.dry_run:
        print(subject)
        print(body)
        print(f"html_body_bytes={len(html_body.encode('utf-8'))}")
        for mail_subject, text, _ in copy_mails:
            print(f"copy_mail bytes={len(text.encode('utf-8'))} subject={mail_subject}")
        return

    config = load_gmail_config()
    if config is None:
        raise RuntimeError("GMAIL_USER/GMAIL_APP_PASSWORD/MAIL_TO が未設定です")

    # 米国市場の営業日で判断する。JPXの休場ゲートは使わない（日本の祝日でも米国は開く）。
    if not send_gmail(subject, body, config, allow_non_business_day=True, html_body=html_body):
        print("us_digest_mail=failed")
        return
    print(f"us_digest_mail=sent html_body_bytes={len(html_body.encode('utf-8'))}")

    if args.no_copy_mails:
        print(f"us_copy_mails=skipped reason=disabled count={len(copy_mails)}")
        return
    sent = 0
    for mail_subject, text, anchor in copy_mails:
        if send_gmail(
            mail_subject,
            text,
            config,
            allow_non_business_day=True,
            html_body=build_copy_mail_html(text, anchor, page_url=US_COPY_PAGE_URL),
        ):
            sent += 1
    print(f"us_copy_mails=sent {sent}/{len(copy_mails)}")


if __name__ == "__main__":
    main()
