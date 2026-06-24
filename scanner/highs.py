from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


HIGH_TYPE_ORDER = (
    "SWING_HIGH_BREAK",
    "52W_NEW_HIGH",
    "52W_NEAR_HIGH",
    "RECENT_NEW_HIGH",
    "RECENT_NEAR_HIGH",
)

HIGH_LABELS = {
    "SWING_HIGH_BREAK": "直近スイング高値ブレイク",
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
        return _as_dict(HighProfile()) | detect_swing_high_break(history)

    close = history["Close"].astype(float)
    swing = detect_swing_high_break(history)
    if len(close) < 60:
        base = _as_dict(HighProfile())
        if swing.get("swing_high_break"):
            base |= _swing_as_high_profile(swing)
        return base | swing

    recent = window_high_profile(history, 60)
    wide = window_high_profile(history, 252) if len(close) >= 252 else None

    chosen = _choose_profile(wide, recent)
    base = _as_dict(chosen or HighProfile())
    if swing.get("swing_high_break"):
        base |= _swing_as_high_profile(swing)
    return base | swing


def _swing_as_high_profile(swing: dict[str, object]) -> dict[str, object]:
    return {
        "high_type": "SWING_HIGH_BREAK",
        "high_label": HIGH_LABELS["SWING_HIGH_BREAK"],
        "high_window_days": 30,
        "high_price": swing.get("swing_high_price", ""),
        "high_date": swing.get("swing_high_date", ""),
        "dist_to_high_pct": swing.get("swing_high_break_pct", ""),
    }


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



def detect_swing_high_break(history: pd.DataFrame, min_lookback: int = 5, max_lookback: int = 30) -> dict[str, object]:
    """Detect break above a clear recent swing high from 5-30 sessions ago.

    Latest bar is the trigger bar. The swing high is selected from bars 5-30
    sessions before the latest bar, using clear local highs first. Today's high
    or close/current price breaking above that swing high triggers detection.
    """
    empty = {
        "swing_high_price": "",
        "swing_high_date": "",
        "swing_high_break_pct": "",
        "swing_high_break": False,
        "swing_high_label": "",
    }
    if history.empty or "High" not in history.columns or len(history) < min_lookback + 1:
        return empty

    high = history["High"].astype(float)
    close = history["Close"].astype(float) if "Close" in history.columns else high
    latest_pos = len(history) - 1
    start = max(0, latest_pos - max_lookback)
    end = latest_pos - min_lookback + 1
    if end <= start:
        return empty

    candidates: list[tuple[int, float]] = []
    for pos in range(start, end):
        if _is_clear_local_high(high, pos):
            candidates.append((pos, float(high.iloc[pos])))
    if not candidates:
        return empty
    pos, swing_price = max(candidates, key=lambda item: (item[1], item[0]))

    trigger_price = max(float(high.iloc[-1]), float(close.iloc[-1]))
    break_pct = (trigger_price / swing_price - 1.0) * 100 if swing_price > 0 else 0.0
    swing_break = bool(swing_price > 0 and trigger_price > swing_price)
    return {
        "swing_high_price": round(swing_price, 1),
        "swing_high_date": pd.Timestamp(history.index[pos]).date().isoformat(),
        "swing_high_break_pct": round(break_pct, 2),
        "swing_high_break": swing_break,
        "swing_high_label": HIGH_LABELS["SWING_HIGH_BREAK"] if swing_break else "",
    }


def _is_clear_local_high(high: pd.Series, pos: int, left: int = 2, right: int = 2) -> bool:
    target = float(high.iloc[pos])
    start = max(0, pos - left)
    end = min(len(high), pos + right + 1)
    for other in range(start, end):
        if other == pos:
            continue
        if float(high.iloc[other]) >= target:
            return False
    return True

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

    sections = [
        ("【直近高値ブレイク】", _filter_swing_email_candidates(candidates[candidates["high_type"].eq("SWING_HIGH_BREAK")].copy()), "swing"),
        ("【52週高値更新】", candidates[candidates["high_type"].eq("52W_NEW_HIGH")].copy(), "standard"),
        ("【その他】", candidates[~candidates["high_type"].isin(["SWING_HIGH_BREAK", "52W_NEW_HIGH"])].copy(), "standard"),
    ]

    lines: list[str] = []
    for title, group, style in sections:
        lines.append(f"## {title}")
        lines.append("")
        if group.empty:
            lines.append("- 該当なし")
            lines.append("")
            continue
        group = group.sort_values(["_rank_order", "score", "dist_to_high_pct", "code"], ascending=[True, False, True, True])
        if style == "swing":
            lines.append("| コード | 銘柄名 | ランク | スコア | swing高値 | swing日 | 上抜け率 | 出来高比 | 売買代金 | 理由 |")
            lines.append("|---|---|---:|---:|---:|---|---:|---:|---:|---|")
            for _, row in group.head(max_rows).iterrows():
                lines.append(
                    "| {code} | {name} | {rank} | {score} | {swing_price} | {swing_date} | {break_pct} | {vol} | {turnover} | {reason} |".format(
                        code=_text(row, "code"),
                        name=_text(row, "name"),
                        rank=_text(row, "rank"),
                        score=_text(row, "score"),
                        swing_price=_text(row, "swing_high_price"),
                        swing_date=_text(row, "swing_high_date"),
                        break_pct=_text(row, "swing_high_break_pct"),
                        vol=_text(row, "volume_ratio_5d_20d"),
                        turnover=_text(row, "turnover_20d"),
                        reason=_text(row, "reason"),
                    )
                )
        else:
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


def _filter_swing_email_candidates(group: pd.DataFrame) -> pd.DataFrame:
    if group.empty:
        return group
    required = ["swing_high_break", "volume_ratio_5d_20d", "current_price", "ma25", "turnover_20d"]
    for column in required:
        if column not in group.columns:
            return group.iloc[0:0]
    filtered = group.copy()
    sector = filtered["sector"].astype(str) if "sector" in filtered.columns else pd.Series("", index=filtered.index)
    name = filtered["name"].astype(str) if "name" in filtered.columns else pd.Series("", index=filtered.index)
    etf_reit = sector.str.contains("ETF|REIT|リート|不動産投資", case=False, regex=True) | name.str.contains("ETF|REIT|リート|上場投信|投信", case=False, regex=True)
    return filtered[
        (~etf_reit)
        & (filtered["swing_high_break"].astype(bool))
        & (pd.to_numeric(filtered["volume_ratio_5d_20d"], errors="coerce").fillna(0) >= 1.5)
        & (pd.to_numeric(filtered["current_price"], errors="coerce") > pd.to_numeric(filtered["ma25"], errors="coerce"))
        & (pd.to_numeric(filtered["turnover_20d"], errors="coerce").fillna(0) >= 100_000_000)
    ]


def _text(row: pd.Series, key: str) -> str:
    value = row.get(key, "")
    if pd.isna(value):
        return ""
    return str(value)
