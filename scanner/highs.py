from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from scanner.openwork import format_openwork_score


HIGH_TYPE_ORDER = (
    "SWING_HIGH_BREAK",
    "52W_NEW_HIGH",
    "52W_NEAR_HIGH",
    "RECENT_NEW_HIGH",
    "RECENT_NEAR_HIGH",
)

HIGH_LABELS = {
    "SWING_HIGH_BREAK": "直近スイング高値ブレイク",
    "52W_NEW_HIGH": "52週新高値",
    "52W_NEAR_HIGH": "52週高値接近",
    "RECENT_NEW_HIGH": "直近高値更新",
    "RECENT_NEAR_HIGH": "直近高値接近",
    "OTHER": "分類外",
}


@dataclass(frozen=True)
class HighProfile:
    high_type: str = "OTHER"
    high_label: str = "分類外"
    high_window_days: int = 0
    high_price: float = 0.0
    high_date: str = ""
    dist_to_high_pct: float = 999.0


def classify_high_profile(history: pd.DataFrame) -> dict[str, object]:
    if history.empty or "Close" not in history.columns:
        return _as_dict(HighProfile()) | detect_swing_high_break(history)

    close = history["Close"].astype(float)
    swing = detect_swing_high_break(history)
    if len(close) < 60:
        base = _as_dict(HighProfile())
        if swing.get("swing_high_break"):
            base |= _swing_as_high_profile(swing)
        return base | swing

    recent = window_high_profile(history, 60)
    # 上場1年未満(len<252)でもカブタン同様に「上場来ベース」で52週高値判定する。
    wide = window_high_profile(history, 252)

    chosen = _choose_profile(wide, recent)
    base = _as_dict(chosen or HighProfile())
    # T-K修正(2026-07-16): 52週/直近高値の判定が付いた銘柄を SWING_HIGH_BREAK で
    # 上書きしない。52週新高値を付けた銘柄は定義上ほぼ必ず30日スイング高値も
    # ブレイクするため、旧実装では「到達」銘柄が全員 SWING_HIGH_BREAK に化けて
    # 52週リスト(_collect_highs_row)の入口で弾かれ、到達が常に0件になっていた。
    # スイング情報自体は swing_* 列として全行に残る（情報は失わない）。
    if swing.get("swing_high_break") and base.get("high_type") in ("", None, "OTHER"):
        base |= _swing_as_high_profile(swing)
    return base | swing


def _swing_as_high_profile(swing: dict[str, object]) -> dict[str, object]:
    return {
        "high_type": "SWING_HIGH_BREAK",
        "high_label": HIGH_LABELS["SWING_HIGH_BREAK"],
        "high_window_days": 30,
        "high_price": swing.get("swing_high_price", ""),
        "high_date": swing.get("swing_high_date", ""),
        "dist_to_high_pct": swing.get("swing_high_break_pct", ""),
    }


def _choose_profile(wide: HighProfile | None, recent: HighProfile | None) -> HighProfile | None:
    if wide and wide.high_type in ("52W_NEW_HIGH", "52W_NEAR_HIGH"):
        return wide
    if recent and recent.high_type in ("RECENT_NEW_HIGH", "RECENT_NEAR_HIGH"):
        return recent
    return wide or recent


def window_high_profile(history: pd.DataFrame, window_days: int) -> HighProfile | None:
    """カブタン準拠の高値判定。

    - 新高値: 当日ザラバ高値(High) > 「当日を除く」窓内の最高値(High)。
      終値ベースだった旧実装は「ザラバで更新して終値が押した銘柄」を取りこぼしていた。
    - 接近: 現在値(終値)が窓内最高値から3%以内。
    - window_days=252 のとき、上場1年未満は「上場来（実データ全期間）」で判定する
      （カブタンの52週高値更新リストも上場1年未満を含むため）。
    """
    n = len(history)
    if n < 60:
        return None
    effective_days = min(window_days, n)
    label_52w = window_days == 252

    window = history.tail(effective_days)
    close = window["Close"].astype(float)
    high = window["High"].astype(float) if "High" in window.columns else close
    current = float(close.iloc[-1])
    today_high = float(high.iloc[-1])
    prior = high.iloc[:-1]
    if prior.empty:
        return None
    prior_high = float(prior.max())
    if prior_high <= 0 or current <= 0:
        return None

    if today_high > prior_high:
        high_type = "52W_NEW_HIGH" if label_52w else "RECENT_NEW_HIGH"
        high_price = today_high
        high_dt = window.index[-1]
        dist_to_high_pct = (today_high - current) / today_high * 100
    else:
        dist_to_high_pct = (prior_high - current) / prior_high * 100
        if dist_to_high_pct > 3:
            return None
        high_type = "52W_NEAR_HIGH" if label_52w else "RECENT_NEAR_HIGH"
        high_price = prior_high
        matches = prior.reset_index(drop=True).eq(prior_high)
        last_pos = int(matches[matches].index[-1])
        high_dt = window.index[last_pos]

    return HighProfile(
        high_type=high_type,
        high_label=HIGH_LABELS[high_type],
        high_window_days=effective_days,
        high_price=round(high_price, 1),
        high_date=pd.Timestamp(high_dt).date().isoformat(),
        dist_to_high_pct=round(dist_to_high_pct, 2),
    )


def analyze_high_freshness(
    history: pd.DataFrame,
    window_days: int = 252,
    recent_days: int = 20,
    fresh_days: int = 60,
) -> dict[str, object]:
    """52週高値ブレイクの「鮮度」を判定する（純関数・通信なし）。

    - breaks_20d: 直近20営業日で52週高値を更新した回数（当日含む）。
      連日更新している銘柄（イナゴタワー化しやすい）を見分ける材料。
    - first_break_60d: 当日が更新日で、かつ直前60営業日に一度も更新がない＝「初回ブレイク」。
    - days_since_prev_break: 前回の更新から何営業日経過したか（無ければ空文字）。
    """
    out: dict[str, object] = {
        "breaks_20d": 0,
        "first_break_60d": False,
        "days_since_prev_break": "",
        "is_new_high_today": False,
    }
    if history.empty or "Close" not in history.columns or len(history) < 61:
        return out
    close = history["Close"].astype(float)
    high = history["High"].astype(float) if "High" in history.columns else close
    prior_max = high.rolling(window_days, min_periods=30).max().shift(1)
    breaks = (high > prior_max).fillna(False).reset_index(drop=True)
    out["is_new_high_today"] = bool(breaks.iloc[-1])
    out["breaks_20d"] = int(breaks.iloc[-recent_days:].sum())
    prev = breaks.iloc[:-1]
    prev_positions = prev[prev].index
    if len(prev_positions) > 0:
        out["days_since_prev_break"] = int(len(prev) - int(prev_positions[-1]))
    if out["is_new_high_today"]:
        out["first_break_60d"] = not bool(prev.iloc[-fresh_days:].any())
    return out


def detect_quality_flags(history: pd.DataFrame) -> dict[str, object]:
    """イナゴタワー疑い・TOB疑いのフラグ（純関数・通信なし）。

    - inago_suspect: 直近5営業日で+25%以上 または 25MA乖離+30%以上（急騰過熱）。
    - tob_suspect: 直近5営業日の終値がほぼ一定（振れ幅0.4%以内）・日中値幅も極小
      （平均0.6%以内）・52週高値圏（乖離1%以内）＝TOB価格張り付きの典型形。
      ※ヒューリスティックであり確定情報ではない（表示は「疑い」に留める）。
    """
    out: dict[str, object] = {
        "inago_suspect": False,
        "tob_suspect": False,
        "surge_5d_pct": "",
        "quality_flags": "",
    }
    if history.empty or "Close" not in history.columns or len(history) < 30:
        return out
    close = history["Close"].astype(float)
    high = history["High"].astype(float) if "High" in history.columns else close
    low = history["Low"].astype(float) if "Low" in history.columns else close
    current = float(close.iloc[-1])
    if current <= 0:
        return out

    surge_5d = 0.0
    if len(close) >= 6 and float(close.iloc[-6]) > 0:
        surge_5d = (current / float(close.iloc[-6]) - 1.0) * 100
    out["surge_5d_pct"] = round(surge_5d, 2)
    ma25 = float(close.rolling(25).mean().iloc[-1]) if len(close) >= 25 else 0.0
    ma25_gap = (current / ma25 - 1.0) * 100 if ma25 > 0 else 0.0
    if surge_5d >= 25.0 or ma25_gap >= 30.0:
        out["inago_suspect"] = True

    tail_close = close.tail(5)
    tail_high = high.tail(5)
    tail_low = low.tail(5)
    mean_close = float(tail_close.mean())
    if len(tail_close) == 5 and mean_close > 0:
        close_span_pct = (float(tail_close.max()) - float(tail_close.min())) / mean_close * 100
        candle = ((tail_high - tail_low) / tail_close.replace(0.0, pd.NA)).dropna()
        range_pct = float(candle.mean()) * 100 if not candle.empty else 999.0
        hi52 = float(high.tail(min(252, len(high))).max())
        near_high = hi52 > 0 and (hi52 - current) / hi52 * 100 <= 1.0
        if close_span_pct <= 0.4 and range_pct <= 0.6 and near_high:
            out["tob_suspect"] = True

    labels: list[str] = []
    if out["inago_suspect"]:
        labels.append("イナゴ疑い")
    if out["tob_suspect"]:
        labels.append("TOB疑い")
    out["quality_flags"] = " / ".join(labels)
    return out


def high_quality_flags(history: pd.DataFrame) -> dict[str, object]:
    """鮮度＋品質フラグをまとめて返す（screening_highs 行に付与する用）。"""
    return analyze_high_freshness(history) | detect_quality_flags(history)


def _as_dict(profile: HighProfile) -> dict[str, object]:
    return {
        "high_type": profile.high_type,
        "high_label": profile.high_label,
        "high_window_days": profile.high_window_days,
        "high_price": profile.high_price,
        "high_date": profile.high_date,
        "dist_to_high_pct": profile.dist_to_high_pct,
    }



def detect_swing_high_break(history: pd.DataFrame, min_lookback: int = 5, max_lookback: int = 30) -> dict[str, object]:
    """Detect break above a clear recent swing high from 5-30 sessions ago.

    Latest bar is the trigger bar. The swing high is selected from bars 5-30
    sessions before the latest bar, using clear local highs first. Today's high
    or close/current price breaking above that swing high triggers detection.
    """
    empty = {
        "swing_high_price": "",
        "swing_high_date": "",
        "swing_high_break_pct": "",
        "swing_high_break": False,
        "swing_high_label": "",
    }
    if history.empty or "High" not in history.columns or len(history) < min_lookback + 1:
        return empty

    high = history["High"].astype(float)
    close = history["Close"].astype(float) if "Close" in history.columns else high
    latest_pos = len(history) - 1
    start = max(0, latest_pos - max_lookback)
    end = latest_pos - min_lookback + 1
    if end <= start:
        return empty

    candidates: list[tuple[int, float]] = []
    for pos in range(start, end):
        if _is_clear_local_high(high, pos):
            candidates.append((pos, float(high.iloc[pos])))
    if not candidates:
        return empty
    pos, swing_price = max(candidates, key=lambda item: (item[1], item[0]))

    trigger_price = max(float(high.iloc[-1]), float(close.iloc[-1]))
    break_pct = (trigger_price / swing_price - 1.0) * 100 if swing_price > 0 else 0.0
    swing_break = bool(swing_price > 0 and trigger_price > swing_price)
    return {
        "swing_high_price": round(swing_price, 1),
        "swing_high_date": pd.Timestamp(history.index[pos]).date().isoformat(),
        "swing_high_break_pct": round(break_pct, 2),
        "swing_high_break": swing_break,
        "swing_high_label": HIGH_LABELS["SWING_HIGH_BREAK"] if swing_break else "",
    }


def detect_52w_high_retest(
    history: pd.DataFrame,
    window_days: int = 252,
    retest_pct: float = 3.0,
    collapse_pct: float = 8.0,
) -> dict[str, object]:
    """T-C(2026-06-28): 52週新高値ブレイク後のリテスト候補を検出（純関数・通信なし）。

    高重さんの定義に忠実:
      ①52週新高値（窓内の過去終値を上抜け＝最初に抜けた新高値ライン line_price）
      ②その後さらに上昇（その後の高値 post_high > line_price）
      ③そこから下落（現値 < post_high）
      ④最初に抜けた新高値ライン付近まで戻る（|現値-line_price|/line_price ≤ retest_pct）
    トレンド崩壊（現値が line_price から -collapse_pct% より下）は除外。
    データ不足・条件未達は False（捏造しない）。
    """
    empty = {
        "retest_52w": False,
        "retest_line_price": "",
        "retest_breakout_date": "",
        "retest_dist_pct": "",
        "retest_post_high": "",
    }
    if history.empty or "Close" not in history.columns or len(history) < 60:
        return empty

    close = history["Close"].astype(float)
    window = close.tail(window_days)
    series = window.reset_index(drop=True)
    n = len(series)
    if n < 30:
        return empty

    current = float(series.iloc[-1])
    prior_running_high = series.cummax().shift(1)
    # ①最初に「窓内の過去終値」を上抜けした日（最初に抜けた新高値ライン）
    breakout_positions = [
        i for i in range(1, n)
        if pd.notna(prior_running_high.iloc[i]) and float(series.iloc[i]) > float(prior_running_high.iloc[i])
    ]
    if not breakout_positions:
        return empty
    first_bo = breakout_positions[0]
    line_price = float(prior_running_high.iloc[first_bo])
    if line_price <= 0:
        return empty

    # ②ブレイク後にさらに上昇したか（その後の最高値）
    post_high = float(series.iloc[first_bo:].max())
    if post_high <= line_price:
        return empty
    # ③ピークから下落して現在は天井より下
    if not (current < post_high):
        return empty
    # ④新高値ライン付近まで戻っているか
    dist_pct = (current - line_price) / line_price * 100.0
    if abs(dist_pct) > retest_pct:
        return empty
    # トレンド崩壊（ラインを大きく割り込み）は除外
    if dist_pct < -collapse_pct:
        return empty

    breakout_date = pd.Timestamp(window.index[first_bo]).date().isoformat()
    return {
        "retest_52w": True,
        "retest_line_price": round(line_price, 1),
        "retest_breakout_date": breakout_date,
        "retest_dist_pct": round(dist_pct, 2),
        "retest_post_high": round(post_high, 1),
    }


def detect_previous_52w_high_line_retest(
    history: pd.DataFrame,
    indicators: dict[str, float],
    breakout_lookback_days: int = 120,
    line_min_age_days: int = 10,
    min_turnover_20d: float = 100_000_000,
    min_volume_20d: float = 50_000,
) -> dict[str, object]:
    """Detect pullbacks to the prior 52-week high line after a recent new high.

    前回52週高値ライン = 52週新高値を上抜いた日の直前252営業日終値高値。
    このラインまでの押し戻りを、300万円運用候補の専用スコアとして評価する。
    """
    empty = {
        "prev_52w_retest": False,
        "prev_52w_retest_score": 0,
        "prev_52w_retest_rank": "見送り",
        "prev_52w_retest_reason": "",
        "prev_52w_retest_signs": "",
        "previous_52w_high_line": "",
        "previous_52w_high_date": "",
        "breakout_52w_date": "",
        "recent_52w_high": "",
        "recent_52w_high_date": "",
        "line_deviation_pct": "",
        "drawdown_from_recent_high_pct": "",
        "candidate_action": "CASH",
    }
    required = {"Open", "High", "Low", "Close", "Volume"}
    if history.empty or not required.issubset(history.columns) or len(history) < 253:
        return empty | {"prev_52w_retest_reason": "価格データ不足"}
    if float(indicators.get("turnover_20d", 0.0)) < min_turnover_20d:
        return empty | {"prev_52w_retest_reason": "売買代金20日平均1億円未満"}
    if float(indicators.get("volume_20d", 0.0)) < min_volume_20d:
        return empty | {"prev_52w_retest_reason": "出来高20日平均不足"}

    close = history["Close"].astype(float)
    prior_52w_high = close.rolling(252).max().shift(1)
    start = max(252, len(close) - breakout_lookback_days)
    breakout_positions = [
        pos for pos in range(start, len(close))
        if pd.notna(prior_52w_high.iloc[pos]) and close.iloc[pos] > prior_52w_high.iloc[pos]
    ]
    if not breakout_positions:
        return empty | {"prev_52w_retest_reason": "過去120営業日以内の52週新高値更新なし"}

    breakout_pos = breakout_positions[0]
    line_price = float(prior_52w_high.iloc[breakout_pos])
    if line_price <= 0:
        return empty | {"prev_52w_retest_reason": "前回52週高値ラインなし"}

    previous_window = close.iloc[:breakout_pos]
    previous_matches = previous_window.reset_index(drop=True).eq(line_price)
    if not previous_matches.any():
        return empty | {"prev_52w_retest_reason": "前回52週高値日を特定できない"}
    previous_pos = int(previous_matches[previous_matches].index[-1])
    if breakout_pos - previous_pos < line_min_age_days:
        return empty | {"prev_52w_retest_reason": "前回52週高値ラインが近すぎる"}

    post_breakout = close.iloc[breakout_pos:]
    recent_high = float(post_breakout.max())
    recent_high_rel_pos = int(post_breakout.reset_index(drop=True).idxmax())
    recent_high_pos = breakout_pos + recent_high_rel_pos
    current = float(close.iloc[-1])
    if recent_high <= line_price:
        return empty | {"prev_52w_retest_reason": "ブレイク後の上昇不足"}

    line_deviation_pct = (current - line_price) / line_price * 100.0
    drawdown_pct = (current / recent_high - 1.0) * 100.0
    hard_fail: list[str] = []
    if not (-3.0 <= line_deviation_pct <= 5.0):
        hard_fail.append("前回高値ライン乖離が-3%〜+5%外")
    if not (-30.0 <= drawdown_pct <= -8.0):
        hard_fail.append("直近高値からの押し幅が-8%〜-30%外")
    if not current > float(indicators.get("ma75", float("inf"))):
        hard_fail.append("75日線以下")
    if not (bool(indicators.get("ma25_rising")) or bool(indicators.get("ma50_rising"))):
        hard_fail.append("25日線/50日線が上向きでない")

    signs = _recent_rebound_signs(history)
    score, score_reasons = _score_previous_52w_retest(indicators, line_deviation_pct, drawdown_pct, signs)
    rank = _rank_previous_52w_retest(score, bool(hard_fail))
    reasons = score_reasons + signs + hard_fail

    return {
        "prev_52w_retest": rank != "見送り",
        "prev_52w_retest_score": score,
        "prev_52w_retest_rank": rank,
        "prev_52w_retest_reason": " / ".join(reasons),
        "prev_52w_retest_signs": " / ".join(signs),
        "previous_52w_high_line": round(line_price, 1),
        "previous_52w_high_date": pd.Timestamp(history.index[previous_pos]).date().isoformat(),
        "breakout_52w_date": pd.Timestamp(history.index[breakout_pos]).date().isoformat(),
        "recent_52w_high": round(recent_high, 1),
        "recent_52w_high_date": pd.Timestamp(history.index[recent_high_pos]).date().isoformat(),
        "line_deviation_pct": round(line_deviation_pct, 2),
        "drawdown_from_recent_high_pct": round(drawdown_pct, 2),
        "candidate_action": "BUY" if rank == "S" else "CASH",
    }



def detect_duke_old_high_support(
    history: pd.DataFrame,
    indicators: dict[str, float],
    breakout_lookback_days: int = 120,
    min_turnover_20d: float = 100_000_000,
) -> dict[str, object]:
    """DUKE式「旧52週高値サポート押し目」判定。

    52週新高値を一度突破した後、ブレイク前の旧52週高値ライン付近まで
    押してきた銘柄を検出する。ETF/REIT等はユニバース側で除外し、ここでは
    価格・出来高・売買代金の条件だけを評価する。
    """
    empty = {
        "duke_old_high_support": False,
        "old_52w_high": "",
        "old_52w_high_date": "",
        "dist_to_old_52w_high_pct": "",
        "recent_high_after_breakout": "",
        "pullback_from_recent_high_pct": "",
        "duke_support_score": 0,
        "duke_support_signal": False,
        "duke_support_rank": "見送り",
        "duke_support_reason": "",
    }
    required = {"Open", "High", "Low", "Close", "Volume"}
    if history.empty or not required.issubset(history.columns) or len(history) < 260:
        return empty | {"duke_support_reason": "価格データ260営業日未満"}
    if float(indicators.get("turnover_20d", 0.0)) < min_turnover_20d:
        return empty | {"duke_support_reason": "売買代金20日平均1億円未満"}

    high = history["High"].astype(float)
    close = history["Close"].astype(float)
    volume = history["Volume"].astype(float)
    prior_52w_high = high.rolling(252).max().shift(1)
    start = max(252, len(high) - breakout_lookback_days)
    breakout_positions = [
        pos for pos in range(start, len(high))
        if pd.notna(prior_52w_high.iloc[pos]) and high.iloc[pos] > prior_52w_high.iloc[pos]
    ]
    if not breakout_positions:
        return empty | {"duke_support_reason": "直近120営業日以内の52週新高値更新なし"}

    # 最初に旧52週高値を突破した日をブレイク日とする。
    breakout_pos = breakout_positions[0]
    old_line = float(prior_52w_high.iloc[breakout_pos])
    if old_line <= 0:
        return empty | {"duke_support_reason": "旧52週高値ラインなし"}

    old_window = high.iloc[:breakout_pos]
    old_matches = old_window.reset_index(drop=True).eq(old_line)
    if not old_matches.any():
        return empty | {"duke_support_reason": "旧52週高値日を特定できない"}
    old_pos = int(old_matches[old_matches].index[-1])

    current = float(close.iloc[-1])
    recent_high = float(high.iloc[breakout_pos:].max())
    if recent_high <= old_line:
        return empty | {"duke_support_reason": "ブレイク後の上昇不足"}

    dist_pct = (current - old_line) / old_line * 100.0
    pullback_pct = (current / recent_high - 1.0) * 100.0
    signs = _recent_rebound_signs(history)

    score = 0
    reasons: list[str] = []
    hard_fail: list[str] = []

    if 0.0 <= dist_pct <= 3.0:
        score += 30
        reasons.append("旧52週高値ライン0〜3%以内")
    elif -3.0 <= dist_pct <= 5.0:
        score += 20
        reasons.append("旧52週高値ライン-3〜+5%以内")
    else:
        hard_fail.append("旧52週高値ライン-3%〜+5%外")

    if -20.0 <= pullback_pct <= -10.0:
        score += 20
        reasons.append("直近高値から10〜20%押し")
    elif -30.0 <= pullback_pct <= -8.0:
        score += 10
        reasons.append("直近高値から8〜30%押し")
    else:
        hard_fail.append("直近高値からの下落率が-8%〜-30%外")

    if current > float(indicators.get("ma75", float("inf"))):
        score += 15
        reasons.append("75日線より上")
    if bool(indicators.get("ma25_rising")):
        score += 15
        reasons.append("25日線上向き")
    if float(indicators.get("volume_5d", 0.0)) > float(indicators.get("volume_20d", float("inf"))):
        score += 10
        reasons.append("出来高5日平均>20日平均")
    if "直近5日内に陽線" in signs:
        score += 5
        reasons.append("直近5日以内に陽線")
    if "直近5日内に下ヒゲ" in signs:
        score += 5
        reasons.append("直近5日以内に下ヒゲ")

    rank = _rank_duke_support(score)
    if hard_fail:
        rank = "見送り"
    all_reasons = reasons + signs + hard_fail

    return {
        "duke_old_high_support": rank != "見送り",
        "old_52w_high": round(old_line, 1),
        "old_52w_high_date": pd.Timestamp(history.index[old_pos]).date().isoformat(),
        "dist_to_old_52w_high_pct": round(dist_pct, 2),
        "recent_high_after_breakout": round(recent_high, 1),
        "pullback_from_recent_high_pct": round(pullback_pct, 2),
        "duke_support_score": int(score),
        "duke_support_signal": rank != "見送り",
        "duke_support_rank": rank,
        "duke_support_reason": " / ".join(all_reasons),
    }


def _rank_duke_support(score: int) -> str:
    if score >= 80:
        return "S"
    if score >= 65:
        return "A"
    if score >= 50:
        return "B"
    return "見送り"

def _recent_rebound_signs(history: pd.DataFrame, lookback_days: int = 5) -> list[str]:
    recent = history.tail(lookback_days).copy()
    if recent.empty:
        return []
    open_ = recent["Open"].astype(float)
    high = recent["High"].astype(float)
    low = recent["Low"].astype(float)
    close = recent["Close"].astype(float)
    volume = history["Volume"].astype(float)
    volume_20d = float(volume.rolling(20).mean().iloc[-1]) if len(volume) >= 20 else 0.0

    signs: list[str] = []
    if bool((close > open_).any()):
        signs.append("直近5日内に陽線")
    candle_range = (high - low).replace(0, pd.NA)
    lower_wick = pd.concat([open_, close], axis=1).min(axis=1) - low
    body = (close - open_).abs()
    lower_wick_signal = ((lower_wick >= body) & ((lower_wick / candle_range) >= 0.35)).fillna(False)
    if bool(lower_wick_signal.any()):
        signs.append("直近5日内に下ヒゲ")
    if volume_20d > 0 and float(recent["Volume"].max()) > volume_20d:
        signs.append("直近5日内に出来高増加")
    if len(close) >= 2 and float(close.iloc[-1]) > float(close.iloc[-2]):
        signs.append("終値反発")
    return signs


def _score_previous_52w_retest(
    indicators: dict[str, float],
    line_deviation_pct: float,
    drawdown_pct: float,
    signs: list[str],
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    if 0.0 <= line_deviation_pct <= 3.0:
        score += 30
        reasons.append("前回高値ライン0〜3%以内")
    elif -3.0 <= line_deviation_pct <= 5.0:
        score += 20
        reasons.append("前回高値ライン-3〜+5%以内")
    if -20.0 <= drawdown_pct <= -10.0:
        score += 20
        reasons.append("直近高値から10〜20%押し")
    elif -30.0 <= drawdown_pct <= -8.0:
        score += 12
        reasons.append("直近高値から8〜30%押し")
    if float(indicators.get("current_price", 0.0)) > float(indicators.get("ma75", float("inf"))):
        score += 15
        reasons.append("75日線より上")
    if bool(indicators.get("ma25_rising")):
        score += 15
        reasons.append("25日線上向き")
    elif bool(indicators.get("ma50_rising")):
        score += 10
        reasons.append("50日線上向き")
    if "直近5日内に出来高増加" in signs:
        score += 10
    if "直近5日内に陽線" in signs or "直近5日内に下ヒゲ" in signs:
        score += 10
    return score, reasons


def _rank_previous_52w_retest(score: int, hard_fail: bool) -> str:
    if hard_fail:
        return "見送り"
    if score >= 85:
        return "S"
    if score >= 70:
        return "A"
    if score >= 55:
        return "B"
    return "見送り"


def _is_clear_local_high(high: pd.Series, pos: int, left: int = 2, right: int = 2) -> bool:
    target = float(high.iloc[pos])
    start = max(0, pos - left)
    end = min(len(high), pos + right + 1)
    for other in range(start, end):
        if other == pos:
            continue
        if float(high.iloc[other]) >= target:
            return False
    return True

def build_high_sections_markdown(screening: pd.DataFrame, max_rows: int = 5) -> list[str]:
    if screening.empty or "rank" not in screening.columns:
        return []

    candidates = screening[screening["rank"].astype(str).str.upper().isin(["S", "A", "B"])].copy()
    if candidates.empty:
        return []

    if "score" not in candidates.columns:
        candidates["score"] = 0
    candidates["score"] = pd.to_numeric(candidates["score"], errors="coerce").fillna(0)
    if "dist_to_high_pct" not in candidates.columns:
        candidates["dist_to_high_pct"] = 999
    candidates["dist_to_high_pct"] = pd.to_numeric(candidates["dist_to_high_pct"], errors="coerce").fillna(999)
    if "high_type" not in candidates.columns:
        candidates["high_type"] = "OTHER"
    candidates["high_type"] = candidates["high_type"].astype(str)

    rank_order = {"S": 0, "A": 1, "B": 2}
    candidates["_rank_order"] = candidates["rank"].map(rank_order).fillna(9)

    sections = [
        ("【直近高値ブレイク】", _filter_swing_email_candidates(candidates[candidates["high_type"].eq("SWING_HIGH_BREAK")].copy()), "swing"),
        ("【52週高値更新】", candidates[candidates["high_type"].eq("52W_NEW_HIGH")].copy(), "standard"),
        ("【その他】", candidates[~candidates["high_type"].isin(["SWING_HIGH_BREAK", "52W_NEW_HIGH"])].copy(), "standard"),
    ]

    lines: list[str] = []
    for title, group, style in sections:
        lines.append(f"## {title}")
        lines.append("")
        if group.empty:
            lines.append("- 該当なし")
            lines.append("")
            continue
        group = group.sort_values(["_rank_order", "score", "dist_to_high_pct", "code"], ascending=[True, False, True, True])
        if style == "swing":
            lines.append("| コード | 銘柄名 | ランク | スコア | OpenWork | swing高値 | swing日 | 上抜け率 | 出来高比 | 売買代金 | 理由 |")
            lines.append("|---|---|---:|---:|---:|---:|---|---:|---:|---:|---|")
            for _, row in group.head(max_rows).iterrows():
                lines.append(
                    "| {code} | {name} | {rank} | {score} | {openwork} | {swing_price} | {swing_date} | {break_pct} | {vol} | {turnover} | {reason} |".format(
                        code=_text(row, "code"),
                        name=_text(row, "name"),
                        rank=_text(row, "rank"),
                        score=_text(row, "score"),
                        openwork=format_openwork_score(row.get("openwork_score")),
                        swing_price=_text(row, "swing_high_price"),
                        swing_date=_text(row, "swing_high_date"),
                        break_pct=_text(row, "swing_high_break_pct"),
                        vol=_text(row, "volume_ratio_5d_20d"),
                        turnover=_text(row, "turnover_20d"),
                        reason=_text(row, "reason"),
                    )
                )
        else:
            lines.append("| コード | 銘柄名 | ランク | スコア | OpenWork | 高値種別 | 高値日 | 高値まで | 理由 |")
            lines.append("|---|---|---:|---:|---:|---|---|---:|---|")
            for _, row in group.head(max_rows).iterrows():
                lines.append(
                    "| {code} | {name} | {rank} | {score} | {openwork} | {high_label} | {high_date} | {dist} | {reason} |".format(
                        code=_text(row, "code"),
                        name=_text(row, "name"),
                        rank=_text(row, "rank"),
                        score=_text(row, "score"),
                        openwork=format_openwork_score(row.get("openwork_score")),
                        high_label=_text(row, "high_label"),
                        high_date=_text(row, "high_date"),
                        dist=_text(row, "dist_to_high_pct"),
                        reason=_text(row, "reason"),
                    )
                )
        lines.append("")

    return lines


def _filter_swing_email_candidates(group: pd.DataFrame) -> pd.DataFrame:
    if group.empty:
        return group
    required = ["swing_high_break", "volume_ratio_5d_20d", "current_price", "ma25", "turnover_20d"]
    for column in required:
        if column not in group.columns:
            return group.iloc[0:0]
    filtered = group.copy()
    sector = filtered["sector"].astype(str) if "sector" in filtered.columns else pd.Series("", index=filtered.index)
    name = filtered["name"].astype(str) if "name" in filtered.columns else pd.Series("", index=filtered.index)
    etf_reit = sector.str.contains("ETF|REIT|リート|不動産投資", case=False, regex=True) | name.str.contains("ETF|REIT|リート|上場投信|投信", case=False, regex=True)
    return filtered[
        (~etf_reit)
        & (filtered["swing_high_break"].astype(bool))
        & (pd.to_numeric(filtered["volume_ratio_5d_20d"], errors="coerce").fillna(0) >= 1.5)
        & (pd.to_numeric(filtered["current_price"], errors="coerce") > pd.to_numeric(filtered["ma25"], errors="coerce"))
        & (pd.to_numeric(filtered["turnover_20d"], errors="coerce").fillna(0) >= 100_000_000)
    ]


def _text(row: pd.Series, key: str) -> str:
    value = row.get(key, "")
    if pd.isna(value):
        return ""
    return str(value)
