from __future__ import annotations

import os
import re
import shutil
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import yfinance as yf


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRICE_CACHE_ROOT = PROJECT_ROOT / "cache" / "prices"
PREFETCH_BATCH_SIZE = 200
CACHE_KEEP_DAYS = 3


def _cache_enabled() -> bool:
    return os.environ.get("PRICE_CACHE_DISABLE", "").strip().lower() not in {"1", "true", "yes"}


def _cache_dir(period: str, run_date: date | None = None) -> Path:
    run_date = run_date or date.today()
    safe_period = re.sub(r"[^A-Za-z0-9]+", "_", str(period))
    return PRICE_CACHE_ROOT / f"{run_date.isoformat()}__{safe_period}"


def _cache_path(ticker: str, period: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(ticker))
    return _cache_dir(period) / f"{safe}.parquet"


def _empty_marker_path(cache_path: Path) -> Path:
    return cache_path.with_suffix(".empty")


def _save_price_cache(df: pd.DataFrame, cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if df is None or df.empty:
        _empty_marker_path(cache_path).touch()
        return
    try:
        df.to_parquet(cache_path)
    except Exception:
        try:
            df.to_pickle(cache_path.with_suffix(".pkl"))
        except Exception:
            pass


def _read_price_cache(cache_path: Path) -> pd.DataFrame | None:
    """キャッシュ読込。ヒットなしはNone、空マーカーは空DataFrameを返す。"""
    if _empty_marker_path(cache_path).exists():
        return pd.DataFrame()
    if cache_path.exists():
        try:
            return pd.read_parquet(cache_path)
        except Exception:
            pass
    pkl_path = cache_path.with_suffix(".pkl")
    if pkl_path.exists():
        try:
            return pd.read_pickle(pkl_path)
        except Exception:
            pass
    return None


def cleanup_old_price_cache(keep_days: int = CACHE_KEEP_DAYS) -> None:
    """当日を含む直近keep_days日より古い日付ディレクトリを削除する。"""
    if not PRICE_CACHE_ROOT.exists():
        return
    today = date.today()
    for entry in PRICE_CACHE_ROOT.iterdir():
        if not entry.is_dir():
            continue
        date_part = entry.name.split("__", 1)[0]
        try:
            entry_date = date.fromisoformat(date_part)
        except ValueError:
            continue
        if (today - entry_date).days >= keep_days:
            shutil.rmtree(entry, ignore_errors=True)


def _split_batch_frame(batch: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if batch is None or batch.empty:
        return pd.DataFrame()
    if isinstance(batch.columns, pd.MultiIndex):
        top = batch.columns.get_level_values(0)
        if ticker in set(top):
            sub = batch[ticker]
        else:
            return pd.DataFrame()
    else:
        sub = batch
    sub = sub.dropna(how="all")
    return sub


def prefetch_price_histories(
    tickers: list[str],
    period: str = "18mo",
    batch_size: int = PREFETCH_BATCH_SIZE,
) -> dict[str, int]:
    """複数銘柄をバッチ取得してキャッシュに保存する。

    既にキャッシュ済みの銘柄はスキップ。戻り値は集計 {"cached", "fetched", "empty"}。
    """
    stats = {"cached": 0, "fetched": 0, "empty": 0}
    if not tickers or not _cache_enabled():
        return stats

    cleanup_old_price_cache()

    pending: list[str] = []
    seen: set[str] = set()
    for ticker in tickers:
        ticker = str(ticker).strip()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        if _read_price_cache(_cache_path(ticker, period)) is not None:
            stats["cached"] += 1
        else:
            pending.append(ticker)

    for start in range(0, len(pending), batch_size):
        chunk = pending[start:start + batch_size]
        try:
            batch = yf.download(
                chunk,
                period=period,
                interval="1d",
                auto_adjust=True,
                progress=False,
                group_by="ticker",
                threads=True,
            )
        except Exception:
            batch = None
        for ticker in chunk:
            raw = _split_batch_frame(batch, ticker) if batch is not None else pd.DataFrame()
            try:
                normalized = normalize_price_history(raw)
            except ValueError:
                normalized = pd.DataFrame()
            _save_price_cache(normalized, _cache_path(ticker, period))
            if normalized.empty:
                stats["empty"] += 1
            else:
                stats["fetched"] += 1
    return stats


def fetch_price_history(ticker: str, period: str = "18mo") -> pd.DataFrame:
    if _cache_enabled():
        cache_path = _cache_path(ticker, period)
        cached = _read_price_cache(cache_path)
        if cached is not None:
            return cached if cached.empty else normalize_price_history(cached)

    df = yf.download(ticker, period=period, interval="1d", auto_adjust=True, progress=False)
    result = normalize_price_history(df)
    if _cache_enabled():
        _save_price_cache(result, _cache_path(ticker, period))
    return result


def normalize_price_history(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    if isinstance(df.columns, pd.MultiIndex):
        df = df.droplevel(-1, axis=1)

    needed = ["Open", "High", "Low", "Close", "Volume"]
    missing = [col for col in needed if col not in df.columns]
    if missing:
        raise ValueError(f"Price history is missing columns: {missing}")

    out = df[needed].copy()
    out.index = pd.to_datetime(out.index)
    out = out.dropna(subset=["Close", "Volume"])
    return out


def fetch_next_earnings_date(ticker: str) -> date | None:
    try:
        calendar = yf.Ticker(ticker).calendar
    except Exception:
        return None

    if calendar is None:
        return None

    value = None
    if isinstance(calendar, dict):
        value = calendar.get("Earnings Date") or calendar.get("EarningsDate")
    elif isinstance(calendar, pd.DataFrame):
        for key in ("Earnings Date", "EarningsDate"):
            if key in calendar.index:
                value = calendar.loc[key].dropna().iloc[0]
                break
            if key in calendar.columns:
                value = calendar[key].dropna().iloc[0]
                break

    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    if value is None:
        return None

    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    if isinstance(parsed, pd.DatetimeIndex):
        parsed = parsed[0]
    return parsed.date()


def ensure_output_dir(path: str | Path) -> Path:
    output_dir = Path(path)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def timestamped_csv_path(output_dir: str | Path, prefix: str = "screening_result") -> Path:
    directory = ensure_output_dir(output_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return directory / f"{prefix}_{stamp}.csv"
