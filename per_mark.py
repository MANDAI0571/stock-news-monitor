#!/usr/bin/env python3
"""52週高値の銘柄に、PER が同じ業種の中でどのくらいかの印を付ける（2026-11-20 持ち主
「52週高値でPERが同じ業種で平均より安い場合は二重丸、かなり安い場合ははなまる。リアルタイムメールでまる、二重丸」）。

- PER = 現在値（終値） ÷ 1株あたり利益（直近12か月の実績 EPS）。EPS は data/jp_eps.csv（コード・EPS・業種・取得日）。
- 業種の平均的な PER は、東証33業種ごとの PER の中央値（data/jp_sector_per.csv）。極端な PER（赤字すれすれで数百倍など）に
  引っぱられないよう、平均ではなく真ん中の値を使う。PER がマイナス・無い銘柄（赤字・データ無し）は数えない。
- 印の段階: 1 = PER が業種の中央値より低い、2 = 中央値の 0.7倍以下（3割以上低い）。
  記事・日々のメール: 1 → ◎、2 → 💮。リアルタイムの高値メール: 1 → ○、2 → ◎。
- EPS と業種の中央値は、週1回 market_cap_refresh.yml（土曜朝）がまとめて取り直す（Yahoo Finance の quote。100銘柄ずつ）。
  取れなかった銘柄は前の値を残す。推測では埋めない。ファイルにはコード・数字・業種名だけを入れ、銘柄名は入れない。
- 物差しが1つ（PER）だけの印で、売買をすすめるものではない。文には「割安」などの言葉を使わない。

  python3 per_mark.py                 # いまのファイルの件数を出す
  python3 per_mark.py --refresh       # EPS と業種の中央値を取り直す
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import statistics
import time
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EPS_CSV = ROOT / "data" / "jp_eps.csv"
SECTOR_CSV = ROOT / "data" / "jp_sector_per.csv"
QUOTE_URL = "https://query2.finance.yahoo.com/v7/finance/quote"
BATCH = 100
PER_MAX = 3000.0          # fundamentals.py の per_actual と同じ上限。これより上は使わない
MIN_SECTOR_N = 5          # 業種の中央値を出すのに要る銘柄の数
MUCH_LOWER = 0.7          # 中央値のこの倍率以下を「かなり低い」とする

ARTICLE_MARKS = {1: "◎", 2: "💮"}
REALTIME_MARKS = {1: "○", 2: "◎"}
ARTICLE_NOTE = ("◎＝PER が同じ業種の平均的な PER（33業種ごとの中央値）より低い、💮＝中央値の0.7倍以下（3割以上低い）。"
                "PER は現在値（終値）÷直近12か月の1株あたり利益で、赤字の銘柄には付けません。"
                "物差しは PER だけで、売買をすすめるものではありません。")
REALTIME_NOTE = ("○＝PER が同じ業種の平均的な PER（33業種ごとの中央値）より低い、◎＝中央値の0.7倍以下（3割以上低い）。"
                 "52週高値更新の銘柄だけに付けています。PER は現在値÷直近12か月の1株あたり利益で、赤字の銘柄には付けません。"
                 "物差しは PER だけで、売買をすすめるものではありません。")


def _code(value: object) -> str:
    text = str(value or "").strip().upper()
    return text[:-2] if text.endswith(".T") else text


def _float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number == number else None


@lru_cache(maxsize=4)
def _load_eps(path: str) -> dict[str, tuple[float, str, str]]:
    table: dict[str, tuple[float, str, str]] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                eps = _float(row.get("eps"))
                if eps is not None:
                    table[_code(row.get("code"))] = (eps, str(row.get("sector") or ""), str(row.get("asof") or ""))
    except OSError:
        pass
    return table


@lru_cache(maxsize=4)
def _load_sector(path: str) -> dict[str, float]:
    table: dict[str, float] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                median = _float(row.get("median_per"))
                if median and median > 0:
                    table[str(row.get("sector") or "")] = median
    except OSError:
        pass
    return table


def per_of(code: object, price: object, eps_path: Path = EPS_CSV) -> float | None:
    """PER（倍）。EPS が無い・0以下（赤字）・値段が無いときは None。"""
    px = _float(price)
    found = _load_eps(str(eps_path)).get(_code(code))
    if not px or px <= 0 or not found or found[0] <= 0:
        return None
    per = px / found[0]
    return per if 0 < per <= PER_MAX else None


def per_facts(code: object, price: object, eps_path: Path = EPS_CSV,
              sector_path: Path = SECTOR_CSV) -> dict | None:
    """{"per", "sector", "median", "level"}。level は 0（中央値以上）・1（中央値より低い）・2（0.7倍以下）。
    PER か業種の中央値が出ないときは None（印も文も出さない）。"""
    per = per_of(code, price, eps_path)
    found = _load_eps(str(eps_path)).get(_code(code))
    if per is None or not found:
        return None
    median = _load_sector(str(sector_path)).get(found[1])
    if not median:
        return None
    level = 2 if per <= median * MUCH_LOWER else 1 if per < median else 0
    return {"per": per, "sector": found[1], "median": median, "level": level}


def mark(facts: dict | None, marks: dict[int, str] = ARTICLE_MARKS) -> str:
    return marks.get(int(facts["level"]), "") if facts else ""


def per_text(facts: dict | None, marks: dict[int, str] = ARTICLE_MARKS) -> str:
    """「PER 12.3倍（電気機器の中央値 18.5倍）◎」。出せないときは空。"""
    if not facts:
        return ""
    sign = mark(facts, marks)
    return (f"PER {facts['per']:,.1f}倍（{facts['sector']}の中央値 {facts['median']:,.1f}倍）"
            + (f" {sign}" if sign else ""))


# ---------------------------------------------------------------------------
# 取り直し（週1回）
# ---------------------------------------------------------------------------
def _fetch_batch(codes: list[str]) -> dict[str, tuple[float, float | None]]:
    """{コード: (EPS, 値段)}。Yahoo の quote（100銘柄ずつ）。yfinance の接続（cookie・crumb）を借りる。"""
    from yfinance.data import YfData

    data = YfData()
    for attempt in range(3):
        try:
            response = data.get(QUOTE_URL, params={"symbols": ",".join(f"{c}.T" for c in codes),
                                                   "fields": "epsTrailingTwelveMonths,regularMarketPrice"})
            rows = response.json()["quoteResponse"]["result"]
            return {_code(r.get("symbol")): (float(r["epsTrailingTwelveMonths"]), _float(r.get("regularMarketPrice")))
                    for r in rows if _float(r.get("epsTrailingTwelveMonths")) is not None}
        except Exception:                                       # noqa: BLE001 - 次のまとまりへ進む
            time.sleep(2 + 2 * attempt)
    return {}


def sector_medians(eps: dict[str, tuple[float, str, str]], prices: dict[str, float]) -> dict[str, tuple[float, int]]:
    """{業種: (PER の中央値, 数えた銘柄の数)}。PER が 0 より大きく PER_MAX 以下の銘柄だけ。"""
    by_sector: dict[str, list[float]] = {}
    for code, (value, sector, _) in eps.items():
        price = prices.get(code)
        if not sector or sector == "-" or not price or value <= 0:
            continue
        per = price / value
        if 0 < per <= PER_MAX:
            by_sector.setdefault(sector, []).append(per)
    return {s: (statistics.median(v), len(v)) for s, v in by_sector.items() if len(v) >= MIN_SECTOR_N}


def refresh(listed: Iterable[tuple[str, str]], eps_path: Path = EPS_CSV, sector_path: Path = SECTOR_CSV,
            today: dt.date | None = None, budget_s: float = 600.0) -> dict[str, int]:
    """上場銘柄（コード・業種）の EPS を取り直し、業種ごとの PER の中央値を書く。取れなかった銘柄は前の値を残す。"""
    today = today or dt.date.today()
    sectors = {_code(c): str(s or "") for c, s in listed if _code(c)}
    current = dict(_load_eps(str(eps_path)))
    codes, started, found = sorted(sectors), time.monotonic(), {}
    for i in range(0, len(codes), BATCH):
        if time.monotonic() - started > budget_s:
            break
        found.update(_fetch_batch(codes[i:i + BATCH]))
        time.sleep(0.5)
    for code, (value, _) in found.items():
        current[code] = (value, sectors.get(code, ""), today.isoformat())
    for code, (value, sector, asof) in list(current.items()):   # 業種が変わった銘柄・業種の無い古い行を直す
        if sectors.get(code) and sectors[code] != sector:
            current[code] = (value, sectors[code], asof)
    prices = {c: p for c, (_, p) in found.items() if p}
    medians = sector_medians(current, prices) if prices else {}
    eps_path.parent.mkdir(parents=True, exist_ok=True)
    with open(eps_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["code", "eps", "sector", "asof"])
        for code in sorted(current):
            value, sector, asof = current[code]
            writer.writerow([code, f"{value:.2f}", sector, asof])
    if medians:   # 値段が取れなかった回は、前の中央値を残す
        with open(sector_path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sector", "median_per", "n", "asof"])
            for sector in sorted(medians):
                writer.writerow([sector, f"{medians[sector][0]:.2f}", medians[sector][1], today.isoformat()])
    _load_eps.cache_clear()
    _load_sector.cache_clear()
    return {"wanted": len(codes), "updated": len(found), "total": len(current), "sectors": len(medians)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="PER の印に使う EPS と業種の中央値を取り直す")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--budget", type=float, default=600.0, help="取得にかける秒数の上限")
    a = ap.parse_args(argv)
    if not a.refresh:
        print(f"per_mark eps_rows={len(_load_eps(str(EPS_CSV)))} sectors={len(_load_sector(str(SECTOR_CSV)))}")
        return 0
    from scanner.universe import UniverseConfig, load_jpx_listed

    listed = load_jpx_listed(UniverseConfig(markets=("prime", "standard", "growth")))
    result = refresh(zip(listed["code"].astype(str), listed["sector"].astype(str)), budget_s=a.budget)
    print(f"per_mark_refresh={result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
