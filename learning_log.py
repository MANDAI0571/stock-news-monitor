from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SCREENING_PATH = PROJECT_ROOT / "outputs" / "screening_result.csv"
DEFAULT_LEARNING_PATH = PROJECT_ROOT / "data" / "learning_candidates.csv"

LEARNING_COLUMNS = [
    "date",
    "code",
    "ticker",
    "name",
    "market",
    "sector",
    "score",
    "rank",
    "strategy",
    "reason",
    "current",
    "dist_to_52w_high_pct",
    "pullback_pct",
    "volume_ratio",
    "turnover_20d",
    "high_type",
    "high_label",
    "swing_high_price",
    "swing_high_date",
    "duke_old_high_support",
    "duke_support_score",
    "duke_support_rank",
    "duke_support_signal",
    "cwh_signal",
    "earnings_date",
    "openwork_score",
]


@dataclass(frozen=True)
class LearningLogResult:
    output_path: Path
    input_rows: int
    appended_rows: int
    total_rows: int


def _safe_text(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "<na>", "nat"} else text


def _boolish(value) -> bool:
    if isinstance(value, bool):
        return value
    text = _safe_text(value).lower()
    return text in {"1", "1.0", "true", "yes", "y"}


def _num(value, default=""):
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _code(value) -> str:
    text = _safe_text(value)
    return text[:-2] if text.endswith(".0") else text


def infer_strategy(row: pd.Series) -> str:
    """候補の主戦略ラベルを1つに正規化する。"""
    if _boolish(row.get("duke_old_high_support")) or _boolish(row.get("duke_support_signal")):
        return "duke_old_high_support"
    high_type = _safe_text(row.get("high_type"))
    if high_type:
        if high_type == "SWING_HIGH_BREAK":
            return "swing_high_break"
        if high_type == "RECENT_NEAR_HIGH":
            return "recent_near_high"
        if high_type == "52W_NEW_HIGH":
            return "52w_new_high"
        if high_type == "52W_NEAR_HIGH":
            return "52w_near_high"
        if high_type != "OTHER":
            return high_type.lower()
    if _boolish(row.get("cwh_signal")):
        return "cup_with_handle"
    if _safe_text(row.get("rank")) in {"S", "A", "B"}:
        return "rank_candidate"
    return "other"


def build_learning_rows(screening: pd.DataFrame, run_date: str | None = None) -> pd.DataFrame:
    run_date = run_date or date.today().isoformat()
    rows: list[dict[str, object]] = []
    if screening is None or screening.empty:
        return pd.DataFrame(columns=LEARNING_COLUMNS)

    for _, row in screening.iterrows():
        rows.append(
            {
                "date": run_date,
                "code": _code(row.get("code")),
                "ticker": _safe_text(row.get("ticker")),
                "name": _safe_text(row.get("name")),
                "market": _safe_text(row.get("market")),
                "sector": _safe_text(row.get("sector")),
                "score": _num(row.get("score")),
                "rank": _safe_text(row.get("rank")),
                "strategy": infer_strategy(row),
                "reason": _safe_text(row.get("reason")),
                "current": _num(row.get("current_price")),
                "dist_to_52w_high_pct": _num(row.get("dist_52w_high_pct")),
                "pullback_pct": _num(row.get("pullback_from_recent_high_pct")),
                "volume_ratio": _num(row.get("volume_ratio_5d_20d")),
                "turnover_20d": _num(row.get("turnover_20d")),
                "high_type": _safe_text(row.get("high_type")),
                "high_label": _safe_text(row.get("high_label")),
                "swing_high_price": _num(row.get("swing_high_price")),
                "swing_high_date": _safe_text(row.get("swing_high_date")),
                "duke_old_high_support": _boolish(row.get("duke_old_high_support")),
                "duke_support_score": _num(row.get("duke_support_score")),
                "duke_support_rank": _safe_text(row.get("duke_support_rank")),
                "duke_support_signal": _boolish(row.get("duke_support_signal")),
                "cwh_signal": _boolish(row.get("cwh_signal")),
                "earnings_date": _safe_text(row.get("earnings_date")),
                "openwork_score": _num(row.get("openwork_score")),
            }
        )
    out = pd.DataFrame(rows, columns=LEARNING_COLUMNS)
    out = out[out["code"].astype(str).str.len() > 0].copy()
    return out


def append_learning_candidates(
    screening_path: str | Path = DEFAULT_SCREENING_PATH,
    output_path: str | Path = DEFAULT_LEARNING_PATH,
    run_date: str | None = None,
) -> LearningLogResult:
    screening_path = Path(screening_path)
    output_path = Path(output_path)
    run_date = run_date or date.today().isoformat()

    if screening_path.exists():
        screening = pd.read_csv(screening_path, dtype={"code": str})
    else:
        screening = pd.DataFrame()

    new_rows = build_learning_rows(screening, run_date=run_date)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        existing = pd.read_csv(output_path, dtype={"code": str})
    else:
        existing = pd.DataFrame(columns=LEARNING_COLUMNS)

    for col in LEARNING_COLUMNS:
        if col not in existing.columns:
            existing[col] = ""
        if col not in new_rows.columns:
            new_rows[col] = ""

    before_keys = set(zip(existing["date"].astype(str), existing["code"].astype(str))) if not existing.empty else set()
    combined = pd.concat([existing[LEARNING_COLUMNS], new_rows[LEARNING_COLUMNS]], ignore_index=True)
    combined["code"] = combined["code"].astype(str).map(_code)
    combined["date"] = combined["date"].astype(str)
    combined = combined.drop_duplicates(subset=["date", "code"], keep="first")
    combined = combined.sort_values(["date", "code"], kind="stable").reset_index(drop=True)
    combined.to_csv(output_path, index=False, encoding="utf-8-sig")

    after_keys = set(zip(combined["date"].astype(str), combined["code"].astype(str))) if not combined.empty else set()
    appended = len(after_keys - before_keys)
    return LearningLogResult(
        output_path=output_path,
        input_rows=len(new_rows),
        appended_rows=appended,
        total_rows=len(combined),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="スクリーニング候補を学習用CSVへ蓄積する")
    parser.add_argument("--input", default=str(DEFAULT_SCREENING_PATH), help="入力screening_result.csv")
    parser.add_argument("--output", default=str(DEFAULT_LEARNING_PATH), help="追記先learning_candidates.csv")
    parser.add_argument("--date", default=None, help="記録日 YYYY-MM-DD。省略時は今日")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = append_learning_candidates(args.input, args.output, args.date)
    print(f"learning_log_input_rows={result.input_rows}")
    print(f"learning_log_appended_rows={result.appended_rows}")
    print(f"learning_log_total_rows={result.total_rows}")
    print(f"learning_log_csv={result.output_path}")


if __name__ == "__main__":
    main()
