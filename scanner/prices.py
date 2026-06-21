from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import yfinance as yf


def fetch_price_history(ticker: str, period: str = "18mo") -> pd.DataFrame:
    df = yf.download(ticker, period=period, interval="1d", auto_adjust=True, progress=False)
    return normalize_price_history(df)


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
