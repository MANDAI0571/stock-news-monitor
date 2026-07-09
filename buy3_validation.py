from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from decision_engine import build_decisions
from run_screening import (
    CAPITAL,
    _is_rank_excluded_security,
    _normalize_screening_schema,
    format_cwh,
    format_indicators,
)
from scanner.highs import (
    classify_high_profile,
    detect_duke_old_high_support,
)
from scanner.indicators import calculate_indicators, passes_base_filters
from scanner.patterns import detect_cup_with_handle
from scanner.prices import fetch_price_history
from scanner.scoring import rejection_row, score_stock
from scanner.universe import UniverseConfig, load_jpx_listed


PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "outputs"
DETAIL_CSV = OUTPUT_DIR / "buy3_validation_detail.csv"
SUMMARY_CSV = OUTPUT_DIR / "buy3_validation_summary.csv"
FALSE_POSITIVE_CSV = OUTPUT_DIR / "buy3_false_positive.csv"
MISSED_CSV = OUTPUT_DIR / "buy3_missed_opportunity.csv"
REPORT_MD = OUTPUT_DIR / "buy3_validation_report.md"
MANIFEST_JSON = OUTPUT_DIR / "buy3_validation_manifest.json"

FORWARD_DAYS = (1, 3, 5, 10)
SCREEN_TYPES = [
    "MULTI",
    "52W_BREAKOUT",
    "52W_MOMENTUM",
    "52W_PULLBACK",
    "25MA_PULLBACK",
    "200MA_TOUCH",
    "WATCH",
    "SKIP",
]


@dataclass(frozen=True)
class ValidationConfig:
    days: int
    max_symbols: int | None
    period: str
    top_skip_per_day: int
    markets: tuple[str, ...]
    slippage_roundtrip_pct: float
    min_history_days: int = 260
    forward_max_days: int = 10


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).replace(",", "").replace("%", "").replace("円", "").strip()
    if not text or text.lower() in {"nan", "none", "<na>", "nat"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _safe_round(value: object, digits: int = 4) -> object:
    number = _to_float(value)
    if number is None or not math.isfinite(number):
        return ""
    return round(number, digits)


def _load_universe(config: ValidationConfig) -> pd.DataFrame:
    universe = load_jpx_listed(UniverseConfig(markets=config.markets))
    if universe.empty:
        return universe
    mask = ~universe.apply(
        lambda row: _is_rank_excluded_security(
            str(row.get("name", "")),
            str(row.get("market", "")),
            str(row.get("sector", "")),
        ),
        axis=1,
    )
    universe = universe[mask].drop_duplicates("ticker").reset_index(drop=True)
    preferred = _preferred_tickers_from_screening_outputs()
    if preferred:
        universe["_preferred_order"] = universe["ticker"].map({ticker: idx for idx, ticker in enumerate(preferred)})
        preferred_part = universe[universe["_preferred_order"].notna()].sort_values("_preferred_order")
        rest = universe[universe["_preferred_order"].isna()].sort_values("ticker")
        universe = pd.concat([preferred_part, rest], ignore_index=True).drop(columns=["_preferred_order"])
    if config.max_symbols:
        universe = universe.head(config.max_symbols).reset_index(drop=True)
    return universe


def _preferred_tickers_from_screening_outputs(limit: int = 500) -> list[str]:
    paths = sorted((PROJECT_ROOT / "outputs").glob("screening_result*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    scored: dict[str, tuple[int, float, float]] = {}
    rank_order = {"S": 0, "A": 1, "B": 2, "C": 3, "SKIP": 4}
    for path in paths[:80]:
        try:
            df = pd.read_csv(path, dtype=str).fillna("")
        except Exception:
            continue
        if "ticker" not in df.columns and "code" not in df.columns:
            continue
        for _, row in df.iterrows():
            ticker = str(row.get("ticker") or "").strip()
            code = str(row.get("code") or "").strip()
            if not ticker and code.isdigit():
                ticker = f"{code}.T"
            if not ticker:
                continue
            rank = str(row.get("rank") or "SKIP").strip().upper()
            order = rank_order.get(rank, 4)
            score = _to_float(row.get("score")) or 0.0
            dist = _to_float(row.get("dist_52w_high_pct")) or 999.0
            current = scored.get(ticker)
            candidate = (order, -score, dist)
            if current is None or candidate < current:
                scored[ticker] = candidate
    return [
        ticker for ticker, _ in sorted(scored.items(), key=lambda item: item[1])
    ][:limit]


def _validation_earnings() -> dict[str, object]:
    # 過去時点の正確な決算予定表がこのリポジトリに無いため、未来の予定表は使わない。
    # score_stock が「未確認なら最大A」に落とすため、検証では価格ロジックだけを測る。
    return {
        "earnings_status": "検証対象外",
        "earnings_date": "",
        "exclude_for_earnings": False,
        "earnings_note": "過去時点の決算予定データなし（未来情報回避のため検証対象外）",
    }


def _regime_for_date(market: pd.DataFrame | None, asof: pd.Timestamp) -> str:
    if market is None or market.empty or asof not in market.index:
        return "NORMAL"
    row = market.loc[asof]
    vix = _to_float(row.get("vix_close"))
    nikkei_gap = _to_float(row.get("nikkei_ma25_gap_pct"))
    topix_gap = _to_float(row.get("topix_ma25_gap_pct"))
    if vix is not None and vix >= 35:
        return "STOP"
    if vix is not None and vix >= 28:
        return "RISK"
    if (nikkei_gap is not None and nikkei_gap <= -3) or (topix_gap is not None and topix_gap <= -3):
        return "RISK"
    if (vix is not None and vix >= 22) or (nikkei_gap is not None and nikkei_gap < 0) or (topix_gap is not None and topix_gap < 0):
        return "CAUTION"
    return "NORMAL"


def _download_market_history(period: str) -> pd.DataFrame:
    try:
        import yfinance as yf
    except Exception:
        return pd.DataFrame()
    try:
        raw = yf.download(
            ["^N225", "^TOPX", "^VIX"],
            period=period,
            interval="1d",
            auto_adjust=True,
            progress=False,
            group_by="ticker",
            threads=False,
            timeout=20,
        )
    except Exception:
        return pd.DataFrame()
    if raw is None or raw.empty or not isinstance(raw.columns, pd.MultiIndex):
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    for symbol, prefix in (("^N225", "nikkei"), ("^TOPX", "topix"), ("^VIX", "vix")):
        if symbol not in raw.columns.get_level_values(0):
            continue
        sub = raw[symbol].copy()
        if "Close" not in sub.columns:
            continue
        tmp = pd.DataFrame(index=pd.to_datetime(sub.index))
        tmp[f"{prefix}_close"] = pd.to_numeric(sub["Close"], errors="coerce")
        tmp[f"{prefix}_ma25_gap_pct"] = (tmp[f"{prefix}_close"] - tmp[f"{prefix}_close"].rolling(25).mean()) / tmp[f"{prefix}_close"].rolling(25).mean() * 100
        frames.append(tmp)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, axis=1).dropna(how="all")
    out.index = pd.to_datetime(out.index).normalize()
    return out


def _build_screening_row(stock: pd.Series, history: pd.DataFrame, asof_pos: int) -> dict[str, object]:
    asof_history = history.iloc[: asof_pos + 1].copy()
    base = {
        "code": str(stock["code"]),
        "ticker": str(stock["ticker"]),
        "name": str(stock["name"]),
        "market": str(stock["market"]),
        "sector": str(stock.get("sector", "")),
    }
    indicators = calculate_indicators(asof_history)
    if indicators is None:
        return base | rejection_row(None, "価格データ不足")

    high_info = classify_high_profile(asof_history)
    duke_support = detect_duke_old_high_support(asof_history, indicators)
    passed, reject_reasons = passes_base_filters(indicators)
    if not passed:
        return base | format_indicators(indicators) | high_info | duke_support | rejection_row(indicators, " / ".join(reject_reasons))

    earnings = _validation_earnings()
    cwh = detect_cup_with_handle(asof_history["Close"])
    scored = score_stock(
        indicators,
        cwh,
        earnings,
        capital=CAPITAL,
        name=str(stock["name"]),
        sector=str(stock.get("sector", "")),
        strict=False,
        duke_support=duke_support,
    )
    return base | format_indicators(indicators) | high_info | duke_support | format_cwh(cwh) | earnings | scored


def _prepare_histories(universe: pd.DataFrame, period: str) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    histories: dict[str, pd.DataFrame] = {}
    failures: dict[str, str] = {}
    total = len(universe)
    for idx, row in enumerate(universe.itertuples(index=False), start=1):
        ticker = str(row.ticker)
        if idx == 1 or idx % 25 == 0 or idx == total:
            print(f"[buy3-validation] price {idx}/{total} {ticker}", flush=True)
        try:
            hist = fetch_price_history(ticker, period=period)
            if hist is None or hist.empty:
                failures[ticker] = "price-data-missing"
                continue
            hist = hist.sort_index().copy()
            hist.index = pd.to_datetime(hist.index).normalize()
            histories[ticker] = hist
        except Exception as exc:  # noqa: BLE001 - 欠損理由を明示して検証を続ける
            failures[ticker] = f"price-fetch-error: {exc}"
    return histories, failures


def _evaluation_positions(histories: dict[str, pd.DataFrame], config: ValidationConfig) -> list[pd.Timestamp]:
    all_dates: set[pd.Timestamp] = set()
    for hist in histories.values():
        if len(hist) < config.min_history_days + config.forward_max_days + 2:
            continue
        usable = hist.index[config.min_history_days : len(hist) - config.forward_max_days - 1]
        all_dates.update(pd.Timestamp(d).normalize() for d in usable)
    return sorted(all_dates)[-config.days :]


def _outcome_for_row(
    decision_row: pd.Series,
    history: pd.DataFrame,
    asof_date: pd.Timestamp,
    config: ValidationConfig,
) -> dict[str, object] | None:
    if asof_date not in history.index:
        return None
    asof_pos = int(history.index.get_loc(asof_date))
    entry_pos = asof_pos + 1
    if entry_pos + config.forward_max_days - 1 >= len(history):
        return None
    entry_date = pd.Timestamp(history.index[entry_pos]).date().isoformat()
    entry_open = float(history["Open"].iloc[entry_pos])
    if not math.isfinite(entry_open) or entry_open <= 0:
        return None
    out: dict[str, object] = {
        "asof_date": pd.Timestamp(asof_date).date().isoformat(),
        "entry_date": entry_date,
        "entry_open": round(entry_open, 2),
        "code": str(decision_row.get("code", "")),
        "ticker": str(decision_row.get("ticker", "")),
        "name": str(decision_row.get("name", "")),
        "decision": str(decision_row.get("decision", "")),
        "rank": str(decision_row.get("rank", "")),
        "score": _safe_round(decision_row.get("score"), 2),
        "confidence": _safe_round(decision_row.get("confidence"), 2),
        "screen_type": str(decision_row.get("screen_type", "")),
        "screen_tags": str(decision_row.get("screen_tags", "")),
        "regime": str(decision_row.get("_regime", "NORMAL")),
        "buy_reason": str(decision_row.get("buy_reason", "")),
        "entry_reason": str(decision_row.get("entry_reason", "")),
        "skip_reason": str(decision_row.get("skip_reason", "")),
        "volume_ratio_5d_20d": _safe_round(decision_row.get("volume_ratio_5d_20d"), 4),
        "dist_52w_high_pct": _safe_round(decision_row.get("dist_52w_high_pct"), 4),
        "dist_25ma_pct": _safe_round(decision_row.get("dist_25ma_pct"), 4),
        "dist_200ma_pct": _safe_round(decision_row.get("dist_200ma_pct"), 4),
    }

    for days in FORWARD_DAYS:
        pos = entry_pos + days - 1
        close = float(history["Close"].iloc[pos])
        gross = (close / entry_open - 1) * 100
        out[f"return_{days}d_pct"] = round(gross, 4)
        out[f"return_{days}d_net_pct"] = round(gross - config.slippage_roundtrip_pct, 4)
        out[f"close_{days}d"] = round(close, 2)

    window = history.iloc[entry_pos : entry_pos + config.forward_max_days].copy()
    highs = window["High"].astype(float)
    lows = window["Low"].astype(float)
    max_up = (float(highs.max()) / entry_open - 1) * 100
    max_down = (float(lows.min()) / entry_open - 1) * 100
    out["max_up_10d_pct"] = round(max_up, 4)
    out["max_down_10d_pct"] = round(max_down, 4)
    out["max_drawdown_like_pct"] = round(max_down, 4)
    for threshold in (3, 5):
        plus_key = f"hit_plus_{threshold}_first"
        minus_key = f"hit_minus_{threshold}_first"
        out[plus_key], out[minus_key] = _first_barrier_hit(window, entry_open, threshold)
    ret5 = _to_float(out.get("return_5d_pct")) or 0.0
    if ret5 > 0:
        out["result_5d"] = "WIN"
    elif ret5 < 0:
        out["result_5d"] = "LOSS"
    else:
        out["result_5d"] = "DRAW"
    return out


def _first_barrier_hit(window: pd.DataFrame, entry_open: float, threshold: int) -> tuple[str, str]:
    plus_level = entry_open * (1 + threshold / 100)
    minus_level = entry_open * (1 - threshold / 100)
    plus_date = ""
    minus_date = ""
    for idx, row in window.iterrows():
        hit_plus = float(row["High"]) >= plus_level
        hit_minus = float(row["Low"]) <= minus_level
        date_text = pd.Timestamp(idx).date().isoformat()
        if hit_plus and not plus_date:
            plus_date = date_text
        if hit_minus and not minus_date:
            minus_date = date_text
        if plus_date or minus_date:
            break
    return plus_date, minus_date


def _select_rows_for_validation(decisions: pd.DataFrame, top_skip_per_day: int) -> pd.DataFrame:
    if decisions.empty:
        return decisions
    keep = decisions[decisions["decision"].isin(["BUY", "WATCH"])].copy()
    skip = decisions[decisions["decision"].eq("SKIP")].copy()
    if not skip.empty and top_skip_per_day > 0:
        skip = skip.sort_values(["confidence", "score"], ascending=[False, False]).head(top_skip_per_day)
        keep = pd.concat([keep, skip], ignore_index=True)
    return keep.reset_index(drop=True)


def _summarize_group(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    if df.empty:
        return pd.DataFrame()
    grouped = df.groupby(keys, dropna=False)
    for group_key, sub in grouped:
        key_values = group_key if isinstance(group_key, tuple) else (group_key,)
        row = {key: value for key, value in zip(keys, key_values)}
        row.update(_metrics(sub))
        rows.append(row)
    return pd.DataFrame(rows)


def _metrics(sub: pd.DataFrame) -> dict[str, object]:
    ret5 = pd.to_numeric(sub.get("return_5d_pct"), errors="coerce").dropna()
    ret10 = pd.to_numeric(sub.get("return_10d_pct"), errors="coerce").dropna()
    max_up = pd.to_numeric(sub.get("max_up_10d_pct"), errors="coerce").dropna()
    max_down = pd.to_numeric(sub.get("max_down_10d_pct"), errors="coerce").dropna()
    gains = ret5[ret5 > 0].sum()
    losses = ret5[ret5 < 0].sum()
    return {
        "count": int(len(sub)),
        "win_rate_5d_pct": round(float((ret5 > 0).mean() * 100), 2) if len(ret5) else "",
        "avg_return_5d_pct": round(float(ret5.mean()), 4) if len(ret5) else "",
        "median_return_5d_pct": round(float(ret5.median()), 4) if len(ret5) else "",
        "win_rate_10d_pct": round(float((ret10 > 0).mean() * 100), 2) if len(ret10) else "",
        "avg_return_10d_pct": round(float(ret10.mean()), 4) if len(ret10) else "",
        "median_return_10d_pct": round(float(ret10.median()), 4) if len(ret10) else "",
        "max_profit_pct": round(float(ret5.max()), 4) if len(ret5) else "",
        "max_loss_pct": round(float(ret5.min()), 4) if len(ret5) else "",
        "avg_max_up_10d_pct": round(float(max_up.mean()), 4) if len(max_up) else "",
        "avg_max_down_10d_pct": round(float(max_down.mean()), 4) if len(max_down) else "",
        "profit_factor_like": round(float(gains / abs(losses)), 4) if losses < 0 else "",
        "max_drawdown_like_pct": round(float(max_down.min()), 4) if len(max_down) else "",
    }


def _write_summary(detail: pd.DataFrame) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for label, keys in (
        ("decision", ["decision"]),
        ("screen_type", ["screen_type"]),
        ("rank", ["rank"]),
        ("regime", ["regime"]),
        ("decision_screen_type", ["decision", "screen_type"]),
        ("screen_type_regime", ["screen_type", "regime"]),
    ):
        part = _summarize_group(detail, keys)
        if part.empty:
            continue
        part.insert(0, "group", label)
        frames.append(part)
    summary = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
    summary.to_csv(SUMMARY_CSV, index=False, encoding="utf-8-sig")
    return summary


def _false_positive(detail: pd.DataFrame) -> pd.DataFrame:
    if detail.empty:
        return pd.DataFrame()
    ret5 = pd.to_numeric(detail["return_5d_pct"], errors="coerce")
    draw = pd.to_numeric(detail["max_down_10d_pct"], errors="coerce")
    out = detail[
        detail["decision"].eq("BUY")
        & ((ret5 <= -3) | (draw <= -5))
    ].copy()
    if out.empty:
        return out
    out["error_type"] = np.where(ret5.loc[out.index] <= -3, "BUY_5D_MINUS_3", "BUY_MAX_DOWN_MINUS_5")
    return out.sort_values(["return_5d_pct", "max_down_10d_pct"], ascending=[True, True])


def _missed_opportunity(detail: pd.DataFrame) -> pd.DataFrame:
    if detail.empty:
        return pd.DataFrame()
    max_up = pd.to_numeric(detail["max_up_10d_pct"], errors="coerce")
    ret10 = pd.to_numeric(detail["return_10d_pct"], errors="coerce")
    watch = detail[detail["decision"].eq("WATCH") & (max_up >= 5)].copy()
    watch["miss_type"] = "WATCH_5PCT_UP_WITHIN_10D"
    skip = detail[detail["decision"].eq("SKIP") & (ret10 >= 10)].copy()
    skip["miss_type"] = "SKIP_10D_PLUS_10"
    out = pd.concat([watch, skip], ignore_index=True, sort=False)
    if out.empty:
        return out
    return out.sort_values(["max_up_10d_pct", "return_10d_pct"], ascending=[False, False])


def _write_report(
    detail: pd.DataFrame,
    summary: pd.DataFrame,
    false_positive: pd.DataFrame,
    missed: pd.DataFrame,
    manifest: dict[str, object],
) -> None:
    buy = detail[detail["decision"].eq("BUY")] if not detail.empty else pd.DataFrame()
    lines: list[str] = []
    lines.append("# BUY3精度検証レポート")
    lines.append("")
    lines.append(f"- 生成日時: {manifest['generated_at']}")
    lines.append(f"- 検証期間: {manifest.get('start_date')} 〜 {manifest.get('end_date')}")
    lines.append(f"- 検証銘柄数: {manifest.get('symbols_loaded')} / 取得失敗: {manifest.get('price_fetch_failures')}")
    lines.append(f"- 詳細行: {len(detail)} / BUY: {int((detail['decision'] == 'BUY').sum()) if not detail.empty else 0} / WATCH: {int((detail['decision'] == 'WATCH').sum()) if not detail.empty else 0} / SKIP: {int((detail['decision'] == 'SKIP').sum()) if not detail.empty else 0}")
    lines.append("- 決算予定ゲート: 過去時点の正確な予定表が無いため、未来情報回避のため今回の成績検証から除外")
    lines.append("- 約定仮定: 判定日の翌営業日寄り付きで100株、手数料なし版と往復スリッページ控除版を併記")
    lines.append(f"- 異常値要確認: {manifest.get('anomaly_rows', 0)}件（短期リターン±80%超または最大上昇100%超）")
    lines.append(f"- CASH判断日: {manifest.get('cash_days', 0)}日 / CASH日に大幅上昇候補あり: {manifest.get('cash_days_with_opportunity', 0)}日")
    lines.append("")
    lines.append("## 現行ロジック要約")
    lines.append("")
    lines.extend([
        "- Sランク: score>=85 かつ 52週高値3%以内、MA25/75/200上、MA25/75上向き、出来高が20日平均超、20日平均売買代金1億円以上。未達なら最大A。",
        "- BUY判定: Sランク、100株購入額60万円以内、52週高値3%以内、出来高5日/20日が1.1倍以上、MA25/75/200上、地合いNORMAL/CAUTION。",
        "- WATCH判定: S/Aで高値接近またはMA条件があり、BUY必須条件の一部が未達。STOP時はWATCH化しない。",
        "- 地合い制御: NORMALは最大3、CAUTIONは最大1、RISK/STOPはBUYゼロ。",
        "- 出来高条件: base filterは売買代金20日平均1億円以上。BUYは出来高5日/20日が1.1倍以上。",
        "- 52週高値距離: scoringは3/7/15%以内で加点。BUYは3%以内。",
        "- MA条件: base filterはMA25/75/200上。SゲートはMA25/75上向き。押し目分類は25MA/200MAから3%以内。",
        "- 決算除外: 本番は決算14営業日前〜翌営業日を除外し、決算未確認は最大A。",
        "- 資金管理: 300万円、100株単位、1銘柄60万円以内、最大3銘柄。",
        "- BUY3未満: 無理に埋めず、残りはCASH。",
    ])
    lines.append("")
    lines.append("## 主要成績")
    lines.append("")
    if not summary.empty:
        view = _display_df(summary[summary["group"].eq("decision")])
        lines.append(_markdown_table(view))
    else:
        lines.append("集計対象なし。")
    lines.append("")
    lines.append("## screen_type別成績")
    lines.append("")
    if not summary.empty:
        view = _display_df(summary[summary["group"].eq("screen_type")])
        lines.append(_markdown_table(view))
    else:
        lines.append("集計対象なし。")
    lines.append("")
    lines.append("## 地合い別成績")
    lines.append("")
    if not summary.empty:
        view = _display_df(summary[summary["group"].eq("regime")])
        lines.append(_markdown_table(view))
    else:
        lines.append("集計対象なし。")
    lines.append("")
    lines.append("## 誤判定上位10件")
    lines.append("")
    lines.append(_top_table(false_positive, ["asof_date", "code", "name", "return_5d_pct", "max_down_10d_pct", "screen_type", "rank", "score", "skip_reason"]))
    lines.append("")
    lines.append("## 見逃し上位10件")
    lines.append("")
    lines.append(_top_table(missed, ["miss_type", "asof_date", "code", "name", "decision", "return_10d_pct", "max_up_10d_pct", "screen_type", "rank", "score", "skip_reason"]))
    lines.append("")
    lines.append("## 現行ロジックの弱点候補")
    lines.append("")
    lines.extend(_weakness_lines(detail, false_positive, missed))
    lines.append("")
    lines.append("## 改善案（未実装）")
    lines.append("")
    lines.extend(_proposal_lines(detail, false_positive, missed))
    lines.append("")
    lines.append("## 出力ファイル")
    lines.append("")
    for path in (DETAIL_CSV, SUMMARY_CSV, FALSE_POSITIVE_CSV, MISSED_CSV, MANIFEST_JSON):
        lines.append(f"- {path}")
    lines.append("")
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


def _top_table(df: pd.DataFrame, columns: list[str]) -> str:
    if df.empty:
        return "該当なし。"
    existing = [c for c in columns if c in df.columns]
    return _markdown_table(_display_df(df.head(10)[existing]))


def _markdown_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "該当なし。"
    text_df = _display_df(df)
    columns = [str(c) for c in text_df.columns]
    lines = [
        "| " + " | ".join(columns) + " |",
        "|" + "|".join(["---"] * len(columns)) + "|",
    ]
    for values in text_df.to_numpy().tolist():
        cells = [str(v).replace("\n", " ").replace("|", "/") for v in values]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _display_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    return df.astype(object).where(pd.notna(df), "")


def _weakness_lines(detail: pd.DataFrame, false_positive: pd.DataFrame, missed: pd.DataFrame) -> list[str]:
    lines: list[str] = []
    if detail.empty:
        return ["- データ不足で弱点判定不可。"]
    buy = detail[detail["decision"].eq("BUY")]
    if len(buy) < 20:
        lines.append("- BUYサンプルが20件未満の分類は有効性を断定しない。")
    if not false_positive.empty:
        lines.append("- BUY後に短期下落した銘柄があり、出来高急増後の反落・高値近辺の過熱を追加確認する余地あり。")
    if not missed.empty:
        lines.append("- WATCH/SKIPから大きく上昇した銘柄があり、Aランクや押し目型の取りこぼし確認が必要。")
    if "regime" in detail.columns:
        bad = detail[detail["regime"].isin(["RISK", "STOP", "CAUTION"])]
        if not bad.empty:
            lines.append("- 悪地合いのサンプルは別集計し、NORMALと混ぜて判断しない。")
    lines.append("- 決算予定データは今回検証対象外のため、決算回避ルールの精度は別データで検証が必要。")
    return lines


def _proposal_lines(detail: pd.DataFrame, false_positive: pd.DataFrame, missed: pd.DataFrame) -> list[str]:
    lines = [
        "- 変更案1: BUY直前の5日上昇率・出来高急増後の陰線を減点。理由: 高値掴みの誤判定を減らす。副作用: 強いブレイク初動を取り逃がす可能性。",
        "- 変更案2: WATCHのうちAランク・screen_type=MULTI・出来高比高めを準BUYとして別枠検証。理由: 見逃し上昇の拾い直し。副作用: BUY件数が増え資金分散が薄くなる。",
        "- 変更案3: 25MA/200MA押し目は地合いNORMAL時だけ閾値を緩め、CAUTION時は厳格化。理由: 押し目失敗を地合いで抑える。副作用: サンプル数が少ないと過剰最適化しやすい。",
        "- 比較方法: 本CSVを固定し、条件変更後に同期間・同銘柄集合で5営業日勝率、平均リターン、最大下落、見逃し件数を差分比較する。",
    ]
    if false_positive.empty:
        lines.insert(0, "- 誤判定BUYが少ない場合は、まずBUY数を増やす調整ではなくWATCH見逃しの検証を優先。")
    if missed.empty:
        lines.insert(0, "- 見逃しが少ない場合は、BUY条件を緩めずドローダウン低減を優先。")
    return lines


def run_validation(config: ValidationConfig) -> dict[str, object]:
    started = time.perf_counter()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    universe = _load_universe(config)
    histories, failures = _prepare_histories(universe, config.period)
    market = _download_market_history(config.period)
    eval_dates = _evaluation_positions(histories, config)
    detail_rows: list[dict[str, object]] = []
    date_summaries: list[dict[str, object]] = []
    duplicate_guard: set[tuple[str, str, str]] = set()
    stock_map = {str(row.ticker): row._asdict() for row in universe.itertuples(index=False)}

    for d_idx, asof_date in enumerate(eval_dates, start=1):
        if d_idx == 1 or d_idx % 10 == 0 or d_idx == len(eval_dates):
            print(f"[buy3-validation] date {d_idx}/{len(eval_dates)} {asof_date.date().isoformat()}", flush=True)
        screening_rows: list[dict[str, object]] = []
        for ticker, hist in histories.items():
            if asof_date not in hist.index:
                continue
            asof_pos = int(hist.index.get_loc(asof_date))
            if asof_pos < config.min_history_days or asof_pos + config.forward_max_days >= len(hist):
                continue
            stock = pd.Series(stock_map[ticker])
            row = _build_screening_row(stock, hist, asof_pos)
            screening_rows.append(row)
        if not screening_rows:
            continue
        screening = _normalize_screening_schema(pd.DataFrame(screening_rows))
        regime = _regime_for_date(market, asof_date)
        decisions = build_decisions(screening, learning=pd.DataFrame(), regime=regime, today=asof_date.date())
        decisions["_regime"] = regime
        decisions["ticker"] = screening.set_index("code").reindex(decisions["code"].astype(str))["ticker"].fillna("").to_numpy()
        selected = _select_rows_for_validation(decisions, config.top_skip_per_day)
        date_summaries.append({
            "asof_date": asof_date.date().isoformat(),
            "regime": regime,
            "buy": int(decisions["decision"].eq("BUY").sum()),
            "watch": int(decisions["decision"].eq("WATCH").sum()),
            "skip": int(decisions["decision"].eq("SKIP").sum()),
        })
        for _, row in selected.iterrows():
            ticker = str(row.get("ticker", ""))
            key = (asof_date.date().isoformat(), ticker, str(row.get("decision", "")))
            if key in duplicate_guard or ticker not in histories:
                continue
            duplicate_guard.add(key)
            outcome = _outcome_for_row(row, histories[ticker], asof_date, config)
            if outcome is not None:
                detail_rows.append(outcome)

    detail = pd.DataFrame(detail_rows)
    if not detail.empty:
        detail = detail.sort_values(["asof_date", "decision", "confidence"], ascending=[True, True, False]).reset_index(drop=True)
    detail.to_csv(DETAIL_CSV, index=False, encoding="utf-8-sig")
    summary = _write_summary(detail)
    false_positive = _false_positive(detail)
    false_positive.to_csv(FALSE_POSITIVE_CSV, index=False, encoding="utf-8-sig")
    missed = _missed_opportunity(detail)
    missed.to_csv(MISSED_CSV, index=False, encoding="utf-8-sig")
    anomaly_rows = _anomaly_rows(detail)
    cash_days = _cash_opportunity_days(detail, date_summaries)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days_requested": config.days,
        "period": config.period,
        "markets": list(config.markets),
        "max_symbols": config.max_symbols,
        "top_skip_per_day": config.top_skip_per_day,
        "slippage_roundtrip_pct": config.slippage_roundtrip_pct,
        "start_date": eval_dates[0].date().isoformat() if eval_dates else "",
        "end_date": eval_dates[-1].date().isoformat() if eval_dates else "",
        "symbols_loaded": int(len(universe)),
        "price_histories_ok": int(len(histories)),
        "price_fetch_failures": int(len(failures)),
        "failure_reasons_sample": dict(list(failures.items())[:20]),
        "detail_rows": int(len(detail)),
        "buy_rows": int((detail["decision"] == "BUY").sum()) if not detail.empty else 0,
        "watch_rows": int((detail["decision"] == "WATCH").sum()) if not detail.empty else 0,
        "skip_rows": int((detail["decision"] == "SKIP").sum()) if not detail.empty else 0,
        "anomaly_rows": int(len(anomaly_rows)),
        "anomaly_sample": anomaly_rows.head(20).to_dict("records") if not anomaly_rows.empty else [],
        "cash_days": int(len(cash_days)),
        "cash_days_with_opportunity": int(sum(1 for row in cash_days if row.get("opportunity_count", 0) > 0)),
        "cash_opportunity_days_sample": cash_days[:20],
        "date_summaries": date_summaries,
        "earnings_gate_backtested": False,
        "lookahead_price_check": "PASS: indicators use history up to asof_date; returns use only later bars",
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "outputs": {
            "detail": str(DETAIL_CSV),
            "summary": str(SUMMARY_CSV),
            "false_positive": str(FALSE_POSITIVE_CSV),
            "missed_opportunity": str(MISSED_CSV),
            "report": str(REPORT_MD),
        },
    }
    MANIFEST_JSON.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_report(detail, summary, false_positive, missed, manifest)
    return manifest


def _anomaly_rows(detail: pd.DataFrame) -> pd.DataFrame:
    if detail.empty:
        return pd.DataFrame()
    ret_cols = [col for col in detail.columns if col.startswith("return_") and col.endswith("d_pct")]
    if not ret_cols:
        return pd.DataFrame()
    ret_abs = detail[ret_cols].apply(pd.to_numeric, errors="coerce").abs().max(axis=1)
    max_up = pd.to_numeric(detail.get("max_up_10d_pct"), errors="coerce").fillna(0)
    return detail[(ret_abs >= 80) | (max_up >= 100)][
        ["asof_date", "code", "name", "decision", "return_5d_pct", "return_10d_pct", "max_up_10d_pct", "max_down_10d_pct", "screen_type", "rank"]
    ].copy()


def _cash_opportunity_days(detail: pd.DataFrame, date_summaries: list[dict[str, object]]) -> list[dict[str, object]]:
    if detail.empty:
        return []
    rows: list[dict[str, object]] = []
    by_date = {str(row["asof_date"]): row for row in date_summaries}
    for asof_date, sub in detail.groupby("asof_date"):
        summary = by_date.get(str(asof_date), {})
        if int(summary.get("buy", 0) or 0) > 0:
            continue
        max_up = pd.to_numeric(sub.get("max_up_10d_pct"), errors="coerce")
        ret10 = pd.to_numeric(sub.get("return_10d_pct"), errors="coerce")
        opportunity = sub[(max_up >= 5) | (ret10 >= 10)]
        rows.append({
            "asof_date": str(asof_date),
            "regime": summary.get("regime", ""),
            "watch": int(summary.get("watch", 0) or 0),
            "skip": int(summary.get("skip", 0) or 0),
            "opportunity_count": int(len(opportunity)),
            "top_opportunity": (
                f"{opportunity.iloc[0].get('code')} {opportunity.iloc[0].get('name')}"
                if not opportunity.empty else ""
            ),
        })
    return rows


def parse_args() -> ValidationConfig:
    parser = argparse.ArgumentParser(description="BUY3候補の過去データ精度検証")
    parser.add_argument("--days", type=int, default=int(os.environ.get("BUY3_VALIDATION_DAYS", "80")))
    parser.add_argument("--max-symbols", type=int, default=int(os.environ.get("BUY3_VALIDATION_MAX_SYMBOLS", "120")))
    parser.add_argument("--period", default=os.environ.get("BUY3_VALIDATION_PERIOD", "3y"))
    parser.add_argument("--top-skip-per-day", type=int, default=int(os.environ.get("BUY3_VALIDATION_TOP_SKIP", "5")))
    parser.add_argument("--markets", default=os.environ.get("BUY3_VALIDATION_MARKETS", "prime,standard,growth"))
    parser.add_argument("--slippage-roundtrip-pct", type=float, default=float(os.environ.get("BUY3_VALIDATION_SLIPPAGE_RT_PCT", "0.30")))
    args = parser.parse_args()
    max_symbols = None if args.max_symbols <= 0 else args.max_symbols
    markets = tuple(x.strip() for x in args.markets.split(",") if x.strip())
    return ValidationConfig(
        days=max(1, args.days),
        max_symbols=max_symbols,
        period=args.period,
        top_skip_per_day=max(0, args.top_skip_per_day),
        markets=markets or ("prime", "standard", "growth"),
        slippage_roundtrip_pct=max(0.0, args.slippage_roundtrip_pct),
    )


def main() -> int:
    config = parse_args()
    manifest = run_validation(config)
    print(
        "buy3_validation_summary "
        f"period={manifest.get('start_date')}..{manifest.get('end_date')} "
        f"symbols={manifest.get('symbols_loaded')} histories_ok={manifest.get('price_histories_ok')} "
        f"rows={manifest.get('detail_rows')} buy={manifest.get('buy_rows')} "
        f"watch={manifest.get('watch_rows')} skip={manifest.get('skip_rows')} "
        f"failures={manifest.get('price_fetch_failures')}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
