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
    ma50_series = close.rolling(50).mean()
    ma75_series = close.rolling(75).mean()
    ma200_series = close.rolling(200).mean()
    # T-A(2026-06-28): 240日移動平均(Yahoo標準の長期線)を追加。len>=252 を保証済みなので240本は安全。
    ma240_series = close.rolling(240).mean()
    ma25 = float(ma25_series.iloc[-1])
    ma50 = float(ma50_series.iloc[-1])
    ma75 = float(ma75_series.iloc[-1])
    ma200 = float(ma200_series.iloc[-1])
    ma240 = float(ma240_series.iloc[-1])
    # ③④ 移動平均線の「上向き」判定: 直近 slope_lookback 営業日での移動平均の変化量。
    # 正なら上向き（＝上昇トレンド継続）。len>=252 を保証しているのでMA200/240の参照も安全。
    slope_lookback = 5
    ma25_slope = ma25 - float(ma25_series.iloc[-1 - slope_lookback])
    ma50_slope = ma50 - float(ma50_series.iloc[-1 - slope_lookback])
    ma75_slope = ma75 - float(ma75_series.iloc[-1 - slope_lookback])
    ma200_slope = ma200 - float(ma200_series.iloc[-1 - slope_lookback])
    ma240_slope = ma240 - float(ma240_series.iloc[-1 - slope_lookback])
    volume_5d = float(volume.rolling(5).mean().iloc[-1])
    volume_20d = float(volume.rolling(20).mean().iloc[-1])
    turnover_20d = float(turnover.rolling(20).mean().iloc[-1])

    if min(current, high_52w, ma25, ma50, ma75, ma200, ma240, volume_20d) <= 0:
        return None

    return {
        "current_price": current,
        "latest_volume": latest_volume,
        "high_52w": high_52w,
        "dist_52w_high_pct": (high_52w - current) / high_52w * 100,
        "days_since_52w_high": days_since_52w_high,
        "ma25": ma25,
        "ma50": ma50,
        "ma75": ma75,
        "ma200": ma200,
        "ma240": ma240,
        "ma25_slope": ma25_slope,
        "ma50_slope": ma50_slope,
        "ma75_slope": ma75_slope,
        "ma200_slope": ma200_slope,
        "ma240_slope": ma240_slope,
        "ma25_rising": ma25_slope > 0,
        "ma50_rising": ma50_slope > 0,
        "ma75_rising": ma75_slope > 0,
        "ma200_rising": ma200_slope > 0,
        "ma240_rising": ma240_slope > 0,
        "ma25_gap_pct": (current - ma25) / ma25 * 100,
        "ma50_gap_pct": (current - ma50) / ma50 * 100,
        "ma75_gap_pct": (current - ma75) / ma75 * 100,
        "ma200_gap_pct": (current - ma200) / ma200 * 100,
        "ma240_gap_pct": (current - ma240) / ma240 * 100,
        "ma25_touch_pct": abs(current - ma25) / ma25 * 100,
        "ma200_touch_pct": abs(current - ma200) / ma200 * 100,
        "ma240_touch_pct": abs(current - ma240) / ma240 * 100,
        "volume_5d": volume_5d,
        "volume_20d": volume_20d,
        "volume_ratio_5d_20d": volume_5d / volume_20d,
        "volume_above_20d": latest_volume > volume_20d,
        "turnover_20d": turnover_20d,
        "lot_value_100": current * 100,
    }


def calculate_indicators_lenient(history: pd.DataFrame, min_days: int = 60) -> dict[str, float] | None:
    """上場1年未満（len<252）向けの簡易指標。52週高値スクリーニング収集専用。

    カブタンの52週高値更新リストは上場1年未満の銘柄も含むため、
    メイン指標(calculate_indicators, 252日必須)が計算できない銘柄でも
    「上場来ベース」で高値系スクリーニングに載せられるようにする。
    データが足りないMAは float('nan') を入れる（捏造しない）。
    """
    if history.empty or len(history) < min_days:
        return None

    close = history["Close"].astype(float)
    volume = history["Volume"].astype(float)
    turnover = close * volume
    nan = float("nan")

    current = float(close.iloc[-1])
    high_52w = float(close.tail(min(252, len(close))).max())
    window = close.tail(min(252, len(close)))
    positions = window.reset_index(drop=True).eq(high_52w)
    last_pos = int(positions[positions].index[-1]) if positions.any() else len(window) - 1
    days_since = int(len(window) - 1 - last_pos)

    def _ma(days: int) -> float:
        return float(close.rolling(days).mean().iloc[-1]) if len(close) >= days else nan

    ma25, ma50, ma200 = _ma(25), _ma(50), _ma(200)
    volume_5d = float(volume.rolling(5).mean().iloc[-1])
    volume_20d = float(volume.rolling(20).mean().iloc[-1])
    turnover_20d = float(turnover.rolling(20).mean().iloc[-1])
    if min(current, high_52w, volume_20d) <= 0:
        return None

    return {
        "current_price": current,
        "high_52w": high_52w,
        "dist_52w_high_pct": (high_52w - current) / high_52w * 100,
        "days_since_52w_high": days_since,
        "ma25": ma25,
        "ma50": ma50,
        "ma200": ma200,
        "ma25_gap_pct": (current - ma25) / ma25 * 100 if ma25 and ma25 == ma25 else nan,
        "ma200_gap_pct": (current - ma200) / ma200 * 100 if ma200 and ma200 == ma200 else nan,
        "volume_5d": volume_5d,
        "volume_20d": volume_20d,
        "volume_ratio_5d_20d": volume_5d / volume_20d if volume_20d > 0 else nan,
        "turnover_20d": turnover_20d,
    }


def detect_ma_touches(indicators: dict[str, float], touch_pct: float = 3.0) -> dict[str, object]:
    """T-B(2026-06-28): 25/200/240MA への『押し目タッチ』判定（純関数・通信なし）。

    各MAについて「上昇トレンド中(該当MAが右肩上がり)に、現値がそのMAから touch_pct% 以内まで
    押した」状態を True とする。単なる25MA上抜けではなく、上昇トレンドの押し目を拾う狙い。
    データが揃わない(ma240等が無い)場合は False。捏造しない。
    """
    result: dict[str, object] = {}
    touched_labels: list[str] = []
    specs = (
        ("ma25", "ma25_rising", "ma25_touch_pct", "25MAタッチ"),
        ("ma200", "ma200_rising", "ma200_touch_pct", "200MAタッチ"),
        ("ma240", "ma240_rising", "ma240_touch_pct", "240MAタッチ"),
    )
    for key, rising_key, touch_key, label in specs:
        rising = bool(indicators.get(rising_key, False))
        touch_distance = indicators.get(touch_key)
        try:
            touch_distance = float(touch_distance)
        except (TypeError, ValueError):
            touch_distance = None
        is_touch = bool(rising and touch_distance is not None and touch_distance <= touch_pct)
        result[f"{key}_touch"] = is_touch
        if is_touch:
            touched_labels.append(label)
    result["ma_touch_any"] = bool(touched_labels)
    result["ma_touch_labels"] = " / ".join(touched_labels)
    return result


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
