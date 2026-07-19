from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from html import escape
from datetime import datetime
from pathlib import Path

import pandas as pd

from scanner.highs import build_high_sections_markdown
from scanner.openwork import add_openwork_scores, format_openwork_score
from scanner.prices import fetch_next_earnings_date
from paper_open_fill import portfolio_view_for_note


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
    return add_openwork_scores(df)


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
    lines.append("| コード | 銘柄名 | ランク | スコア | 現在値 | OpenWork | 理由 |")
    lines.append("|---|---:|---:|---:|---:|---:|---|")
    for _, row in df.head(max_rows).iterrows():
        lines.append(
            "| {code} | {name} | {rank} | {score} | {price} | {openwork} | {reason} |".format(
                code=safe_text(row.get("code")),
                name=safe_text(row.get("name")),
                rank=safe_text(row.get("rank")),
                score=safe_text(row.get("score")),
                price=safe_text(row.get("current_price")),
                openwork=format_openwork_score(row.get("openwork_score")),
                reason=safe_text(row.get("reason")),
            )
        )
    return lines


def build_note_body(screening: pd.DataFrame, discipline: pd.DataFrame, backtest: dict | None, sources: SourceFiles) -> str:
    discipline = portfolio_view_for_note(discipline, screening)
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
        lines.append("| コード | 銘柄名 | ランク | スコア | 現在値 | OpenWork | 理由 |")
        lines.append("|---|---|---:|---:|---:|---:|---|")
        for _, row in top10.iterrows():
            lines.append(
                "| {code} | {name} | {rank} | {score} | {price} | {openwork} | {reason} |".format(
                    code=safe_text(row.get("code")),
                    name=safe_text(row.get("name")),
                    rank=safe_text(row.get("rank")),
                    score=safe_text(row.get("score")),
                    price=safe_text(row.get("current_price")),
                    openwork=(
                        safe_text(row.get("openwork_score"))
                        if not str(row.get("openwork_score")).lower() in ("", "nan", "none", "<na>")
                        else "未取得"
                    ),
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
            lines.append(f"- {safe_text(row.get('code'))} {safe_text(row.get('name'))}: OpenWork: {format_openwork_score(row.get('openwork_score'))} / {safe_text(row.get('reason'))}")

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


def render_inline_markdown(text: str) -> str:
    """Escape text and render simple Markdown links for note HTML previews."""
    raw = str(text)
    pattern = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
    out: list[str] = []
    last = 0
    for match in pattern.finditer(raw):
        out.append(escape(raw[last:match.start()]))
        label = escape(match.group(1))
        url = escape(match.group(2), quote=True)
        out.append(f'<a href="{url}" target="_blank" rel="noopener noreferrer">{label}</a>')
        last = match.end()
    out.append(escape(raw[last:]))
    return "".join(out)


def _split_table_row(line: str) -> list[str]:
    return [render_inline_markdown(part.strip()) for part in line.strip().strip("|").split("|")]


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
        # chart_image 等のHTMLコメントマーカーはプレビューに出さない（autosaveが別途処理）
        if stripped.startswith("<!--") and stripped.endswith("-->"):
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
            html_lines.extend(f"<li>{render_inline_markdown(item)}</li>" for item in items)
            html_lines.append("</ul>")
            continue
        if stripped.startswith("■ "):
            html_lines.append(f"<p><strong>{render_inline_markdown(stripped)}</strong></p>")
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
            paragraph.append(render_inline_markdown(cur_stripped))
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


# ============================================================================
# T-E(2026-06-28): Note 4本分割
#   ① ChatGPTが300万円運用 ② Claudeが300万円運用
#   ③ 52週新高値後の押し目候補(リテスト/25MA/200MA/240MAタッチ)
#   ④ 52週新高値タッチ・接近銘柄
# 既存の note_daily.* は後方互換のため残す（健全性チェック・CIが参照）。
# データが無いバケットは必ず「該当なし」（捏造・空想Noteは作らない）。
# ============================================================================

NOTE4_TITLES = {
    "chatgpt": "ChatGPTが300万円運用｜本日のAI売買候補",
    "claude": "Claudeが300万円運用｜本日のAI売買候補",
    "pullback": "52週新高値後の押し目候補｜新高値ライン戻り・25MA・200MA・240MAタッチ銘柄",
    "highs": "52週新高値タッチ・接近銘柄｜本日の高値更新候補",
}
NOTE4_MANIFEST_PATH = OUTPUT_DIR / "note_drafts_manifest.json"
NOTE4_VALID_REGIMES = ("NORMAL", "CAUTION", "RISK", "STOP")


def _market_status_block() -> list[str]:
    """4本すべての冒頭に入れる市場ステータス。空欄では絶対に返さない。

    優先1: outputs/market_snapshot.json（fetch_market.py が regime + 指標判定を書く）
    優先2: market_regime.fetch_regime()（raw regime.txt → ローカル regime.txt → 安全側STOP）
    """
    regime_value = ""
    source = ""
    note = ""
    indicator_regime = ""
    snap = OUTPUT_DIR / "market_snapshot.json"
    if snap.exists():
        try:
            data = json.loads(snap.read_text(encoding="utf-8"))
            regime_value = str(data.get("regime") or "").strip().upper()
            source = str(data.get("regime_source") or data.get("source") or "market_snapshot.json")
            note = str(data.get("regime_note") or data.get("note") or "").strip()
            indicator_regime = str(data.get("indicator_regime") or "").strip().upper()
        except (json.JSONDecodeError, OSError):
            pass
    if regime_value not in NOTE4_VALID_REGIMES:
        from market_regime import fetch_regime

        regime = fetch_regime()
        regime_value, source, note = regime.value, regime.source, regime.note
    lines = ["## 市場ステータス", "", f"- 本日の地合い: **{regime_value}**"]
    if indicator_regime in NOTE4_VALID_REGIMES:
        lines.append(f"- 指標ベース判定: {indicator_regime}")
    if note:
        lines.append(f"- 補足: {note}")
    lines.append(f"- 判定元: {source}")
    lines.append("")
    return lines


def _insert_market_status(note_markdown: str, status_lines: list[str]) -> str:
    """タイトル(# ...)の直後に市場ステータスを挿入する。タイトルが無ければ先頭に。"""
    lines = note_markdown.splitlines()
    insert_at = 1 if lines and lines[0].startswith("# ") else 0
    return "\n".join(lines[:insert_at] + [""] + status_lines + lines[insert_at:])

# 各noteの代表銘柄コード（chart_images.SPECS と対応）。
# チャートPNGは outputs/charts_YYYYMMDD/chart_<key>_<code>.png。
# note_autosave がこの相対パスを読み、note.com本文の冒頭へ画像を挿入する。
NOTE4_CHART_CODES = {
    "chatgpt": "7173",
    "claude": "8524",
    "pullback": "7011",
    "highs": "6951",
}


def chart_rel_path(key: str) -> str | None:
    """key に対応する本日のチャートPNGの相対パス（リポジトリ基準）。無ければ None。"""
    code = NOTE4_CHART_CODES.get(key)
    if not code:
        return None
    day = datetime.now().strftime("%Y%m%d")
    return f"outputs/charts_{day}/chart_{key}_{code}.png"


def inject_chart_marker(note_markdown: str, chart_rel: str | None) -> str:
    """タイトル直下に画像挿入マーカーを差し込む（本文の見た目は崩さない＝HTML側はコメントを無視）。
    例: <!-- chart_image: outputs/charts_20260628/chart_chatgpt_7173.png -->"""
    if not chart_rel:
        return note_markdown
    lines = note_markdown.splitlines()
    marker = f"<!-- chart_image: {chart_rel} -->"
    if any(marker in ln for ln in lines):
        return note_markdown
    # 先頭の見出し(# ...)の直後に入れる。見出しが無ければ先頭に。
    insert_at = 0
    for idx, ln in enumerate(lines):
        if ln.strip().startswith("# "):
            insert_at = idx + 1
            break
    lines.insert(insert_at, marker)
    lines.insert(insert_at, "")  # 見出しとマーカーの間に空行
    return "\n".join(lines)


def _val(row, key, digits: int | None = None) -> str:
    value = row.get(key)
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "未取得"
    if digits is not None:
        try:
            return f"{float(value):.{digits}f}"
        except (TypeError, ValueError):
            return str(value)
    text = str(value).strip()
    return text if text else "未取得"



def _is_missing(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text == "" or text.lower() in {"nan", "none", "<na>", "nat"}


def _first_value(row, keys: tuple[str, ...]):
    for key in keys:
        if key in row and not _is_missing(row.get(key)):
            return row.get(key)
    return None


def _fmt_number(value, digits: int = 1) -> str:
    if _is_missing(value):
        return "未取得"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return safe_text(value)


def _fmt_pct(value, digits: int = 1, signed: bool = False) -> str:
    if _is_missing(value):
        return "未取得"
    try:
        num = float(value)
        sign = "+" if signed and num > 0 else ""
        return f"{sign}{num:.{digits}f}%"
    except (TypeError, ValueError):
        return safe_text(value)


def _fmt_yen(value) -> str:
    if _is_missing(value):
        return "未取得"
    try:
        return f"{float(value):,.1f}円"
    except (TypeError, ValueError):
        return f"{safe_text(value)}円"


def _fmt_oku(value) -> str:
    if _is_missing(value):
        return "未取得"
    try:
        num = float(value)
        oku = num / 100_000_000 if abs(num) >= 10_000 else num
        return f"{oku:,.1f}億円"
    except (TypeError, ValueError):
        return safe_text(value)


def _fmt_market_cap(value) -> str:
    return _fmt_oku(value)


def _code_text(row) -> str:
    code = safe_text(row.get("code"))
    return code[:-2] if code.endswith(".0") else code


def _chart_url(code: str) -> str:
    return f"https://finance.yahoo.co.jp/quote/{code}.T/chart"


def _business_days_until(date_text: str) -> int | None:
    if _is_missing(date_text) or date_text == "未取得":
        return None
    try:
        target = pd.to_datetime(date_text).date()
    except Exception:
        return None
    today = pd.Timestamp(datetime.now().date())
    target_ts = pd.Timestamp(target)
    if target_ts < today:
        return None
    return max(len(pd.bdate_range(today, target_ts)) - 1, 0)


@lru_cache(maxsize=512)
def _fetch_earnings_safe(code: str):
    # note生成をネットワーク失敗で止めない。明示時だけYahoo Financeへ取得を試す。
    if os.environ.get("NOTE_FETCH_EARNINGS", "0").lower() not in {"1", "true", "yes"}:
        return None
    try:
        return fetch_next_earnings_date(f"{code}.T")
    except Exception:
        return None


def _format_earnings_date(row, code: str) -> str:
    value = _first_value(row, ("earnings_date", "next_earnings_date", "決算予定日"))
    if _is_missing(value):
        value = _fetch_earnings_safe(code)
    text = "未取得" if _is_missing(value) else safe_text(value)
    days = _business_days_until(text)
    if days is not None and days <= 7:
        return f"{text} ⚠️ 決算接近"
    return text


def _enrich_openwork(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    try:
        return add_openwork_scores(df)
    except Exception:
        out = df.copy()
        if "openwork_score" not in out.columns:
            out["openwork_score"] = pd.NA
        return out


def _editor_comment(row) -> str:
    """編集者: 取得済みの事実データだけから読者向けの一言コメントを組み立てる。
    数値・材料の捏造はしない。使える事実が無ければ空文字を返す。"""
    parts: list[str] = []
    vr = _first_value(row, ("volume_ratio_5d_20d", "volume_ratio", "出来高倍率"))
    if not _is_missing(vr):
        try:
            v = float(vr)
            if v >= 3:
                parts.append(f"出来高が平常時の{v:.1f}倍に膨らみ、資金が集中")
            elif v >= 1.5:
                parts.append(f"出来高{v:.1f}倍と商いを伴う動き")
        except (TypeError, ValueError):
            pass
    dist = _first_value(row, ("dist_to_high_pct", "dist_52w_high_pct", "retest_dist_pct"))
    if not _is_missing(dist):
        try:
            d = abs(float(dist))
            if d < 0.5:
                parts.append("52週高値の目前")
            elif d <= 3:
                parts.append(f"52週高値まであと{d:.1f}%")
        except (TypeError, ValueError):
            pass
    roe = _first_value(row, ("roe", "ROE"))
    if not _is_missing(roe):
        try:
            r = float(roe)
            r = r * 100 if r < 1 else r
            if r >= 15:
                parts.append(f"ROE{r:.0f}%と資本効率も高い")
        except (TypeError, ValueError):
            pass
    growth = _first_value(row, ("profit_growth", "earnings_growth", "利益成長率"))
    if not _is_missing(growth):
        try:
            g = float(growth)
            g = g * 100 if abs(g) < 1 else g
            if g >= 20:
                parts.append(f"利益成長+{g:.0f}%と業績も追い風")
        except (TypeError, ValueError):
            pass
    if not parts:
        return ""
    return "、".join(parts[:3]) + "。"


def build_stock_cards(df: pd.DataFrame, max_rows: int | None = None) -> list[str]:
    """銘柄をnote向けカード型紹介にする。取得できない項目は未取得で続行。"""
    if df.empty:
        return ["- 該当なし"]
    data = _enrich_openwork(df)
    if max_rows is not None:
        data = data.head(max_rows)
    lines: list[str] = []
    for _, row in data.iterrows():
        code = _code_text(row)
        name = safe_text(row.get("name"))
        price = _first_value(row, ("current_price", "entry_price", "close", "Close"))
        volume_ratio = _first_value(row, ("volume_ratio_5d_20d", "volume_ratio", "出来高倍率"))
        turnover = _first_value(row, ("turnover_20d", "turnover", "売買代金"))
        change_pct = _first_value(row, ("change_pct", "day_change_pct", "prev_change_pct", "前日比"))
        range_pct = _first_value(row, ("range_pct", "intraday_range_pct", "値幅"))
        per = _first_value(row, ("per", "PER"))
        forward_per = _first_value(row, ("forward_per", "予想PER"))
        pbr = _first_value(row, ("pbr", "PBR"))
        dividend = _first_value(row, ("dividend_yield", "配当利回り"))
        roe = _first_value(row, ("roe", "ROE"))
        op_margin = _first_value(row, ("operating_margin", "営業利益率"))
        net_margin = _first_value(row, ("net_margin", "profit_margin", "純利益率"))
        sales_growth = _first_value(row, ("sales_growth", "revenue_growth", "売上成長率"))
        profit_growth = _first_value(row, ("profit_growth", "earnings_growth", "利益成長率"))
        market_cap = _first_value(row, ("market_cap", "時価総額"))
        sector = _first_value(row, ("sector", "業種", "セクター"))
        openwork = format_openwork_score(row.get("openwork_score"))
        earnings = _format_earnings_date(row, code)
        vr = _fmt_number(volume_ratio, 2) if not _is_missing(volume_ratio) else "未取得"
        lines.extend([
            f"{code} {name} ⚡出来高{vr}倍",
            f"現在値: {_fmt_yen(price)} / 前日比: {_fmt_pct(change_pct, signed=True)}",
            f"売買代金: {_fmt_oku(turnover)} / 出来高倍率: {vr}x / 値幅: {_fmt_pct(range_pct)}",
            f"📊 PER {_fmt_number(per, 1)} / 予想PER {_fmt_number(forward_per, 1)} / PBR {_fmt_number(pbr, 2)} / 配当 {_fmt_pct(dividend, 2)}",
            f"💪 ROE {_fmt_pct(roe, 1)} / 営業利益率 {_fmt_pct(op_margin, 1)} / 純利益率 {_fmt_pct(net_margin, 1)}",
            f"🚀 売上 {_fmt_pct(sales_growth, 1, signed=True)} (前年比) / 利益 {_fmt_pct(profit_growth, 1, signed=True)} (前年比)",
            f"🗓 決算予定日: {earnings}",
            f"👥 OpenWork評価: {openwork if openwork != '未取得' else '未取得'}",
            f"🏢 時価総額 {_fmt_market_cap(market_cap)} / セクター: {safe_text(sector)}",
            f"[📈 チャートを見る]({_chart_url(code)})",
        ])
        comment = _editor_comment(row)
        if comment:
            lines.append(f"💬 {comment}")
        lines.append("")
    return lines


def latest_aux(prefix: str) -> Path | None:
    return latest_file(f"{prefix}_*.csv")


def load_aux(path: Path | None) -> pd.DataFrame:
    if path is None or not path.exists():
        return pd.DataFrame()
    try:
        return _enrich_openwork(pd.read_csv(path))
    except Exception:
        return pd.DataFrame()


def build_chatgpt_note(discipline: pd.DataFrame, screening: pd.DataFrame, sources: SourceFiles) -> str:
    """①ChatGPT版。Claudeは意見を書かず、両AI共通の事実データ(300万保有・候補)だけを枠として用意する。
    本文の相場観・売買コメントは高重さんがChatGPTに書かせる（Claudeが捏造しない）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    portfolio = portfolio_view_for_note(discipline, screening)
    lines = [f"# {NOTE4_TITLES['chatgpt']} {today}", ""]
    lines.append("> この記事の相場コメントはChatGPTが執筆します。以下はClaude側スクリーニングの共通素材データ（事実）です。")
    lines.append("")
    lines.append("## 本日の300万円運用（規律版データ）")
    lines.append("")
    lines.extend(summarize_discipline(portfolio))
    lines.append("")
    lines.extend(_portfolio_status_block(portfolio))
    buys = portfolio[portfolio.get("action", pd.Series(dtype=str)).astype(str).str.upper() == "BUY"] if not portfolio.empty else portfolio
    lines.extend(["## 300万円運用BUY候補カード", ""])
    lines.extend(build_stock_cards(buys, None))
    lines.extend(["", "## 300万円運用BUY候補（表）", ""])
    lines.extend(_discipline_holdings_table(portfolio))
    lines.extend(["", "## 買い候補TOP10カード（共通素材）", ""])
    lines.extend(build_stock_cards(top_buy_candidates(screening, 10), 10))
    lines.extend(["", "## 買い候補TOP10（表）", ""])
    lines.extend(_top10_block(screening))
    lines.extend([
        "",
        "## 注意書き",
        "",
        "- これは投資助言ではありません。スクリーニング結果（事実）です。",
        f"- source_screening={sources.screening.name} / source_discipline={sources.discipline.name}",
    ])
    return "\n".join(lines)


def build_claude_note(screening: pd.DataFrame, discipline: pd.DataFrame, backtest: dict | None, sources: SourceFiles) -> str:
    """②Claude版。既存の本文をそのまま使い、タイトルだけ4本構成に合わせる。"""
    portfolio = portfolio_view_for_note(discipline, screening)
    body = build_note_body(screening, portfolio, backtest, sources)
    out_lines = body.splitlines()
    if out_lines and out_lines[0].startswith("# "):
        today = datetime.now().strftime("%Y-%m-%d")
        out_lines[0] = f"# {NOTE4_TITLES['claude']} {today}"
    if not any("Claude候補TOP10カード" in line for line in out_lines):
        card_lines = ["", "## Claude候補TOP10カード", ""]
        card_lines.extend(build_stock_cards(top_buy_candidates(screening, 10), 10))
        out_lines[1:1] = card_lines
    if not any(line.startswith(PORTFOLIO_SECTION_HOLDINGS) for line in out_lines):
        out_lines.append("")
        out_lines.extend(_portfolio_status_block(portfolio))
    return "\n".join(out_lines)


def _discipline_holdings_table(discipline: pd.DataFrame) -> list[str]:
    if discipline.empty:
        return ["- 300万円候補データなし"]
    buys = discipline[discipline.get("action", pd.Series(dtype=str)).astype(str).str.upper() == "BUY"]
    if buys.empty:
        return ["- 本日は新規買い建てなし（現金保有）"]
    lines = ["| 枠 | コード | 銘柄 | ランク | 株数 | 取得価格 | 投資額 |", "|---|---|---|---:|---:|---:|---:|"]
    for _, row in buys.iterrows():
        lines.append(
            f"| {_val(row,'slot')} | {_val(row,'code')} | {_val(row,'name')} | {_val(row,'rank')} | "
            f"{_val(row,'shares')} | {_val(row,'entry_price')} | {_val(row,'position_value')} |"
        )
    return lines


def _top10_block(screening: pd.DataFrame) -> list[str]:
    top10 = top_buy_candidates(screening, 10)
    if top10.empty:
        return ["- 該当なし"]
    lines = ["| コード | 銘柄名 | ランク | スコア | 現在値 | 理由 |", "|---|---|---:|---:|---:|---|"]
    for _, row in top10.iterrows():
        lines.append(
            f"| {safe_text(row.get('code'))} | {safe_text(row.get('name'))} | {safe_text(row.get('rank'))} | "
            f"{safe_text(row.get('score'))} | {safe_text(row.get('current_price'))} | {safe_text(row.get('reason'))} |"
        )
    return lines


PORTFOLIO_CAPITAL = 3_000_000  # paper_portfolio_discipline.CAPITAL と同値（表示用）

# 品質ゲート（validate_note_artifact.py）が確認する必須セクション見出し
PORTFOLIO_SECTION_HOLDINGS = "## 保有銘柄・CASH判断"
PORTFOLIO_SECTION_REASONS = "## 売買理由"
PORTFOLIO_SECTION_VALUATION = "## 評価額・現金比率"
PORTFOLIO_SECTION_PNL = "## 損益（未実現損益）"
PORTFOLIO_SECTION_NEXT_DAY = "## 次営業日の方針"


def _portfolio_status_block(discipline: pd.DataFrame) -> list[str]:
    """300万円運用の運用状況セクション（ChatGPT版・Claude版の両方に共通で入れる事実データ）。
    discipline CSVから機械的に作れる事実のみ記載し、無いものは「データ不足」と明記する。"""
    lines: list[str] = []
    empty = discipline is None or discipline.empty
    buys = (
        discipline[discipline.get("action", pd.Series(dtype=str)).astype(str).str.upper() == "BUY"]
        if not empty else pd.DataFrame()
    )
    cashes = (
        discipline[discipline.get("action", pd.Series(dtype=str)).astype(str).str.upper() == "CASH"]
        if not empty else pd.DataFrame()
    )

    # ① 保有銘柄・CASH判断
    lines.extend([PORTFOLIO_SECTION_HOLDINGS, ""])
    if empty:
        lines.append("- データ不足：discipline CSVが未生成または空のため、保有/CASH判断を表示できません。")
    elif buys.empty:
        lines.append(f"- 本日は新規買いなし → CASH判断（現金維持 / CASH枠 {len(cashes)}件）")
    else:
        for _, row in buys.iterrows():
            current = _val(row, "current_price")
            current_text = f" / 現在値 {current}円" if current not in ("", "未取得") else ""
            lines.append(
                f"- 枠{_val(row,'slot')}: {_val(row,'code')} {_val(row,'name')} "
                f"{_val(row,'shares')}株 @ {_val(row,'entry_price')}円（投資額 {_val(row,'position_value')}円{current_text}）"
            )
        if not cashes.empty:
            lines.append(f"- 残り {len(cashes)}枠はCASH（現金）")
    lines.append("")

    # ② 売買理由
    lines.extend([PORTFOLIO_SECTION_REASONS, ""])
    if empty:
        lines.append("- データ不足：discipline CSVが未生成のため、売買理由を表示できません。")
    else:
        wrote = False
        for _, row in buys.iterrows():
            rule = _val(row, "rule")
            if rule and rule != "未取得":
                lines.append(f"- BUY {_val(row,'code')} {_val(row,'name')}: {rule}")
                wrote = True
        cash_reasons = [
            _val(row, "cash_reason") for _, row in cashes.iterrows()
            if _val(row, "cash_reason") not in ("", "未取得")
        ]
        for reason in dict.fromkeys(cash_reasons):  # 重複除去・順序維持
            lines.append(f"- CASH: {reason}")
            wrote = True
        if not wrote:
            lines.append("- データ不足：rule / cash_reason 列が未出力のため、売買理由を表示できません。")
    lines.append("")

    # ③ 評価額・現金比率
    lines.extend([PORTFOLIO_SECTION_VALUATION, ""])
    invested = None
    if not empty and "position_value" in discipline.columns:
        invested = int(pd.to_numeric(discipline["position_value"], errors="coerce").fillna(0).sum())
    market_value = None
    if not empty and "market_value" in discipline.columns:
        market_value = int(pd.to_numeric(discipline["market_value"], errors="coerce").fillna(0).sum())
    if invested is None:
        lines.append("- データ不足：position_value 列が未出力のため、評価額・現金比率を算出できません。")
    else:
        cash = PORTFOLIO_CAPITAL - invested
        cash_pct = cash / PORTFOLIO_CAPITAL * 100
        lines.append(f"- 運用資金: {PORTFOLIO_CAPITAL:,}円")
        lines.append(f"- 投資額合計（寄り付き約定）: {invested:,}円")
        lines.append(f"- 現金: {cash:,}円（現金比率 {cash_pct:.1f}%）")
        if market_value is not None and market_value > 0:
            total_value = cash + market_value
            lines.append(f"- 保有評価額: {market_value:,}円")
            lines.append(f"- 運用総額: {total_value:,}円")
        else:
            lines.append(f"- 想定評価額: {PORTFOLIO_CAPITAL:,}円（寄り付き約定直後の取得額ベース）")
    lines.append("")

    # ④ 損益（未実現損益）
    lines.extend([PORTFOLIO_SECTION_PNL, ""])
    pnl_col = next((c for c in ("unrealized_pnl", "pnl", "profit_loss") if not empty and c in discipline.columns), None)
    if pnl_col:
        pnl = pd.to_numeric(discipline[pnl_col], errors="coerce").fillna(0).sum()
        invested_for_pct = pd.to_numeric(discipline.get("position_value", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()
        pct = pnl / invested_for_pct * 100 if invested_for_pct else 0
        lines.append(f"- 未実現損益合計: {pnl:+,.0f}円（{pct:+.2f}%）")
    elif not empty and not buys.empty:
        lines.append("- 未実現損益: 0円（寄り付き約定直後のため）")
        lines.append("- データ不足：現値ベースの未実現損益列（unrealized_pnl）は本CSVに未出力です。クラウドの寄り付き記録と終値データで更新します。")
    else:
        lines.append("- 未実現損益: 0円（保有なし・現金のみ）")
    lines.append("")

    # ⑤ 次営業日の方針（paper_portfolio_discipline.py の規律ルールをそのまま記載。新規判断は書かない）
    lines.extend([PORTFOLIO_SECTION_NEXT_DAY, ""])
    regime = ""
    if not empty and "regime" in discipline.columns:
        vals = discipline["regime"].astype(str).replace("nan", "").tolist()
        regime = next((v for v in vals if v), "")
    next_day_policy = {
        "NORMAL": "地合いNORMAL: 規律どおりSランク上位を最大3銘柄・1枠100万円で買付（損切-7% / 利確+15% / 10営業日タイムアウト）。",
        "CAUTION": "地合いCAUTION: 新規買いは最大1銘柄に制限。既存保有は損切・利確ルールを継続。",
        "RISK": "地合いRISK: 新規買い停止・現金維持。既存保有は損切・利確ルールで手仕舞いのみ。",
        "STOP": "地合いSTOP: 新規買い停止・現金維持。",
    }
    if regime in next_day_policy:
        lines.append(f"- {next_day_policy[regime]}")
        lines.append("- 翌朝の regime.txt / 市場ステータスが変わった場合はそちらを優先。")
    else:
        lines.append("- データ不足：regime 列が未出力のため、次営業日の規律方針を確定できません（安全側＝新規買い見送り）。")
    lines.append("")
    return lines


def build_pullback_note(pullback: pd.DataFrame, source: Path | None) -> str:
    """③押し目候補。4バケット: 52週新高値リテスト / 25MAタッチ / 200MAタッチ / 240MAタッチ。
    データが無いバケットは「該当なし」。空想は作らない。"""
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# {NOTE4_TITLES['pullback']} {today}", ""]

    def bucket(df: pd.DataFrame, flag: str) -> pd.DataFrame:
        if df.empty or flag not in df.columns:
            return df.iloc[0:0] if not df.empty else df
        mask = df[flag].astype(str).str.lower().isin(["true", "1", "1.0"])
        return df[mask]

    rt_cnt = len(bucket(pullback, "retest_52w"))
    ma_cnt = len(bucket(pullback, "ma25_touch")) + len(bucket(pullback, "ma200_touch")) + len(bucket(pullback, "ma240_touch"))
    # 編集者: リード文（事実の件数だけで組み立てる。相場観の捏造はしない）
    lines.append(
        f"52週新高値をブレイクした後、ラインまで戻ってきた「リテスト」候補が**{rt_cnt}銘柄**、"
        f"上昇トレンド（移動平均線が右肩上がり）のまま25/200/240日線にタッチした押し目候補が**{ma_cnt}銘柄**です。"
    )
    lines.append(
        "強い銘柄を高値で追いかけるのではなく、**強い銘柄が休んだところ**を狙うのがこの記事のテーマです。"
        "移動平均線が上向きのままのタッチだけを拾うので、下落トレンドの「落ちるナイフ」は含みません。"
    )
    lines.append("")
    if source is None or pullback.empty:
        lines.append("> データ不足：本日の押し目スクリーニング出力（screening_pullback）が未生成または空のため、候補を表示できません。下書きは規定どおり生成しています。")
        lines.append("")

    # ①52週新高値リテスト
    lines.append("## 【52週新高値後リテスト】")
    lines.append("")
    rt = bucket(pullback, "retest_52w")
    if rt.empty:
        lines.append("- 該当なし")
    else:
        lines.append("### カード型候補（上位10件）")
        lines.append("")
        lines.extend(build_stock_cards(rt, 10))
        lines.append("### 従来表")
        lines.append("")
        lines.append("| コード | 銘柄 | 現在値 | 新高値ライン | ブレイク日 | ライン乖離% | 直近天井 | 売買代金 |")
        lines.append("|---|---|---:|---:|---|---:|---:|---:|")
        for _, row in rt.iterrows():
            lines.append(
                f"| {_val(row,'code')} | {_val(row,'name')} | {_val(row,'current_price')} | "
                f"{_val(row,'retest_line_price')} | {_val(row,'retest_breakout_date')} | "
                f"{_val(row,'retest_dist_pct')} | {_val(row,'retest_post_high')} | {_val(row,'turnover_20d')} |"
            )
    lines.append("")

    # ②③④ 25/200/240MAタッチ
    for flag, title in (("ma25_touch", "25MAタッチ"), ("ma200_touch", "200MAタッチ"), ("ma240_touch", "240MAタッチ")):
        lines.append(f"## 【{title}】")
        lines.append("")
        b = bucket(pullback, flag)
        if b.empty:
            lines.append("- 該当なし")
        else:
            lines.append("### カード型候補（上位10件）")
            lines.append("")
            lines.extend(build_stock_cards(b, 10))
            lines.append("### 従来表")
            lines.append("")
            lines.append("| コード | 銘柄 | 現在値 | MA25 | MA200 | MA240 | 52週高値乖離% | 売買代金 |")
            lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
            for _, row in b.iterrows():
                lines.append(
                    f"| {_val(row,'code')} | {_val(row,'name')} | {_val(row,'current_price')} | "
                    f"{_val(row,'ma25')} | {_val(row,'ma200')} | {_val(row,'ma240')} | "
                    f"{_val(row,'dist_52w_high_pct')} | {_val(row,'turnover_20d')} |"
                )
        lines.append("")

    # 編集者: 締め（読者の次の行動につなげる）
    lines.append("## おわりに")
    lines.append("")
    lines.append("- このリストは毎営業日、**同じ基準で機械的に**抽出しています。裁量で候補を足したり引いたりしません。")
    lines.append("- どの銘柄が新高値を付けたのかは、姉妹記事「52週新高値」で毎日確認できます。")
    lines.append("- フォローしておくと毎日の更新を見逃しません。")
    lines.append("")
    lines.append("## 注意書き")
    lines.append("")
    lines.append("- これは投資助言ではありません。スクリーニング結果（事実）です。")
    lines.append(f"- source={source.name if source else '未生成（Mac実行待ち）'}")
    return "\n".join(lines)


def _flag_true(value: object) -> bool:
    """CSV経由でTrue/Falseが文字列化されても真偽を正しく判定する。"""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _split_flagged_highs(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """イナゴ疑い・TOB疑いの銘柄をカード候補から分離する（従来表には全件残す）。"""
    if df.empty:
        return df, df.iloc[0:0]
    flagged_mask = pd.Series(False, index=df.index)
    for column in ("inago_suspect", "tob_suspect"):
        if column in df.columns:
            flagged_mask |= df[column].map(_flag_true)
    return df[~flagged_mask], df[flagged_mask]


# ============================================================================
# T-K(2026-07-12): note1本目「52週新高値 接近・到達銘柄」全面改修
#   - タイトル: 「YYYY年M月D日 52週新高値 接近・到達銘柄」（対象取引日ベース・JST）
#   - 冒頭: 相場観（取得済み指標の事実のみ）→ セクター総評（自データ集計）→ 導線
#   - A: 本日到達 / B: 3%以内接近 / C: 参考掲載（イナゴ・TOB・データ異常・連日更新）
#   - NaN/None/null は本文に出さない（行非表示 or 「取得できず」）。捏造禁止。
#   - OpenWorkは data/openwork_cache.csv のみ参照（日次で通信しない）。
# ============================================================================

_HIGHS_TITLE_SUFFIX = "52週新高値 接近・到達銘柄"


def _jp_date_text(d) -> str:
    return f"{d.year}年{d.month}月{d.day}日"


def _prev_jst_business_day():
    """JSTの直近JPX営業日（土日・日本の祝日・年末年始12/31〜1/3を考慮）。

    T-K修正(2026-07-12): 祝日対応。jptime.prev_jpx_business_day を使用。
    記事の対象日はスクリーニングデータの最終日（data_date）が最優先で、
    ここはデータが無い場合のフォールバック。祝日当日がタイトル日付になることはない。
    """
    from jptime import prev_jpx_business_day

    return prev_jpx_business_day()


def _highs_target_date(highs: pd.DataFrame):
    """記事の対象取引日。スクリーニングの data_date（価格データの最終日）を最優先。"""
    if not highs.empty and "data_date" in highs.columns:
        values = [str(v).strip()[:10] for v in highs["data_date"].dropna().astype(str) if str(v).strip()]
        for value in sorted(values, reverse=True):
            try:
                return datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                continue
    return _prev_jst_business_day()


def _highs_num(row, key) -> float | None:
    try:
        value = float(row.get(key))
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _scrub_forbidden_tokens(text: str) -> str:
    """NaN/None/null/inf を本文に残さない最終防衛（値の欠損は上流で行非表示にしている）。"""
    import re

    return re.sub(
        r"(?<![A-Za-z0-9_])(nan|NaN|NAN|None|none|null|NULL|Null|inf|Inf)(?![A-Za-z0-9_])",
        "取得できず",
        text,
    )


def _sort_highs(df: pd.DataFrame) -> pd.DataFrame:
    """並び順: ①初回ブレイク ②高値までの距離 ③売買代金 ④当日出来高倍率。"""
    if df.empty:
        return df
    work = df.copy()

    def _num_col(name: str, default: float) -> pd.Series:
        # 列が無い場合 work.get() はスカラーになり fillna できないため Series を保証する
        if name in work.columns:
            return pd.to_numeric(work[name], errors="coerce").fillna(default)
        return pd.Series(default, index=work.index, dtype=float)

    work["_k_fb"] = work["first_break_60d"].map(_flag_true) if "first_break_60d" in work.columns else False
    work["_k_dist"] = _num_col("dist_to_high_pct", 99.0)
    work["_k_turn"] = _num_col("turnover_20d", 0.0)
    work["_k_vol"] = _num_col("volume_ratio_today", 0.0)
    work = work.sort_values(by=["_k_fb", "_k_dist", "_k_turn", "_k_vol"], ascending=[False, True, False, False])
    return work.drop(columns=["_k_fb", "_k_dist", "_k_turn", "_k_vol"])


def _split_highs_reference(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """A/B本体と参考掲載(C)に分ける。C=イナゴ疑い/TOB疑い/データ異常/連日更新10回以上。"""
    if df.empty:
        return df, df.iloc[0:0]
    mask = pd.Series(False, index=df.index)
    for column in ("inago_suspect", "tob_suspect", "data_anomaly"):
        if column in df.columns:
            mask |= df[column].map(_flag_true)
    if "breaks_20d" in df.columns:
        mask |= pd.to_numeric(df["breaks_20d"], errors="coerce").fillna(0) >= 10
    return df[~mask], df[mask]


def _reference_reason(row) -> str:
    reasons: list[str] = []
    if _flag_true(row.get("inago_suspect")):
        reasons.append("イナゴ疑い（急騰過熱）")
    if _flag_true(row.get("tob_suspect")):
        reasons.append("TOB疑い（高値張り付き）")
    if _flag_true(row.get("data_anomaly")):
        note = str(row.get("anomaly_note") or "").strip()
        reasons.append(f"データ異常のため参考掲載{'：' + note if note else ''}")
    breaks = _highs_num(row, "breaks_20d")
    if breaks is not None and breaks >= 10:
        reasons.append(f"連日更新{int(breaks)}回/20日")
    return " / ".join(reasons) if reasons else "参考掲載"


def _market_overview_sentence() -> str | None:
    """相場観①: market_snapshot.json の取得済み指標だけで組み立てる（事実のみ）。"""
    snap = OUTPUT_DIR / "market_snapshot.json"
    if not snap.exists():
        return None
    try:
        data = json.loads(snap.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    indicators = data.get("indicators") or {}
    facts: list[str] = []
    for key, label in (("nikkei", "日経平均"), ("topix", "TOPIX"), ("vix", "VIX"), ("sox", "SOX指数"), ("usdjpy", "ドル円")):
        item = indicators.get(key) or {}
        if str(item.get("status")) != "ok":
            continue
        value = str(item.get("display_value") or "").strip()
        change = str(item.get("display_change_pct") or "").strip()
        if not value or "未取得" in value:
            continue
        facts.append(f"{label}は{value}（前日比{change}）" if change and "未取得" not in change else f"{label}は{value}")
    if not facts:
        return None
    return "。".join(["、".join(facts[:2]), "、".join(facts[2:])]).rstrip("。") + "。" if facts[2:] else "、".join(facts) + "。"


def _sector_counts(df: pd.DataFrame) -> list[tuple[str, int, float]]:
    """業種ごとの (業種名, 銘柄数, 売買代金合計) を件数降順で返す。"""
    if df.empty or "sector" not in df.columns:
        return []
    work = df.copy()
    work["sector"] = work["sector"].astype(str).str.strip()
    work = work[(work["sector"] != "") & (~work["sector"].str.lower().isin(("nan", "none", "null")))]
    if work.empty:
        return []
    work["_turn"] = pd.to_numeric(work.get("turnover_20d"), errors="coerce").fillna(0.0)
    grouped = work.groupby("sector").agg(n=("sector", "size"), turn=("_turn", "sum"))
    grouped = grouped.sort_values(by=["n", "turn"], ascending=False)
    return [(str(name), int(r.n), float(r.turn)) for name, r in grouped.iterrows()]


def _highs_intro_lines(all_df: pd.DataFrame, new_cnt: int, near_cnt: int, fb_cnt: int, ref) -> list[str]:
    """冒頭300〜600字目安。①相場観（事実） ②セクター総評（自データ） ③導線。捏造禁止。"""
    lines: list[str] = []
    # 結論先出し
    lead = (
        f"{_jp_date_text(ref)}の日本株で、52週新高値に到達した銘柄は**{new_cnt}銘柄**、"
        f"新高値まで3%以内に接近した銘柄は**{near_cnt}銘柄**でした（うち初回ブレイクは{fb_cnt}銘柄）。"
    )
    lines.append(lead)
    lines.append("")
    market = _market_overview_sentence()
    if market:
        lines.append(f"相場全体では、{market}（取得済みデータに基づく事実。背景の解釈は各自の判断でご確認ください）")
    else:
        lines.append("> データ不足：市場指標が未取得のため、本日の相場観は省略します（推測では書きません）。")
    lines.append("")
    sectors = _sector_counts(all_df)
    if sectors:
        top = sectors[:3]
        parts = "、".join(f"{name}（{n}銘柄）" for name, n, _ in top)
        turn_leader = max(sectors, key=lambda item: item[2])
        sentence = f"新高値圏の候補を業種別に集計すると、{parts}に集中しました。"
        if turn_leader[2] > 0:
            sentence += f"売買代金の合計では{turn_leader[0]}が最大で、この記事の候補群の中では資金の向かい先が比較的はっきりした一日です（当スクリーニング内の集計）。"
        lines.append(sentence)
        hook_theme = top[0][0]
        lines.append(
            f"検索トレンド等の外部データは取得していないため、本日の候補データで最も層が厚い「{hook_theme}」を軸に確認します。"
            "すでに短期資金が集中した銘柄も混ざるため、今回も「初回ブレイク」と「新高値まで3%以内」を分けて掲載します。"
        )
    else:
        lines.append("業種データが取得できないため、セクター総評は省略します。")
    lines.append("")
    return lines


def _earnings_note_lines(row, ref) -> list[str]:
    """決算表示。過去日は次回として出さない。残り日数はJST基準の対象日から計算。"""
    text = str(row.get("earnings_date") or "").strip()[:10]
    parsed = None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        parsed = None
    if parsed is None:
        return ["📅 次回決算：未公表"]
    if parsed < ref:
        return ["📅 次回決算：未公表（取得済みの予定日が過去日のため表示しません）"]
    days = (parsed - ref).days
    return [f"📅 次回決算：{_jp_date_text(parsed)}／あと{days}日"]


def _overheat_text(row) -> str:
    parts: list[str] = []
    breaks = _highs_num(row, "breaks_20d")
    if breaks is not None and breaks >= 5:
        parts.append(f"連日更新{int(breaks)}回/20日")
    surge = _highs_num(row, "surge_5d_pct")
    if surge is not None and surge >= 15:
        parts.append(f"5日間で{surge:+.1f}%")
    if _flag_true(row.get("inago_suspect")):
        parts.append("イナゴ疑い")
    if _flag_true(row.get("tob_suspect")):
        parts.append("TOB疑い")
    return " / ".join(parts) if parts else "特筆なし"


def _highs_comment(row, ref, is_new: bool) -> str:
    """🔍 注目ポイント（150〜300字目安）。取得済みデータの事実だけで銘柄ごとに組み立てる。
    構成: 事実 → なぜ注目か → 確認点 → リスク。断定・煽りは書かない。"""
    code_seed = 0
    try:
        code_seed = int("".join(ch for ch in str(row.get("code", "0")) if ch.isdigit()) or 0)
    except ValueError:
        code_seed = 0
    parts: list[str] = []
    fb = _flag_true(row.get("first_break_60d"))
    dist = _highs_num(row, "dist_to_high_pct")
    if is_new:
        openers = [
            "本日、52週新高値を更新しました。",
            "1年分の高値を上抜け、52週新高値を付けました。",
            "本日の高値で52週レンジの上限を更新しています。",
        ]
        parts.append(openers[code_seed % len(openers)])
        if fb:
            parts.append("直近60営業日で初めての更新（初回ブレイク）で、上値のしこりが比較的軽い局面です。")
    else:
        if dist is not None:
            parts.append(f"52週高値まで残り{dist:.1f}%の位置につけています。高値更新前の「助走」局面です。")
        else:
            parts.append("52週高値のすぐ下に位置しています。")
    sector = str(row.get("sector") or "").strip()
    turnover = _highs_num(row, "turnover_20d")
    if sector and sector.lower() not in ("nan", "none", "null"):
        if turnover is not None and turnover >= 1e9:
            parts.append(f"{sector}の一角で、20日平均売買代金{turnover / 1e8:.0f}億円と流動性は十分です。")
        else:
            parts.append(f"業種は{sector}。売買代金は大型株ほど厚くないため、出来高の変化に注意が必要です。")
    vr = _highs_num(row, "volume_ratio_today")
    if vr is not None and vr >= 2:
        parts.append(f"本日は出来高が平常時の{vr:.1f}倍に膨らみ、需給に変化が出ています。")
    growth = _highs_num(row, "sales_growth_pct")
    if growth is not None:
        parts.append(f"直近の売上高は前年同期比{growth:+.1f}%（yfinance集計）と、業績面の裏付けも確認できます。" if growth > 0 else f"直近の売上高は前年同期比{growth:+.1f}%（yfinance集計）で、株価先行の面があります。")
    # リスク・確認点
    earn_text = str(row.get("earnings_date") or "").strip()[:10]
    try:
        earn_date = datetime.strptime(earn_text, "%Y-%m-%d").date()
        days = (earn_date - ref).days
        if 0 <= days <= 7:
            parts.append(f"決算発表があと{days}日に迫っており、発表またぎのリスクには注意してください。")
    except ValueError:
        pass
    breaks = _highs_num(row, "breaks_20d")
    if breaks is not None and breaks >= 5 and not fb:
        parts.append(f"直近20日で{int(breaks)}回目の更新と過熱感もあり、押し目を待つ選択肢も考えられます。")
    closers = [
        "明日以降は、高値更新後も出来高を維持できるか、終値ベースで高値圏を保てるかが確認ポイントです。",
        "続伸するかよりも、押した時にどこで買いが入るか（25日線など）を観察したい銘柄です。",
        "高値圏で値固めできるか、出来高を伴った続伸があるかを確認していきます。",
    ]
    parts.append(closers[code_seed % len(closers)])
    text = "".join(parts)
    while len(text) > 300 and len(parts) > 3:
        parts.pop(-2)  # 締めは残し、中間の要素から削る
        text = "".join(parts)
    return text


def _highs_overview_table(df: pd.DataFrame, with_reason: bool = False) -> list[str]:
    """セクション冒頭の一覧表（スマホで俯瞰できるように）。値の欠損は「未取得」。"""
    if with_reason:
        lines = [
            "| コード | 銘柄 | 現在値 | 前日比% | 高値乖離% | 参考掲載の理由 |",
            "|---|---|---:|---:|---:|---|",
        ]
    else:
        lines = [
            "| コード | 銘柄 | 現在値 | 前日比% | 高値乖離% | 決算日 | 売買代金 | フラグ |",
            "|---|---|---:|---:|---:|---|---:|---|",
        ]
    for _, row in df.iterrows():
        if with_reason:
            lines.append(
                f"| {_val(row, 'code')} | {_val(row, 'name')} | {_val(row, 'current_price')} | "
                f"{_val(row, 'change_pct')} | {_val(row, 'dist_to_high_pct')} | ⚠️ {_reference_reason(row)} |"
            )
        else:
            lines.append(
                f"| {_val(row, 'code')} | {_val(row, 'name')} | {_val(row, 'current_price')} | "
                f"{_val(row, 'change_pct')} | {_val(row, 'dist_to_high_pct')} | {_val(row, 'earnings_date')} | "
                f"{_fmt_oku(row.get('turnover_20d'))} | {_val(row, 'note_flags')} |"
            )
    return lines


def _stock_detail_block(row, rank: int, ref, ow_cache, is_new: bool) -> list[str]:
    """1銘柄の詳細ブロック。取得できた項目だけを表示（欠損行は非表示・捏造禁止）。"""
    code = str(row.get("code", "")).strip()
    name = safe_text(row.get("name"))
    dist = _highs_num(row, "dist_to_high_pct")
    status = "本日52週新高値を更新" if is_new else (f"新高値まであと{dist:.2f}%" if dist is not None else "新高値接近")
    lines: list[str] = [f"### {rank}. {name}（{code}）　{status}", ""]

    def add(label: str, value: str | None) -> None:
        if value:
            lines.append(f"{label}{value}")

    current = _highs_num(row, "current_price")
    change = _highs_num(row, "change_pct")
    add("株価：", f"{current:,.1f}円" if current is not None else None)
    add("前日比：", f"{change:+.2f}%" if change is not None else None)
    today_high = _highs_num(row, "today_high")
    add("本日高値：", f"{today_high:,.1f}円" if today_high is not None else None)
    high_price = _highs_num(row, "high_price") or _highs_num(row, "high_52w")
    add("直前の52週高値：", f"{high_price:,.1f}円" if high_price is not None else None)
    if not is_new and dist is not None:
        add("52週高値までの距離：", f"{dist:.2f}%")
    elif is_new:
        add("52週高値までの距離：", "本日更新")
    turnover = _highs_num(row, "turnover_20d")
    add("売買代金（20日平均）：", f"{turnover / 1e8:,.1f}億円" if turnover is not None and turnover > 0 else None)
    vr = _highs_num(row, "volume_ratio_today")
    add("出来高倍率（当日/20日平均）：", f"{vr:.2f}倍" if vr is not None else None)
    rng = _highs_num(row, "intraday_range_pct")
    add("日中値幅：", f"{rng:.2f}%" if rng is not None else None)
    sector = str(row.get("sector") or "").strip()
    if sector and sector.lower() not in ("nan", "none", "null"):
        add("業種：", sector)
    mcap = _highs_num(row, "market_cap_oku")
    add("時価総額：", f"{mcap:,.0f}億円" if mcap is not None else None)
    per_a = _highs_num(row, "per_actual")
    add("📊 実績PER：", f"{per_a:.1f}倍" if per_a is not None else None)
    per_f = _highs_num(row, "per_forecast")
    add("📊 予想PER：", f"{per_f:.1f}倍" if per_f is not None else None)
    pbr = _highs_num(row, "pbr")
    add("📊 PBR：", f"{pbr:.2f}倍" if pbr is not None else None)
    dividend = _highs_num(row, "dividend_yield_pct")
    add("📊 予想配当利回り：", f"{dividend:.2f}%" if dividend is not None else None)
    roe = _highs_num(row, "roe_pct")
    add("💪 ROE：", f"{roe:.1f}%" if roe is not None else None)
    opm = _highs_num(row, "op_margin_pct")
    add("💪 営業利益率：", f"{opm:.1f}%" if opm is not None else None)
    npm = _highs_num(row, "net_margin_pct")
    add("💪 純利益率：", f"{npm:.1f}%" if npm is not None else None)
    sales_g = _highs_num(row, "sales_growth_pct")
    add("🚀 売上高成長率：", f"前年同期比 {sales_g:+.1f}%" if sales_g is not None else None)
    profit_g = _highs_num(row, "profit_growth_pct")
    add("🚀 利益成長率：", f"前年同期比 {profit_g:+.1f}%" if profit_g is not None else None)
    add("過熱判定：", _overheat_text(row))
    if _flag_true(row.get("first_break_60d")):
        add("鮮度：", "初回ブレイク（直近60営業日で初の高値更新）")
    # OpenWork（キャッシュのみ・通信しない）
    if ow_cache is not None:
        try:
            from openwork_cache import build_openwork_lines

            lines.extend(build_openwork_lines(code, ow_cache, ref))
        except Exception:
            lines.append("👔 OpenWork：取得できず")
    else:
        lines.append("👔 OpenWork：取得できず")
    lines.extend(_earnings_note_lines(row, ref))
    prev_high_date = str(row.get("high_date") or "").strip()
    if prev_high_date and prev_high_date.lower() not in ("nan", "none", "null"):
        add("📅 52週高値日：", prev_high_date)
    lines.append("")
    lines.append(f"🔍 **注目ポイント**：{_highs_comment(row, ref, is_new)}")
    lines.append("")
    lines.append(
        f"[📈 6ヶ月日足チャート（Yahoo!ファイナンス）]"
        f"(https://finance.yahoo.co.jp/quote/{code}.T/chart?frm=dly&trm=6m&scl=stndrd&styl=cndl&evnts=volume&ovrIndctr=sma%2Cmma%2Clma&addIndctr=&compare=)"
    )
    lines.append("")
    return lines


def _highs_footer_counts(all_df: pd.DataFrame, new_df: pd.DataFrame, near_df: pd.DataFrame, ref_df: pd.DataFrame, ref) -> list[str]:
    """記事末尾の集計（事実のみ）。"""
    def _count_flag(df: pd.DataFrame, column: str) -> int:
        if df.empty or column not in df.columns:
            return 0
        return int(df[column].map(_flag_true).sum())

    fb_cnt = _count_flag(all_df, "first_break_60d")
    inago_cnt = _count_flag(all_df, "inago_suspect")
    tob_cnt = _count_flag(all_df, "tob_suspect")
    anomaly_cnt = _count_flag(all_df, "data_anomaly")
    earnings7 = 0
    if not all_df.empty and "earnings_date" in all_df.columns:
        for value in all_df["earnings_date"].astype(str):
            try:
                d = datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            if 0 <= (d - ref).days <= 7:
                earnings7 += 1
    return [
        "## 本日の集計",
        "",
        f"- 候補数（合計）：{len(all_df)}銘柄",
        f"- 52週新高値 到達：{len(new_df)}銘柄",
        f"- 3%以内 接近：{len(near_df)}銘柄",
        f"- 初回ブレイク：{fb_cnt}銘柄",
        f"- イナゴ疑い（参考掲載へ）：{inago_cnt}銘柄",
        f"- TOB疑い（参考掲載へ）：{tob_cnt}銘柄",
        f"- データ異常（参考掲載へ）：{anomaly_cnt}銘柄",
        f"- 決算発表7日以内：{earnings7}銘柄",
        "",
    ]


def build_highs_note(highs: pd.DataFrame, source: Path | None) -> str:
    """note1本目: 52週新高値 接近・到達銘柄（T-K全面改修版）。

    A=本日到達 / B=3%以内接近 / C=参考掲載。並びは初回ブレイク→距離→売買代金→出来高。
    NaN等は本文に出さない。OpenWorkはキャッシュのみ参照（通信しない）。
    """
    from jptime import jst_today

    ref = _highs_target_date(highs)
    lines = [f"# {_jp_date_text(ref)} {_HIGHS_TITLE_SUFFIX}", ""]
    if jst_today() != ref:
        lines.append(f"※ 対象は直近取引日 **{_jp_date_text(ref)}** の日本株データです（生成日と異なります）。")
        lines.append("")

    def bucket(df: pd.DataFrame, htype: str) -> pd.DataFrame:
        if df.empty or "high_type" not in df.columns:
            return df.iloc[0:0] if not df.empty else df
        return df[df["high_type"].astype(str) == htype]

    new_all = bucket(highs, "52W_NEW_HIGH")
    near_all = bucket(highs, "52W_NEAR_HIGH")
    new_main, new_ref = _split_highs_reference(new_all)
    near_main, near_ref = _split_highs_reference(near_all)
    reference = pd.concat([new_ref, near_ref]) if (not new_ref.empty or not near_ref.empty) else new_all.iloc[0:0]
    new_main = _sort_highs(new_main)
    near_main = _sort_highs(near_main)

    fb_cnt = 0
    if not highs.empty and "first_break_60d" in highs.columns:
        fb_cnt = int(highs["first_break_60d"].map(_flag_true).sum())
    lines.extend(_highs_intro_lines(highs, len(new_all), len(near_all), fb_cnt, ref))

    if source is None or highs.empty:
        lines.append("> データ不足：本日の52週高値スクリーニング出力（screening_highs）が未生成または空のため、候補を表示できません。下書きは規定どおり生成しています。")
        lines.append("")

    try:
        from openwork_cache import load_cache as _ow_load

        ow_cache = _ow_load()
    except Exception:
        ow_cache = None

    sections = (
        ("## 【A】52週新高値に本日到達した銘柄", new_main, True, 15),
        ("## 【B】52週新高値まで3%以内に接近している銘柄", near_main, False, 10),
    )
    for header, df, is_new, detail_cap in sections:
        lines.append(header)
        lines.append("")
        if df.empty:
            lines.append("- 該当なし")
            lines.append("")
            continue
        lines.append("### 一覧表")
        lines.append("")
        lines.extend(_highs_overview_table(df))
        lines.append("")
        lines.append("### 銘柄詳細")
        lines.append("")
        detail = df.head(detail_cap)
        for rank, (_, row) in enumerate(detail.iterrows(), start=1):
            lines.extend(_stock_detail_block(row, rank, ref, ow_cache, is_new))
        if len(df) > detail_cap:
            lines.append(f"※ 残り{len(df) - detail_cap}銘柄は上の一覧表をご覧ください（詳細は優先順位の高い{detail_cap}銘柄に絞っています）。")
            lines.append("")

    lines.append("## 【C】参考掲載（イナゴ疑い・TOB疑い・連日更新・データ異常）")
    lines.append("")
    if reference.empty:
        lines.append("- 該当なし")
        lines.append("")
    else:
        lines.append(
            f"以下の**{len(reference)}銘柄**は基準には該当しますが、短期過熱・TOB観測・データ異常の可能性があるため、"
            "主要候補から外して参考情報として掲載します。"
        )
        lines.append("")
        lines.extend(_highs_overview_table(_sort_highs(reference), with_reason=True))
        lines.append("")

    lines.extend(_highs_footer_counts(highs, new_all, near_all, reference, ref))

    # バックテスト博士: 実績セクション（掲載銘柄のその後。データ不足は明記）
    try:
        from track_record import build_track_record_lines, load_track_record_summary

        lines.extend(build_track_record_lines(load_track_record_summary()))
    except Exception:
        lines.extend([
            "## 実績（過去に掲載した銘柄のその後）",
            "",
            "> データ不足：実績データは掲載記録の蓄積開始後、営業日を重ねると自動表示されます。",
            "",
        ])

    lines.append("## おわりに")
    lines.append("")
    lines.append("- この「52週新高値・新高値まで3%」リストは毎営業日、**同じ基準で機械的に**抽出しています。基準がぶれないことがこの記事の価値です。")
    lines.append("- 新高値銘柄がその後押し目を作ったら、姉妹記事「押し目（25MA・200MAタッチ）」で追跡します。")
    lines.append("- 日本株の高値更新・注目銘柄・決算予定を毎日追うなら、フォローしておくと更新を見逃しません。")
    lines.append("")
    lines.append("## 注意書き")
    lines.append("")
    lines.append("- 本記事は情報提供を目的としたもので、特定銘柄の売買を推奨するものではありません。")
    lines.append("- 数値は取得済みデータに基づく機械集計です。「取得できず」「未公表」は文字どおりの意味で、推測では補いません。")
    lines.append(f"- source={source.name if source else '未生成（Mac実行待ち）'}")
    return _scrub_forbidden_tokens("\n".join(lines))


def write_one_note(key: str, note_markdown: str, chart_rel: str | None = None) -> dict[str, str]:
    title = extract_note_title(note_markdown)
    # タイトル直下に画像マーカーを差し込む（.md は記録として保持・.html はコメント無視で崩れない）
    note_markdown = inject_chart_marker(note_markdown, chart_rel)
    md_path = OUTPUT_DIR / f"note_{key}.md"
    title_path = OUTPUT_DIR / f"note_{key}_title.txt"
    html_path = OUTPUT_DIR / f"note_{key}.html"
    url_path = OUTPUT_DIR / f"note_draft_url_{key}.txt"
    md_path.write_text(note_markdown, encoding="utf-8")
    title_path.write_text(title + "\n", encoding="utf-8")
    html_path.write_text(render_markdown_html(title, note_markdown), encoding="utf-8")
    print(f"saved={md_path}")
    print(f"saved={html_path}")
    entry: dict[str, str] = {
        "key": key,
        "title": title,
        "md_file": md_path.name,
        "title_file": title_path.name,
        "html_file": html_path.name,
        "url_file": url_path.name,
    }
    if chart_rel:
        # manifest に画像パスを入れる（note_autosave が読む）。コードも記録。
        entry["chart_image"] = chart_rel
        entry["chart_code"] = NOTE4_CHART_CODES.get(key, "")
    return entry


def build_note4(sources: SourceFiles, screening: pd.DataFrame, discipline: pd.DataFrame, backtest: dict | None) -> list[dict[str, str]]:
    pullback_src = latest_aux("screening_pullback")
    highs_src = latest_aux("screening_highs")
    pullback = load_aux(pullback_src)
    highs = load_aux(highs_src)

    notes = {
        "chatgpt": build_chatgpt_note(discipline, screening, sources),
        "claude": build_claude_note(screening, discipline, backtest, sources),
        "pullback": build_pullback_note(pullback, pullback_src),
        "highs": build_highs_note(highs, highs_src),
    }
    # 4本すべての冒頭に市場ステータスを挿入（空欄禁止）
    status_lines = _market_status_block()
    notes = {key: _insert_market_status(body, status_lines) for key, body in notes.items()}
    manifest = [write_one_note(key, body, chart_rel_path(key)) for key, body in notes.items()]
    NOTE4_MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved={NOTE4_MANIFEST_PATH}")
    # 完成条件: 4本すべて生成され、各冒頭に市場ステータスが入っていなければ失敗扱い
    broken: list[str] = []
    for key in NOTE4_TITLES:
        md_path = OUTPUT_DIR / f"note_{key}.md"
        if not md_path.exists() or md_path.stat().st_size == 0:
            broken.append(f"note_{key}.md 未生成")
            continue
        text = md_path.read_text(encoding="utf-8")
        if "## 市場ステータス" not in text or not any(f"**{v}**" in text for v in NOTE4_VALID_REGIMES):
            broken.append(f"note_{key}.md 市場ステータス欠落")
    if len(manifest) != len(NOTE4_TITLES) or broken:
        raise RuntimeError(f"note4 generation incomplete: {', '.join(broken) or 'manifest不足'}")
    print("note4=4本生成OK（各冒頭に市場ステータス入り）")
    return manifest


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
    # T-E: 4本のNote下書きを生成（manifestをnote_autosaveが読む）
    build_note4(sources, screening, discipline, backtest)
    print(f"screening={sources.screening}")
    print(f"discipline={sources.discipline}")
    print(f"backtest={sources.backtest if sources.backtest else '未取得'}")


if __name__ == "__main__":
    main()
