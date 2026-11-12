#!/usr/bin/env python3
"""時価総額で並べる（2026-11-13 持ち主「スクリーニングした銘柄はすべて時価総額順に並べる」）。

時価総額 = 発行済株式数 × 終値（または現在値）。株式数は data/jp_shares.csv（コード・株式数・取得日）。
Yahoo Finance の quote（sharesOutstanding。100銘柄ずつ）から取り、週1回 market_cap_refresh.yml が取り直してコミットする。
銘柄名は入れない（コードと数字だけ）。株式数が無い銘柄は時価総額を出さず、一覧の最後に回す（推測で埋めない）。

  python3 market_cap.py --refresh            # 古い（30日より前）・無い銘柄だけ取り直す
  python3 market_cap.py --refresh --all      # 上場の全銘柄を取り直す
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import TypeVar

ROOT = Path(__file__).resolve().parent
SHARES_CSV = ROOT / "data" / "jp_shares.csv"
MAX_AGE_DAYS = 30
T = TypeVar("T")


def _code(value: object) -> str:
    text = str(value or "").strip().upper()
    return text[:-2] if text.endswith(".T") else text


@lru_cache(maxsize=4)
def _load(path: str) -> dict[str, tuple[float, str]]:
    table: dict[str, tuple[float, str]] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    shares = float(row.get("shares") or 0)
                except ValueError:
                    continue
                if shares > 0:
                    table[_code(row.get("code"))] = (shares, str(row.get("asof") or ""))
    except OSError:
        pass
    return table


def shares_table(path: Path = SHARES_CSV) -> dict[str, tuple[float, str]]:
    return _load(str(path))


def market_cap_yen(code: object, price: object, path: Path = SHARES_CSV) -> float | None:
    """時価総額（円）。株式数か値段が無ければ None。"""
    try:
        px = float(price)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not px > 0:
        return None
    found = shares_table(path).get(_code(code))
    return found[0] * px if found else None


def cap_text(yen: float | None) -> str:
    """「時価総額 1兆2,345億円」「時価総額 850億円」。無ければ空。"""
    if yen is None or not yen > 0:
        return ""
    oku = yen / 1e8
    if oku >= 10_000:
        cho, rest = divmod(round(oku), 10_000)
        return f"時価総額 {cho:,}兆{rest:,}億円" if rest else f"時価総額 {cho:,}兆円"
    if oku >= 10:
        return f"時価総額 {oku:,.0f}億円"
    return f"時価総額 {oku:,.1f}億円"


def sort_by_market_cap(items: Iterable[T], code: Callable[[T], object], price: Callable[[T], object],
                       path: Path = SHARES_CSV) -> list[T]:
    """時価総額の大きい順。時価総額が出ない銘柄は最後（もとの順のまま）。"""
    keyed = []
    for i, item in enumerate(items):
        cap = market_cap_yen(code(item), price(item), path)
        keyed.append(((0, -cap, i) if cap is not None else (1, 0.0, i), item))
    return [item for _, item in sorted(keyed, key=lambda pair: pair[0])]


def sort_frame_by_market_cap(frame, code_col: str = "code", price_col: str = "current_price",
                             path: Path = SHARES_CSV):
    """DataFrame を時価総額の大きい順に（列 market_cap_yen を足して返す）。"""
    if frame is None or len(frame) == 0:
        return frame
    out = frame.copy()
    caps = [market_cap_yen(c, p, path) for c, p in zip(out[code_col], out[price_col])]
    out["market_cap_yen"] = caps
    out["_cap_missing"] = [c is None for c in caps]
    out["_cap_sort"] = [-(c or 0.0) for c in caps]
    out["_cap_pos"] = range(len(out))
    out = out.sort_values(["_cap_missing", "_cap_sort", "_cap_pos"], kind="mergesort")
    return out.drop(columns=["_cap_missing", "_cap_sort", "_cap_pos"])


QUOTE_URL = "https://query2.finance.yahoo.com/v7/finance/quote"
BATCH = 100


def _fetch_batch(codes: list[str]) -> dict[str, float]:
    """Yahoo の quote（100銘柄ずつ）で株式数を取る。yfinance の接続（cookie・crumb）を借りる。"""
    from yfinance.data import YfData

    data = YfData()
    for attempt in range(3):
        try:
            response = data.get(QUOTE_URL, params={"symbols": ",".join(f"{c}.T" for c in codes),
                                                   "fields": "sharesOutstanding"})
            rows = response.json()["quoteResponse"]["result"]
            return {_code(r.get("symbol")): float(r["sharesOutstanding"]) for r in rows
                    if r.get("sharesOutstanding")}
        except Exception:                                       # noqa: BLE001 - 次のまとまりへ進む
            time.sleep(2 + 2 * attempt)
    return {}


def _fetch_one(code: str) -> float | None:
    """quote で取れなかった銘柄だけ、info で1つずつ取る。"""
    import yfinance as yf

    try:
        info = yf.Ticker(f"{code}.T").info or {}
    except Exception:                                           # noqa: BLE001
        return None
    value = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
    return float(value) if value else None


def refresh(codes: Iterable[str], path: Path = SHARES_CSV, *, refetch_all: bool = False,
            today: dt.date | None = None, workers: int = 2, budget_s: float = 1500.0) -> dict[str, int]:
    """古い（MAX_AGE_DAYS より前）・無い銘柄の株式数を取り直して書く。取れなかった銘柄は前の値を残す。"""
    today = today or dt.date.today()
    current = dict(shares_table(path))
    wanted = []
    for code in sorted({_code(c) for c in codes if _code(c)}):
        old = current.get(code)
        stale = True
        if old and not refetch_all:
            try:
                stale = (today - dt.date.fromisoformat(old[1])).days > MAX_AGE_DAYS
            except ValueError:
                stale = True
        if stale:
            wanted.append(code)
    started, found = time.monotonic(), {}
    for i in range(0, len(wanted), BATCH):
        if time.monotonic() - started > budget_s:
            break
        found.update(_fetch_batch(wanted[i:i + BATCH]))
        time.sleep(0.5)
    rest = [c for c in wanted if c not in found]
    with ThreadPoolExecutor(workers) as pool:
        for code, shares in zip(rest, pool.map(lambda c: None if time.monotonic() - started > budget_s
                                                else _fetch_one(c), rest)):
            if shares:
                found[code] = shares
    for code, shares in found.items():
        current[code] = (shares, today.isoformat())
    got, missed = len(found), len(wanted) - len(found)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["code", "shares", "asof"])
        for code in sorted(current):
            writer.writerow([code, f"{current[code][0]:.0f}", current[code][1]])
    _load.cache_clear()
    return {"wanted": len(wanted), "updated": got, "missed": missed, "total": len(current)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="時価総額用の発行済株式数を取り直す")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--all", action="store_true", help="古さに関係なく全部取り直す")
    ap.add_argument("--budget", type=float, default=1500.0, help="取得にかける秒数の上限")
    a = ap.parse_args(argv)
    if not a.refresh:
        print(f"market_cap_shares rows={len(shares_table())}")
        return 0
    from scanner.universe import UniverseConfig, load_jpx_listed

    universe = load_jpx_listed(UniverseConfig(markets=("prime", "standard", "growth")))
    stats = refresh(universe["code"].astype(str), refetch_all=a.all, budget_s=a.budget)
    print("market_cap_refresh " + " ".join(f"{k}={v}" for k, v in stats.items()), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
