from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pandas as pd
import requests


JPX_LISTED_URLS = [
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq00000030ne-att/data_j.xls",
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls",
]

MARKET_LABELS = {
    "prime": "プライム",
    "standard": "スタンダード",
    "growth": "グロース",
}

CACHE_DIR = Path(__file__).resolve().parents[1] / "cache"
JPX_CACHE_PATH = CACHE_DIR / "jpx_listed.csv"
JPX_CACHE_META_PATH = CACHE_DIR / "jpx_listed.meta.json"


@dataclass(frozen=True)
class UniverseConfig:
    markets: tuple[str, ...] = ("prime", "standard", "growth")
    timeout: int = 30


def load_jpx_listed(config: UniverseConfig | None = None) -> pd.DataFrame:
    config = config or UniverseConfig()
    try:
        raw, source_url = _download_jpx_excel(config.timeout)
        source = pd.read_excel(BytesIO(raw), sheet_name=0)
        full = normalize_jpx_listed(source, ("prime", "standard", "growth"))
        _save_jpx_cache(full, source_url)
        return _filter_markets(full, config.markets)
    except Exception as exc:
        cached = _load_jpx_cache()
        if cached is not None:
            print(
                f"[JPX] download failed: {exc}. using cache: {JPX_CACHE_PATH}",
                file=sys.stderr,
            )
            return _filter_markets(cached, config.markets)
        print(f"[JPX] download failed and cache missing: {exc}", file=sys.stderr)
        raise RuntimeError(
            "JPX銘柄一覧を取得できません。正常なキャッシュが存在しないため中断します。"
        ) from exc


def normalize_jpx_listed(source: pd.DataFrame, markets: tuple[str, ...]) -> pd.DataFrame:
    code_col = _find_col(source, "コード")
    name_col = _find_col(source, "銘柄名")
    market_col = _find_col(source, "市場", "区分")
    sector_col = _find_col(source, "33業種区分")

    if not code_col or not name_col or not market_col:
        raise ValueError(f"JPX file format is not supported: {source.columns.tolist()}")

    allowed_market_words = tuple(MARKET_LABELS[m] for m in markets)
    rows: list[dict[str, str]] = []

    for _, item in source.iterrows():
        code = str(item[code_col]).strip()
        # Excelでコードが数値として読まれると "1000.0" 形式になるため末尾の .0 を除去する。
        if code.endswith(".0"):
            code = code[:-2]
        name = str(item[name_col]).strip()
        market_raw = str(item[market_col]).strip()
        sector = str(item[sector_col]).strip() if sector_col else "-"

        if not code.isdigit() or len(code) != 4:
            continue
        if _is_excluded_security(market_raw, name):
            continue
        if not any(word in market_raw for word in allowed_market_words):
            continue

        market = _market_label(market_raw)
        if market is None:
            continue

        rows.append(
            {
                "ticker": f"{code}.T",
                "code": code,
                "name": name,
                "market": market,
                "sector": sector,
            }
        )

    return pd.DataFrame(rows).drop_duplicates("ticker").reset_index(drop=True)


def _download_jpx_excel(timeout: int) -> tuple[bytes, str]:
    errors: list[str] = []
    for url in JPX_LISTED_URLS:
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            if len(response.content) > 1000:
                return response.content, url
            errors.append(f"{url}: response too small")
        except requests.RequestException as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("Failed to download JPX listed company file. " + " / ".join(errors))


def _save_jpx_cache(df: pd.DataFrame, source_url: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(JPX_CACHE_PATH, index=False, encoding="utf-8-sig")
    meta = {
        "source_url": source_url,
        "rows": int(len(df)),
        "columns": list(df.columns),
    }
    JPX_CACHE_META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_jpx_cache() -> pd.DataFrame | None:
    if not JPX_CACHE_PATH.exists():
        return None
    try:
        cached = pd.read_csv(JPX_CACHE_PATH, dtype={"code": str, "ticker": str, "name": str, "market": str, "sector": str})
    except Exception as exc:
        print(f"[JPX] cache read failed: {exc}", file=sys.stderr)
        return None
    required = {"ticker", "code", "name", "market", "sector"}
    if not required.issubset(cached.columns):
        print(f"[JPX] cache format invalid: {JPX_CACHE_PATH}", file=sys.stderr)
        return None
    return cached[list(["ticker", "code", "name", "market", "sector"])].drop_duplicates("ticker").reset_index(drop=True)


def _filter_markets(df: pd.DataFrame, markets: tuple[str, ...]) -> pd.DataFrame:
    if df.empty:
        return df
    allowed = {f"東証{MARKET_LABELS[m]}" for m in markets}
    return df[df["market"].isin(allowed)].reset_index(drop=True)


def _find_col(df: pd.DataFrame, *keywords: str) -> str | None:
    for col in df.columns:
        text = str(col)
        if all(keyword in text for keyword in keywords):
            return col
    return None


def _is_excluded_security(market_raw: str, name: str) -> bool:
    text = f"{market_raw} {name}"
    excluded_words = (
        "ETF",
        "ETN",
        "REIT",
        "リート",
        "投資法人",
        "連動型",
        "インデックス",
        "指数",
        "指数連動",
        "上場投信",
        "投資信託",
        "投信",
        "インフラファンド",
        "出資証券",
        "優先",
        "PRO Market",
    )
    return any(word in text for word in excluded_words)


def _market_label(market_raw: str) -> str | None:
    if "プライム" in market_raw:
        return "東証プライム"
    if "スタンダード" in market_raw:
        return "東証スタンダード"
    if "グロース" in market_raw:
        return "東証グロース"
    return None
