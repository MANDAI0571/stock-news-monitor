from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from gmail_notify import build_candidate_body, build_subject, load_gmail_config, send_gmail
from market_regime import fetch_regime


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs"
NOTE_URL_FILE = "note_draft_url.txt"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="note下書きURL付きでGmail通知を送る")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--mail-max-rows", type=int, default=30)
    return parser.parse_args()


def latest_csv(output_dir: Path, pattern: str) -> Path | None:
    files = list(output_dir.glob(pattern))
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def load_dataframe(path: Path | None) -> pd.DataFrame:
    if path is None or not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def load_note_url(output_dir: Path) -> str | None:
    path = output_dir / NOTE_URL_FILE
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def build_mail_body(
    screening: pd.DataFrame,
    discipline: pd.DataFrame,
    regime: str,
    note_url: str | None,
) -> str:
    lines = [build_candidate_body(screening, regime)]
    lines.extend([
        "",
        "## 規律版",
        "",
    ])
    if discipline.empty:
        lines.append("- 未取得")
    else:
        action_counts = discipline.get("action", pd.Series(dtype=str)).astype(str).value_counts().to_dict()
        lines.append(f"- BUY: {int(action_counts.get('BUY', 0))}件")
        lines.append(f"- CASH: {int(action_counts.get('CASH', 0))}件")
        if "regime" in discipline.columns and not discipline["regime"].dropna().empty:
            lines.append(f"- 地合い: {discipline['regime'].dropna().astype(str).iloc[0]}")
        preview_cols = [c for c in ["slot", "action", "code", "name", "rank", "score", "cash_reason"] if c in discipline.columns]
        if preview_cols:
            lines.append("")
            lines.append("| " + " | ".join(preview_cols) + " |")
            lines.append("|" + "|".join(["---"] * len(preview_cols)) + "|")
            for _, row in discipline.head(3).iterrows():
                lines.append(
                    "| "
                    + " | ".join("" if pd.isna(row.get(col)) else str(row.get(col)) for col in preview_cols)
                    + " |"
                )

    lines.extend([
        "",
        "---",
        "Note投稿用ファイルを添付しています。",
        "添付:",
        "note_title.txt",
        "note_daily.md",
        "note_daily.html",
        "",
        f"Note下書きURL: {note_url or '未取得'}",
    ])
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)

    screening_path = latest_csv(output_dir, "screening_result_*.csv")
    discipline_path = latest_csv(output_dir, "discipline_portfolio_*.csv")
    if screening_path is None:
        raise FileNotFoundError("screening_result_*.csv が見つかりません")
    if discipline_path is None:
        raise FileNotFoundError("discipline_portfolio_*.csv が見つかりません")

    screening = load_dataframe(screening_path)
    discipline = load_dataframe(discipline_path)
    regime = "UNKNOWN"
    if not discipline.empty and "regime" in discipline.columns and not discipline["regime"].dropna().empty:
        regime = str(discipline["regime"].dropna().astype(str).iloc[0])
    else:
        regime = fetch_regime().value

    note_url = load_note_url(output_dir)
    body = build_mail_body(screening, discipline, regime, note_url)

    config = load_gmail_config()
    if config is None:
        print("gmail_notification=skipped reason=missing_secrets required=GMAIL_USER,GMAIL_APP_PASSWORD,MAIL_TO")
        return

    note_md_path = output_dir / "note_daily.md"
    attachments = [note_md_path] if note_md_path.exists() else None
    send_gmail(build_subject(), body, config, attachments=attachments)
    print(f"gmail_notification=sent to={config.mail_to} subject={build_subject()} note_url={note_url or 'missing'}")


if __name__ == "__main__":
    main()
