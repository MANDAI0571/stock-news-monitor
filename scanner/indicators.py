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
    high_52w_window = close.tail(252)
    high_52w_positions = high_52w_window.reset_index(drop=True).eq(high_52w)
    last_high_52w_position = int(high_52w_positions[high_52w_positions].index[-1])
    days_since_52w_high = int(len(high_52w_window) - 1 - last_high_52w_position)
    ma25_series = close.rolling(25).mean()
    ma75_series = close.rolling(75).mean()
    ma200_series = close.rolling(200).mean()
    ma25 = float(ma25_series.iloc[-1])
    ma75 = float(ma75_series.iloc[-1])
    ma200 = float(ma200_series.iloc[-1])
    # ③④ 移動平均線の「上向き」判定: 直近 slope_lookback 営業日での移動平均の変化量。
    # 正なら上向き（＝上昇トレンド継続）。len>=252 を保証しているのでMA200の参照も安全。
    slope_lookback = 5
    ma25_slope = ma25 - float(ma25_series.iloc[-1 - slope_lookback])
    ma75_slope = ma75 - float(ma75_series.iloc[-1 - slope_lookback])
    ma200_slope = ma200 - float(ma200_series.iloc[-1 - slope_lookback])
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
        "days_since_52w_high": days_since_52w_high,
        "ma25": ma25,
        "ma75": ma75,
        "ma200": ma200,
        "ma25_slope": ma25_slope,
        "ma75_slope": ma75_slope,
        "ma200_slope": ma200_slope,
        "ma25_rising": ma25_slope > 0,
        "ma75_rising": ma75_slope > 0,
        "ma200_rising": ma200_slope > 0,
        "ma25_gap_pct": (current - ma25) / ma25 * 100,
        "ma75_gap_pct": (current - ma75) / ma75 * 100,
        "ma200_gap_pct": (current - ma200) / ma200 * 100,
        "ma200_touch_pct": abs(current - ma200) / ma200 * 100,
        "volume_5d": volume_5d,
        "volume_20d": volume_20d,
        "volume_ratio_5d_20d": volume_5d / volume_20d,
        "volume_above_20d": latest_volume > volume_20d,
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
