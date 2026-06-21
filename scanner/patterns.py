from __future__ import annotations

import numpy as np
import pandas as pd


def detect_cup_with_handle(
    closes: pd.Series | np.ndarray,
    cup_depth_min: float = 0.12,
    cup_depth_max: float = 0.50,
    handle_depth_max: float = 0.15,
    breakout_margin: float = 0.05,
) -> dict[str, float] | None:
    values = np.asarray(pd.Series(closes).dropna().tail(130), dtype=float)
    if len(values) < 60:
        return None

    cup_end = int(len(values) * 0.75)
    cup = values[:cup_end]
    handle = values[cup_end:]
    if len(cup) < 30 or len(handle) < 5:
        return None

    left_half = cup[: len(cup) // 2]
    left_hi_idx = int(np.argmax(left_half))
    left_hi = float(left_half[left_hi_idx])
    if left_hi <= 0:
        return None

    after_left = cup[left_hi_idx:]
    if len(after_left) < 10:
        return None

    bot_rel = int(np.argmin(after_left))
    cup_low = float(after_left[bot_rel])
    cup_low_abs = left_hi_idx + bot_rel
    after_bottom = cup[cup_low_abs:]
    if len(after_bottom) < 5:
        return None

    right_hi = float(np.max(after_bottom))
    depth = (left_hi - cup_low) / left_hi
    if not cup_depth_min <= depth <= cup_depth_max:
        return None

    if right_hi < left_hi * 0.88:
        return None

    handle_hi = right_hi
    handle_low = float(np.min(handle))
    if handle_hi <= 0:
        return None

    handle_depth = (handle_hi - handle_low) / handle_hi
    if not 0.02 <= handle_depth <= handle_depth_max:
        return None

    cup_mid = cup_low + (left_hi - cup_low) * 0.5
    if handle_low < cup_mid:
        return None

    current = float(values[-1])
    if current > handle_hi * (1 + breakout_margin):
        return None

    return {
        "cwh_signal": True,
        "breakout_price": handle_hi,
        "pct_to_breakout": (handle_hi - current) / current * 100,
        "cup_depth_pct": depth * 100,
        "handle_depth_pct": handle_depth * 100,
    }
