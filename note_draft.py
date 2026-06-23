from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import escape
from datetime import datetime
from pathlib import Path

import pandas as pd

from scanner.highs import build_high_sections_markdown


PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "outputs"
NOTE_PATH = OUTPUT_DIR / "note_daily.md"
NOTE_TITLE_PATH = OUTPUT_DIR / "note_title.txt"
NOTE_HTML_PATH = OUTPUT_DIR / "note_daily.html"


@dataclass(frozen=True)
class SourceFiles:
    screening: Path
    discipline: Path
    backtest: Path | None


def latest_file(pattern: str) -> Path | None:
    paths = list(OUTPUT_DIR.glob(pattern))
    if not paths:
        return None
    return max(paths, key=lambda p: p.stat().st_mtime)


def preferred_backtest_report() -> Path | None:
    reports = list(OUTPUT_DIR.glob("backtest_report_*.json"))
    if not reports:
        return None

    def matches_current_rule(path: Path) -> bool:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        params = data.get("params", {})
        return (
            params.get("selection_rule", "current") == "current"
            and params.get("electric_volume_min") == 1.1
            and int(params.get("timeout_bdays", 0)) == 20
        )

    current = [p for p in reports if matches_current_rule(p)]
    if current:
        return max(current, key=lambda p: p.stat().st_mtime)
    return max(reports, key=lambda p: p.stat().st_mtime)


def load_sources() -> SourceFiles:
    screening = latest_file("screening_result_*.csv")
    discipline = latest_file("discipline_portfolio_*.csv")
    backtest = preferred_backtest_report()
    if screening is None:
        raise FileNotFoundError("screening_result_*.csv が見つかりません")
    if discipline is None:
        raise FileNotFoundError("discipline_portfolio_*.csv が見つかりません")
    return SourceFiles(screening=screening, discipline=discipline, backtest=backtest)


def load_screening(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "rank" not in df.columns:
        return df.iloc[0:0].copy()
    df = df.copy()
    df["rank"] = df["rank"].astype(str)
    df["score"] = pd.to_numeric(df.get("score"), errors="coerce")
    df["current_price"] = pd.to_numeric(df.get("current_price"), errors="coerce")
    df["dist_52w_high_pct"] = pd.to_numeric(df.get("dist_52w_high_pct"), errors="coerce")
    return df


def load_discipline(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    return df.copy()


def load_backtest(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def rank_sort_key(df: pd.DataFrame) -> pd.DataFrame:
    order = {"S": 0, "A": 1, "B": 2}
    out = df.copy()
    out["_rank_order"] = out["rank"].map(order).fillna(9)
    if "score" not in out.columns:
        out["score"] = pd.NA
    if "dist_52w_high_pct" not in out.columns:
        out["dist_52w_high_pct"] = pd.NA
    sort_cols = ["_rank_order", "score", "dist_52w_high_pct", "code"]
    ascending = [True, False, True, True]
    existing_cols = [c for c in sort_cols if c in out.columns]
    existing_asc = [ascending[sort_cols.index(c)] for c in existing_cols]
    out = out.sort_values(existing_cols, ascending=existing_asc)
    return out.drop(columns=["_rank_order"], errors="ignore")


def fmt_num(value, digits: int = 1) -> str:
    if pd.isna(value):
        return "未取得"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}"
    return str(value)


def safe_text(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "未取得"
    text = str(value).strip()
    return text if text else "未取得"


def summarize_discipline(df: pd.DataFrame) -> list[str]:
    lines: list[str] = []
    if df.empty:
        lines.append("- 300万円候補CSVは空です。")
        return lines

    action_counts = df.get("action", pd.Series(dtype=str)).astype(str).value_counts().to_dict()
    lines.append(f"- BUY: {int(action_counts.get('BUY', 0))}件")
    lines.append(f"- CASH: {int(action_counts.get('CASH', 0))}件")
    if "regime" in df.columns:
        regime = df["regime"].astype(str).dropna().head(1)
        if not regime.empty:
            lines.append(f"- 地合い: {regime.iloc[0]}")
    return lines


def top_buy_candidates(screening: pd.DataFrame, max_rows: int = 10) -> pd.DataFrame:
    if screening.empty:
        return screening.iloc[0:0].copy()
    candidate = screening[screening["rank"].astype(str).str.upper().isin(["S", "A", "B"])].copy()
    candidate = rank_sort_key(candidate)
    return candidate.head(max_rows)


def build_backtest_section(report: dict | None) -> list[str]:
    if report is None:
        return [
            "## バックテスト指標",
            "",
            "- PF: 未取得",
            "- DD: 未取得",
            "- 採用数: 未取得",
        ]

    metrics = report.get("metrics", {})
    return [
        "## バックテスト指標",
        "",
        f"- PF: {fmt_num(metrics.get('profit_factor'), 3)}",
        f"- DD: {fmt_num(metrics.get('max_drawdown_pct'), 2)}%",
        f"- 採用数: {int(metrics.get('n_trades', 0))}",
    ]


def build_candidates_table(df: pd.DataFrame, title: str, max_rows: int = 10) -> list[str]:
    lines = [title, ""]
    if df.empty:
        lines.append("- 該当なし")
        return lines

    headers = ["code", "name", "rank", "score", "current_price", "reason"]
    lines.append("| コード | 銘柄名 | ランク | スコア | 現在値 | 理由 |")
    lines.append("|---|---:|---:|---:|---:|---|")
    for _, row in df.head(max_rows).iterrows():
        lines.append(
            "| {code} | {name} | {rank} | {score} | {price} | {reason} |".format(
                code=safe_text(row.get("code")),
                name=safe_text(row.get("name")),
                rank=safe_text(row.get("rank")),
                score=safe_text(row.get("score")),
                price=safe_text(row.get("current_price")),
                reason=safe_text(row.get("reason")),
            )
        )
    return lines


def build_note_body(screening: pd.DataFrame, discipline: pd.DataFrame, backtest: dict | None, sources: SourceFiles) -> str:
    top10 = top_buy_candidates(screening, 10)
    today = datetime.now().strftime("%Y-%m-%d")

    lines: list[str] = []
    lines.extend([
        f"# 本日の300万円運用候補 {today}",
        "",
        "## 本日の300万円運用候補",
        "",
    ])
    lines.extend(summarize_discipline(discipline))
    high_lines = build_high_sections_markdown(screening, max_rows=5)
    if high_lines:
        lines.extend([""])
        lines.extend(high_lines)
    lines.extend([
        "",
        "## 買い候補TOP10",
        "",
    ])

    if top10.empty:
        lines.append("- 該当なし")
    else:
        lines.append("| コード | 銘柄名 | ランク | スコア | 現在値 | 理由 |")
        lines.append("|---|---:|---:|---:|---:|---|")
        for _, row in top10.iterrows():
            lines.append(
                "| {code} | {name} | {rank} | {score} | {price} | {reason} |".format(
                    code=safe_text(row.get("code")),
                    name=safe_text(row.get("name")),
                    rank=safe_text(row.get("rank")),
                    score=safe_text(row.get("score")),
                    price=safe_text(row.get("current_price")),
                    reason=safe_text(row.get("reason")),
                )
            )

    lines.extend([
        "",
        "## 各銘柄の理由",
        "",
    ])
    if top10.empty:
        lines.append("- 該当なし")
    else:
        for _, row in top10.iterrows():
            lines.append(f"- {safe_text(row.get('code'))} {safe_text(row.get('name'))}: {safe_text(row.get('reason'))}")

    lines.extend([
        "",
        "## 現在の本番ルール",
        "",
        "- electric_volume_min=1.1",
        "- selection_rule=current",
        "",
    ])
    lines.extend(build_backtest_section(backtest))

    lines.extend([
        "",
        "## 注意書き",
        "",
        "- これは投資助言ではありません。",
        "- 架空運用・検証目的のMarkdownです。",
        "",
        "## そのままnoteに貼れる文章",
        "",
        f"本日の300万円運用候補を整理しました。screening結果は `{sources.screening.name}`、規律版は `{sources.discipline.name}` を参照しています。",
        "",
        "候補はS/A/Bを優先し、現在の本番ルールは electric_volume_min=1.1 / selection_rule=current です。",
        "",
        "バックテスト指標は上記の通りです。実運用では地合いと決算確認を併せて判断してください。",
        "",
        "※これは投資助言ではなく、スクリーニング結果です。売買判断は自己責任で行ってください。",
    ])

    lines.append("")
    lines.append(f"source_screening={sources.screening}")
    lines.append(f"source_discipline={sources.discipline}")
    lines.append(f"source_backtest={sources.backtest if sources.backtest else '未取得'}")
    return "\n".join(lines)


def extract_note_title(note_markdown: str) -> str:
    for line in note_markdown.splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("# "):
            return text[2:].strip() or "note_daily"
        return text
    return "note_daily"


def _is_table_separator(line: str) -> bool:
    cleaned = line.strip()
    if not cleaned.startswith("|"):
        return False
    parts = [part.strip() for part in cleaned.strip("|").split("|")]
    return all(re.fullmatch(r"[:\-\s]+", part or "-") for part in parts)


def _split_table_row(line: str) -> list[str]:
    return [escape(part.strip()) for part in line.strip().strip("|").split("|")]


def render_markdown_html(title: str, note_markdown: str) -> str:
    body_lines = note_markdown.splitlines()
    if body_lines and body_lines[0].strip().startswith("# "):
        body_lines = body_lines[1:]

    html_lines = [
        "<!doctype html>",
        "<html lang=\"ja\">",
        "<head>",
        "<meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        f"<title>{escape(title)}</title>",
        "<style>",
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.6;max-width:980px;margin:24px auto;padding:0 16px;color:#111}",
        "h1,h2,h3{line-height:1.3}",
        "table{border-collapse:collapse;width:100%;margin:12px 0}",
        "th,td{border:1px solid #ccc;padding:6px 8px;vertical-align:top;text-align:left}",
        "ul{padding-left:1.4em}",
        "blockquote{margin:12px 0;padding:8px 12px;border-left:4px solid #ccc;background:#f8f8f8}",
        "code{background:#f2f2f2;padding:0 4px;border-radius:4px}",
        "</style>",
        "</head>",
        "<body>",
        f"<h1>{escape(title)}</h1>",
    ]

    i = 0
    while i < len(body_lines):
        line = body_lines[i].rstrip()
        stripped = line.strip()
        if not stripped:
            i += 1
            continue

        if stripped.startswith("### "):
            html_lines.append(f"<h3>{escape(stripped[4:].strip())}</h3>")
            i += 1
            continue
        if stripped.startswith("## "):
            html_lines.append(f"<h2>{escape(stripped[3:].strip())}</h2>")
            i += 1
            continue
        if stripped.startswith("# "):
            html_lines.append(f"<h2>{escape(stripped[2:].strip())}</h2>")
            i += 1
            continue
        if stripped == "---":
            html_lines.append("<hr>")
            i += 1
            continue
        if stripped.startswith("|") and "|" in stripped[1:]:
            table_lines = [stripped]
            i += 1
            while i < len(body_lines):
                nxt = body_lines[i].rstrip()
                if not nxt.strip():
                    break
                if not nxt.strip().startswith("|"):
                    break
                table_lines.append(nxt.strip())
                i += 1
            headers = _split_table_row(table_lines[0])
            rows_start = 1
            if len(table_lines) > 1 and _is_table_separator(table_lines[1]):
                rows_start = 2
            html_lines.append("<table>")
            if headers:
                html_lines.append("<thead><tr>" + "".join(f"<th>{cell}</th>" for cell in headers) + "</tr></thead>")
            body_rows = table_lines[rows_start:]
            if body_rows:
                html_lines.append("<tbody>")
                for row_line in body_rows:
                    cells = _split_table_row(row_line)
                    html_lines.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
                html_lines.append("</tbody>")
            html_lines.append("</table>")
            continue
        if stripped.startswith("- "):
            items = []
            while i < len(body_lines):
                cur = body_lines[i].strip()
                if not cur.startswith("- "):
                    break
                items.append(cur[2:].strip())
                i += 1
            html_lines.append("<ul>")
            html_lines.extend(f"<li>{escape(item)}</li>" for item in items)
            html_lines.append("</ul>")
            continue
        if stripped.startswith("■ "):
            html_lines.append(f"<p><strong>{escape(stripped)}</strong></p>")
            i += 1
            continue

        paragraph: list[str] = []
        while i < len(body_lines):
            cur = body_lines[i].rstrip()
            cur_stripped = cur.strip()
            if not cur_stripped:
                break
            if cur_stripped.startswith(("# ", "## ", "### ", "- ", "■ ", "|", "---")):
                break
            paragraph.append(escape(cur_stripped))
            i += 1
        if paragraph:
            html_lines.append("<p>" + "<br>".join(paragraph) + "</p>")
            continue
        i += 1

    html_lines.extend(["</body>", "</html>"])
    return "\n".join(html_lines)


def write_note_outputs(note_markdown: str) -> tuple[Path, Path, Path]:
    title = extract_note_title(note_markdown)
    NOTE_PATH.write_text(note_markdown, encoding="utf-8")
    NOTE_TITLE_PATH.write_text(title + "\n", encoding="utf-8")
    NOTE_HTML_PATH.write_text(render_markdown_html(title, note_markdown), encoding="utf-8")
    return NOTE_PATH, NOTE_TITLE_PATH, NOTE_HTML_PATH


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    sources = load_sources()
    screening = load_screening(sources.screening)
    discipline = load_discipline(sources.discipline)
    backtest = load_backtest(sources.backtest)
    note = build_note_body(screening, discipline, backtest, sources)
    note_path, title_path, html_path = write_note_outputs(note)
    print(f"saved={NOTE_PATH}")
    print(f"saved={title_path}")
    print(f"saved={html_path}")
    print(f"screening={sources.screening}")
    print(f"discipline={sources.discipline}")
    print(f"backtest={sources.backtest if sources.backtest else '未取得'}")


if __name__ == "__main__":
    main()
