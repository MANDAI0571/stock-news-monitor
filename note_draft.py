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
            "",
        ])
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
    lines = [f"# {NOTE4_TITLES['chatgpt']} {today}", ""]
    lines.append("> この記事の相場コメントはChatGPTが執筆します。以下はClaude側スクリーニングの共通素材データ（事実）です。")
    lines.append("")
    lines.append("## 本日の300万円運用（規律版データ）")
    lines.append("")
    lines.extend(summarize_discipline(discipline))
    lines.append("")
    buys = discipline[discipline.get("action", pd.Series(dtype=str)).astype(str).str.upper() == "BUY"] if not discipline.empty else discipline
    lines.extend(["## 300万円運用BUY候補カード", ""])
    lines.extend(build_stock_cards(buys, None))
    lines.extend(["", "## 300万円運用BUY候補（表）", ""])
    lines.extend(_discipline_holdings_table(discipline))
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
    body = build_note_body(screening, discipline, backtest, sources)
    out_lines = body.splitlines()
    if out_lines and out_lines[0].startswith("# "):
        today = datetime.now().strftime("%Y-%m-%d")
        out_lines[0] = f"# {NOTE4_TITLES['claude']} {today}"
    if not any("Claude候補TOP10カード" in line for line in out_lines):
        card_lines = ["", "## Claude候補TOP10カード", ""]
        card_lines.extend(build_stock_cards(top_buy_candidates(screening, 10), 10))
        out_lines[1:1] = card_lines
    return "\n".join(out_lines)


def _discipline_holdings_table(discipline: pd.DataFrame) -> list[str]:
    if discipline.empty:
        return ["- 300万円候補データなし"]
    buys = discipline[discipline.get("action", pd.Series(dtype=str)).astype(str).str.upper() == "BUY"]
    if buys.empty:
        return ["- 本日は新規買い建てなし（現金保有）"]
    lines = ["| 枠 | コード | 銘柄 | ランク | 株数 | 取得想定 | 投資額 |", "|---|---|---|---:|---:|---:|---:|"]
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


def build_pullback_note(pullback: pd.DataFrame, source: Path | None) -> str:
    """③押し目候補。4バケット: 52週新高値リテスト / 25MAタッチ / 200MAタッチ / 240MAタッチ。
    データが無いバケットは「該当なし」。空想は作らない。"""
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# {NOTE4_TITLES['pullback']} {today}", ""]
    lines.append("52週新高値ブレイク後にラインまで戻った銘柄（リテスト）と、上昇トレンド中の25/200/240日線タッチ銘柄をまとめました。")
    lines.append("")

    def bucket(df: pd.DataFrame, flag: str) -> pd.DataFrame:
        if df.empty or flag not in df.columns:
            return df.iloc[0:0] if not df.empty else df
        mask = df[flag].astype(str).str.lower().isin(["true", "1", "1.0"])
        return df[mask]

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

    lines.append("## 注意書き")
    lines.append("")
    lines.append("- これは投資助言ではありません。スクリーニング結果（事実）です。")
    lines.append(f"- source={source.name if source else '未生成（Mac実行待ち）'}")
    return "\n".join(lines)


def build_highs_note(highs: pd.DataFrame, source: Path | None) -> str:
    """④52週新高値タッチ・接近。2バケット: 52週新高値 / 52週高値接近。空は「該当なし」。"""
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# {NOTE4_TITLES['highs']} {today}", ""]
    lines.append("本日の52週新高値更新・接近銘柄です。")
    lines.append("")

    def bucket(df: pd.DataFrame, htype: str) -> pd.DataFrame:
        if df.empty or "high_type" not in df.columns:
            return df.iloc[0:0] if not df.empty else df
        return df[df["high_type"].astype(str) == htype]

    for htype, title in (("52W_NEW_HIGH", "52週新高値更新"), ("52W_NEAR_HIGH", "52週高値接近（3%以内）")):
        lines.append(f"## 【{title}】")
        lines.append("")
        b = bucket(highs, htype)
        if b.empty:
            lines.append("- 該当なし")
        else:
            if htype == "52W_NEW_HIGH":
                lines.append("### カード型候補（全件）")
                lines.append("")
                lines.extend(build_stock_cards(b, None))
            else:
                lines.append("### カード型候補（上位20件）")
                lines.append("")
                lines.extend(build_stock_cards(b, 20))
            lines.append("### 従来表")
            lines.append("")
            lines.append("| コード | 銘柄 | 現在値 | 52週高値 | 高値乖離% | 高値日 | 売買代金 |")
            lines.append("|---|---|---:|---:|---:|---|---:|")
            for _, row in b.iterrows():
                lines.append(
                    f"| {_val(row,'code')} | {_val(row,'name')} | {_val(row,'current_price')} | "
                    f"{_val(row,'high_52w')} | {_val(row,'dist_to_high_pct')} | {_val(row,'high_date')} | {_val(row,'turnover_20d')} |"
                )
        lines.append("")

    lines.append("## 注意書き")
    lines.append("")
    lines.append("- これは投資助言ではありません。スクリーニング結果（事実）です。")
    lines.append(f"- source={source.name if source else '未生成（Mac実行待ち）'}")
    return "\n".join(lines)


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
    manifest = [write_one_note(key, body, chart_rel_path(key)) for key, body in notes.items()]
    NOTE4_MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved={NOTE4_MANIFEST_PATH}")
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
