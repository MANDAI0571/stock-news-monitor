from __future__ import annotations

import argparse
import os
from pathlib import Path

import pandas as pd


def latest_file(output_dir: Path, pattern: str) -> Path | None:
    files = [p for p in output_dir.glob(pattern) if p.exists() and p.stat().st_size > 0]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def read_csv(path: Path | None) -> pd.DataFrame:
    if path is None or not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, encoding="utf-8-sig")


def summarize_outputs(output_dir: str | Path = "outputs") -> dict[str, object]:
    output_dir = Path(output_dir)
    result_path = output_dir / "screening_result.csv"
    highs_path = latest_file(output_dir, "screening_highs_*.csv")
    pullback_path = latest_file(output_dir, "screening_pullback_*.csv")
    retest_path = latest_file(output_dir, "screening_52w_retest_*.csv")
    decision_path = output_dir / "decision_result.csv"

    result = read_csv(result_path)
    highs = read_csv(highs_path)
    pullback = read_csv(pullback_path)
    retest = read_csv(retest_path)
    decision = read_csv(decision_path)

    rank = result.get("rank", pd.Series(dtype=object)).astype(str).str.upper()
    reason = result.get("reason", pd.Series(dtype=object)).astype(str)
    high_type = highs.get("high_type", pd.Series(dtype=object)).astype(str)

    if not decision.empty and "decision" in decision.columns:
        buy_count = int(decision["decision"].astype(str).eq("BUY").sum())
        watch_count = int(decision["decision"].astype(str).eq("WATCH").sum())
        skip_count = int(decision["decision"].astype(str).eq("SKIP").sum())
    else:
        buy_count = int(rank.isin(["S", "A", "B"]).sum())
        watch_count = 0
        skip_count = 0

    return {
        "quick_mode": os.environ.get("QUICK_MODE", ""),
        "max_symbols": os.environ.get("MAX_SYMBOLS", ""),
        "screening_result_rows": int(len(result)),
        "rank_s": int(rank.eq("S").sum()),
        "rank_a": int(rank.eq("A").sum()),
        "rank_b": int(rank.eq("B").sum()),
        "rank_c": int(rank.eq("C").sum()),
        "rank_skip": int(rank.eq("SKIP").sum()),
        "buy_candidates": buy_count,
        "watch_candidates": watch_count,
        "skip_candidates": skip_count,
        "new_52w_high_candidates": int(high_type.eq("52W_NEW_HIGH").sum()),
        "near_52w_high_candidates": int(high_type.eq("52W_NEAR_HIGH").sum()),
        "pullback_rows": int(len(pullback)),
        "ma25_pullback_candidates": int(pullback.get("ma25_touch", pd.Series(dtype=bool)).fillna(False).astype(bool).sum()),
        "ma200_touch_candidates": int(pullback.get("ma200_touch", pd.Series(dtype=bool)).fillna(False).astype(bool).sum()),
        "retest_52w_candidates": int(pullback.get("retest_52w", pd.Series(dtype=bool)).fillna(False).astype(bool).sum()),
        "retest_52w_buy_candidates": int(retest.get("candidate_action", pd.Series(dtype=object)).astype(str).eq("BUY").sum()),
        "error_rows": int(reason.str.contains("エラー|ERROR|Traceback|Exception", case=False, regex=True, na=False).sum()),
        "missing_price_rows": int(reason.str.contains("価格データ不足", regex=False, na=False).sum()),
        "missing_files": [
            name
            for name, path in {
                "screening_result.csv": result_path,
                "decision_result.csv": decision_path,
                "screening_highs_*.csv": highs_path,
                "screening_pullback_*.csv": pullback_path,
                "screening_52w_retest_*.csv": retest_path,
            }.items()
            if path is None or not Path(path).exists() or Path(path).stat().st_size == 0
        ],
    }


def build_markdown(summary: dict[str, object]) -> str:
    missing = summary.get("missing_files") or []
    lines = [
        "## JP Screening Summary",
        "",
        f"- QUICK_MODE: `{summary['quick_mode']}`",
        f"- MAX_SYMBOLS: `{summary['max_symbols']}`",
        f"- screening_result rows: `{summary['screening_result_rows']}`",
        f"- S/A/B/C/SKIP ranks: `S={summary['rank_s']} A={summary['rank_a']} B={summary['rank_b']} C={summary['rank_c']} SKIP={summary['rank_skip']}`",
        f"- BUY candidates: `{summary['buy_candidates']}`",
        f"- WATCH/SKIP: `WATCH={summary['watch_candidates']} SKIP={summary['skip_candidates']}`",
        f"- 52w new-high candidates: `{summary['new_52w_high_candidates']}`",
        f"- 52w near-high candidates: `{summary['near_52w_high_candidates']}`",
        f"- 25MA pullback candidates: `{summary['ma25_pullback_candidates']}`",
        f"- 200MA touch candidates: `{summary['ma200_touch_candidates']}`",
        f"- 52w retest candidates: `{summary['retest_52w_candidates']}`",
        f"- 52w retest BUY candidates: `{summary['retest_52w_buy_candidates']}`",
        f"- error rows: `{summary['error_rows']}`",
        f"- price-data-missing rows: `{summary['missing_price_rows']}`",
    ]
    if missing:
        lines.append(f"- Missing/empty result files: `{', '.join(str(x) for x in missing)}`")
    return "\n".join(lines) + "\n"


def print_summary(output_dir: str | Path = "outputs", github_step_summary: bool = True) -> dict[str, object]:
    summary = summarize_outputs(output_dir)
    markdown = build_markdown(summary)
    print(markdown, flush=True)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if github_step_summary and step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(markdown)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Print JP screening output summary for logs and GitHub Actions.")
    parser.add_argument("--output-dir", default="outputs")
    args = parser.parse_args()
    print_summary(args.output_dir)


if __name__ == "__main__":
    main()
