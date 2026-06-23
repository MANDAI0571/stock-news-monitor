from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


HIGH_TYPE_ORDER = (
    "52W_NEW_HIGH",
    "52W_NEAR_HIGH",
    "RECENT_NEW_HIGH",
    "RECENT_NEAR_HIGH",
)

HIGH_LABELS = {
    "52W_NEW_HIGH": "52週新高値",
    "52W_NEAR_HIGH": "52週高値接近",
    "RECENT_NEW_HIGH": "直近高値更新",
    "RECENT_NEAR_HIGH": "直近高値接近",
    "OTHER": "分類外",
}


@dataclass(frozen=True)
class HighProfile:
    high_type: str = "OTHER"
    high_label: str = "分類外"
    high_window_days: int = 0
    high_price: float = 0.0
    high_date: str = ""
    dist_to_high_pct: float = 999.0


def classify_high_profile(history: pd.DataFrame) -> dict[str, object]:
    if history.empty or "Close" not in history.columns:
        return _as_dict(HighProfile())

    close = history["Close"].astype(float)
    if len(close) < 60:
        return _as_dict(HighProfile())

    recent = window_high_profile(history, 60)
    wide = window_high_profile(history, 252) if len(close) >= 252 else None

    chosen = _choose_profile(wide, recent)
    return _as_dict(chosen or HighProfile())


def _choose_profile(wide: HighProfile | None, recent: HighProfile | None) -> HighProfile | None:
    if wide and wide.high_type in ("52W_NEW_HIGH", "52W_NEAR_HIGH"):
        return wide
    if recent and recent.high_type in ("RECENT_NEW_HIGH", "RECENT_NEAR_HIGH"):
        return recent
    return wide or recent


def window_high_profile(history: pd.DataFrame, window_days: int) -> HighProfile | None:
    if len(history) < window_days:
        return None

    window = history.tail(window_days)
    close = window["Close"].astype(float)
    current = float(close.iloc[-1])
    high_price = float(close.max())
    if high_price <= 0:
        return None

    matches = close.reset_index(drop=True).eq(high_price)
    if not matches.any():
        return None
    last_pos = int(matches[matches].index[-1])
    high_dt = window.index[last_pos]
    dist_to_high_pct = (high_price - current) / high_price * 100

    if last_pos == len(window) - 1 and abs(current - high_price) <= max(1e-8, high_price * 0.000001):
        high_type = "52W_NEW_HIGH" if window_days == 252 else "RECENT_NEW_HIGH"
    elif dist_to_high_pct <= 3:
        high_type = "52W_NEAR_HIGH" if window_days == 252 else "RECENT_NEAR_HIGH"
    else:
        return None

    return HighProfile(
        high_type=high_type,
        high_label=HIGH_LABELS[high_type],
        high_window_days=window_days,
        high_price=round(high_price, 1),
        high_date=pd.Timestamp(high_dt).date().isoformat(),
        dist_to_high_pct=round(dist_to_high_pct, 2),
    )


def _as_dict(profile: HighProfile) -> dict[str, object]:
    return {
        "high_type": profile.high_type,
        "high_label": profile.high_label,
        "high_window_days": profile.high_window_days,
        "high_price": profile.high_price,
        "high_date": profile.high_date,
        "dist_to_high_pct": profile.dist_to_high_pct,
    }


def build_high_sections_markdown(screening: pd.DataFrame, max_rows: int = 5) -> list[str]:
    if screening.empty or "rank" not in screening.columns:
        return []

    candidates = screening[screening["rank"].astype(str).str.upper().isin(["S", "A", "B"])].copy()
    if candidates.empty:
        return []

    if "score" not in candidates.columns:
        candidates["score"] = 0
    candidates["score"] = pd.to_numeric(candidates["score"], errors="coerce").fillna(0)
    if "dist_to_high_pct" not in candidates.columns:
        candidates["dist_to_high_pct"] = 999
    candidates["dist_to_high_pct"] = pd.to_numeric(candidates["dist_to_high_pct"], errors="coerce").fillna(999)
    if "high_type" not in candidates.columns:
        candidates["high_type"] = "OTHER"
    candidates["high_type"] = candidates["high_type"].astype(str)

    rank_order = {"S": 0, "A": 1, "B": 2}
    candidates["_rank_order"] = candidates["rank"].map(rank_order).fillna(9)

    lines: list[str] = []
    for high_type in HIGH_TYPE_ORDER:
        group = candidates[candidates["high_type"].eq(high_type)].copy()
        lines.append(f"## {HIGH_LABELS[high_type]}")
        lines.append("")
        if group.empty:
            lines.append("- 該当なし")
            lines.append("")
            continue

        group = group.sort_values(["_rank_order", "score", "dist_to_high_pct", "code"], ascending=[True, False, True, True])
        lines.append("| コード | 銘柄名 | ランク | スコア | 高値種別 | 高値日 | 高値まで | 理由 |")
        lines.append("|---|---|---:|---:|---|---|---:|---|")
        for _, row in group.head(max_rows).iterrows():
            lines.append(
                "| {code} | {name} | {rank} | {score} | {high_label} | {high_date} | {dist} | {reason} |".format(
                    code=_text(row, "code"),
                    name=_text(row, "name"),
                    rank=_text(row, "rank"),
                    score=_text(row, "score"),
                    high_label=_text(row, "high_label"),
                    high_date=_text(row, "high_date"),
                    dist=_text(row, "dist_to_high_pct"),
                    reason=_text(row, "reason"),
                )
            )
        lines.append("")

    return lines


def _text(row: pd.Series, key: str) -> str:
    value = row.get(key, "")
    if pd.isna(value):
        return ""
    return str(value)
