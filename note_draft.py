from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from html import escape
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

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


def _trade_history_metrics() -> dict | None:
    """v10(2026-07-19): バックテストレポートが無い日のための実運用実績フォールバック。
    data/trade_history.csv のCLOSEDトレード（exit_return_pct）から事実のみを算出する。
    データが無い・全て未決済なら None（記事は未取得表示のまま）。捏造はしない。"""
    path = PROJECT_ROOT / "data" / "trade_history.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path)
        closed = df[df["status"].astype(str).str.upper() == "CLOSED"].copy()
        closed["exit_return_pct"] = pd.to_numeric(closed["exit_return_pct"], errors="coerce")
        closed = closed.dropna(subset=["exit_return_pct"])
        if closed.empty:
            return None
        if "exit_date" in closed.columns:
            closed = closed.sort_values("exit_date")
        returns = closed["exit_return_pct"].astype(float)
        gains = returns[returns > 0].sum()
        losses = abs(returns[returns < 0].sum())
        pf = (gains / losses) if losses > 0 else None
        equity = returns.cumsum()
        drawdown = (equity - equity.cummax()).min()
        return {
            "profit_factor": pf,
            "max_drawdown_pct": abs(float(drawdown)) if drawdown == drawdown else None,
            "n_trades": int(len(closed)),
        }
    except Exception:
        return None


def build_backtest_section(report: dict | None) -> list[str]:
    if report is None:
        # バックテスト未実施日は実運用トレード実績（事実）で代替。出所を明記する。
        actual = _trade_history_metrics()
        if actual is not None:
            pf = actual.get("profit_factor")
            return [
                "## バックテスト指標",
                "",
                "※本日はバックテスト未実施のため、実運用トレードの確定実績（trade_history）を記載します。",
                "",
                f"- PF（実運用・確定分）: {fmt_num(pf, 3) if pf is not None else '損失トレードなしのため算出不可'}",
                f"- DD（実運用・確定分の累積リターン最大下落）: {fmt_num(actual.get('max_drawdown_pct'), 2)}%",
                f"- 採用数（決済済みトレード数）: {actual.get('n_trades')}",
            ]
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
# T-E(2026-06-28): Note 分割
# fix25(2026-08-23): ChatGPT(Codex)版は高重さんの指示で廃止。以下の3本を作る。
#   ① Claudeが300万円運用
#   ② 52週新高値後の押し目候補(リテスト/25MA/200MA/240MAタッチ)
#   ③ 52週新高値タッチ・接近銘柄
# 既存の note_daily.* は後方互換のため残す（健全性チェック・CIが参照）。
# データが無いバケットは必ず「該当なし」（捏造・空想Noteは作らない）。
# ============================================================================

NOTE4_TITLES = {
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
    # fix29(2026-08-23): 高重さんの指示で「判定元」の行とURLは載せない。
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
    "claude": "8524",
    "pullback": "7011",
    "highs": "6951",
}

# 日付なしで繰り返し使う固定見出し画像。
NOTE4_FIXED_HEADERS = {
    "claude": "note_header_claude_300man.png",
    "pullback": "note_header_pullback.png",
    "highs": "note_header_highs.png",
}


def chart_rel_path(key: str) -> str | None:
    """key に対応する本日のチャートPNGの相対パス（リポジトリ基準）。無ければ None。"""
    fixed_header = NOTE4_FIXED_HEADERS.get(key)
    if fixed_header:
        fixed_path = PROJECT_ROOT / fixed_header
        if not fixed_path.is_file() or fixed_path.stat().st_size == 0:
            raise FileNotFoundError(f"固定見出し画像が見つかりません: {fixed_path}")
        return fixed_header
    code = NOTE4_CHART_CODES.get(key)
    if not code:
        return None
    day = datetime.now().strftime("%Y%m%d")
    return f"outputs/charts_{day}/chart_{key}_{code}.png"


def inject_chart_marker(note_markdown: str, chart_rel: str | None) -> str:
    """タイトル直下に画像挿入マーカーを差し込む（本文の見た目は崩さない＝HTML側はコメントを無視）。
    例: <!-- chart_image: outputs/charts_20260628/chart_claude_8524.png -->"""
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
    # fix30(2026-08-23): 高重さんの指示で3営業日以内に絞り、あと何日かを書く。
    if days is not None and days <= 3:
        if days == 0:
            return f"{text} ⚠️ 本日決算（発表前後は値動きが荒くなります）"
        return f"{text} ⚠️ あと{days}営業日で決算（発表前後は値動きが荒くなります）"
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


def _candidate_intro(row) -> str:
    """外部APIに依存せず、スクリーニング済みの事実から紹介文を作る。"""
    name = safe_text(row.get("name"))
    sector = _first_value(row, ("sector", "業種", "セクター"))
    rank = _first_value(row, ("rank", "ランク"))
    screen_type = _first_value(row, ("screen_type", "high_type", "signal_type"))
    reason = _first_value(row, ("reason", "buy_reason", "selection_reason"))

    lead = f"{name}は"
    if not _is_missing(sector):
        lead += f"{safe_text(sector)}に属する銘柄で、"
    facts: list[str] = []
    if not _is_missing(rank):
        facts.append(f"スクリーニング評価は{safe_text(rank)}ランク")
    comment = _editor_comment(row).rstrip("。")
    if comment:
        facts.append(comment)
    if not _is_missing(screen_type):
        labels = {
            "52W_NEW_HIGH": "52週新高値の更新候補",
            "52W_NEAR_HIGH": "52週高値圏の候補",
            "MA25_PULLBACK": "25日移動平均線付近の押し目候補",
            "MA200_TOUCH": "200日移動平均線付近の候補",
        }
        facts.append(labels.get(str(screen_type).upper(), safe_text(screen_type)))
    if not _is_missing(reason):
        reason_text = re.sub(r"\s+", " ", safe_text(reason)).strip()
        if reason_text and reason_text.lower() not in {"nan", "none", "null", "未取得"}:
            facts.append(reason_text[:90])
    if not facts:
        facts.append("300万円運用の資金・流動性条件を通過した監視候補")
    return lead + "。".join(dict.fromkeys(facts[:3])) + "。"


# fix31(2026-08-23): 配当が高い銘柄は見出しで目立たせる。
# しきい値は高重さんの指示で5%。表示は事実（実際の利回り）だけを書く。
HIGH_DIVIDEND_PCT = 5.0


def _high_dividend_mark(row: object) -> str:
    """配当利回りが5%以上なら見出しに足す印。取れない・届かないなら空文字。"""
    value = _first_value(row, ("dividend_yield", "配当利回り"))
    if _is_missing(value):
        return ""
    try:
        pct = float(str(value).replace("%", "").replace(",", ""))
    except (TypeError, ValueError):
        return ""
    if pct < HIGH_DIVIDEND_PCT:
        return ""
    return f" 💰高配当{pct:.2f}%"


# fix29(2026-08-23): カードに OpenWork の検索リンクを置く。
# OpenWorkの規約（第11条）は機械的アクセスと情報の転載を禁じているので、
# 評価値そのものは取らず・載せず、読んだ人が自分で見に行くリンクだけを置く。
OPENWORK_SEARCH_BASE = "https://www.openwork.jp/search.php?src_str="


def _openwork_url(name: object) -> str:
    """社名から OpenWork の検索URLを組み立てる。外部通信は一切しない。"""
    text = str(name or "").strip()
    if not text or text.lower() in ("nan", "none", "null"):
        return ""
    return OPENWORK_SEARCH_BASE + quote(text, safe="")


def build_stock_cards(df: pd.DataFrame, max_rows: int | None = None) -> list[str]:
    """銘柄をnote向けカード型紹介にする。欠損項目は表示せず、事実ベースの紹介文で補う。"""
    if df.empty:
        return ["- 該当なし"]
    data = _enrich_openwork(df)
    if max_rows is not None:
        data = data.head(max_rows)
    # v10(2026-07-19): 表示行だけyfinanceでPER/PBR/ROE等を補完（失敗時は未取得のまま）
    try:
        from note_fundamentals import enrich_fundamentals

        data = enrich_fundamentals(data)
    except Exception:
        pass
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
        vr = _fmt_number(volume_ratio, 2) if not _is_missing(volume_ratio) else ""
        heading = f"{code} {name}" + (f" ⚡出来高{vr}倍" if vr else "")
        # fix41(2026-09-03): 押し目で同じ銘柄が25MA/200MA/240MAに重複して出ていた。
        # 1回だけ載せ、ほかにどの線に触れているかを見出しに添える。
        _also = str(row.get("pullback_also") or "").strip()
        if _also:
            heading += f"　🔁 {_also}にも同時タッチ"
        # fix31(2026-08-23): 配当5%以上は見出しで目立たせる（描画側で点滅させる印）。
        heading += _high_dividend_mark(row)
        lines.extend([heading, f"📝 紹介: {_candidate_intro(row)}"])

        price_parts: list[str] = []
        if not _is_missing(price):
            price_parts.append(f"現在値: {_fmt_yen(price)}")
        if not _is_missing(change_pct):
            price_parts.append(f"前日比: {_fmt_pct(change_pct, signed=True)}")
        if price_parts:
            lines.append(" / ".join(price_parts))

        trade_parts: list[str] = []
        if not _is_missing(turnover):
            trade_parts.append(f"売買代金: {_fmt_oku(turnover)}")
        if vr:
            trade_parts.append(f"出来高倍率: {vr}x")
        if not _is_missing(range_pct):
            trade_parts.append(f"値幅: {_fmt_pct(range_pct)}")
        if trade_parts:
            lines.append(" / ".join(trade_parts))

        valuation_parts: list[str] = []
        for label, value, digits in (("PER", per, 1), ("予想PER", forward_per, 1), ("PBR", pbr, 2)):
            if not _is_missing(value):
                valuation_parts.append(f"{label} {_fmt_number(value, digits)}")
        if not _is_missing(dividend):
            valuation_parts.append(f"配当 {_fmt_pct(dividend, 2)}")
        if valuation_parts:
            lines.append("📊 " + " / ".join(valuation_parts))

        quality_parts: list[str] = []
        for label, value in (("ROE", roe), ("営業利益率", op_margin), ("純利益率", net_margin)):
            if not _is_missing(value):
                quality_parts.append(f"{label} {_fmt_pct(value, 1)}")
        if quality_parts:
            lines.append("💪 " + " / ".join(quality_parts))

        growth_parts: list[str] = []
        if not _is_missing(sales_growth):
            growth_parts.append(f"売上 {_fmt_pct(sales_growth, 1, signed=True)} (前年比)")
        if not _is_missing(profit_growth):
            growth_parts.append(f"利益 {_fmt_pct(profit_growth, 1, signed=True)} (前年比)")
        if growth_parts:
            lines.append("🚀 " + " / ".join(growth_parts))
        if earnings != "未取得":
            lines.append(f"🗓 決算予定日: {earnings}")
        if openwork != "未取得":
            lines.append(f"👥 OpenWork評価: {openwork}")
        company_parts: list[str] = []
        if not _is_missing(market_cap):
            company_parts.append(f"時価総額 {_fmt_market_cap(market_cap)}")
        if not _is_missing(sector):
            company_parts.append(f"セクター: {safe_text(sector)}")
        if company_parts:
            lines.append("🏢 " + " / ".join(company_parts))
        lines.append(f"📈 チャート: {_chart_url(code)}")
        openwork_url = _openwork_url(name)
        if openwork_url:
            lines.append(f"👥 OpenWork: {openwork_url}")
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


def build_claude_note(screening: pd.DataFrame, discipline: pd.DataFrame, backtest: dict | None, sources: SourceFiles) -> str:
    """②Claude版。

    T-K改稿(2026-08-03): 高重さんの指摘（同じ10銘柄がカード・表・箇条書きで3回出る／
    「そのままnoteに貼れる文章」という記事内記事がある／source_*= のデバッグ行が本文に残る／
    一番読みたい運用状況が最下部）を受けて、構成を上から一本道に作り直した。
      タイトル → 市場ステータス(後段で挿入) → いまの運用状況(台帳) → 本日の候補(未約定)
      → 買い候補TOP10(表1回) → 上位3銘柄カード → 選定ルール → 免責
    """
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# {NOTE4_TITLES['claude']} {today}", ""]

    # ① いまの運用状況（本物の台帳）＋② 本日の候補（未約定・当日試算）
    lines.extend(_portfolio_status_block(discipline, operation="claude"))

    # ③ 買い候補TOP10。表は1回だけ。カードは上位3銘柄に絞る（同じ銘柄の3回掲載をやめる）
    lines.extend(["## 買い候補TOP10（一覧表）", ""])
    lines.extend(_top10_block(screening))
    lines.extend(["", "## 上位3銘柄カード", ""])
    lines.extend(build_stock_cards(top_buy_candidates(screening, 3), 3))

    # ④ 選定ルール
    lines.extend([
        "",
        "## 候補の選定ルール",
        "",
        "- Sランク上位から最大3銘柄・1枠100万円（地合いCAUTIONは1銘柄、RISK/STOPは新規停止）",
        "- 損切 -7% / 利確 +15% / 10営業日タイムアウト",
        "- 出来高フィルタ electric_volume_min=1.1 / selection_rule=current",
        "",
    ])
    lines.extend(build_backtest_section(backtest))

    # ⑤ 免責（社内向けの参照ファイル名はHTMLコメントに隠す）
    lines.extend([
        "",
        "## 免責",
        "",
        "- これは投資助言ではありません。スクリーニング結果（事実）です。",
        "- 架空資金によるペーパー運用の記録です。",
        "",
        "※これは投資助言ではなく、スクリーニング結果です。売買判断は自己責任で行ってください。",
        "",
        f"<!-- source_screening={sources.screening.name} source_discipline={sources.discipline.name} -->",
    ])
    return "\n".join(lines)


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


# ランクの色。noteはHTML/CSSを受け付けないので、色は絵文字で付ける。
RANK_MARKS = {"S": "\U0001F7E2", "A": "\U0001F535", "B": "\U0001F7E1", "C": "\U0001F7E0", "D": "\U0001F534"}


def rank_mark(rank: object) -> str:
    """ランク文字（S/A/B…）を色つきの丸に変える。未知は白丸。"""
    key = str(rank or "").strip().upper()[:1]
    return RANK_MARKS.get(key, "\u26AA")


def _top10_price(value: object) -> str:
    """現在値を「4,200円」の形にする。小数の .0 は出さない。"""
    try:
        return f"{float(value):,.0f}\u5186"
    except (TypeError, ValueError):
        return safe_text(value)


def _top10_score(value: object) -> str:
    """スコアの末尾の .0 を落とす。"""
    text = safe_text(value)
    return text[:-2] if text.endswith(".0") else text


def _top10_block(screening: pd.DataFrame) -> list[str]:
    """買い候補TOP10。

    T-P(2026-08-18): 高重さんの指示「コード・銘柄・ランク・スコアなど色分けして、
    縦の仕切りはいらない」。マークダウンの表（| 区切り）をやめて、
    1銘柄を数行の色つき表示にする。noteでもメールでも同じ見え方になる。
    """
    top10 = top_buy_candidates(screening, 10)
    if top10.empty:
        return ["- 該当なし"]
    lines: list[str] = []
    for order, (_, row) in enumerate(top10.iterrows(), start=1):
        rank = safe_text(row.get("rank"))
        code = safe_text(row.get("code"))
        name = safe_text(row.get("name"))
        score = _top10_score(row.get("score"))
        price = _top10_price(row.get("current_price"))
        reason = safe_text(row.get("reason"))
        lines.append(f"**{order}. {rank_mark(rank)} {rank}\u30E9\u30F3\u30AF\u3000{code}\u3000{name}**")
        lines.append(f"\u3000\U0001F4CA \u30B9\u30B3\u30A2 {score}\u3000\U0001F4B4 {price}")
        if reason and reason != "\u672A\u53D6\u5F97":
            lines.append(f"\u3000\U0001F4DD {reason}")
        lines.append("")
    return lines


PORTFOLIO_CAPITAL = 3_000_000  # paper_portfolio_discipline.CAPITAL と同値（表示用）

# 品質ゲート（validate_note_artifact.py）が確認する必須セクション見出し
PORTFOLIO_SECTION_HOLDINGS = "## 保有銘柄・CASH判断"
PORTFOLIO_SECTION_REASONS = "## 売買理由"
PORTFOLIO_SECTION_VALUATION = "## 評価額・現金比率"
PORTFOLIO_SECTION_PNL = "## 損益（未実現損益）"
PORTFOLIO_SECTION_NEXT_DAY = "## 次営業日の方針"
# T-K修正(2026-08-03): 「その日の配分案」を保有と誤読させないための独立見出し
PORTFOLIO_SECTION_CANDIDATES = "## 本日の買い候補（未約定・当日試算）"

# 本物の運用台帳。fix25(2026-08-23)でCodex(ChatGPT)勘定を廃止し、Claude勘定だけにした。
LEDGER_PATHS = {
    "claude": (
        PROJECT_ROOT / "data" / "claude_300man_orders.csv",
        PROJECT_ROOT / "data" / "claude_300man_journal.csv",
    ),
}


def _read_ledger(path: Path) -> pd.DataFrame:
    """本物の運用台帳を読む。無い・壊れているときは空を返す（推測で埋めない）。"""
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype=str).fillna("")
    except Exception:
        return pd.DataFrame()


def _ledger_rows(journal: pd.DataFrame, status: str) -> pd.DataFrame:
    if journal.empty or "status" not in journal.columns:
        return pd.DataFrame()
    return journal[journal["status"].astype(str).str.upper().eq(status)]


def _sum_col(df: pd.DataFrame, column: str) -> float:
    """列が無い／空なら0。台帳の列構成が運用ごとに違うため必ずこれを通す。"""
    if df is None or df.empty or column not in df.columns:
        return 0.0
    return float(pd.to_numeric(df[column], errors="coerce").fillna(0).sum())


def _today_candidate_block(discipline: pd.DataFrame) -> list[str]:
    """その日のスクリーニングから機械的に作った配分案。

    T-K修正(2026-08-03): これを「## 保有銘柄・CASH判断」の下に書いていたため、
    2026-07-16配信分で 9861 吉野家ホールディングス を1株も買っていないのに
    「枠2: 9861 吉野家ホールディングス 200株 @ 3626.0円」と保有のように配信した。
    paper_portfolio_discipline.build_discipline_portfolio() は当日のスクリーニングと
    地合いだけを入力にして毎回ゼロから作り直す（過去の注文も保有も読まない）ので、
    翌日には候補が入れ替わって消える。約定記録ではないことを見出しと各行で明示する。
    """
    lines = [PORTFOLIO_SECTION_CANDIDATES, ""]
    if discipline is None or discipline.empty:
        lines.append("- データ不足：本日の候補CSVが未生成のため、買い候補を表示できません。")
        lines.append("")
        return lines
    action = discipline.get("action", pd.Series(dtype=str)).astype(str).str.upper()
    buys = discipline[action == "BUY"]
    cashes = discipline[action == "CASH"]
    lines.append(
        "- ここは「もし本日ゼロから300万円を配分したら」という当日限りの試算です。"
        "**まだ約定していません**。実際の保有は上の運用台帳が正です。"
    )
    if buys.empty:
        lines.append(f"- 本日の買い候補: なし（CASH判断 {len(cashes)}枠）")
    else:
        for _, row in buys.iterrows():
            lines.append(
                f"- 候補枠{_val(row,'slot')}（未約定）: {_val(row,'code')} {_val(row,'name')} "
                f"{_val(row,'shares')}株 @ {_val(row,'entry_price')}円"
                f"（想定投資額 {_val(row,'position_value')}円）"
            )
        if not cashes.empty:
            lines.append(f"- 残り {len(cashes)}枠はCASH（現金）")
    lines.append("")
    return lines


def _next_day_block(discipline: pd.DataFrame) -> list[str]:
    """次営業日の規律方針。paper_portfolio_discipline.py のルールをそのまま書く（新規判断は書かない）。"""
    lines = [PORTFOLIO_SECTION_NEXT_DAY, ""]
    regime = ""
    if discipline is not None and not discipline.empty and "regime" in discipline.columns:
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



# fix37(2026-09-03): 保有銘柄の現在値と評価損益。
#   取れない銘柄は「現値未取得」と正直に書く（推測で埋めない）。
#   ネットワークが無い環境でも記事生成は止めない。
def _last_close(code: str) -> float | None:
    """銘柄コードの直近終値。取れなければ None。"""
    try:
        from scanner.prices import fetch_price_history

        history = fetch_price_history(f"{code}.T", period="1mo")
        if history is None or history.empty or "Close" not in history.columns:
            return None
        values = pd.to_numeric(history["Close"], errors="coerce").dropna()
        if values.empty:
            return None
        return float(values.iloc[-1])
    except Exception:
        return None


def _holding_prices(rows: pd.DataFrame) -> dict[str, float]:
    """保有中の銘柄について コード -> 直近終値。取れなかった銘柄は入らない。"""
    prices: dict[str, float] = {}
    if rows is None or rows.empty or "code" not in rows.columns:
        return prices
    for code in rows["code"].astype(str).str.strip().unique():
        if not code:
            continue
        price = _last_close(code)
        if price is not None:
            prices[code] = price
    print(f"holding_prices={len(prices)}/{len(rows)}", flush=True)
    return prices


def _holding_lines(rows: pd.DataFrame, prices: dict[str, float]) -> list[str]:
    """保有1件を2行で書く。

    1行目: 銘柄名 コード  損益  購入日（高重さんの指示の並び）
    2行目: 株数 ／ 取得単価 → 現在値（全角スペース始まり。描画側が小さく出す）
    """
    out: list[str] = []
    for _, row in rows.iterrows():
        code = str(row.get("code", "")).strip()
        name = _val(row, "name")
        shares = pd.to_numeric(row.get("shares"), errors="coerce")
        entry = pd.to_numeric(row.get("entry_price"), errors="coerce")
        price = prices.get(code)
        if price is not None and pd.notna(shares) and pd.notna(entry) and float(entry) > 0:
            pnl = (float(price) - float(entry)) * float(shares)
            pct = (float(price) / float(entry) - 1) * 100
            pnl_text = f"{pnl:+,.0f}円 ({pct:+.1f}%)"
            sub = f"　{shares:,.0f}株 ／ 取得 @{float(entry):,.0f}円 → 現在 {float(price):,.0f}円"
        else:
            pnl_text = "現値未取得"
            entry_text = f"@{float(entry):,.0f}円" if pd.notna(entry) else "取得単価 未取得"
            share_text = f"{shares:,.0f}株" if pd.notna(shares) else "株数 未取得"
            sub = f"　{share_text} ／ 取得 {entry_text}"
        out.append(f"{name} {code}  {pnl_text}  {_val(row, 'entry_date')} 購入")
        out.append(sub)
    return out


# fix39(2026-09-03): 「売買理由が読者にわからない」への対応。
# 高重さんの指摘：「ランクはわからない。ファンダメンタルなのかニュースなのかチャートなのかを書く」
#
# 事実として、この仕組みが銘柄を選ぶ根拠は scanner/scoring.py の score_stock だけで、
# 中身は「株価の位置・移動平均・高値更新・出来高・売買代金・1単元の金額」＝すべてチャートと出来高。
# ニュース本文や業績予想は選定に使っていない。決算は「発表日が確認できているか」だけを見て、
# 未確認ならSランクをAに落としている。ここではそれを読者の言葉で書く。

BUY_REASON_INTRO = (
    "この運用は「チャートの形」と「出来高・売買代金」だけで銘柄を選んでいます。"
    "ニュースの中身や業績の予想では買いません。決算は「発表日が確認できているか」だけを見ます。"
)

# 台帳に残っている条件名を、読者が読める言葉に置き換える。
# 置き換えられない語はそのまま出す（勝手に意味を足さない）。
BUY_CONDITION_WORDS = {
    "MA25上": "25日線の上",
    "MA75上": "75日線の上",
    "MA200上": "200日線の上",
    "MA25上向き": "25日線が上向き",
    "MA75上向き": "75日線が上向き",
    "MA200タッチ±3%": "200日線に接近",
    "CWH候補": "カップ・ウィズ・ハンドルの形",
    "出来高増加": "出来高が増えている",
    "52週高値3%以内": "1年の高値まで3%以内",
    "52週高値7%以内": "1年の高値まで7%以内",
    "52週高値15%以内": "1年の高値まで15%以内",
    "52週高値更新3日以内": "3日以内に1年の高値を更新",
    "52週高値更新7日以内": "7日以内に1年の高値を更新",
    "52週高値更新14日以内": "14日以内に1年の高値を更新",
}

# 出来高・売買代金・金額まわりの条件は別の行にまとめる
BUY_VOLUME_HINTS = ("売買代金", "出来高", "購入額")


def _explain_condition(word: str) -> str:
    """条件名を読める言葉にする。DUKEなど接頭辞つきのものも拾う。"""
    text = word.strip()
    if not text:
        return ""
    if text in BUY_CONDITION_WORDS:
        return BUY_CONDITION_WORDS[text]
    if text.startswith("テーマ加点:"):
        return f"テーマ株（{text.split(':', 1)[1]}）"
    if text.startswith("DUKE旧52週高値サポート"):
        return "前の高値が支えになっている"
    return text


def _split_buy_reason(reason: str) -> tuple[str, list[str], list[str], str]:
    """台帳の理由を「ランクの部分」「チャート条件」「出来高・規模の条件」「格下げの注記」に分ける。"""
    text = str(reason or "").strip()
    head, _, tail = text.partition("｜")
    chart: list[str] = []
    volume: list[str] = []
    note = ""
    for raw in tail.split("/"):
        word = raw.strip()
        if not word:
            continue
        if "ゲート未達" in word or "決算未確認" in word:
            note = word
            continue
        explained = _explain_condition(word)
        if any(hint in word for hint in BUY_VOLUME_HINTS):
            volume.append(explained)
        else:
            chart.append(explained)
    return head.strip(), chart, volume, note


def _rank_meaning(head: str, note: str) -> str:
    """「Aランク・スコア166」だけでは読者に伝わらないので、点の意味を添える。"""
    body = head.replace("自動発注（S→A→B順）", "").strip() or "ランクの記録なし"
    if note:
        gate = note
        if "で最大A" in gate:
            gate = gate.replace("で最大A", "").replace("Sゲート未達", "").strip("() ")
            gate = gate.replace("strict", "").strip("() ")
            return f"{body}（Sの必須条件に届かずAどまり：{gate}）"
        return f"{body}（{gate}）"
    return f"{body}（S＝85点以上で必須条件すべて / A＝70点以上 / B＝55点以上）"


def _buy_reason_lines(open_rows: pd.DataFrame, orders: pd.DataFrame) -> list[str]:
    """「なぜ買ったか」。いま持っている銘柄ごとに、選んだ根拠とチャートを出す。

    fix39(2026-09-03): ランクとスコアだけでは読者に伝わらないので、
    「チャートで選んでいる」ことと、実際に当てはまった条件を書く。
    条件は台帳に記録されているものだけを出す。記録が無い買付は「記録なし」と正直に書く。
    """
    lines: list[str] = []
    if orders is None or orders.empty:
        return ["- 台帳に注文がないため、売買理由はありません（約定ゼロ）。"]

    buys = orders[orders.get("side", pd.Series(dtype=str)).astype(str).str.upper() == "BUY"]
    held = set(open_rows["code"].astype(str).str.strip()) if not open_rows.empty else set()
    shown = 0
    for _, row in buys.iterrows():
        code = str(row.get("code", "")).strip()
        if code not in held:
            continue
        if shown == 0:
            lines.append(BUY_REASON_INTRO)
            lines.append("")
        shown += 1
        head, chart, volume, note = _split_buy_reason(_val(row, "reason"))
        lines.append(f"{_val(row, 'name')} {code}")
        if chart:
            lines.append(f"　チャートの形：{' ／ '.join(chart)}")
        if volume:
            lines.append(f"　出来高・規模：{' ／ '.join(volume)}")
        if not chart and not volume:
            lines.append(
                "　当てはまった条件：買った当時の記録が台帳にありません"
                "（2026-09-03以降に買ったぶんから残します）"
            )
        lines.append(f"　ランク：{_rank_meaning(head, note)}")
        lines.append(
            f"　いつ：{_val(row, 'decision_date')} に判断 → "
            f"{_val(row, 'execution_date')} の寄付きで買付"
        )
        lines.append(f"　📈 チャート: {_chart_url(code)}")
        lines.append("")
    if shown == 0:
        lines.append("- いま保有している銘柄はありません。")

    sells = orders[orders.get("side", pd.Series(dtype=str)).astype(str).str.upper() == "SELL"]
    if not sells.empty:
        lines.append("手じまい済み")
        for _, row in sells.iterrows():
            lines.append(
                f"- {_val(row, 'decision_date')} 判断 → {_val(row, 'execution_date')} 売却 "
                f"{_val(row, 'name')} {_val(row, 'code')}：{_val(row, 'reason')}"
            )
    return lines


def _portfolio_status_block(discipline: pd.DataFrame, operation: str = "claude") -> list[str]:
    """300万円運用の運用状況セクション。

    T-K修正(2026-08-03): 保有・売買理由・評価額・損益は data/*_300man_journal.csv と
    data/*_300man_orders.csv（実際に約定した本物の台帳）から出す。
    fix37(2026-09-03): 並びを「保有 → 評価額 → 損益 → なぜ買ったか → 次営業日」に変更。
    保有は「銘柄名 コード 損益 購入日」の順。現在値を取りに行って損益を出す。
    """
    orders_path, journal_path = LEDGER_PATHS.get(operation, LEDGER_PATHS["claude"])
    orders = _read_ledger(orders_path)
    journal = _read_ledger(journal_path)
    open_rows = _ledger_rows(journal, "OPEN")
    closed_rows = _ledger_rows(journal, "CLOSED")

    invested = _sum_col(open_rows, "position_value")
    bought = _sum_col(journal, "position_value")
    sold = _sum_col(journal, "exit_value")
    realized = _sum_col(closed_rows, "realized_pnl")
    cash = PORTFOLIO_CAPITAL - bought + sold
    exit_value_missing = (
        not closed_rows.empty and "exit_value" not in closed_rows.columns
    )

    prices = _holding_prices(open_rows)
    market_value = 0.0
    unrealized = 0.0
    priced = 0
    for _, _row in open_rows.iterrows():
        _code = str(_row.get("code", "")).strip()
        _shares = pd.to_numeric(_row.get("shares"), errors="coerce")
        _entry = pd.to_numeric(_row.get("entry_price"), errors="coerce")
        _price = prices.get(_code)
        if _price is None or pd.isna(_shares) or pd.isna(_entry):
            continue
        priced += 1
        market_value += float(_price) * float(_shares)
        unrealized += (float(_price) - float(_entry)) * float(_shares)
    all_priced = priced > 0 and priced == len(open_rows)

    lines: list[str] = []

    # ① 保有銘柄・CASH判断（台帳＝約定した記録だけ）
    lines.extend([PORTFOLIO_SECTION_HOLDINGS, ""])
    # fix38: どういう決まりで売り買いしているのかを毎回1行で置く。
    lines.extend([PORTFOLIO_RULE_LINE, ""])
    if journal.empty:
        lines.append(f"- 約定はまだありません → CASH（現金 {PORTFOLIO_CAPITAL:,}円）")
    elif open_rows.empty:
        lines.append(f"- 保有なし → CASH（現金 {cash:,.0f}円）")
    else:
        lines.extend(_holding_lines(open_rows, prices))
        lines.append(f"現金 {cash:,.0f}円")
    # 出所は末尾に小さく。品質ゲートがこの台帳名を見て「本物の記録か」を判定する
    # （2026-07-16 に買っていない銘柄を保有として配信した事故の再発防止）。
    lines.append(f"　出所 {journal_path.name}（実際に約定した記録だけ）")
    lines.append("")

    # ② 評価額・現金比率
    lines.extend([PORTFOLIO_SECTION_VALUATION, ""])
    lines.append(f"- 運用資金: {PORTFOLIO_CAPITAL:,}円")
    lines.append(f"- 投資額（取得原価・保有中）: {invested:,.0f}円")
    lines.append(f"- 現金: {cash:,.0f}円（現金比率 {cash / PORTFOLIO_CAPITAL * 100:.1f}%）")
    if exit_value_missing:
        lines.append("- データ不足：台帳に exit_value 列が無いため、売却代金を現金に反映できていません。")
    if all_priced:
        lines.append(f"- 評価額（現値ベース・保有中）: {market_value:,.0f}円")
        lines.append(f"- 総資産（評価額＋現金）: {market_value + cash:,.0f}円")
    elif priced > 0:
        lines.append(f"- 評価額（現値が取れた{priced}銘柄ぶん）: {market_value:,.0f}円")
        lines.append(f"- データ不足：{len(open_rows) - priced}銘柄は現値を取得できませんでした。")
    elif not open_rows.empty:
        lines.append("- データ不足：現値を取得できなかったため、時価評価は出していません（取得原価ベースです）。")
    lines.append("")

    # ③ 損益
    lines.extend([PORTFOLIO_SECTION_PNL, ""])
    lines.append(f"- 実現損益（累計）: {realized:+,.0f}円")
    if open_rows.empty:
        lines.append("- 未実現損益: 0円（保有なし）")
    elif priced > 0:
        _pct = f"（{unrealized / invested * 100:+.1f}%）" if all_priced and invested else ""
        lines.append(f"- 未実現損益: {unrealized:+,.0f}円{_pct}")
        if not all_priced:
            lines.append(f"- データ不足：現値を取得できた{priced}銘柄ぶんの合計です。")
        lines.append(f"- 合計損益（実現＋未実現）: {realized + unrealized:+,.0f}円")
    else:
        lines.append("- データ不足：現値が未取得のため、未実現損益は算出していません（推測では書きません）。")
    lines.append("")

    # ④ 確定トレードの成績（fix38: 勝率と平均。台帳にある確定分だけ）
    lines.extend(_closed_trade_record_lines(closed_rows))

    # ⑤ なぜ買ったか（高重さんの指示で損益のあとに置く）
    lines.extend([PORTFOLIO_SECTION_REASONS, ""])
    lines.extend(_buy_reason_lines(open_rows, orders))
    lines.append("")

    # ⑤ 次営業日の方針
    lines.extend(_next_day_block(discipline))

    # ⑥ 本日の買い候補（未約定・当日試算）— 台帳と混ぜない
    lines.extend(_today_candidate_block(discipline))
    return lines



# fix38(2026-09-03): 確定したトレードの成績と、運用ルールの1行。
# 台帳（claude_300man_journal.csv）に実際に残っている記録だけから作る。推測はしない。

PORTFOLIO_SECTION_RECORD = "## 確定トレードの成績"

# 規律の値。paper_portfolio_discipline.py / claude_300man_declare.py と同じ。
PORTFOLIO_RULE_LINE = (
    "ルール：1枠100万円・最大3銘柄 ／ 損切 -7% ／ 利確 +15% ／ 10営業日で手じまい"
)


def _hold_days(entry_date: str, exit_date: str) -> int | None:
    """買った日から売った日までの日数。片方でも読めなければ None（推測しない）。"""
    from datetime import date as _date

    def _parse(value: str):
        text = str(value or "").strip()[:10]
        if len(text) != 10:
            return None
        try:
            return _date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
        except (ValueError, TypeError):
            return None

    start, end = _parse(entry_date), _parse(exit_date)
    if start is None or end is None:
        return None
    return (end - start).days


def _closed_trade_record_lines(closed_rows) -> list[str]:
    """手じまいが済んだトレードだけを数えて、勝率と平均を出す。

    fix38(2026-09-03): 読者が「このやり方は当たっているのか」を判断できるようにする。
    件数が少ないうちは参考値だと断る。
    """
    lines: list[str] = [PORTFOLIO_SECTION_RECORD, ""]
    if closed_rows is None or closed_rows.empty:
        lines.append("- まだ手じまいが済んだトレードがありません（成績は次回以降）。")
        lines.append("")
        return lines

    trades: list[tuple[str, str, float, float, int | None, str, str]] = []
    for _, row in closed_rows.iterrows():
        pnl = pd.to_numeric(row.get("realized_pnl"), errors="coerce")
        entry = pd.to_numeric(row.get("entry_price"), errors="coerce")
        shares = pd.to_numeric(row.get("shares"), errors="coerce")
        if pd.isna(pnl) or pd.isna(entry) or pd.isna(shares):
            continue
        cost = float(entry) * float(shares)
        if cost <= 0:
            continue
        trades.append((
            _val(row, "name"),
            _val(row, "code"),
            float(pnl),
            float(pnl) / cost * 100,
            _hold_days(_val(row, "entry_date"), _val(row, "exit_date")),
            _val(row, "entry_date"),
            _val(row, "exit_date"),
        ))

    if not trades:
        lines.append("- データ不足：確定した売買の値が読めないため、成績は出していません。")
        lines.append("")
        return lines

    total = len(trades)
    wins = [t for t in trades if t[2] > 0]
    losses = [t for t in trades if t[2] < 0]
    net = sum(t[2] for t in trades)
    held = [t[4] for t in trades if t[4] is not None]

    lines.append(f"- 確定したトレード: {total}件（勝ち {len(wins)}件 ／ 負け {len(losses)}件）")
    lines.append(f"- 勝率: {len(wins) / total * 100:.0f}%")
    lines.append(f"- 確定損益の合計: {net:+,.0f}円")
    lines.append(f"- 1トレードあたりの平均: {sum(t[3] for t in trades) / total:+.1f}%")
    if wins:
        lines.append(f"- 平均の勝ち: {sum(t[3] for t in wins) / len(wins):+.1f}%")
    if losses:
        lines.append(f"- 平均の負け: {sum(t[3] for t in losses) / len(losses):+.1f}%")
    if held:
        lines.append(f"- 平均の保有日数: {sum(held) / len(held):.0f}日")
    best = max(trades, key=lambda item: item[3])
    worst = min(trades, key=lambda item: item[3])
    lines.append(f"- 一番良かった: {best[0]} {best[1]} {best[3]:+.1f}%（{best[5]} → {best[6]}）")
    if worst[1] != best[1]:
        lines.append(f"- 一番悪かった: {worst[0]} {worst[1]} {worst[3]:+.1f}%（{worst[5]} → {worst[6]}）")
    if total < 10:
        lines.append(
            f"- ※ まだ{total}件だけなので、勝率も平均も参考値です。件数が増えるまで当てになりません。"
        )
    lines.append("")
    return lines


# fix30(2026-08-23): 52週新高値の記事にある「業種別の偏り」を押し目にも入れる。
def _pullback_sector_line(pullback: pd.DataFrame) -> str:
    """候補が多い業種を上位3つ並べた1行。数えられなければ空文字（捏造しない）。"""
    if pullback is None or pullback.empty or "sector" not in pullback.columns:
        return ""
    counts = (
        pullback["sector"].astype(str).str.strip()
        .replace({"": None, "nan": None, "None": None})
        .dropna()
        .value_counts()
    )
    if counts.empty:
        return ""
    top = [f"{name}（{int(n)}銘柄）" for name, n in counts.head(3).items()]
    return (
        "本日の押し目候補を業種別に数えると、" + "、".join(top) + "に集まりました。"
        "同じ業種に偏っている日は、その業種の地合いに引きずられやすい点に注意してください。"
    )


# fix30(2026-08-23): 新規読者が毎回ゼロから理解しなくて済むよう、読み方を固定で置く。
PULLBACK_HOWTO_LINES = (
    "## この記事の読み方",
    "",
    "- **52週新高値後リテスト**：一度新高値を取った銘柄が、その高値ラインまで戻ってきたところです。",
    "- **25MA／200MA／240MAタッチ**：株価が25日／200日／240日移動平均線に触れたところです。",
    "- **上向きの移動平均線に触れた銘柄だけ**を拾います。下向き（下落トレンド）は「落ちるナイフ」なので入れません。",
    "- カードは各分類の**上位数件**です。全件はメール添付のCSVにあります。",
    "- 掲載は毎営業日、**同じ基準で機械的に**行います。裁量で足したり引いたりしません。",
    "",
)


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
    sector_line = _pullback_sector_line(pullback)
    if sector_line:
        lines.append(sector_line)
    lines.append("")
    lines.extend(PULLBACK_HOWTO_LINES)
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
        lines.extend(build_stock_cards(rt, PULLBACK_CARD_CAP))
        # fix28(2026-08-23): 従来表は高重さんの指示で廃止。全件は添付CSVで見る。
        lines.append("")
        lines.append(
            f"※ この分類の候補は全{len(rt)}件です。上位{min(len(rt), PULLBACK_CARD_CAP)}件だけカードで掲載しています"
            f"（全件はメール添付の screening_pullback_*.csv）。"
        )
    lines.append("")

    # ②③④ 25/200/240MAタッチ
    # fix41(2026-09-03): 同じ銘柄が3つの分類に重複して出ていた（最大10回）。
    #   先に出たところだけに載せ、残りは「〜にも同時タッチ」と見出しに書く。
    _ma_flags = (("ma25_touch", "25日線"), ("ma200_touch", "200日線"), ("ma240_touch", "240日線"))
    _touched: dict[str, list[str]] = {}
    for _flag, _label in _ma_flags:
        for _code in bucket(pullback, _flag).get("code", pd.Series(dtype=str)).astype(str).str.strip():
            _touched.setdefault(_code, []).append(_label)
    _shown: set[str] = set()
    for flag, title in (("ma25_touch", "25MAタッチ"), ("ma200_touch", "200MAタッチ"), ("ma240_touch", "240MAタッチ")):
        lines.append(f"## 【{title}】")
        lines.append("")
        b = bucket(pullback, flag)
        if not b.empty:
            _codes = b["code"].astype(str).str.strip()
            b = b[~_codes.isin(_shown)].copy()
            if not b.empty:
                _this = str(dict(_ma_flags)[flag])
                b["pullback_also"] = [
                    "・".join(label for label in _touched.get(str(c).strip(), []) if label != _this)
                    for c in b["code"]
                ]
                _shown.update(b["code"].astype(str).str.strip().head(PULLBACK_CARD_CAP))
        if b.empty:
            lines.append("- 該当なし（ほかの分類で掲載ずみ）" if flag != "ma25_touch" else "- 該当なし")
        else:
            lines.extend(build_stock_cards(b, PULLBACK_CARD_CAP))
            # fix28(2026-08-23): 従来表は高重さんの指示で廃止。全件は添付CSVで見る。
            lines.append("")
            lines.append(
                f"※ この分類の候補は全{len(b)}件です。上位{min(len(b), PULLBACK_CARD_CAP)}件だけカードで掲載しています"
                f"（全件はメール添付の screening_pullback_*.csv）。"
            )
        lines.append("")

    # fix30(2026-08-23): バックテスト博士の実績セクション（押し目版）。
    # 記録が浅いうちは「データ不足」と正直に出る。ここが落ちても記事は止めない。
    try:
        from track_record import (
            build_pullback_track_record_lines,
            load_pullback_track_record_summary,
        )

        lines.extend(build_pullback_track_record_lines(load_pullback_track_record_summary()))
    except Exception:
        lines.extend([
            "## 実績（過去に掲載した銘柄のその後）",
            "",
            "> データ不足：押し目候補の実績は2026-08-23から記録を始めました。営業日を重ねると自動表示されます。",
            "",
        ])

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


def _filter_highs_to_target_date(highs: pd.DataFrame) -> tuple[pd.DataFrame, object, int]:
    """Keep only rows from the latest valid data_date.

    Empty candidate data remains a valid "no candidates" article. Non-empty data
    without any valid data_date is treated as broken input and must not publish.
    """
    if highs.empty:
        return highs.copy(), _prev_jst_business_day(), 0
    if "data_date" not in highs.columns:
        raise ValueError("screening_highs has rows but no data_date column")
    parsed = pd.to_datetime(highs["data_date"], errors="coerce").dt.date
    valid = parsed.dropna()
    if valid.empty:
        raise ValueError("screening_highs has rows but all data_date values are missing or invalid")
    target = max(valid)
    keep = parsed.eq(target)
    filtered = highs.loc[keep].copy()
    excluded = int((~keep).sum())
    print(
        f"note_highs_data_date={target.isoformat()} kept={len(filtered)} excluded_mixed_or_missing={excluded}",
        flush=True,
    )
    return filtered, target, excluded


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
            f"対象営業日（{ref.isoformat()}）に52週新高値を更新しました。",
            "1年分の高値を上抜け、52週新高値を付けました。",
            f"対象営業日（{ref.isoformat()}）の高値で52週レンジの上限を更新しています。",
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
        parts.append(f"対象営業日は出来高が平常時の{vr:.1f}倍に膨らみ、需給に変化が出ています。")
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
    status = f"対象営業日（{ref.isoformat()}）に52週新高値を更新" if is_new else (f"新高値まであと{dist:.2f}%" if dist is not None else "新高値接近")
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
        add("52週高値までの距離：", f"対象営業日（{ref.isoformat()}）に更新")
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
            # fix41(2026-09-03): 取れなかったことは読者に何も伝えないので書かない。
            pass
    lines.extend(_earnings_note_lines(row, ref))
    prev_high_date = str(row.get("high_date") or "").strip()
    if prev_high_date and prev_high_date.lower() not in ("nan", "none", "null"):
        add("📅 52週高値日：", prev_high_date)
    lines.append("")
    lines.append(f"🔍 **注目ポイント**：{_highs_comment(row, ref, is_new)}")
    lines.append("")
    lines.append(
        f"📈 6ヶ月日足チャート（Yahoo!ファイナンス）: "
        f"https://finance.yahoo.co.jp/quote/{code}.T/chart?frm=dly&trm=6m&scl=stndrd&styl=cndl&evnts=volume&ovrIndctr=sma%2Cmma%2Clma&addIndctr=&compare="
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
        "## 対象営業日の集計",
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

    highs, ref, excluded = _filter_highs_to_target_date(highs)
    lines = [f"# {_jp_date_text(ref)} {_HIGHS_TITLE_SUFFIX}", ""]
    lines.append(f"※ 基準日（価格データ最終日 data_as_of）：**{ref.isoformat()}**")
    if excluded:
        lines.append(f"※ 基準日と異なる行・日付欠損行は{excluded}件除外しました。")
    lines.append("")
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
        (f"## 【A】52週新高値に対象営業日（{ref.isoformat()}）に到達した銘柄", new_main, True, HIGHS_DETAIL_CAP),
        ("## 【B】52週新高値まで3%以内に接近している銘柄", near_main, False, HIGHS_DETAIL_CAP),
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
        # fix34(2026-08-28): 一覧表は全件だと4分割になるので行数を絞る。
        table_df = df.head(HIGHS_TABLE_ROW_CAP)
        lines.extend(_highs_overview_table(table_df))
        if len(df) > len(table_df):
            lines.append("")
            lines.append(
                f"※ この分類は全{len(df)}銘柄です。優先度の高い{len(table_df)}銘柄を表に載せています"
                f"（全件はメール添付の screening_highs_*.csv）。"
            )
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


# ============================================================================
# T-P(2026-08-10): noteの下書きがスマホで開けない問題への対策。
#   note本文が3万字級になるとnoteのエディタ（ProseMirror）が描画しきれず、
#   スマホでは開けない／保存後の再確認も失敗する（52週新高値の保存失敗）。
#   そこで「見出し単位で複数の下書きに分割」する。内容は一切削らない。
# ============================================================================

NOTE_SPLIT_MAX_CHARS = int(os.getenv("NOTE_SPLIT_MAX_CHARS", "12000") or "12000")
# fix34(2026-08-28): 記事を1本に収めるための掲載件数の上限。
# 高重さんの指示「四分割はやめて、要約して軽くして」。設定は「ほどほどに軽く」。
# まずはこの値で作り、それでも分割しきい値を超える日は build_note4 が1件ずつ減らす。
# 削った分は全件がメール添付のCSVに残る（記事には件数を明記する）。
HIGHS_TABLE_ROW_CAP = 20   # 52週新高値：【A】【B】それぞれの一覧表の行数
HIGHS_DETAIL_CAP = 4       # 52週新高値：【A】【B】それぞれの銘柄詳細の件数
PULLBACK_CARD_CAP = 6      # 押し目：分類ごとのカード件数
NOTE_SHRINK_FLOOR = 2      # 自動で減らすときの下限（これより下げない）


NOTE_SPLIT_KEYS = tuple(
    k.strip() for k in os.getenv("NOTE_SPLIT_KEYS", "highs,pullback").split(",") if k.strip()
)


def _split_by_heading(text: str, prefix: str) -> list[str]:
    """指定した見出し記号（'## ' など）で本文を塊に分ける。見出し前の文章は先頭の塊に残す。"""
    parts: list[str] = []
    cur: list[str] = []
    for line in text.split("\n"):
        if line.startswith(prefix) and cur:
            parts.append("\n".join(cur))
            cur = [line]
        else:
            cur.append(line)
    if cur:
        parts.append("\n".join(cur))
    return parts


def _atomize_markdown(text: str, max_chars: int) -> list[str]:
    """max_chars を超えない塊のリストにする。'## ' → '### ' → 行 の順に細かくする。"""
    if len(text) <= max_chars:
        return [text]
    for prefix in ("## ", "### "):
        chunks = _split_by_heading(text, prefix)
        if len(chunks) > 1:
            out: list[str] = []
            for chunk in chunks:
                out.extend(_atomize_markdown(chunk, max_chars))
            return out
    # 見出しで割れない（巨大な表など）→ 行単位で詰める
    out = []
    cur: list[str] = []
    size = 0
    for line in text.split("\n"):
        if cur and size + len(line) + 1 > max_chars:
            out.append("\n".join(cur))
            cur = []
            size = 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        out.append("\n".join(cur))
    return out


def split_note_markdown(body: str, max_chars: int | None = None) -> list[str]:
    """noteの本文を複数の下書きに分割する。短ければ1本のまま返す（内容は削らない）。"""
    limit = NOTE_SPLIT_MAX_CHARS if max_chars is None else max_chars
    if limit <= 0 or len(body) <= limit:
        return [body]
    lines = body.split("\n")
    if lines and lines[0].startswith("# "):
        title_line = lines[0]
        rest = "\n".join(lines[1:]).lstrip("\n")
    else:
        title_line = ""
        rest = body
    budget = max(limit - 200, 1000)
    packed: list[str] = []
    cur: list[str] = []
    size = 0
    for block in _atomize_markdown(rest, budget):
        if cur and size + len(block) + 1 > budget:
            packed.append("\n".join(cur))
            cur = []
            size = 0
        cur.append(block)
        size += len(block) + 1
    if cur:
        packed.append("\n".join(cur))
    if len(packed) <= 1:
        return [body]
    total = len(packed)
    out: list[str] = []
    for index, part in enumerate(packed, start=1):
        head: list[str] = []
        if title_line:
            head.append(f"{title_line}（{index}/{total}）")
            head.append("")
        head.append(
            f"※ スマホでも開けるように、この記事は全{total}本に分割しています。これは{index}本目です。"
        )
        head.append("")
        out.append("\n".join(head) + part.strip("\n") + "\n")
    return out


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

    # fix34(2026-08-28): 高重さんの指示で記事は1本に収める。
    #   まず上限どおりに作り、分割しきい値を超えていたら件数を1件ずつ減らして作り直す。
    #   市場ステータスを後から差し込む分、しきい値には余裕（800字）を見る。
    global HIGHS_TABLE_ROW_CAP, HIGHS_DETAIL_CAP, PULLBACK_CARD_CAP
    budget = max(NOTE_SPLIT_MAX_CHARS - 800, 3000)
    notes: dict[str, str] = {}
    for attempt in range(8):
        notes = {
            "claude": build_claude_note(screening, discipline, backtest, sources),
            "pullback": build_pullback_note(pullback, pullback_src),
            "highs": build_highs_note(highs, highs_src),
        }
        over = {k: len(v) for k, v in notes.items() if k in NOTE_SPLIT_KEYS and len(v) > budget}
        if not over:
            break
        if HIGHS_DETAIL_CAP <= NOTE_SHRINK_FLOOR and PULLBACK_CARD_CAP <= NOTE_SHRINK_FLOOR:
            print(f"note_shrink=floor_reached over={over}", flush=True)
            break
        if "highs" in over:
            HIGHS_DETAIL_CAP = max(NOTE_SHRINK_FLOOR, HIGHS_DETAIL_CAP - 1)
            HIGHS_TABLE_ROW_CAP = max(10, HIGHS_TABLE_ROW_CAP - 3)
        if "pullback" in over:
            PULLBACK_CARD_CAP = max(NOTE_SHRINK_FLOOR, PULLBACK_CARD_CAP - 1)
        print(
            f"note_shrink=retry{attempt + 1} over={over} "
            f"highs_detail={HIGHS_DETAIL_CAP} highs_rows={HIGHS_TABLE_ROW_CAP} "
            f"pullback_cards={PULLBACK_CARD_CAP}",
            flush=True,
        )
    # 3本すべての冒頭に市場ステータスを挿入（空欄禁止）
    status_lines = _market_status_block()
    notes = {key: _insert_market_status(body, status_lines) for key, body in notes.items()}
    manifest: list[dict[str, str]] = []
    for key, body in notes.items():
        parts = split_note_markdown(body) if key in NOTE_SPLIT_KEYS else [body]
        print(f"note_chars[{key}]={len(body)} split={len(parts)}")
        for index, part in enumerate(parts, start=1):
            part_key = key if index == 1 else f"{key}{index}"
            manifest.append(write_one_note(part_key, part, chart_rel_path(key) if index == 1 else None))
    # fix25(2026-08-23): ChatGPT(Codex)版を廃止したので、作った3本をそのまま保存する。
    autosave_manifest = list(manifest)
    NOTE4_MANIFEST_PATH.write_text(json.dumps(autosave_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved={NOTE4_MANIFEST_PATH}")
    print(f"note_manifest_keys={','.join(str(e.get('key')) for e in autosave_manifest)}")
    # 完成条件: 3本すべて生成され、各冒頭に市場ステータスが入っていなければ失敗扱い
    broken: list[str] = []
    for key in NOTE4_TITLES:
        md_path = OUTPUT_DIR / f"note_{key}.md"
        if not md_path.exists() or md_path.stat().st_size == 0:
            broken.append(f"note_{key}.md 未生成")
            continue
        text = md_path.read_text(encoding="utf-8")
        if "## 市場ステータス" not in text or not any(f"**{v}**" in text for v in NOTE4_VALID_REGIMES):
            broken.append(f"note_{key}.md 市場ステータス欠落")
    if len(manifest) < len(NOTE4_TITLES) or broken:
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
