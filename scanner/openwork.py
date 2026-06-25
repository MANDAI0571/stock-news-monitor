from __future__ import annotations

from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OPENWORK_PATH = PROJECT_ROOT / "data" / "openwork_scores.csv"


def load_openwork_scores(path: Path | str = DEFAULT_OPENWORK_PATH) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=["code", "openwork_score"])
    df = pd.read_csv(path, dtype={"code": str})
    required = {"code", "openwork_score"}
    if not required.issubset(df.columns):
        return pd.DataFrame(columns=["code", "openwork_score"])
    out = df[["code", "openwork_score"]].copy()
    out["code"] = out["code"].astype(str).str.strip().str.removesuffix(".0")
    out["openwork_score"] = pd.to_numeric(out["openwork_score"], errors="coerce")
    return out.drop_duplicates("code", keep="last")


def add_openwork_scores(screening: pd.DataFrame, path: Path | str = DEFAULT_OPENWORK_PATH) -> pd.DataFrame:
    out = screening.copy()
    if "code" not in out.columns:
        out["openwork_score"] = pd.NA
        return out
    scores = load_openwork_scores(path)
    if scores.empty:
        out["openwork_score"] = pd.NA
        return out
    out["code"] = out["code"].astype(str).str.strip().str.removesuffix(".0")
    return out.merge(scores, on="code", how="left")


def format_openwork_score(value) -> str:
    if pd.isna(value):
        return "未取得"
    try:
        return f"{float(value):.2f}"
    except Exception:
        return "未取得"
