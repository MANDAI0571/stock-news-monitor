"""ファンダメンタル指標の取得・単位検証・異常値チェック（T-K 2026-07-12）。

方針（捏造しない）:
- 取得できた値だけを返す。欠損は辞書にキー自体を入れない（note側は行を非表示にする）。
- 単位・桁・通貨の異常が疑われる値は「捨てる」（推測で補正しない）。
- 株価の整合チェック（前日終値×前日比 ≒ 現在値）で桁ずれ・列取り違えを検出する。

データ優先順位の現状:
- 本モジュールは yfinance（補助データ、優先順位4）のみを実装している。
- 決算短信・EDINET・TDnet の自動取得は未実装（既知の制限）。取得元は
  fundamentals_source 列に明記し、note側では断定を避ける。
"""

from __future__ import annotations

import math

# ---- 単位・値域の妥当性ルール -------------------------------------------------
# 日本株の常識的な範囲。範囲外は「異常」ではなく「使わない」（捏造・誤掲載の防止）。
_BOUNDS = {
    "per_actual": (0.1, 3000.0),
    "per_forecast": (0.1, 3000.0),
    "pbr": (0.01, 200.0),
    "dividend_yield_pct": (0.0, 15.0),
    "roe_pct": (-300.0, 300.0),
    "op_margin_pct": (-300.0, 100.0),
    "net_margin_pct": (-300.0, 100.0),
    "sales_growth_pct": (-100.0, 1000.0),
    "profit_growth_pct": (-100.0, 10000.0),
    # 時価総額（億円）: 1億円〜150兆円
    "market_cap_oku": (1.0, 1_500_000.0),
}


def _num(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _bounded(key: str, value: float | None) -> float | None:
    if value is None:
        return None
    low, high = _BOUNDS[key]
    if not (low <= value <= high):
        return None
    return value


def sanitize_fundamentals(info: dict, current_price: float | None = None) -> dict[str, object]:
    """yfinance の info 辞書から、妥当性チェック済みの指標だけを取り出す。

    - 通貨が JPY 以外なら金額系（時価総額）は使わない（単位事故防止）。
    - 比率系は yfinance の小数（0.12=12%）を % に変換する。
    - 値域外の値は返さない（欠損扱い）。
    """
    if not isinstance(info, dict) or not info:
        return {}
    out: dict[str, object] = {}
    currency = str(info.get("currency") or "").upper()

    per_actual = _bounded("per_actual", _num(info.get("trailingPE")))
    per_forecast = _bounded("per_forecast", _num(info.get("forwardPE")))
    pbr = _bounded("pbr", _num(info.get("priceToBook")))
    if per_actual is not None:
        out["per_actual"] = round(per_actual, 1)
    if per_forecast is not None:
        out["per_forecast"] = round(per_forecast, 1)
    if pbr is not None:
        out["pbr"] = round(pbr, 2)

    dividend = _num(info.get("dividendYield"))
    if dividend is not None:
        # yfinance は 0.0123（小数）の時期と 1.23（%）の時期がある。5%超の小数は
        # ありえないので、1未満なら小数とみなして%へ変換する。
        pct = dividend * 100 if dividend < 1 else dividend
        pct = _bounded("dividend_yield_pct", pct)
        if pct is not None:
            out["dividend_yield_pct"] = round(pct, 2)

    for src, dst in (
        ("returnOnEquity", "roe_pct"),
        ("operatingMargins", "op_margin_pct"),
        ("profitMargins", "net_margin_pct"),
        ("revenueGrowth", "sales_growth_pct"),
        ("earningsGrowth", "profit_growth_pct"),
    ):
        value = _num(info.get(src))
        if value is None:
            continue
        pct = _bounded(dst, value * 100)
        if pct is not None:
            out[dst] = round(pct, 1)

    market_cap = _num(info.get("marketCap"))
    if market_cap is not None and currency in ("JPY", ""):
        oku = _bounded("market_cap_oku", market_cap / 1e8)
        if oku is not None:
            # 現在値とyfinance側の株価が大きく乖離していたら時価総額も信用しない
            yf_price = _num(info.get("currentPrice")) or _num(info.get("regularMarketPrice"))
            cp = _num(current_price)
            if yf_price is not None and cp is not None and cp > 0:
                if abs(yf_price - cp) / cp > 0.2:
                    oku = None  # 株価不整合 → 桁・分割ズレの疑い
            if oku is not None:
                out["market_cap_oku"] = round(oku, 0)

    if out:
        out["fundamentals_source"] = "yfinance"
    return out


def fetch_fundamentals(ticker: str, current_price: float | None = None) -> dict[str, object]:
    """yfinance からファンダを取得（失敗は空辞書。noteパイプラインを止めない）。"""
    try:
        import yfinance as yf

        info = yf.Ticker(ticker).info or {}
    except Exception:
        return {}
    return sanitize_fundamentals(info, current_price)


# ---- 株価・単位の異常値チェック ------------------------------------------------

def detect_price_anomalies(row: dict) -> list[str]:
    """記事掲載前の異常値検出。異常理由のリストを返す（空なら正常）。

    検出対象:
    - 前日比と前日終値から逆算した現在値が合わない（桁ずれ・列取り違え）
    - 1日の変化率が値幅制限を大きく超える（±35%超）
    - 株価・売買代金が非現実的（0以下、または極端な桁）
    - 本日高値 < 現在値（高値列の取り違え）
    """
    issues: list[str] = []
    current = _num(row.get("current_price"))
    prev_close = _num(row.get("prev_close"))
    change_pct = _num(row.get("change_pct"))
    today_high = _num(row.get("today_high"))
    turnover = _num(row.get("turnover_20d"))

    if current is not None and not (1.0 <= current <= 10_000_000.0):
        issues.append(f"株価が非現実的({current})")
    if current is not None and prev_close is not None and change_pct is not None and prev_close > 0:
        implied = prev_close * (1 + change_pct / 100)
        if current > 0 and abs(implied - current) / current > 0.02:
            issues.append("前日比と前日終値から逆算した現在値が不整合（桁ずれ・分割の疑い）")
    if change_pct is not None and abs(change_pct) > 35.0:
        issues.append(f"前日比{change_pct:+.1f}%は値幅制限を超過（データ異常の疑い）")
    if today_high is not None and current is not None and today_high < current * 0.999:
        issues.append("本日高値が現在値を下回る（高値データ異常）")
    if turnover is not None and (turnover < 0 or turnover > 5e13):
        issues.append("売買代金の単位異常の疑い")
    market_cap = _num(row.get("market_cap_oku"))
    if market_cap is not None and not (1.0 <= market_cap <= 1_500_000.0):
        issues.append("時価総額の単位変換異常の疑い")
    return issues
