from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pandas as pd

from scanner.highs import classify_high_profile, window_high_profile
from scanner.indicators import calculate_indicators, passes_base_filters
from scanner.patterns import detect_cup_with_handle
from scanner.prices import fetch_next_earnings_date, fetch_price_history, timestamped_csv_path
from scanner.scoring import assess_earnings_window, score_stock
from scanner.universe import UniverseConfig, load_jpx_listed


PROJECT_ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="銘柄ごとの取り逃し理由を確認")
    parser.add_argument("--codes-file", default=str(PROJECT_ROOT / "codes.txt"))
    parser.add_argument("--codes", nargs="*", default=None)
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "outputs"))
    return parser.parse_args()


def load_codes(args: argparse.Namespace) -> list[str]:
    if args.codes:
        return _normalize_codes(args.codes)
    path = Path(args.codes_file)
    if not path.exists():
        raise FileNotFoundError(f"{path} が見つかりません。--codes で直接指定してください。")
    codes = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    return _normalize_codes(codes)


def _normalize_codes(codes: list[str]) -> list[str]:
    cleaned = []
    for code in codes:
        text = code.strip()
        if not text or text.startswith("#"):
            continue
        cleaned.append(text.upper().removesuffix(".T"))
    return cleaned


def load_universe_lookup() -> dict[str, dict[str, object]]:
    try:
        universe = load_jpx_listed(UniverseConfig())
    except Exception:
        return {}
    lookup: dict[str, dict[str, object]] = {}
    for row in universe.itertuples(index=False):
        lookup[str(row.code)] = {
            "name": getattr(row, "name", ""),
            "sector": getattr(row, "sector", ""),
            "ticker": getattr(row, "ticker", f"{row.code}.T"),
        }
    return lookup


def analyze_code(code: str, lookup: dict[str, dict[str, object]]) -> dict[str, object]:
    ticker = f"{code}.T"
    meta = lookup.get(code, {})
    name = meta.get("name", "")
    sector = meta.get("sector", "")
    today = date.today()

    try:
        history = fetch_price_history(ticker)
    except Exception as exc:
        return {
            "code": code,
            "ticker": ticker,
            "name": name,
            "sector": sector,
            "history_days": 0,
            "history_start": "",
            "history_end": "",
            "current_close": "",
            "today_high": "",
            "52w_high": "",
            "52w_high_date": "",
            "52w_high_hit": False,
            "52w_high_label": "",
            "52w_high_dist_to_high_pct": "",
            "60d_high": "",
            "60d_high_date": "",
            "recent_high_hit": False,
            "recent_high_label": "",
            "recent_high_dist_to_high_pct": "",
            "high_type": "OTHER",
            "high_label": "分類外",
            "high_window_days": 0,
            "high_price": 0,
            "high_date": "",
            "dist_to_high_pct": 999,
            "volume_ratio_5d_20d": "",
            "volume_ok": False,
            "ma25": "",
            "ma25_ok": False,
            "ma75": "",
            "ma75_ok": False,
            "turnover_20d": "",
            "turnover_ok": False,
            "base_filter_ok": False,
            "base_filter_reasons": f"価格取得失敗: {exc}",
            "earnings_status": "",
            "earnings_note": "",
            "score": "",
            "rank": "",
            "rejection_reason": f"価格取得失敗: {exc}",
        }

    indicators = calculate_indicators(history)
    current_close = float(history["Close"].iloc[-1]) if not history.empty else 0.0
    today_high = float(history["High"].iloc[-1]) if not history.empty else 0.0
    high_52w = window_high_profile(history, 252)
    high_60d = window_high_profile(history, 60)
    high_profile = classify_high_profile(history)
    volume_ratio = float(indicators["volume_ratio_5d_20d"]) if indicators else 0.0
    volume_ok = bool(indicators and indicators.get("volume_above_20d", False))
    ma25_ok = bool(indicators and indicators.get("current_price", 0) > indicators.get("ma25", float("inf")))
    ma75_ok = bool(indicators and indicators.get("current_price", 0) > indicators.get("ma75", float("inf")))
    turnover_ok = bool(indicators and indicators.get("turnover_20d", 0) >= 100_000_000)

    if indicators is None:
        return {
            "code": code,
            "ticker": ticker,
            "name": name,
            "sector": sector,
            "history_days": len(history),
            "history_start": history.index[0].date().isoformat() if not history.empty else "",
            "history_end": history.index[-1].date().isoformat() if not history.empty else "",
            "current_close": round(current_close, 1),
            "today_high": round(today_high, 1),
            "52w_high": high_52w.high_price if high_52w else "",
            "52w_high_date": high_52w.high_date if high_52w else "",
            "52w_high_hit": bool(high_52w),
            "52w_high_label": high_52w.high_label if high_52w else "",
            "52w_high_dist_to_high_pct": high_52w.dist_to_high_pct if high_52w else "",
            "60d_high": high_60d.high_price if high_60d else "",
            "60d_high_date": high_60d.high_date if high_60d else "",
            "recent_high_hit": bool(high_60d),
            "recent_high_label": high_60d.high_label if high_60d else "",
            "recent_high_dist_to_high_pct": high_60d.dist_to_high_pct if high_60d else "",
            **high_profile,
            "volume_ratio_5d_20d": "",
            "volume_ok": False,
            "ma25": "",
            "ma25_ok": False,
            "ma75": "",
            "ma75_ok": False,
            "turnover_20d": "",
            "turnover_ok": False,
            "base_filter_ok": False,
            "base_filter_reasons": "価格データ不足",
            "earnings_status": "",
            "earnings_note": "",
            "score": "",
            "rank": "",
            "rejection_reason": "価格データ不足",
        }

    base_ok, base_reasons = passes_base_filters(indicators)
    earnings_date = fetch_next_earnings_date(ticker)
    earnings = assess_earnings_window(today, earnings_date)
    cwh = detect_cup_with_handle(history["Close"])
    scored = score_stock(indicators, cwh, earnings, capital=3_000_000, name=name, sector=sector)
    rejection_reasons: list[str] = []
    if not base_ok:
        rejection_reasons.extend(base_reasons)
    if earnings["exclude_for_earnings"]:
        rejection_reasons.append(str(earnings["earnings_note"]))
    if scored.get("rank") == "見送り":
        rejection_reasons.append(str(scored.get("reason", "")))

    return {
        "code": code,
        "ticker": ticker,
        "name": name,
        "sector": sector,
        "history_days": len(history),
        "history_start": history.index[0].date().isoformat() if not history.empty else "",
        "history_end": history.index[-1].date().isoformat() if not history.empty else "",
        "current_close": round(indicators["current_price"], 1),
        "today_high": round(today_high, 1),
        "52w_high": high_52w.high_price if high_52w else round(indicators["high_52w"], 1),
        "52w_high_date": high_52w.high_date if high_52w else "",
        "52w_high_hit": bool(high_52w),
        "52w_high_label": high_52w.high_label if high_52w else "",
        "52w_high_dist_to_high_pct": high_52w.dist_to_high_pct if high_52w else "",
        "60d_high": high_60d.high_price if high_60d else "",
        "60d_high_date": high_60d.high_date if high_60d else "",
        "recent_high_hit": bool(high_60d),
        "recent_high_label": high_60d.high_label if high_60d else "",
        "recent_high_dist_to_high_pct": high_60d.dist_to_high_pct if high_60d else "",
        **high_profile,
        "volume_ratio_5d_20d": round(volume_ratio, 2),
        "volume_ok": volume_ok,
        "ma25": round(indicators["ma25"], 1),
        "ma25_ok": ma25_ok,
        "ma75": round(indicators["ma75"], 1),
        "ma75_ok": ma75_ok,
        "turnover_20d": int(indicators["turnover_20d"]),
        "turnover_ok": turnover_ok,
        "base_filter_ok": base_ok,
        "base_filter_reasons": " / ".join(base_reasons) if base_reasons else "",
        "earnings_status": earnings.get("earnings_status", ""),
        "earnings_note": earnings.get("earnings_note", ""),
        "score": scored.get("score", ""),
        "rank": scored.get("rank", ""),
        "rejection_reason": " / ".join([reason for reason in rejection_reasons if reason]),
    }


def main() -> None:
    args = parse_args()
    codes = load_codes(args)
    lookup = load_universe_lookup()
    rows = [analyze_code(code, lookup) for code in codes]
    df = pd.DataFrame(rows)
    path = timestamped_csv_path(args.output_dir, prefix="miss_check")
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(df.to_string(index=False))
    print(f"保存しました: {path}")


if __name__ == "__main__":
    main()
