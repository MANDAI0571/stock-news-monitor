from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "outputs"
NOTE_PATH = OUTPUT_DIR / "note_daily.md"


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


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    sources = load_sources()
    screening = load_screening(sources.screening)
    discipline = load_discipline(sources.discipline)
    backtest = load_backtest(sources.backtest)
    note = build_note_body(screening, discipline, backtest, sources)
    NOTE_PATH.write_text(note, encoding="utf-8")
    print(f"saved={NOTE_PATH}")
    print(f"screening={sources.screening}")
    print(f"discipline={sources.discipline}")
    print(f"backtest={sources.backtest if sources.backtest else '未取得'}")


if __name__ == "__main__":
    main()
