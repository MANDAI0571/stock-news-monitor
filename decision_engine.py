from __future__ import annotations

"""
decision_engine.py — 300万円運用の売買判定エンジン

候補（outputs/screening_result.csv）を BUY / WATCH / SKIP に分類し、
買う理由・見送る理由・損切り目安・利確目安・想定株数・想定保有日数を出す。
過去の候補履歴（data/learning_candidates.csv）を「類似条件の登場回数」として
参照できる形で添える（勝敗ラベルは未整備＝ここでは件数のみ・勝率は捏造しない）。

入力:
  outputs/screening_result.csv          … run_screening.py の当日候補
  data/learning_candidates.csv          … learning_log.py が貯めた履歴（任意）
出力:
  outputs/decision_result.csv           … 1銘柄1行の判定結果
  outputs/decision_report.md            … 人が読むサマリー（BUY/WATCH/SKIP別）

300万円運用ルール（paper_portfolio_discipline.py と一致）:
  資金300万 / 100株単位 / 最大3銘柄 / 1銘柄 原則100株 /
  損切 -7% / 利確 +15% / 保有 2〜10営業日。
  BUYは「100株購入額が資金20%以内(=60万円)」に収まる銘柄のみ。
  高すぎる銘柄は SKIP か WATCH。BUYが無ければ無理に買わず現金。

依存は pandas と標準ライブラリのみ（yfinance を読み込まない＝オフラインでも動く）。
地合い(regime)は任意。main() でだけ market_regime を試し、取れなければ既定で進める。
"""

import argparse
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from jptime import jst_today


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SCREENING = PROJECT_ROOT / "outputs" / "screening_result.csv"
DEFAULT_LEARNING = PROJECT_ROOT / "data" / "learning_candidates.csv"
DEFAULT_OUT_DIR = PROJECT_ROOT / "outputs"

# ── 300万円運用ルール（paper_portfolio_discipline.py のミラー）──────────
CAPITAL = 3_000_000
MAX_POSITIONS = 3
STOP_LOSS_PCT = 0.07
TAKE_PROFIT_PCT = 0.15
HOLD_MIN_BDAYS = 2
HOLD_MAX_BDAYS = 10

# ── 判定しきい値 ───────────────────────────────────────────────
MAX_POSITION_PCT = 0.20                       # 100株購入額が資金の20%以内
AFFORD_CAP = int(CAPITAL * MAX_POSITION_PCT)  # = 600,000 円
NEAR_52W_PCT = 3.0                            # 52週高値から3%以内
VOL_INCREASE_MIN = 1.1                        # 出来高5/20が1.1倍以上で「増加」
EARN_AVOID_BDAYS = 3                          # 決算まで3営業日以内は新規回避
SCORE_REF = 60.0                              # confidence 正規化の基準スコア
LOT = 100                                     # 売買単位

OUTPUT_COLUMNS: List[str] = [
    "code", "name", "decision", "confidence", "score", "rank",
    "screen_type", "screen_tags", "strategy", "high_type", "high_label",
    "current_price", "lot_value_100", "dist_52w_high_pct",
    "dist_25ma_pct", "dist_200ma_pct",
    "volume_ratio_5d_20d", "turnover_20d", "buy_reason",
    "entry_reason", "skip_reason",
    "stop_loss_price", "take_profit_price",
    "position_size", "estimated_holding_days",
]

SHARED_SCHEMA_COLUMNS = [
    "screen_type", "screen_tags", "dist_25ma_pct", "dist_200ma_pct", "buy_reason",
]

SCREEN_TYPE_VALUES = {
    "MULTI",
    "52W_BREAKOUT",
    "52W_MOMENTUM",
    "52W_PULLBACK",
    "25MA_PULLBACK",
    "200MA_TOUCH",
    "WATCH",
    "SKIP",
}
SCREEN_TAG_VALUES = {
    "52W_BREAKOUT",
    "52W_MOMENTUM",
    "52W_PULLBACK",
    "25MA_PULLBACK",
    "200MA_TOUCH",
}
LEGACY_SCREEN_TYPE_TAGS = {
    "52W": ["52W_MOMENTUM"],
    "25MA": ["25MA_PULLBACK"],
    "200MA": ["200MA_TOUCH"],
}

# 入力列名のゆらぎ吸収（run_screening.py の新旧スキーマ両対応）
ALIASES: Dict[str, List[str]] = {
    "code":        ["code", "証券コード", "ticker", "symbol"],
    "name":        ["name", "銘柄名", "銘柄"],
    "score":       ["score", "スコア"],
    "rank":        ["rank", "judge", "grade", "ランク", "判定"],
    "price":       ["current_price", "current", "close", "today_close", "現在値", "終値"],
    "vol_ratio":   ["volume_ratio_5d_20d", "volume_ratio", "vol_ratio", "出来高倍率"],
    # 52週高値基準を最優先にする（dist_to_high_pct は分類窓の高値距離で別物のため後ろ）
    "dist_high":   ["dist_52w_high_pct", "dist_to_52w_high_pct", "dist_to_high_pct",
                    "52週高値差"],
    "lot_value":   ["lot_value_100", "lot_value", "100株購入額"],
    "ma25":        ["ma25", "MA25"],
    "ma75":        ["ma75", "MA75"],
    "ma200":       ["ma200", "MA200"],
    "ma75_gap":    ["ma75_gap_pct"],
    "ma200_gap":   ["ma200_gap_pct"],
    "dist_25ma":   ["dist_25ma_pct", "ma25_gap_pct", "ma25_distance_pct"],
    "dist_200ma":  ["dist_200ma_pct", "ma200_gap_pct", "ma200_distance_pct"],
    "reason":      ["reason", "買い候補理由", "理由"],
    "buy_reason":  ["buy_reason", "買い理由", "買い候補理由", "reason"],
    "earn_bdays":  ["days_to_earnings", "earnings_within_bdays", "days_until_earnings",
                    "決算まで営業日"],
    "earn_date":   ["earnings_date", "next_earnings_date", "決算予定日"],
    "theme":       ["theme", "themes", "テーマ", "theme_top3"],
    "screen_type": ["screen_type"],
    "screen_tags": ["screen_tags"],
    "high_type":   ["high_type"],
    "high_label":  ["high_label"],
    "turnover":    ["turnover_20d", "turnover_20d_avg"],
    "duke_support":["duke_old_high_support", "duke_support_signal"],
    "days_since_52w": ["days_since_52w_high"],
    "retest_52w":  ["retest_52w", "prev_52w_retest"],
    "ma25_touch":  ["ma25_touch"],
    "ma200_touch": ["ma200_touch"],
    "prev_52w_line": ["previous_52w_high_line", "line_deviation_pct", "old_52w_high", "dist_to_old_52w_high_pct"],
    "cwh_signal":  ["cwh_signal"],
}


# ─────────────────────────────────────────
# 取り出しユーティリティ
# ─────────────────────────────────────────
def _norm_row(row: Dict[str, object]) -> Dict[str, str]:
    return {(str(k) or "").strip().lower(): ("" if v is None else str(v)) for k, v in row.items()}


def _pick(nrow: Dict[str, str], key: str) -> str:
    for a in ALIASES.get(key, []):
        v = nrow.get(a.strip().lower())
        if v is not None and str(v).strip() != "" and str(v).strip().lower() != "nan":
            return str(v).strip()
    return ""


def _pick_buy_reason(nrow: Dict[str, str]) -> str:
    if "buy_reason" in nrow:
        value = nrow.get("buy_reason", "")
        text = str(value).strip()
        return "" if text.lower() in {"nan", "none", "<na>", "nat"} else text
    return _pick(nrow, "buy_reason")


def _normalize_rank(value: str) -> str:
    rank = str(value or "").strip().upper()
    mapping = {"S": "S", "A": "A", "B": "B", "C": "C", "SKIP": "SKIP", "見送り": "SKIP"}
    return mapping.get(rank, "SKIP")


def _append_tag(tags: List[str], tag: str) -> None:
    if tag in SCREEN_TAG_VALUES and tag not in tags:
        tags.append(tag)


def _split_screen_tags(value: str) -> List[str]:
    tags: List[str] = []
    for raw in str(value or "").replace("|", ",").replace(";", ",").split(","):
        tag = raw.strip().upper()
        if not tag:
            continue
        if tag in LEGACY_SCREEN_TYPE_TAGS:
            for mapped in LEGACY_SCREEN_TYPE_TAGS[tag]:
                _append_tag(tags, mapped)
        else:
            _append_tag(tags, tag)
    return tags


def _infer_screen_tags(nrow: Dict[str, str]) -> List[str]:
    explicit_tags = _split_screen_tags(_pick(nrow, "screen_tags"))
    if explicit_tags:
        return explicit_tags

    tags: List[str] = []
    explicit = _pick(nrow, "screen_type").upper()
    if explicit in SCREEN_TAG_VALUES:
        _append_tag(tags, explicit)
    elif explicit in LEGACY_SCREEN_TYPE_TAGS:
        for mapped in LEGACY_SCREEN_TYPE_TAGS[explicit]:
            _append_tag(tags, mapped)

    high_type = _pick(nrow, "high_type").upper()
    dist_high = _to_float(_pick(nrow, "dist_high"))
    days_since_52w = _to_float(_pick(nrow, "days_since_52w"))
    if high_type == "52W_NEW_HIGH" or (days_since_52w is not None and days_since_52w <= 0):
        _append_tag(tags, "52W_BREAKOUT")
    elif high_type == "52W_NEAR_HIGH" or (
        dist_high is not None and dist_high <= 3
    ) or (
        days_since_52w is not None and days_since_52w <= 14
    ):
        _append_tag(tags, "52W_MOMENTUM")

    if _any_truthy(nrow, "retest_52w") or _any_truthy(nrow, "duke_support") or _pick(nrow, "prev_52w_line"):
        _append_tag(tags, "52W_PULLBACK")

    dist_25ma = _to_float(_pick(nrow, "dist_25ma"))
    dist_200ma = _to_float(_pick(nrow, "dist_200ma"))
    if _any_truthy(nrow, "ma25_touch") or (dist_25ma is not None and abs(dist_25ma) <= 3):
        _append_tag(tags, "25MA_PULLBACK")
    if _any_truthy(nrow, "ma200_touch") or (dist_200ma is not None and abs(dist_200ma) <= 3):
        _append_tag(tags, "200MA_TOUCH")
    return tags


def _screen_type_from_tags(tags: List[str], rank_u: str, explicit: str = "") -> str:
    tags = [tag for tag in dict.fromkeys(tags) if tag in SCREEN_TAG_VALUES]
    explicit = str(explicit or "").strip().upper()
    if rank_u == "SKIP":
        return "SKIP"
    if len(tags) >= 2:
        return "MULTI"
    if tags:
        return tags[0]
    if explicit in SCREEN_TYPE_VALUES and explicit not in {"MULTI", "SKIP"}:
        return explicit
    return "WATCH"


def _screen_tags_text(tags: List[str], screen_type: str) -> str:
    tags = [tag for tag in dict.fromkeys(tags) if tag in SCREEN_TAG_VALUES]
    return ",".join(tags) if tags else screen_type


def _infer_screen_type(nrow: Dict[str, str], rank_u: str = "") -> str:
    tags = _infer_screen_tags(nrow)
    return _screen_type_from_tags(tags, rank_u, _pick(nrow, "screen_type"))


def _to_float(s: str) -> Optional[float]:
    if s is None:
        return None
    t = str(s).replace(",", "").replace("%", "").replace("円", "").strip()
    if t == "" or t.lower() == "nan":
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _round1(x: Optional[float]) -> object:
    return round(float(x), 1) if isinstance(x, (int, float)) else ""


def _infer_strategy(nrow: Dict[str, str], rank_u: str) -> str:
    high_type = _pick(nrow, "high_type")
    if _any_truthy(nrow, "duke_support"):
        return "duke_old_high_support"
    if high_type == "SWING_HIGH_BREAK":
        return "swing_high_break"
    if high_type == "52W_NEW_HIGH":
        return "52w_new_high"
    if high_type == "52W_NEAR_HIGH":
        return "52w_near_high"
    if high_type == "RECENT_NEW_HIGH":
        return "recent_new_high"
    if high_type == "RECENT_NEAR_HIGH":
        return "recent_near_high"
    if _pick(nrow, "cwh_signal").lower() in {"1", "1.0", "true", "yes"}:
        return "cup_with_handle"
    if rank_u in {"S", "A", "B"}:
        return "rank_candidate"
    return "other"


def _any_truthy(nrow: Dict[str, str], key: str) -> bool:
    for alias in ALIASES.get(key, []):
        value = nrow.get(alias.strip().lower(), "")
        if str(value).strip().lower() in {"1", "1.0", "true", "yes", "y"}:
            return True
    return False


def _max_buys_for_regime(regime: str) -> int:
    """地合い別の新規BUY枠。悪い日は買わない/少なくする。"""
    regime = (regime or "NORMAL").upper()
    if regime in {"STOP", "RISK"}:
        return 0
    if regime == "CAUTION":
        return 1
    return MAX_POSITIONS


def _bdays_until(date_str: str, today: Optional[date] = None) -> Optional[int]:
    d = str(date_str).strip()
    if not d:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d", "%m/%d/%Y"):
        try:
            from datetime import datetime as _dt
            parsed = _dt.strptime(d, fmt).date()
            if fmt == "%m/%d":
                parsed = parsed.replace(year=(today or jst_today()).year)
            base = today or jst_today()
            n = int(np.busday_count(base, parsed))
            return n
        except ValueError:
            continue
    return None


# ─────────────────────────────────────────
# MA より上か（新旧スキーマ両対応）
# ─────────────────────────────────────────
def _above_mas(nrow: Dict[str, str], price: Optional[float]) -> Tuple[Optional[bool], str]:
    checks: List[bool] = []
    labels: List[str] = []
    if price is not None:
        for key, label in (("ma25", "25"), ("ma75", "75"), ("ma200", "200")):
            mv = _to_float(_pick(nrow, key))
            if mv is not None and mv > 0:
                checks.append(price > mv)
                labels.append(label)
    if not checks:  # 数値MAが無ければ gap% で判定
        for key, label in (("ma75_gap", "75"), ("ma200_gap", "200")):
            gv = _to_float(_pick(nrow, key))
            if gv is not None:
                checks.append(gv > 0)
                labels.append(label)
    if not checks:
        return None, "MA判定材料なし"
    ok = all(checks)
    detail = "MA" + "/".join(labels) + ("を上回る" if ok else "を一部下回る")
    return ok, detail


def _earnings_state(nrow: Dict[str, str], today: Optional[date] = None) -> Tuple[str, str]:
    """('ok'|'near'|'unknown', 表示文) を返す。near=決算が近い/当日。"""
    bd = _to_float(_pick(nrow, "earn_bdays"))
    if bd is None:
        bd_from_date = _bdays_until(_pick(nrow, "earn_date"), today)
        bd = float(bd_from_date) if bd_from_date is not None else None
    if bd is None:
        return "unknown", "決算未確認"
    if -1 <= bd <= EARN_AVOID_BDAYS:
        return "near", f"決算が近い（あと{int(bd)}営業日）"
    return "ok", f"決算まで{int(bd)}営業日"


# ─────────────────────────────────────────
# 学習ログ参照（類似条件の登場回数・勝率は捏造しない）
# ─────────────────────────────────────────
def load_learning(path: Path = DEFAULT_LEARNING) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
    except Exception:
        return pd.DataFrame()


def _past_reference(learning: pd.DataFrame, code: str, rank: str) -> str:
    if learning is None or learning.empty or "code" not in learning.columns:
        return ""
    same_code = int((learning["code"].astype(str) == str(code)).sum())
    same_rank = 0
    if "rank" in learning.columns and str(rank):
        same_rank = int((learning["rank"].astype(str).str.upper() == str(rank).upper()).sum())
    parts = []
    if same_code:
        parts.append(f"この銘柄は過去{same_code}回登場")
    if same_rank:
        parts.append(f"同ランク{rank}は過去{same_rank}件")
    return "／".join(parts)


# ─────────────────────────────────────────
# 1銘柄の判定
# ─────────────────────────────────────────
def decide_one(row: Dict[str, object], learning: Optional[pd.DataFrame] = None,
               regime: str = "NORMAL", today: Optional[date] = None) -> Dict[str, object]:
    nrow = _norm_row(row)
    regime = (regime or "NORMAL").upper()

    code = _pick(nrow, "code")
    name = _pick(nrow, "name")
    rank = _normalize_rank(_pick(nrow, "rank"))
    rank_u = rank
    screen_tags = _infer_screen_tags(nrow)
    screen_type = _screen_type_from_tags(screen_tags, rank_u, _pick(nrow, "screen_type"))
    strategy = _infer_strategy(nrow, rank_u)
    score = _to_float(_pick(nrow, "score"))
    price = _to_float(_pick(nrow, "price"))
    vol_ratio = _to_float(_pick(nrow, "vol_ratio"))
    dist_high = _to_float(_pick(nrow, "dist_high"))
    lot_value = _to_float(_pick(nrow, "lot_value"))
    turnover = _to_float(_pick(nrow, "turnover"))
    dist_25ma = _to_float(_pick(nrow, "dist_25ma"))
    dist_200ma = _to_float(_pick(nrow, "dist_200ma"))
    buy_reason = _pick_buy_reason(nrow)
    high_type = _pick(nrow, "high_type")
    high_label = _pick(nrow, "high_label")
    if lot_value is None and price is not None:
        lot_value = price * LOT

    ma_ok, ma_detail = _above_mas(nrow, price)
    earn_state, earn_text = _earnings_state(nrow, today)

    # 各ゲート（None=判定材料なし）
    g_price = price is not None and price > 0
    g_rank_s = rank_u == "S"
    g_afford = (lot_value is not None) and (lot_value <= AFFORD_CAP)
    g_near = (dist_high is not None) and (dist_high <= NEAR_52W_PCT)
    g_vol = (vol_ratio is not None) and (vol_ratio >= VOL_INCREASE_MIN)
    g_ma = ma_ok  # True/False/None
    g_earn = earn_state != "near"  # near のみ弾く（unknown はブロックしないが注意）

    entry: List[str] = []
    skip: List[str] = []

    if g_rank_s:
        entry.append("Sランク")
    elif rank_u == "A":
        entry.append("Aランク")
    else:
        skip.append(f"ランクがSでない（{rank or '不明'}）")

    if score is not None:
        entry.append(f"スコア{score:g}")
    if g_near:
        entry.append(f"52週高値まで{dist_high:g}%（近い）")
    elif dist_high is not None:
        skip.append(f"52週高値から{dist_high:g}%（遠い）")
    if g_vol:
        entry.append(f"出来高{vol_ratio:g}倍（増加）")
    elif vol_ratio is not None:
        skip.append(f"出来高{vol_ratio:g}倍（細り）")
    if g_ma is True:
        entry.append(ma_detail)
    elif g_ma is False:
        skip.append(ma_detail)
    if g_afford and lot_value is not None:
        entry.append(f"100株=¥{int(lot_value):,}（資金20%以内）")
    elif lot_value is not None and not g_afford:
        skip.append(f"100株=¥{int(lot_value):,} が資金20%(¥{AFFORD_CAP:,})超（高すぎ）")
    if earn_state == "near":
        skip.append(earn_text)
    elif earn_state == "unknown":
        skip.append("決算未確認")
    else:
        entry.append(earn_text)

    past = _past_reference(learning, code, rank) if learning is not None else ""
    if past:
        entry.append(past)

    # 地合い
    if regime == "STOP":
        skip.append("地合いSTOP（新規買い停止）")
    elif regime == "RISK":
        skip.append("地合いRISK（新規は見送り）")
    elif regime == "CAUTION":
        entry.append("地合いCAUTION（BUY枠を1銘柄に縮小）")

    # ── 分類 ───────────────────────────────
    buy_ready = (
        g_price and g_rank_s and g_afford and g_near and g_vol
        and (g_ma is True) and g_earn and regime in ("NORMAL", "CAUTION")
    )
    if not g_price:
        decision = "SKIP"
    elif buy_ready:
        decision = "BUY"
    elif rank_u in ("S", "A") and (g_near or g_ma is True) and regime != "STOP":
        # 高値圏で有望だが未達ゲートあり＝様子見
        decision = "WATCH"
    else:
        decision = "SKIP"

    # confidence（ゲート充足率70% + スコア30%）
    gates = [g for g in (g_rank_s, g_afford, g_near, g_vol,
                         (g_ma if g_ma is not None else False), g_earn) ]
    frac = sum(1 for g in gates if g) / len(gates)
    score_norm = min((score or 0) / SCORE_REF, 1.0) if score is not None else 0.0
    confidence = int(round(100 * (0.7 * frac + 0.3 * score_norm)))
    confidence = max(0, min(100, confidence))

    # 目安価格・サイズ・保有日数
    if decision == "SKIP" or price is None:
        stop_px: object = ""
        tp_px: object = ""
        pos_size: object = 0
        hold: object = ""
    else:
        stop_px = _round1(price * (1 - STOP_LOSS_PCT))
        tp_px = _round1(price * (1 + TAKE_PROFIT_PCT))
        pos_size = LOT if decision == "BUY" else 0
        hold = HOLD_MAX_BDAYS

    return {
        "code": code,
        "name": name,
        "decision": decision,
        "confidence": confidence,
        "score": (f"{score:g}" if score is not None else ""),
        "rank": rank,
        "screen_type": screen_type,
        "screen_tags": _screen_tags_text(screen_tags, screen_type),
        "strategy": strategy,
        "high_type": high_type,
        "high_label": high_label,
        "current_price": _round1(price),
        "lot_value_100": int(lot_value) if lot_value is not None else "",
        "dist_52w_high_pct": (round(float(dist_high), 2) if dist_high is not None else ""),
        "dist_25ma_pct": (round(float(dist_25ma), 2) if dist_25ma is not None else ""),
        "dist_200ma_pct": (round(float(dist_200ma), 2) if dist_200ma is not None else ""),
        "volume_ratio_5d_20d": (round(float(vol_ratio), 2) if vol_ratio is not None else ""),
        "turnover_20d": int(turnover) if turnover is not None else "",
        "buy_reason": buy_reason,
        "entry_reason": " / ".join(entry) if decision in ("BUY", "WATCH") else "",
        "skip_reason": " / ".join(skip) if decision in ("WATCH", "SKIP") else "",
        "stop_loss_price": stop_px,
        "take_profit_price": tp_px,
        "position_size": pos_size,
        "estimated_holding_days": hold,
        # 内部ソート用（出力CSVには残すが判定に使う）
        "_score_sort": score if score is not None else -1,
        "_dist_sort": dist_high if dist_high is not None else 1e9,
    }


# ─────────────────────────────────────────
# 全体（枠上限3銘柄の適用込み）
# ─────────────────────────────────────────
def build_decisions(screening: pd.DataFrame,
                    learning: Optional[pd.DataFrame] = None,
                    regime: str = "NORMAL",
                    today: Optional[date] = None) -> pd.DataFrame:
    if screening is None or screening.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    results = [decide_one(rec, learning=learning, regime=regime, today=today)
               for rec in screening.to_dict("records")]

    # BUY を score降順・高値まで近い順で並べ、地合い別の枠上限を適用。
    buys = [r for r in results if r["decision"] == "BUY"]
    buys.sort(key=lambda r: (-r["_score_sort"], r["_dist_sort"]))
    max_buys = _max_buys_for_regime(regime)
    for i, r in enumerate(buys):
        if i >= max_buys:
            r["decision"] = "WATCH"
            r["position_size"] = 0
            extra = f"地合い{regime}の枠上限（最大{max_buys}銘柄）で見送り"
            r["skip_reason"] = (r["skip_reason"] + " / " + extra).strip(" /") if r["skip_reason"] else extra

    df = pd.DataFrame(results, columns=OUTPUT_COLUMNS + ["_score_sort", "_dist_sort"])
    # 表示順: BUY→WATCH→SKIP、その中は confidence 降順
    order = {"BUY": 0, "WATCH": 1, "SKIP": 2}
    df["_ord"] = df["decision"].map(order).fillna(3)
    df = df.sort_values(["_ord", "confidence"], ascending=[True, False])
    return df[OUTPUT_COLUMNS].reset_index(drop=True)


def _consistency_value(value: object) -> str:
    text = str(value if value is not None else "").strip()
    if text.lower() in {"nan", "none", "<na>", "nat"}:
        return ""
    number = _to_float(text)
    if number is not None:
        return f"{number:.2f}"
    return text


def validate_decision_consistency(screening: pd.DataFrame, decisions: pd.DataFrame) -> None:
    """screening_result.csv と decision_result.csv の行・中核列の対応を保証する。"""
    missing = [col for col in SHARED_SCHEMA_COLUMNS if col not in decisions.columns]
    if missing:
        raise ValueError(f"decision_result.csv missing required columns: {', '.join(missing)}")
    if screening is None:
        screening = pd.DataFrame()
    if len(screening) != len(decisions):
        raise ValueError(f"decision_result row mismatch: screening={len(screening)} decision={len(decisions)}")
    if "code" in screening.columns and "code" in decisions.columns:
        left = set(screening["code"].astype(str))
        right = set(decisions["code"].astype(str))
        if left != right:
            missing_decisions = sorted(left - right)[:10]
            extra_decisions = sorted(right - left)[:10]
            raise ValueError(
                "decision_result code mismatch: "
                f"missing={missing_decisions} extra={extra_decisions}"
            )
        for col in SHARED_SCHEMA_COLUMNS:
            if col not in screening.columns:
                continue
            if col == "screen_type":
                source_types = screening[col].astype(str).str.upper()
                known = source_types.isin(SCREEN_TYPE_VALUES) | source_types.str.strip().eq("")
                if not bool(known.all()):
                    continue
            left_df = screening[["code", col]].copy()
            right_df = decisions[["code", col]].copy()
            left_df["code"] = left_df["code"].astype(str)
            right_df["code"] = right_df["code"].astype(str)
            left_map = left_df.drop_duplicates("code", keep="last").set_index("code")[col].to_dict()
            right_map = right_df.drop_duplicates("code", keep="last").set_index("code")[col].to_dict()
            mismatches = [
                code for code in sorted(left & right)
                if _consistency_value(left_map.get(code)) != _consistency_value(right_map.get(code))
            ]
            if mismatches:
                sample = mismatches[:5]
                raise ValueError(f"decision_result shared column mismatch: {col} codes={sample}")


# ─────────────────────────────────────────
# レポート
# ─────────────────────────────────────────
def build_report(decisions: pd.DataFrame, regime: str = "NORMAL",
                 today: Optional[date] = None) -> str:
    today = today or jst_today()
    n_buy = int((decisions["decision"] == "BUY").sum()) if not decisions.empty else 0
    n_watch = int((decisions["decision"] == "WATCH").sum()) if not decisions.empty else 0
    n_skip = int((decisions["decision"] == "SKIP").sum()) if not decisions.empty else 0

    lines: List[str] = []
    lines.append(f"# 300万円運用 売買判定レポート（{today.isoformat()}）")
    lines.append("")
    lines.append(f"- 地合い: **{regime}**")
    lines.append(f"- 判定: BUY {n_buy} / WATCH {n_watch} / SKIP {n_skip}")
    lines.append(f"- ルール: 資金¥{CAPITAL:,} ・最大{MAX_POSITIONS}銘柄・1銘柄{LOT}株・"
                 f"損切-{int(STOP_LOSS_PCT*100)}%・利確+{int(TAKE_PROFIT_PCT*100)}%・"
                 f"保有{HOLD_MIN_BDAYS}〜{HOLD_MAX_BDAYS}営業日")
    lines.append(f"- BUY条件: Sランク／100株が資金20%(¥{AFFORD_CAP:,})以内／52週高値{NEAR_52W_PCT:g}%以内／"
                 f"出来高{VOL_INCREASE_MIN:g}倍以上／MA25・75・200を上回る／決算{EARN_AVOID_BDAYS}営業日以内を回避")
    lines.append("")
    if n_buy == 0:
        lines.append("> BUY該当なし＝無理に買わず**現金**で待機（全額投入しない方針）。")
        lines.append("")

    def _section(title: str, key: str, cols_extra: str) -> None:
        sub = decisions[decisions["decision"] == key]
        lines.append(f"## {title}（{len(sub)}件）")
        if sub.empty:
            lines.append("")
            lines.append("該当なし。")
            lines.append("")
            return
        lines.append("")
        for _, r in sub.iterrows():
            head = f"### {r['code']} {r['name']}｜confidence {r['confidence']}｜{r['rank']}"
            lines.append(head)
            if key in ("BUY", "WATCH"):
                lines.append(f"- 買う理由: {r['entry_reason'] or '—'}")
            if key in ("WATCH", "SKIP"):
                lines.append(f"- 見送り理由: {r['skip_reason'] or '—'}")
            if key in ("BUY", "WATCH"):
                lines.append(f"- {cols_extra.format(**r.to_dict())}")
            lines.append("")

    _section("BUY（買い候補）", "BUY",
             "損切 ¥{stop_loss_price} ／ 利確 ¥{take_profit_price} ／ "
             "{position_size}株 ／ 想定保有 〜{estimated_holding_days}営業日")
    _section("WATCH（様子見）", "WATCH",
             "参考: 損切 ¥{stop_loss_price} ／ 利確 ¥{take_profit_price}")
    # SKIP は理由のみ簡潔に
    sub = decisions[decisions["decision"] == "SKIP"]
    lines.append(f"## SKIP（見送り）（{len(sub)}件）")
    lines.append("")
    if sub.empty:
        lines.append("該当なし。")
    else:
        for _, r in sub.iterrows():
            lines.append(f"- {r['code']} {r['name']}: {r['skip_reason'] or '—'}")
    lines.append("")
    lines.append("---")
    lines.append("最終判断は地合いと出来高を見て高重さんが行ってください（本レポートは推奨ではありません）。")
    lines.append("")
    return "\n".join(lines)


def write_outputs(decisions: pd.DataFrame, out_dir: Path = DEFAULT_OUT_DIR,
                  regime: str = "NORMAL", today: Optional[date] = None) -> Tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "decision_result.csv"
    md_path = out_dir / "decision_report.md"
    decisions.to_csv(csv_path, index=False, encoding="utf-8-sig")
    md_path.write_text(build_report(decisions, regime=regime, today=today), encoding="utf-8")
    return csv_path, md_path


def run(screening_path: Path = DEFAULT_SCREENING,
        learning_path: Path = DEFAULT_LEARNING,
        out_dir: Path = DEFAULT_OUT_DIR,
        regime: str = "NORMAL",
        today: Optional[date] = None) -> Dict[str, object]:
    screening_path = Path(screening_path)
    if not screening_path.exists():
        empty = pd.DataFrame(columns=OUTPUT_COLUMNS)
        csv_path, md_path = write_outputs(empty, out_dir, regime=regime, today=today)
        return {"input_exists": False, "rows": 0, "buy": 0, "watch": 0, "skip": 0,
                "csv": str(csv_path), "md": str(md_path)}
    screening = pd.read_csv(screening_path, dtype=str).fillna("")
    learning = load_learning(learning_path)
    decisions = build_decisions(screening, learning=learning, regime=regime, today=today)
    validate_decision_consistency(screening, decisions)
    csv_path, md_path = write_outputs(decisions, out_dir, regime=regime, today=today)
    return {
        "input_exists": True,
        "rows": int(len(decisions)),
        "buy": int((decisions["decision"] == "BUY").sum()),
        "watch": int((decisions["decision"] == "WATCH").sum()),
        "skip": int((decisions["decision"] == "SKIP").sum()),
        "csv": str(csv_path),
        "md": str(md_path),
    }


def _resolve_regime(cli_regime: Optional[str]) -> str:
    if cli_regime:
        return cli_regime.upper()
    # 任意: market_regime があれば使う。無ければ NORMAL（新規を止めない）。
    try:
        from market_regime import fetch_regime
        return fetch_regime().value
    except Exception:
        return "NORMAL"


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="300万円運用の売買判定エンジン")
    p.add_argument("--input", default=str(DEFAULT_SCREENING))
    p.add_argument("--learning", default=str(DEFAULT_LEARNING))
    p.add_argument("--output-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--regime", default=None, help="NORMAL/CAUTION/RISK/STOP（省略時は自動）")
    args = p.parse_args(argv)

    regime = _resolve_regime(args.regime)
    res = run(Path(args.input), Path(args.learning), Path(args.output_dir), regime=regime)
    if not res["input_exists"]:
        print(f"⚠️  入力が見つかりません: {args.input}（空の結果を出力・捏造しない）")
    print(f"地合い={regime}｜BUY {res['buy']} / WATCH {res['watch']} / SKIP {res['skip']}")
    print(f"→ {res['csv']}")
    print(f"→ {res['md']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
