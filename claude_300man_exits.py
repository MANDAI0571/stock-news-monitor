"""Claude 300万円運用の売り（ルール 3.0.0、2026-10-30 から）。

持ち主「300万運用はもう少しアクティブに運用し、利益を出し損を減らすのも重要だ」（2026-10-30）。
2.x では損切りを終値で判定して翌日の寄付で売っていたため、決めた -5% を超えて -7〜-8% で売っていた
（3回とも）。3.0.0 では、買った日から次の2つの注文を出しておく形にする:

  - 逆指値（損切り）: 買値の -7%。日中の安値が届いたらその値段で売り。寄付で下回っていたら始値。
  - 指値（利確）    : 買値の +15%。日中の高値が届いたらその値段で売り。寄付で上回っていたら始値。
  - 期限            : 買った日から20営業日目の大引けで売り。

日足だけでは同じ日の高値と安値のどちらが先か分からないので、両方届いた日は損切りを先にする（安全側）。
買った日は寄付のあとだけを見る（寄付で買っているので、始値での判定はしない）。
値段は Yahoo Finance の日足。出来高0の足（配信遅れで前日の値のまま返ることがある）は使わない。
直近の足がまだ来ていないときは判定を待ち、次の実行でその日にさかのぼって付ける（推測では埋めない）。

これは架空資金の記録であり、投資助言ではない。
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import dual_300man_fill as F
from dual_300man_config import CONFIG, JOURNAL_COLUMNS, ORDER_COLUMNS, order_metadata
from dual_300man_fill import business_days_between, read_csv, write_csv
from jpx_calendar import add_jpx_business_days, is_jpx_business_day


JST = ZoneInfo("Asia/Tokyo")
# 直近この営業日数ぶんの足が欠けていたら、配信待ちとして判定を止める。それより古い欠けは売買なしの日として飛ばす
WAIT_FOR_BARS_BUSINESS_DAYS = 2
PriceFetcher = Callable[[str], pd.DataFrame]


@dataclass(frozen=True)
class ExitEvent:
    day: date
    price: float
    trigger: float
    exit_type: str
    reason: str
    held_days: int


def exit_levels(entry_price: float) -> tuple[float, float]:
    """(逆指値, 指値) の値段。"""
    return (entry_price * (1 + CONFIG.stop_loss_pct / 100), entry_price * (1 + CONFIG.take_profit_pct / 100))


def deadline(entry_date: date) -> date:
    """期限の営業日（この日の大引けで売る）。"""
    return add_jpx_business_days(entry_date, CONFIG.timeout_days)


def _bars(history: pd.DataFrame | None) -> pd.DataFrame:
    if history is None or history.empty:
        return pd.DataFrame()
    frame = history.copy()
    frame.index = pd.to_datetime(frame.index)
    if getattr(frame.index, "tz", None) is not None:
        frame.index = frame.index.tz_localize(None)
    frame.index = frame.index.normalize()
    needed = ["Open", "High", "Low", "Close"]
    if any(column not in frame.columns for column in needed):
        return pd.DataFrame()
    for column in needed:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=needed)
    if "Volume" in frame.columns:
        volume = pd.to_numeric(frame["Volume"], errors="coerce")
        frame = frame[~volume.eq(0)]
    frame = frame[(frame[needed] > 0).all(axis=1)]
    return frame[~frame.index.duplicated(keep="last")].sort_index()


def find_exit(history: pd.DataFrame | None, entry_date: date, entry_price: float, as_of: date) -> ExitEvent | None:
    """entry_date〜as_of の日足で、最初に約定する売りを返す。まだ無ければ None。"""
    if entry_price <= 0 or as_of < entry_date:
        return None
    frame = _bars(history)
    if frame.empty:
        return None
    stop, take = exit_levels(entry_price)
    stop_pct, take_pct = CONFIG.stop_loss_pct, CONFIG.take_profit_pct
    day = entry_date
    while day <= as_of:
        if not is_jpx_business_day(day):
            day += timedelta(days=1)
            continue
        stamp = pd.Timestamp(day)
        if stamp not in frame.index:
            if business_days_between(day, as_of) < WAIT_FOR_BARS_BUSINESS_DAYS:
                return None                      # 足の配信待ち。次の実行でこの日から見直す
            day += timedelta(days=1)             # 売買のなかった日（売買停止など）
            continue
        row = frame.loc[stamp]
        o, h, low, c = (float(row[k]) for k in ("Open", "High", "Low", "Close"))
        held = business_days_between(entry_date, day)
        if day == entry_date:                    # 寄付で買った日は、寄付のあとだけ
            if low <= stop:
                return ExitEvent(day, stop, stop, "STOP_LOSS", f"逆指値 {stop:,.1f}円（{stop_pct:+.0f}%）に日中の安値 {low:,.1f}円が届いた", held)
            if h >= take:
                return ExitEvent(day, take, take, "TAKE_PROFIT", f"指値 {take:,.1f}円（{take_pct:+.0f}%）に日中の高値 {h:,.1f}円が届いた", held)
        else:
            if o <= stop:
                return ExitEvent(day, o, stop, "STOP_LOSS", f"逆指値 {stop:,.1f}円（{stop_pct:+.0f}%）より下の {o:,.1f}円で寄り付いたため始値で売り", held)
            if o >= take:
                return ExitEvent(day, o, take, "TAKE_PROFIT", f"指値 {take:,.1f}円（{take_pct:+.0f}%）より上の {o:,.1f}円で寄り付いたため始値で売り", held)
            if low <= stop:
                return ExitEvent(day, stop, stop, "STOP_LOSS", f"逆指値 {stop:,.1f}円（{stop_pct:+.0f}%）に日中の安値 {low:,.1f}円が届いた", held)
            if h >= take:
                return ExitEvent(day, take, take, "TAKE_PROFIT", f"指値 {take:,.1f}円（{take_pct:+.0f}%）に日中の高値 {h:,.1f}円が届いた", held)
        if held >= CONFIG.timeout_days:
            return ExitEvent(day, c, c, "TIMEOUT", f"期限の{CONFIG.timeout_days}営業日目の大引けで売り", held)
        day += timedelta(days=1)
    return None


def _fetch_history(ticker: str) -> pd.DataFrame:
    from dual_300man_metrics import fetch_metrics_history

    return fetch_metrics_history(ticker)


def settle(as_of: date, fetcher: PriceFetcher = _fetch_history, account: str = "claude") -> int:
    """保有銘柄の逆指値・指値・期限を as_of までの日足で判定し、約定したものを注文台帳と運用台帳に付ける。
    付けた件数を返す。同じ日に何度動かしても二重には付けない（売った行は CLOSED になるため）。"""
    if not CONFIG.standing_exit_orders:
        return 0
    orders_path, journal_path, _ = F.account_paths(account)
    orders = read_csv(orders_path, ORDER_COLUMNS)
    journal = read_csv(journal_path, JOURNAL_COLUMNS)
    open_rows = journal[journal["status"].str.upper().eq("OPEN")] if not journal.empty else journal
    settled = 0
    cache: dict[str, pd.DataFrame] = {}
    for _, row in open_rows.iterrows():
        code = str(row.get("code", "")).strip()
        try:
            entry_price = float(row.get("entry_price") or 0)
            shares = int(float(row.get("shares") or 0))
            entry_date = date.fromisoformat(str(row.get("entry_date", "")).strip())
        except (TypeError, ValueError):
            print(f"{account}_300man_exits=skip code={code} reason=broken_row")
            continue
        if not code or shares <= 0:
            continue
        ticker = str(row.get("ticker") or f"{code}.T")
        if ticker not in cache:
            try:
                cache[ticker] = fetcher(ticker)
            except Exception as error:  # noqa: BLE001 - 価格の障害で他の銘柄の判定まで止めない
                print(f"{account}_300man_exits=price_error code={code} err={type(error).__name__}")
                cache[ticker] = pd.DataFrame()
        event = find_exit(cache[ticker], entry_date, entry_price, as_of)
        if event is None:
            continue
        order = {
            **{column: "" for column in ORDER_COLUMNS},
            **order_metadata(),
            "decision_date": event.day.isoformat(),
            "execution_date": event.day.isoformat(),
            "side": "SELL",
            "code": code,
            "ticker": ticker,
            "name": str(row.get("name") or ""),
            "sector": str(row.get("sector") or ""),
            "shares": str(shares),
            "decision_price": f"{event.trigger:.2f}",
            "strategy": str(row.get("strategy") or "claude_momentum"),
            "exit_type": event.exit_type,
            "decision_return_pct": f"{(event.trigger / entry_price - 1) * 100:.2f}",
            "holding_business_days": str(event.held_days),
            "reason": event.reason,
            "status": "DECLARED",
        }
        orders = pd.concat([orders, pd.DataFrame([order])], ignore_index=True).fillna("")
        idx = orders.index[-1]
        journal, changed = F._fill_sell(account, orders, journal, idx, orders.loc[idx], event.day, event.price, shares)
        if changed:
            settled += 1
            print(f"{account}_300man_exits=filled code={code} type={event.exit_type} day={event.day} "
                  f"return={(event.price / entry_price - 1) * 100:+.2f}%")
    if settled:
        write_csv(orders, orders_path, ORDER_COLUMNS)
        write_csv(journal, journal_path, JOURNAL_COLUMNS)
        F.write_ledger(account, orders, journal)
    print(f"{account}_300man_exits_as_of={as_of.isoformat()} open={len(open_rows)} settled={settled}")
    return settled


def main() -> None:
    parser = argparse.ArgumentParser(description="逆指値・指値・期限の売りを日足で判定して台帳に付ける")
    parser.add_argument("--date", default=datetime.now(JST).date().isoformat())
    args = parser.parse_args()
    settle(date.fromisoformat(args.date))


if __name__ == "__main__":
    main()
