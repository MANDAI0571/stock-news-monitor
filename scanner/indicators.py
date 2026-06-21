from __future__ import annotations

import pandas as pd


def calculate_indicators(history: pd.DataFrame) -> dict[str, float] | None:
    if history.empty or len(history) < 252:
        return None

    close = history["Close"].astype(float)
    volume = history["Volume"].astype(float)
    turnover = close * volume

    current = float(close.iloc[-1])
    latest_volume = float(volume.iloc[-1])
    high_52w = float(close.tail(252).max())
    ma25 = float(close.rolling(25).mean().iloc[-1])
    ma75 = float(close.rolling(75).mean().iloc[-1])
    ma200 = float(close.rolling(200).mean().iloc[-1])
    volume_5d = float(volume.rolling(5).mean().iloc[-1])
    volume_20d = float(volume.rolling(20).mean().iloc[-1])
    turnover_20d = float(turnover.rolling(20).mean().iloc[-1])

    if min(current, high_52w, ma25, ma75, ma200, volume_20d) <= 0:
        return None

    return {
        "current_price": current,
        "latest_volume": latest_volume,
        "high_52w": high_52w,
        "dist_52w_high_pct": (high_52w - current) / high_52w * 100,
        "ma25": ma25,
        "ma75": ma75,
        "ma200": ma200,
        "volume_5d": volume_5d,
        "volume_20d": volume_20d,
        "volume_ratio_5d_20d": volume_5d / volume_20d,
        "turnover_20d": turnover_20d,
        "lot_value_100": current * 100,
    }


def passes_base_filters(indicators: dict[str, float]) -> tuple[bool, list[str]]:
    reasons: list[str] = []

    if indicators["dist_52w_high_pct"] > 15:
        reasons.append("52週高値から15%超")
    if not (
        indicators["current_price"] > indicators["ma25"]
        and indicators["current_price"] > indicators["ma75"]
        and indicators["current_price"] > indicators["ma200"]
    ):
        reasons.append("MA25/75/200を上回っていない")
    if indicators["turnover_20d"] < 100_000_000:
        reasons.append("20日平均売買代金1億円未満")

    return not reasons, reasons
