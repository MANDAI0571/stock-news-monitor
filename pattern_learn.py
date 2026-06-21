from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from trade_journal import load_journal


PROJECT_ROOT = Path(__file__).resolve().parent
MIN_SAMPLES = 5


def build_pattern_summary(journal: pd.DataFrame) -> pd.DataFrame:
    closed = journal[journal["exit_date"].fillna("").astype(str).ne("")].copy()
    if len(closed) < MIN_SAMPLES:
        return pd.DataFrame([{"section": "status", "metric": "データ蓄積中", "count": len(closed), "detail": f"{MIN_SAMPLES}件未満"}])

    closed["result"] = closed["result"].fillna("")
    rows: list[dict[str, object]] = []
    rows.extend(_summarize_group(closed[closed["result"].eq("WIN")], "勝ちパターン"))
    rows.extend(_summarize_group(closed[closed["result"].eq("LOSS")], "負けパターン"))
    rows.append({"section": "決算失敗", "metric": "件数", "count": int(closed["earnings_failure"].fillna(False).astype(bool).sum()), "detail": ""})
    rows.append({"section": "地合い失敗", "metric": "件数", "count": int(closed["regime_failure"].fillna(False).astype(bool).sum()), "detail": ""})
    return pd.DataFrame(rows)


def _summarize_group(group: pd.DataFrame, section: str) -> list[dict[str, object]]:
    if group.empty:
        return [{"section": section, "metric": "件数", "count": 0, "detail": ""}]
    return [
        {"section": section, "metric": "件数", "count": len(group), "detail": ""},
        {"section": section, "metric": "平均スコア", "count": round(pd.to_numeric(group["entry_score"], errors="coerce").mean(), 2), "detail": ""},
        {"section": section, "metric": "平均出来高倍率", "count": round(pd.to_numeric(group["volume_ratio_5d_20d"], errors="coerce").mean(), 2), "detail": ""},
        {"section": section, "metric": "平均52週高値距離", "count": round(pd.to_numeric(group["dist_52w_high_pct"], errors="coerce").mean(), 2), "detail": "%"},
        {"section": section, "metric": "平均MA75乖離", "count": round(pd.to_numeric(group["ma75_gap_pct"], errors="coerce").mean(), 2), "detail": "%"},
        {"section": section, "metric": "平均MA200乖離", "count": round(pd.to_numeric(group["ma200_gap_pct"], errors="coerce").mean(), 2), "detail": "%"},
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="売買記録から勝ち/負けパターンを集計")
    parser.add_argument("--journal", default=str(PROJECT_ROOT / "outputs" / "trade_journal.csv"))
    parser.add_argument("--output", default=str(PROJECT_ROOT / "outputs" / "pattern_summary.csv"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = build_pattern_summary(load_journal(args.journal))
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(path, index=False, encoding="utf-8-sig")
    print(summary.to_string(index=False))
    print(f"保存しました: {path}")


if __name__ == "__main__":
    main()
