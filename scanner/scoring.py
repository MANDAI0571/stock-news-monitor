from __future__ import annotations

from datetime import date
import unicodedata

import pandas as pd


def assess_earnings_window(today: date, earnings_date: date | None) -> dict[str, object]:
    if earnings_date is None:
        return {
            "earnings_status": "未確認",
            "earnings_date": "",
            "exclude_for_earnings": False,
            "earnings_note": "決算日未確認のため最大A",
        }

    start = _business_days_before(earnings_date, 14)
    end = _business_days_after(earnings_date, 1)
    excluded = start <= today <= end
    return {
        "earnings_status": "確認済",
        "earnings_date": earnings_date.isoformat(),
        "exclude_for_earnings": excluded,
        "earnings_note": "決算回避期間" if excluded else "",
    }


def score_stock(
    indicators: dict[str, float],
    cwh: dict[str, float] | None,
    earnings: dict[str, object],
    capital: float = 3_000_000,
    name: str = "",
    sector: str = "",
    strict: bool = False,
) -> dict[str, object]:
    score = 0
    reasons: list[str] = []

    dist_high = indicators["dist_52w_high_pct"]
    if dist_high <= 3:
        score += 25
        reasons.append("52週高値3%以内")
    elif dist_high <= 7:
        score += 20
        reasons.append("52週高値7%以内")
    elif dist_high <= 15:
        score += 12
        reasons.append("52週高値15%以内")

    if indicators["current_price"] > indicators["ma25"]:
        score += 10
        reasons.append("MA25上")
    if indicators["current_price"] > indicators["ma75"]:
        score += 10
        reasons.append("MA75上")
    if indicators["current_price"] > indicators["ma200"]:
        score += 10
        reasons.append("MA200上")
    if indicators.get("ma25_rising"):
        score += 8
        reasons.append("MA25上向き")
    if indicators.get("ma75_rising"):
        score += 8
        reasons.append("MA75上向き")
    if indicators["ma200_touch_pct"] <= 3:
        score += 8
        reasons.append("MA200タッチ±3%")

    high_freshness = indicators["days_since_52w_high"]
    if high_freshness <= 3:
        score += 12
        reasons.append("52週高値更新3日以内")
    elif high_freshness <= 7:
        score += 8
        reasons.append("52週高値更新7日以内")
    elif high_freshness <= 14:
        score += 5
        reasons.append("52週高値更新14日以内")

    turnover = indicators["turnover_20d"]
    if turnover >= 1_000_000_000:
        score += 15
        reasons.append("売買代金10億円以上")
    elif turnover >= 300_000_000:
        score += 10
        reasons.append("売買代金3億円以上")
    elif turnover >= 100_000_000:
        score += 5
        reasons.append("売買代金1億円以上")

    volume_ratio = indicators["volume_ratio_5d_20d"]
    if volume_ratio >= 2:
        score += 15
        reasons.append("出来高比2倍以上")
    elif volume_ratio >= 1.5:
        score += 10
        reasons.append("出来高比1.5倍以上")
    elif volume_ratio >= 1.1:
        score += 5
        reasons.append("出来高増加")

    if cwh:
        score += 10
        reasons.append("CWH候補")

    theme = detect_theme(name, sector)
    if theme:
        score += 8
        reasons.append(f"テーマ加点:{theme}")

    lot_value = indicators["lot_value_100"]
    if lot_value <= capital * 0.10:
        score += 10
        reasons.append("100株購入額が資金10%以内")
    elif lot_value <= capital * 0.20:
        score += 5
        reasons.append("100株購入額が資金20%以内")

    rank = _rank(score)
    # Sランクは「スコア合計≥85」だけでなく、上昇トレンドのテクニカル必須条件を
    # すべて満たすことを要件にする（DUKE/オニール/ミネルヴィニの本質＝強い上昇トレンドのみ）。
    # ゲート未達はスコアが高くても最大A止まり。
    gate_ok, gate_fail = meets_s_technical_gate(indicators)
    if rank == "S" and not gate_ok:
        rank = "A"
        reasons.append("Sゲート未達(" + "・".join(gate_fail) + ")で最大A")
    strict_gate_ok, strict_gate_fail = meets_strict_s_gate(indicators)
    if strict and rank == "S" and not strict_gate_ok:
        rank = "A"
        reasons.append("strict Sゲート未達(" + "・".join(strict_gate_fail) + ")で最大A")
    if earnings["earnings_status"] == "未確認" and rank == "S":
        rank = "A"
        reasons.append("決算未確認で最大A")

    max_positions_with_capital = int(capital // lot_value) if lot_value > 0 else 0

    return {
        "score": score,
        "rank": rank,
        "lot_value_100": lot_value,
        "max_positions_3m": max_positions_with_capital,
        "reason": " / ".join(reasons),
    }


def rejection_row(
    indicators: dict[str, float] | None,
    reason: str,
    capital: float = 3_000_000,
) -> dict[str, object]:
    current = indicators["current_price"] if indicators else 0
    lot_value = current * 100
    return {
        "score": 0,
        "rank": "見送り",
        "lot_value_100": lot_value,
        "max_positions_3m": int(capital // lot_value) if lot_value > 0 else 0,
        "reason": reason,
    }


def _rank(score: int) -> str:
    if score >= 85:
        return "S"
    if score >= 70:
        return "A"
    if score >= 55:
        return "B"
    return "見送り"


def meets_s_technical_gate(indicators: dict[str, float]) -> tuple[bool, list[str]]:
    """Sランクに必要な「上昇トレンドのテクニカル必須条件」をすべて満たすか判定。

    指定11条件のうち、価格・出来高・移動平均で機械的に再現できる核（②③④⑤⑥⑦⑧）をANDで要求する。
    ①52週高値更新は②(高値3%以内)の最も強い部分集合として包含。
    ⑨好決算・⑩上方修正・⑪テーマ拡張は別データ源が必要なため、この段階のゲートには含めない。
    未達の場合、その理由（不足条件）を返す。
    """
    fail: list[str] = []
    current = indicators.get("current_price", 0.0)
    if indicators.get("dist_52w_high_pct", 999.0) > 3:        # ②(①を含む)
        fail.append("52週高値3%超")
    if not current > indicators.get("ma25", float("inf")):     # ⑤
        fail.append("25日線以下")
    if not current > indicators.get("ma75", float("inf")):     # ⑥
        fail.append("75日線以下")
    if not current > indicators.get("ma200", float("inf")):
        fail.append("200日線以下")
    if not indicators.get("ma25_rising", False):               # ③
        fail.append("25日線が上向きでない")
    if not indicators.get("ma75_rising", False):               # ④
        fail.append("75日線が上向きでない")
    if not indicators.get("volume_above_20d", False):          # ⑦
        fail.append("出来高が20日平均以下")
    if indicators.get("turnover_20d", 0.0) < 100_000_000:      # ⑧
        fail.append("売買代金20日平均1億円未満")
    return not fail, fail


def meets_strict_s_gate(indicators: dict[str, float]) -> tuple[bool, list[str]]:
    fail: list[str] = []
    current = indicators.get("current_price", 0.0)
    if indicators.get("days_since_52w_high", 999) > 0 and indicators.get("dist_52w_high_pct", 999.0) > 1:
        fail.append("52週高値更新または1%以内でない")
    if indicators.get("volume_ratio_5d_20d", 0.0) < 1.5:
        fail.append("出来高倍率1.5倍未満")
    if not indicators.get("ma25_rising", False):
        fail.append("25日線が上向きでない")
    if not indicators.get("ma75_rising", False):
        fail.append("75日線が上向きでない")
    if not current > indicators.get("ma25", float("inf")):
        fail.append("25日線以下")
    if not current > indicators.get("ma75", float("inf")):
        fail.append("75日線以下")
    if not current > indicators.get("ma200", float("inf")):
        fail.append("200日線以下")
    if indicators.get("turnover_20d", 0.0) < 100_000_000:
        fail.append("売買代金20日平均1億円未満")
    return not fail, fail


def detect_theme(name: str, sector: str) -> str:
    text = unicodedata.normalize("NFKC", f"{name} {sector}").lower()
    themes = {
        "半導体": ("半導体", "東京エレクトロン", "レーザーテック", "アドバンテスト", "screen", "ルネサス", "socionext", "ソシオネクスト"),
        "AI": ("ai", "人工知能", "データセンター", "電線", "フジクラ", "古河電気", "swcc", "ソフトバンクグループ"),
        "防衛": ("防衛", "三菱重工", "川崎重工", "ihi", "日本製鋼所"),
        "銀行": ("銀行", "フィナンシャル", "fg"),
        "電力": ("電力", "電気・ガス", "東京電力", "関西電力", "中部電力", "九州電力", "北海道電力", "東北電力"),
    }
    for theme, keywords in themes.items():
        if any(keyword.lower() in text for keyword in keywords):
            return theme
    return ""


def _business_days_before(target: date, days: int) -> date:
    return pd.bdate_range(end=pd.Timestamp(target), periods=days + 1)[0].date()


def _business_days_after(target: date, days: int) -> date:
    return pd.bdate_range(start=pd.Timestamp(target), periods=days + 1)[-1].date()
