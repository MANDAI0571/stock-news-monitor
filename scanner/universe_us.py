"""米株の銘柄一覧（S&P500 ＋ NASDAQ上場の大型株）。

日本株の scanner/universe.py と同じ作りにしてある。
  ・取りに行く → 失敗したらキャッシュ → キャッシュも無ければ中断（捏造しない）
  ・返す列は日本株と同じ（ticker / code / name / market / sector）
    ので、下流のスクリーニングや記事生成をそのまま使い回せる。

入手先（2026-09-06 に実際に取れることを確認した）
  S&P500  : datasets/s-and-p-500-companies の constituents.csv（503銘柄・GICSセクター付き）
  NASDAQ  : rreichel3/US-Stock-Symbols の nasdaq_full_tickers.json（4,129銘柄・時価総額付き）

NASDAQ100 について（正直に）
  公式のNASDAQ-100構成銘柄リストは、この環境から取れる先が見つからなかった。
  そこで「NASDAQ上場のうち、ETF・優先株・変則ティッカーを除いた時価総額 上位N」を使う。
  これは指数そのものではない。記事にもコードにも「近似」と書く。
  正式なリストの入手先が見つかったら、ここを差し替える。
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import requests

SP500_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/"
    "data/constituents.csv"
)
NASDAQ_URL = (
    "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/"
    "nasdaq/nasdaq_full_tickers.json"
)

CACHE_DIR = Path(__file__).resolve().parents[1] / "cache"
US_CACHE_PATH = CACHE_DIR / "us_universe.csv"
US_CACHE_META_PATH = CACHE_DIR / "us_universe.meta.json"

# ETF・優先株・変則ティッカーを弾く言葉。名前に入っていたら採らない。
EXCLUDE_NAME_RE = re.compile(
    r"\b(ETF|ETN|Fund|Trust|Preferred|Depositary|Warrant|Rights?|Units?|"
    r"Notes?|Debenture|Acquisition Corp)\b",
    re.IGNORECASE,
)
# BRK.B / BF.B のような複数クラス株は、yfinanceでは BRK-B / BF-B と書く。
TICKER_RE = re.compile(r"^[A-Z]{1,5}([.\-][A-Z])?$")


def normalize_us_ticker(symbol: str) -> str:
    """S&P500の表記(BRK.B)を、価格取得で使う表記(BRK-B)に直す。"""
    return str(symbol or "").strip().upper().replace(".", "-")


@dataclass(frozen=True)
class UsUniverseConfig:
    nasdaq_top: int = 100      # NASDAQから採る大型株の数（NASDAQ100の近似）
    timeout: int = 30


def load_us_universe(config: UsUniverseConfig | None = None) -> pd.DataFrame:
    """米株の銘柄一覧を返す。取れなければキャッシュ。両方だめなら中断する。"""
    config = config or UsUniverseConfig()
    try:
        sp_raw = _get(SP500_URL, config.timeout)
        nq_raw = _get(NASDAQ_URL, config.timeout)
        full = build_us_universe(sp_raw, nq_raw, config.nasdaq_top)
        if full.empty:
            raise RuntimeError("米株の銘柄一覧が空になりました")
        _save_cache(full)
        return full
    except Exception as exc:
        cached = _load_cache()
        if cached is not None and not cached.empty:
            print(f"[US] download failed: {exc}. using cache: {US_CACHE_PATH}", file=sys.stderr)
            return cached
        print(f"[US] download failed and cache missing: {exc}", file=sys.stderr)
        raise RuntimeError(
            "米株の銘柄一覧を取得できません。正常なキャッシュが存在しないため中断します。"
        ) from exc


def build_us_universe(sp500_csv: str, nasdaq_json: str, nasdaq_top: int = 100) -> pd.DataFrame:
    """取ってきた生データから銘柄一覧を組み立てる。純関数（通信なし）なので自己テストできる。"""
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    for item in csv.DictReader(io.StringIO(sp500_csv)):
        symbol = normalize_us_ticker(item.get("Symbol"))
        name = str(item.get("Security") or "").strip()
        if not TICKER_RE.match(symbol) or not name or symbol in seen:
            continue
        seen.add(symbol)
        rows.append(
            {
                "ticker": symbol,
                "code": symbol,
                "name": name,
                "market": "S&P500",
                "sector": str(item.get("GICS Sector") or "").strip() or "-",
            }
        )

    nasdaq = json.loads(nasdaq_json) if nasdaq_json.strip() else []
    usable = [item for item in nasdaq if _is_usable_nasdaq_row(item)]
    usable.sort(key=lambda item: _market_cap(item), reverse=True)
    for item in usable[: max(0, int(nasdaq_top))]:
        symbol = normalize_us_ticker(item.get("symbol"))
        if symbol in seen:
            continue
        seen.add(symbol)
        rows.append(
            {
                "ticker": symbol,
                "code": symbol,
                "name": str(item.get("name") or "").strip(),
                "market": "NASDAQ大型（NASDAQ100の近似）",
                "sector": str(item.get("sector") or "").strip() or "-",
            }
        )

    return pd.DataFrame(rows).drop_duplicates("ticker").reset_index(drop=True)


def _is_usable_nasdaq_row(item: dict) -> bool:
    symbol = normalize_us_ticker(item.get("symbol"))
    name = str(item.get("name") or "")
    if not TICKER_RE.match(symbol):
        return False
    if not str(item.get("sector") or "").strip():
        return False       # ETF等はセクターが空
    if not str(item.get("industry") or "").strip():
        return False
    if EXCLUDE_NAME_RE.search(name):
        return False
    return _market_cap(item) > 0


def _market_cap(item: dict) -> float:
    try:
        return float(str(item.get("marketCap") or "").replace(",", "").replace("$", "") or 0)
    except (TypeError, ValueError):
        return 0.0


def _get(url: str, timeout: int) -> str:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    if len(response.content) < 1000:
        raise RuntimeError(f"{url}: response too small")
    return response.text


def _save_cache(df: pd.DataFrame) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(US_CACHE_PATH, index=False, encoding="utf-8-sig")
    US_CACHE_META_PATH.write_text(
        json.dumps(
            {"saved_on": date.today().isoformat(), "rows": int(len(df)),
             "sources": [SP500_URL, NASDAQ_URL]},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )


def _load_cache() -> pd.DataFrame | None:
    if not US_CACHE_PATH.exists():
        return None
    try:
        return pd.read_csv(US_CACHE_PATH, dtype=str).fillna("")
    except Exception:
        return None
