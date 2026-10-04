"""無料の日足だけで比較するClaude出口ルールの固定シャドー実験。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd


@dataclass(frozen=True)
class ExitVariant:
    key: str
    label: str
    intraday_stop: bool = False
    trailing_activation_pct: float | None = None
    trailing_drawdown_pct: float | None = None

    @property
    def trailing(self) -> bool:
        return self.trailing_activation_pct is not None and self.trailing_drawdown_pct is not None


# 比較開始後に都合よく条件を増やさない。変更時はkeyを変えて別実験にする。
EXIT_VARIANTS = (
    ExitVariant("current_close_5_tp12_t15", "現行: 終値-5% / +12% / 15日"),
    ExitVariant("intraday_5_tp12_t15", "日中損切り: 安値-5% / +12% / 15日", intraday_stop=True),
    ExitVariant(
        "close_trail_6_3_t15",
        "終値追随: +6%起動 / 高値終値から-3% / 15日",
        trailing_activation_pct=6.0,
        trailing_drawdown_pct=3.0,
    ),
    ExitVariant(
        "intraday_trail_6_3_t15",
        "日中損切り+終値追随: -5% / +6%-3% / 15日",
        intraday_stop=True,
        trailing_activation_pct=6.0,
        trailing_drawdown_pct=3.0,
    ),
)


def normalize_history(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    out = frame.copy()
    out.index = pd.to_datetime(out.index)
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_localize(None)
    out.index = out.index.normalize()
    needed = ["Open", "High", "Low", "Close"]
    if any(column not in out.columns for column in needed):
        return pd.DataFrame()
    for column in needed:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    return out.dropna(subset=needed).sort_index()


def simulate_exit(
    history: pd.DataFrame,
    *,
    entry_date: date,
    entry_price: float,
    variant: ExitVariant,
    stop_loss_pct: float = -5.0,
    take_profit_pct: float = 12.0,
    timeout_days: int = 15,
    roundtrip_cost_pct: float = 0.30,
    as_of: date | None = None,
) -> dict[str, object]:
    """入口を固定し、出口だけを日足で比較する。

    日中損切りは、寄付がストップ以下なら寄付、そうでなければストップ価格で
    約定したと仮定する。同じ足の高値・安値の順序は分からないため損切りを先に
    判定する。終値シグナルは現行運用と同じく次営業日の寄付で決済する。
    """
    frame = normalize_history(history)
    if as_of is not None and not frame.empty:
        frame = frame[frame.index.date <= as_of]
    stamp = pd.Timestamp(entry_date)
    if frame.empty or stamp not in frame.index or entry_price <= 0:
        return {"status": "UNAVAILABLE"}

    entry_pos = int(frame.index.get_loc(stamp))
    if not isinstance(entry_pos, int):
        return {"status": "UNAVAILABLE"}
    stop_price = entry_price * (1 + stop_loss_pct / 100)
    peak_close = entry_price
    peak_date = entry_date
    activated = False
    activation_date = ""
    signal_type = ""
    signal_pos = -1
    direct_exit_price: float | None = None

    last_pos = min(len(frame) - 1, entry_pos + timeout_days)
    for position in range(entry_pos, last_pos + 1):
        row = frame.iloc[position]
        day = frame.index[position].date()
        held = position - entry_pos
        open_price = float(row["Open"])
        low_price = float(row["Low"])
        close_price = float(row["Close"])

        if variant.intraday_stop:
            if open_price <= stop_price:
                signal_type = "STOP_LOSS_GAP"
                signal_pos = position
                direct_exit_price = open_price
                break
            if low_price <= stop_price:
                signal_type = "STOP_LOSS_INTRADAY"
                signal_pos = position
                direct_exit_price = stop_price
                break
        elif close_price <= stop_price:
            signal_type = "STOP_LOSS"
            signal_pos = position
            break

        if close_price > peak_close:
            peak_close = close_price
            peak_date = day
        close_return = (close_price / entry_price - 1) * 100
        if variant.trailing:
            if not activated and close_return >= float(variant.trailing_activation_pct):
                activated = True
                activation_date = day.isoformat()
            trail_price = peak_close * (1 - float(variant.trailing_drawdown_pct) / 100)
            if activated and close_price <= trail_price:
                signal_type = "TRAILING_STOP"
                signal_pos = position
                break
        elif close_return >= take_profit_pct:
            signal_type = "TAKE_PROFIT"
            signal_pos = position
            break

        if held >= timeout_days:
            signal_type = "TIMEOUT"
            signal_pos = position
            break

    observed_end = signal_pos if signal_pos >= 0 else last_pos
    observed = frame.iloc[entry_pos : observed_end + 1]
    base = {
        "activation_date": activation_date,
        "peak_close": round(peak_close, 4),
        "peak_close_date": peak_date.isoformat(),
        "mfe_pct": round((float(observed["High"].max()) / entry_price - 1) * 100, 4),
        "mae_pct": round((float(observed["Low"].min()) / entry_price - 1) * 100, 4),
        "last_observed_date": frame.index[observed_end].date().isoformat(),
    }
    if signal_pos < 0:
        return {**base, "status": "OPEN", "exit_type": ""}

    signal_date = frame.index[signal_pos].date()
    if direct_exit_price is not None:
        exit_pos = signal_pos
        exit_price = direct_exit_price
    elif signal_pos + 1 < len(frame):
        exit_pos = signal_pos + 1
        exit_price = float(frame["Open"].iloc[exit_pos])
    else:
        return {
            **base,
            "status": "PENDING_EXIT",
            "exit_type": signal_type,
            "exit_signal_date": signal_date.isoformat(),
        }

    gross_return = (exit_price / entry_price - 1) * 100
    return {
        **base,
        "status": "CLOSED",
        "exit_type": signal_type,
        "exit_signal_date": signal_date.isoformat(),
        "exit_date": frame.index[exit_pos].date().isoformat(),
        "exit_price": round(exit_price, 4),
        "gross_return_pct": round(gross_return, 4),
        "net_return_pct": round(gross_return - roundtrip_cost_pct, 4),
        "holding_business_days": exit_pos - entry_pos,
    }
