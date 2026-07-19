"""note記事カード用のファンダメンタル自動取得（Yahoo Finance / yfinance）。

v10(2026-07-19): 未取得だらけのカードを埋めるための補完モジュール。
- スクリーニングCSVに無い列（PER/PBR/配当/ROE/利益率/成長率/時価総額/前日比/値幅）を
  表示対象の銘柄（各セクション最大10行）だけ yfinance で取得して補完する。
- 取得失敗・欠損はそのまま None（記事側は「未取得」表示で続行）。捏造はしない。
- 環境変数 NOTE_FETCH_FUNDAMENTALS=0 で完全に無効化できる（デフォルト有効）。
- 同一プロセス内はコードごとにキャッシュし、二重取得しない。
"""

from __future__ import annotations

import os
from functools import lru_cache

import pandas as pd

# カードで使う列 → yfinance Ticker.info のキー
INFO_FIELDS = {
    "per": "trailingPE",
    "forward_per": "forwardPE",
    "pbr": "priceToBook",
    "dividend_yield": "dividendYield",
    "roe": "returnOnEquity",
    "operating_margin": "operatingMargins",
    "net_margin": "profitMargins",
    "sales_growth": "revenueGrowth",
    "profit_growth": "earningsGrowth",
    "market_cap": "marketCap",
}
# 比率（0.12=12%）で返る可能性があるキー → %へ正規化
RATIO_TO_PCT = {"roe", "operating_margin", "net_margin", "sales_growth", "profit_growth", "dividend_yield"}
# 異常値ガード（この範囲外は「取得できず」として捨てる。捏造・誤換算の防止）
SANITY_RANGE = {
    "per": (0.0, 1000.0),
    "forward_per": (0.0, 1000.0),
    "pbr": (0.0, 100.0),
    "dividend_yield": (0.0, 15.0),
    "roe": (-100.0, 100.0),
    "operating_margin": (-100.0, 100.0),
    "net_margin": (-100.0, 100.0),
    "sales_growth": (-100.0, 300.0),
    "profit_growth": (-100.0, 300.0),
    "change_pct": (-30.0, 30.0),
    "range_pct": (0.0, 40.0),
}


def _enabled() -> bool:
    return os.environ.get("NOTE_FETCH_FUNDAMENTALS", "1").lower() in {"1", "true", "yes"}


def _to_float(value):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num or num in (float("inf"), float("-inf")):
        return None
    return num


def _normalize(key: str, num: float) -> float | None:
    # 比率(0.12)なら%へ。既に%表記(12.0)ならそのまま。
    if key in RATIO_TO_PCT and abs(num) < 1.0:
        num = num * 100.0
    lo, hi = SANITY_RANGE.get(key, (float("-inf"), float("inf")))
    if not (lo <= num <= hi):
        return None
    return num


@lru_cache(maxsize=256)
def fetch_fundamentals(code: str) -> tuple[tuple[str, float], ...]:
    """codeは4桁等の証券コード。失敗時は空タプル（記事は未取得のまま続行）。"""
    if not _enabled():
        return ()
    try:
        import yfinance as yf

        info = yf.Ticker(f"{code}.T").info or {}
    except Exception:
        return ()
    out: dict[str, float] = {}
    for col, key in INFO_FIELDS.items():
        num = _to_float(info.get(key))
        if num is None:
            continue
        if col == "market_cap":
            out[col] = num
            continue
        norm = _normalize(col, num)
        if norm is not None:
            out[col] = norm
    # 前日比・値幅は当日のスナップショットから算出（取得できた場合のみ）
    prev = _to_float(info.get("previousClose") or info.get("regularMarketPreviousClose"))
    cur = _to_float(info.get("currentPrice") or info.get("regularMarketPrice"))
    if prev and cur and prev > 0:
        change = _normalize("change_pct", (cur / prev - 1.0) * 100.0)
        if change is not None:
            out["change_pct"] = change
    day_high = _to_float(info.get("dayHigh") or info.get("regularMarketDayHigh"))
    day_low = _to_float(info.get("dayLow") or info.get("regularMarketDayLow"))
    if day_high and day_low and prev and prev > 0 and day_high >= day_low:
        rng = _normalize("range_pct", (day_high - day_low) / prev * 100.0)
        if rng is not None:
            out["range_pct"] = rng
    return tuple(sorted(out.items()))


def enrich_fundamentals(df: pd.DataFrame) -> pd.DataFrame:
    """表示対象の行だけ欠損列をyfinanceで補完する。既存の値は上書きしない。"""
    if df.empty or not _enabled() or "code" not in df.columns:
        return df
    out = df.copy()
    cols = list(INFO_FIELDS.keys()) + ["change_pct", "range_pct"]
    for col in cols:
        if col not in out.columns:
            out[col] = pd.NA
    for idx, row in out.iterrows():
        code = str(row.get("code", "")).strip()
        if code.endswith(".0"):
            code = code[:-2]
        if not code or code.lower() in {"nan", "none"}:
            continue
        fetched = dict(fetch_fundamentals(code))
        if not fetched:
            continue
        for col, value in fetched.items():
            current = out.at[idx, col]
            if pd.isna(current) or str(current).strip() == "":
                out.at[idx, col] = value
    return out
