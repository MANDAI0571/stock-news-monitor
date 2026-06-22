"""統合トレンドフォロー戦略のバックテストエンジン（過去5年）。

設計方針:
- 純関数（compute_indicator_frame / entry_signals / simulate_trade / compute_metrics /
  run_analyses）は通信不要＝クラウドでも self-test 可能。
- 株価・指数の取得（yfinance）と銘柄一覧（JPX）は Mac 専用。--run は Mac で実行する。
- 数字は実データからのみ算出。データが無い分析は「データ無し」と正直に出す（捏造しない）。

エントリーシグナルは戦略ドキュメントの「再現性のある核」を価格・出来高・移動平均で表現:
  52週高値15%以内 / 株価>MA25,MA75,MA200 / MA25・MA75 上向き / 売買代金20日平均1億以上 /
  出来高 当日>20日平均 / （加点として）52週高値接近・新高値鮮度。
  ⑨好決算・⑩上方修正は別データ源が必要なため本エンジンの判定には含めない（フックのみ）。
  ＝価格・出来高で再現できる部分だけを厳密に検証する。

売買ルール（戦略ドキュメント準拠）:
  翌営業日の寄りで建て / -7% 損切り（ザラ場安値で判定）/
  +20% 到達後はトレーリング（高値-8% または 終値<MA25）/ 最長 N営業日でタイムアウト。

使い方:
  python3 backtest.py --self-test           # 純関数の検証（通信不要・クラウド可）
  python3 backtest.py --run --years 5        # Mac: 全市場5年バックテスト
  python3 backtest.py --run --limit 50       # Mac: 先頭50銘柄で動作確認
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "outputs"
CACHE_DIR = PROJECT_ROOT / "cache"
OHLC_CACHE_DIR = CACHE_DIR / "ohlc"
CHECKPOINT_DIR = CACHE_DIR / "checkpoints"
MARKETCAP_CACHE_PATH = CACHE_DIR / "marketcap.json"

# ベンチマーク（Mac取得）。グロースは東証グロース市場指数の代理として 2516.T(東証グロース上場投信)等を使うが
# 指数直接が取れない場合に備え複数候補を順に試す。
BENCHMARKS = {
    "nikkei": ["^N225"],
    "topix": ["^TPX", "1306.T"],
    "growth": ["2516.T", "^TSEMOTHR"],
}

HOLD_PERIODS = (5, 10, 20, 40)


@dataclass(frozen=True)
class BTParams:
    stop_loss_pct: float = 0.07          # -7% 損切り
    trail_arm_gain: float = 0.20         # +20% 到達でトレーリング開始
    trail_giveback: float = 0.08         # トレーリング: 高値から-8%（--trail-giveback で 0.10/0.15 等に変更可）
    timeout_bdays: int = 20              # 最長保有（営業日）
    ma25_exit: bool = True               # trail モードで +20%後に 終値<MA25 でも手仕舞い
    # 手仕舞いモード（期待値最大化の比較用・--exit-mode）:
    #   "trail"   = -7%損切り + (+20%到達後)トレーリング + (任意)MA25終値割れ + N日タイムアウト（既定・従来挙動）
    #   "timeout" = -7%損切り + N日タイムアウトのみ（伸ばさず固定保有の比較用）
    #   "ma25"    = -7%損切り + 25日線 終値割れで即手仕舞い（+20%到達を待たない） + N日タイムアウト
    exit_mode: str = "trail"
    # エントリーゲート
    near_high_pct: float = 15.0          # 52週高値からの距離 上限%
    min_turnover_20d: float = 100_000_000.0
    min_ma25_gap_pct: float | None = None
    max_ma25_gap_pct: float | None = None
    min_52w_dist_pct: float | None = None
    max_52w_dist_pct: float | None = None
    min_volume_ratio_5d_20d: float | None = None
    max_volume_ratio_5d_20d: float | None = None
    electric_volume_min: float | None = None
    selection_rule: str = "current"
    require_vol_increase: bool = True    # 当日出来高 > 20日平均
    require_ma_slope_up: bool = True     # MA25・MA75 上向き
    slope_lookback: int = 5              # 上向き判定の参照日数


@dataclass(frozen=True)
class PortfolioParams:
    initial_capital: float = 3_000_000.0
    slot_capital: float = 1_000_000.0
    max_positions: int = 3
    round_lot: int = 100


# ───────────────────────── 指標（純関数・ベクトル化） ─────────────────────────

def compute_indicator_frame(history: pd.DataFrame) -> pd.DataFrame:
    """OHLCV から日次の指標フレームを作る。通信不要。

    必要列: Open, High, Low, Close, Volume（index=日付）。
    返り値に各日の MA・52週高値・距離・傾き・売買代金20日平均・出来高比などを付与。
    """
    if history is None or history.empty:
        return pd.DataFrame()
    df = history.copy()
    close = df["Close"].astype(float)
    volume = df["Volume"].astype(float)
    turnover = close * volume

    df["ma25"] = close.rolling(25).mean()
    df["ma75"] = close.rolling(75).mean()
    df["ma200"] = close.rolling(200).mean()
    df["ma25_slope"] = df["ma25"] - df["ma25"].shift(5)
    df["ma75_slope"] = df["ma75"] - df["ma75"].shift(5)
    df["ma200_slope"] = df["ma200"] - df["ma200"].shift(5)
    df["high_52w"] = close.rolling(252).max()
    df["dist_52w_high_pct"] = (df["high_52w"] - close) / df["high_52w"] * 100.0
    df["vol_ma20"] = volume.rolling(20).mean()
    df["volume_ratio_5d_20d"] = volume.rolling(5).mean() / df["vol_ma20"]
    df["vol_increase"] = volume > df["vol_ma20"]
    df["turnover_20d"] = turnover.rolling(20).mean()
    df["ma25_gap_pct"] = (close - df["ma25"]) / df["ma25"] * 100.0
    df["close"] = close
    return df


def entry_signals(ind: pd.DataFrame, params: BTParams = BTParams()) -> pd.Series:
    """各日が S級エントリー条件を満たすか（再現性のある価格・出来高の核のみ）。"""
    if ind is None or ind.empty:
        return pd.Series(dtype=bool)
    close = ind["close"]
    cond = (
        ind["ma25"].notna()
        & ind["ma75"].notna()
        & ind["ma200"].notna()
        & ind["high_52w"].notna()
        & (ind["dist_52w_high_pct"] <= params.near_high_pct)
        & (close > ind["ma25"])
        & (close > ind["ma75"])
        & (close > ind["ma200"])
        & (ind["turnover_20d"] >= params.min_turnover_20d)
    )
    if params.min_ma25_gap_pct is not None:
        cond = cond & (ind["ma25_gap_pct"] >= params.min_ma25_gap_pct)
    if params.max_ma25_gap_pct is not None:
        cond = cond & (ind["ma25_gap_pct"] <= params.max_ma25_gap_pct)
    if params.min_52w_dist_pct is not None:
        cond = cond & (ind["dist_52w_high_pct"] >= params.min_52w_dist_pct)
    if params.max_52w_dist_pct is not None:
        cond = cond & (ind["dist_52w_high_pct"] <= params.max_52w_dist_pct)
    if params.min_volume_ratio_5d_20d is not None:
        cond = cond & (ind["volume_ratio_5d_20d"] >= params.min_volume_ratio_5d_20d)
    if params.max_volume_ratio_5d_20d is not None:
        cond = cond & (ind["volume_ratio_5d_20d"] <= params.max_volume_ratio_5d_20d)
    if params.require_ma_slope_up:
        cond = cond & (ind["ma25_slope"] > 0) & (ind["ma75_slope"] > 0)
    if params.require_vol_increase:
        cond = cond & ind["vol_increase"].fillna(False)
    return cond.fillna(False)


# ───────────────────────── 1トレードの結果（純関数） ─────────────────────────

def simulate_trade(ohlc: pd.DataFrame, entry_idx: int, params: BTParams = BTParams()) -> dict:
    """entry_idx の翌営業日の寄り(Open)で建て、ルールに従って手仕舞う。

    返り値: 建値・決済値・損益%・保有営業日・決済理由・各保有期間(5/10/20/40日)の終値リターン。
    データ不足で建てられない場合は None を返す。
    """
    n = len(ohlc)
    fill_idx = entry_idx + 1
    if fill_idx >= n:
        return None
    o = ohlc["Open"].to_numpy(dtype=float)
    h = ohlc["High"].to_numpy(dtype=float)
    low = ohlc["Low"].to_numpy(dtype=float)
    c = ohlc["Close"].to_numpy(dtype=float)
    ma25 = ohlc["ma25"].to_numpy(dtype=float) if "ma25" in ohlc.columns else np.full(n, np.nan)

    entry_price = o[fill_idx]
    if not np.isfinite(entry_price) or entry_price <= 0:
        return None

    stop = entry_price * (1.0 - params.stop_loss_pct)
    armed = False
    peak = entry_price
    exit_idx = None
    exit_price = None
    reason = ""

    mode = getattr(params, "exit_mode", "trail")
    last = min(fill_idx + params.timeout_bdays, n - 1)
    for i in range(fill_idx, last + 1):
        # 損切り（ザラ場安値が損切り値を割ったら、その日に損切り値で約定とみなす）。全モード共通。
        if low[i] <= stop:
            exit_idx, exit_price, reason = i, stop, ("stop7" if not armed else "trail")
            break
        peak = max(peak, h[i])
        if mode == "ma25":
            # 25日線 終値割れで即手仕舞い（+20%到達を待たない）
            if np.isfinite(ma25[i]) and c[i] < ma25[i]:
                exit_idx, exit_price, reason = i, c[i], "ma25_exit"
                break
        elif mode == "trail":
            if not armed and h[i] >= entry_price * (1.0 + params.trail_arm_gain):
                armed = True
            if armed:
                trail_line = peak * (1.0 - params.trail_giveback)
                if low[i] <= trail_line:
                    exit_idx, exit_price, reason = i, trail_line, "trail"
                    break
                if params.ma25_exit and np.isfinite(ma25[i]) and c[i] < ma25[i]:
                    exit_idx, exit_price, reason = i, c[i], "ma25_exit"
                    break
        # mode == "timeout": 損切りとタイムアウトのみ（途中手仕舞いしない）
        if i == last:
            exit_idx, exit_price, reason = i, c[i], "timeout"
            break

    if exit_idx is None:
        exit_idx, exit_price, reason = last, c[last], "timeout"

    hold_days = exit_idx - fill_idx
    pnl_pct = (exit_price - entry_price) / entry_price * 100.0

    horizon = {}
    for d in HOLD_PERIODS:
        j = fill_idx + d
        if j < n and np.isfinite(c[j]):
            horizon[f"ret_{d}d_pct"] = (c[j] - entry_price) / entry_price * 100.0
        else:
            horizon[f"ret_{d}d_pct"] = np.nan

    result = {
        "entry_idx": fill_idx,
        "exit_idx": exit_idx,
        "entry_price": float(entry_price),
        "exit_price": float(exit_price),
        "pnl_pct": float(pnl_pct),
        "hold_days": int(hold_days),
        "exit_reason": reason,
        "win": bool(pnl_pct > 0),
    }
    result.update(horizon)
    return result


# ───────────────────────── 集計（純関数） ─────────────────────────

def compute_metrics(trades: pd.DataFrame) -> dict:
    """勝率・平均利益・平均損失・PF・最大DD・期待値・保有期間別成績。"""
    if trades is None or trades.empty:
        return {"n_trades": 0, "note": "トレードなし＝データ無し"}

    pnl = trades["pnl_pct"].astype(float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    n = len(pnl)
    win_rate = len(wins) / n * 100.0
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    gross_win = float(wins.sum())
    gross_loss = float(-losses.sum())
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    expectancy = float(pnl.mean())  # 期待値（%/トレード）
    max_dd = _max_drawdown(trades, pnl.to_numpy())

    per_hold = {}
    for d in HOLD_PERIODS:
        col = f"ret_{d}d_pct"
        if col in trades.columns:
            series = trades[col].dropna().astype(float)
            if len(series):
                per_hold[f"{d}d"] = {
                    "n": int(len(series)),
                    "win_rate": float((series > 0).mean() * 100.0),
                    "avg_ret_pct": float(series.mean()),
                    "median_ret_pct": float(series.median()),
                }
            else:
                per_hold[f"{d}d"] = {"n": 0, "note": "データ無し"}

    out = {
        "n_trades": int(n),
        "win_rate_pct": round(win_rate, 2),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "profit_factor": round(pf, 3) if np.isfinite(pf) else None,
        "expectancy_pct": round(expectancy, 3),
        "max_drawdown_pct": round(max_dd, 2),
        "gross_win_pct": round(gross_win, 2),
        "gross_loss_pct": round(gross_loss, 2),
        "per_hold_period": per_hold,
    }
    if "pnl_yen" in trades.columns:
        out["total_pnl_yen"] = int(round(float(trades["pnl_yen"].sum())))
    if "ending_equity" in trades.columns and trades["ending_equity"].notna().any():
        out["ending_equity_yen"] = int(round(float(trades["ending_equity"].dropna().iloc[-1])))
    if "entry_amount" in trades.columns:
        out["avg_entry_amount_yen"] = int(round(float(trades["entry_amount"].mean())))
    return out


def _max_drawdown(trades: pd.DataFrame, pnl_pct: np.ndarray) -> float:
    """資金管理済みなら実資産曲線、未指定なら従来のトレード損益%合成で最大DDを出す。"""
    if "ending_equity" in trades.columns and trades["ending_equity"].notna().any():
        ending = trades["ending_equity"].dropna().astype(float).to_numpy()
        if ending.size == 0:
            return 0.0
        initial = float(trades["capital"].dropna().iloc[0]) if "capital" in trades.columns and trades["capital"].notna().any() else ending[0]
        equity = np.concatenate(([initial], ending))
        peak = np.maximum.accumulate(equity)
        dd = (equity - peak) / peak
        return float(dd.min() * 100.0)
    if pnl_pct.size == 0:
        return 0.0
    equity = np.cumprod(1.0 + pnl_pct / 100.0)
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    return float(dd.min() * 100.0)


def run_analyses(trades: pd.DataFrame) -> dict:
    """戦略ドキュメント記載の10項目分析。データが無い軸は「データ無し」と返す。"""
    if trades is None or trades.empty:
        return {"note": "トレードなし＝全分析データ無し"}

    out: dict = {}
    win = trades[trades["win"]]
    loss = trades[~trades["win"]]

    # 1. 勝ちパターン共通点 / 2. 負けパターン共通点
    out["win_pattern"] = _profile(win)
    out["loss_pattern"] = _profile(loss)

    # 3. 最も利益が出る条件（数値軸をビン分割し期待値が高い帯を抽出）
    out["best_conditions"] = {
        axis: _best_bucket(trades, axis)
        for axis in ("dist_52w_high_pct", "volume_ratio_5d_20d", "ma25_gap_pct", "ma200_gap_pct")
        if axis in trades.columns
    }

    # 4. 地合いとの関係（entry時のregime_flag: 1=良/0=悪。ベンチがMA200上か等で付与）
    if "regime_good" in trades.columns:
        out["regime_relation"] = _group_metrics(trades, "regime_good")
    else:
        out["regime_relation"] = {"note": "regime_good 未付与＝データ無し"}

    # 5-7. 日経/TOPIX/グロース相関（各トレードの保有期間ベンチリターンとの相関）
    out["benchmark_correlation"] = {}
    for key in ("nikkei", "topix", "growth"):
        col = f"bench_{key}_ret_pct"
        if col in trades.columns and trades[col].notna().sum() >= 5:
            corr = float(np.corrcoef(trades["pnl_pct"], trades[col].fillna(0.0))[0, 1])
            out["benchmark_correlation"][key] = round(corr, 3)
        else:
            out["benchmark_correlation"][key] = "データ無し"

    # 8. セクター別 / 9. 出来高別 / 10. 時価総額別
    out["by_sector"] = _group_metrics(trades, "sector") if "sector" in trades.columns else {"note": "データ無し"}
    out["by_volume_bucket"] = _group_metrics(trades, "turnover_bucket") if "turnover_bucket" in trades.columns else {"note": "データ無し"}
    out["by_marketcap_bucket"] = _group_metrics(trades, "mktcap_bucket") if "mktcap_bucket" in trades.columns else {"note": "データ無し（時価総額未取得）"}
    return out


def _profile(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return {"n": 0, "note": "データ無し"}
    prof = {"n": int(len(df))}
    for axis in ("dist_52w_high_pct", "volume_ratio_5d_20d", "ma25_gap_pct", "ma200_gap_pct", "hold_days"):
        if axis in df.columns and df[axis].notna().any():
            prof[f"avg_{axis}"] = round(float(df[axis].astype(float).mean()), 3)
    if "exit_reason" in df.columns:
        prof["exit_reason_mix"] = df["exit_reason"].value_counts().to_dict()
    return prof


def _best_bucket(df: pd.DataFrame, axis: str, bins: int = 4) -> dict:
    s = df[axis].dropna().astype(float)
    if len(s) < 8:
        return {"note": "標本不足（<8）＝データ無し"}
    try:
        cats = pd.qcut(df[axis].astype(float), q=min(bins, s.nunique()), duplicates="drop")
    except (ValueError, IndexError):
        return {"note": "ビン分割不可"}
    g = df.assign(_bin=cats).groupby("_bin", observed=True)["pnl_pct"]
    table = g.agg(["count", "mean"]).rename(columns={"count": "n", "mean": "expectancy_pct"})
    table = table.sort_values("expectancy_pct", ascending=False)
    return {str(idx): {"n": int(r["n"]), "expectancy_pct": round(float(r["expectancy_pct"]), 3)}
            for idx, r in table.iterrows()}


def _group_metrics(df: pd.DataFrame, key: str) -> dict:
    out = {}
    for val, sub in df.groupby(key, observed=True):
        if len(sub) < 3:
            continue
        out[str(val)] = {
            "n": int(len(sub)),
            "win_rate_pct": round(float((sub["pnl_pct"] > 0).mean() * 100.0), 2),
            "expectancy_pct": round(float(sub["pnl_pct"].mean()), 3),
        }
    return out or {"note": "各群 標本不足（<3）＝データ無し"}


def _entry_score(dist_52w_high_pct: float, turnover_20d: float,
                 volume_ratio_5d_20d: float, ma25_gap_pct: float,
                 perfect_order: bool) -> float:
    """エントリー時点の「再現性のあるテクニカル質」スコア（通信不要・純関数）。

    バックテスト独自の透明な合成スコア（本番 scoring.py とは別物・選択の第3キー用）。
    #4の勝敗分析の発見を反映:
      - 52週高値に近い（高値圏）ほど加点。
      - 売買代金（流動性）が大きいほど加点（勝ちトレードは高流動性）。
      - 出来高は急増しすぎない方が良い（イナゴ回避）→ 1.2倍付近を最大に、過熱は減点。
      - MA25に近い（浅い押し）ほど加点（勝ちトレードはMA25乖離が小さい）。
      - パーフェクトオーダー（MA25>MA75>MA200）なら加点。
    """
    score = 0.0
    if np.isfinite(dist_52w_high_pct):
        score += max(0.0, 15.0 - dist_52w_high_pct) * 2.0     # 高値接近（近いほど高い）
    if np.isfinite(turnover_20d):
        score += min(turnover_20d / 1e8, 20.0) * 0.5          # 流動性（売買代金億・上限20億）
    if volume_ratio_5d_20d is not None and np.isfinite(volume_ratio_5d_20d):
        score += max(0.0, 5.0 - abs(volume_ratio_5d_20d - 1.2) * 5.0)  # 出来高は1.2倍付近が最良
    if np.isfinite(ma25_gap_pct):
        score += max(0.0, 10.0 - max(0.0, ma25_gap_pct))      # 浅い押し（MA25乖離が小さい）を加点
    if perfect_order:
        score += 12.0
    return round(score, 2)


# ───────────────────────── Mac専用（yfinance / JPX 通信） ─────────────────────────

def backtest_symbol(history: pd.DataFrame, meta: dict, params: BTParams,
                    benchmarks: dict | None = None) -> list:
    """1銘柄の5年ヒストリーから全エントリーを検出しトレードを生成（重複建て防止つき）。"""
    ind = compute_indicator_frame(history)
    if ind.empty:
        return []
    sig = entry_signals(ind, params)
    ohlc = ind  # ind には Open/High/Low/Close/Volume と ma25 が含まれる
    closes = ind["close"]
    vol_ma20 = ind["vol_ma20"]
    trades = []
    blocked_until = -1
    idxs = np.where(sig.to_numpy())[0]
    for entry_idx in idxs:
        if entry_idx <= blocked_until:
            continue
        res = simulate_trade(ohlc, int(entry_idx), params)
        if not res:
            continue
        # エントリー時の特徴量を記録（分析用）
        res["code"] = meta.get("code", "")
        res["name"] = meta.get("name", "")
        res["sector"] = meta.get("sector", "")
        res["entry_date"] = str(ind.index[res["entry_idx"]].date())
        res["exit_date"] = str(ind.index[res["exit_idx"]].date())
        res["dist_52w_high_pct"] = float(ind["dist_52w_high_pct"].iloc[entry_idx])
        res["ma25_gap_pct"] = float((closes.iloc[entry_idx] - ind["ma25"].iloc[entry_idx]) / ind["ma25"].iloc[entry_idx] * 100.0)
        res["ma200_gap_pct"] = float((closes.iloc[entry_idx] - ind["ma200"].iloc[entry_idx]) / ind["ma200"].iloc[entry_idx] * 100.0)
        v = ind["Volume"].iloc[max(0, entry_idx - 4):entry_idx + 1].mean()
        res["volume_ratio_5d_20d"] = float(v / vol_ma20.iloc[entry_idx]) if vol_ma20.iloc[entry_idx] else np.nan
        if not _passes_sector_volume_rule(res["sector"], res["volume_ratio_5d_20d"], params):
            continue
        res["turnover_20d"] = float(ind["turnover_20d"].iloc[entry_idx])
        # 選択ロジック・CSV出力用の明示列（#2）
        res["turnover_20d_avg"] = res["turnover_20d"]
        res["dist_to_ma25_pct"] = res["ma25_gap_pct"]
        res["market_cap"] = np.nan  # run() で取得値を入れる（取れない時は NaN）
        ma25v = float(ind["ma25"].iloc[entry_idx])
        ma75v = float(ind["ma75"].iloc[entry_idx])
        ma200v = float(ind["ma200"].iloc[entry_idx])
        perfect_order = bool(np.isfinite(ma25v) and np.isfinite(ma75v) and np.isfinite(ma200v)
                             and ma25v > ma75v > ma200v)
        res["perfect_order"] = perfect_order
        res["score"] = _entry_score(res["dist_52w_high_pct"], res["turnover_20d"],
                                     res["volume_ratio_5d_20d"], res["ma25_gap_pct"], perfect_order)
        if benchmarks:
            _attach_benchmarks(res, ind.index[res["entry_idx"]], res["hold_days"], benchmarks)
        trades.append(res)
        blocked_until = res["exit_idx"]  # 手仕舞いまで同一銘柄を重ねない
    return trades


def _passes_sector_volume_rule(sector: str, volume_ratio_5d_20d: float, params: BTParams) -> bool:
    if params.electric_volume_min is None:
        return True
    if sector != "電気機器":
        return True
    if volume_ratio_5d_20d is None or not np.isfinite(volume_ratio_5d_20d):
        return False
    return float(volume_ratio_5d_20d) >= float(params.electric_volume_min)


def _attach_benchmarks(res: dict, entry_ts, hold_days: int, benchmarks: dict) -> None:
    for key, bench_close in benchmarks.items():
        try:
            pos = bench_close.index.get_indexer([entry_ts], method="nearest")[0]
            jpos = min(pos + max(hold_days, 1), len(bench_close) - 1)
            base = float(bench_close.iloc[pos])
            res[f"bench_{key}_ret_pct"] = (float(bench_close.iloc[jpos]) - base) / base * 100.0 if base else np.nan
            if key == "topix":
                ma = bench_close.rolling(200).mean().iloc[pos]
                res["regime_good"] = bool(np.isfinite(ma) and base > ma)
        except (KeyError, IndexError, ValueError):
            res[f"bench_{key}_ret_pct"] = np.nan


def _bucket_columns(trades: pd.DataFrame) -> pd.DataFrame:
    if "turnover_20d" in trades.columns and trades["turnover_20d"].notna().any():
        trades["turnover_bucket"] = pd.cut(
            trades["turnover_20d"],
            bins=[0, 3e8, 1e9, 5e9, np.inf],
            labels=["1-3億", "3-10億", "10-50億", "50億+"],
        )
    if "mktcap" in trades.columns and trades["mktcap"].notna().any():
        trades["mktcap_bucket"] = pd.cut(
            trades["mktcap"],
            bins=[0, 3e10, 1e11, 1e12, np.inf],
            labels=["小型(<300億)", "中型(300億-1000億)", "大型(1000億-1兆)", "超大型(1兆+)"],
        )
    return trades


def apply_portfolio_constraints(
    trades: pd.DataFrame,
    portfolio: PortfolioParams = PortfolioParams(),
    selection_rule: str = "current",
) -> pd.DataFrame:
    """候補トレードを300万円・最大3銘柄・1銘柄100万円の実運用制約に通す。

    候補は entry_date 順に処理する。同一銘柄の重複保有は禁止し、手仕舞い後にだけ
    再エントリーを許可する。100株単位で1枠100万円以内に収まらない銘柄は見送る。
    """
    if trades is None or trades.empty:
        return pd.DataFrame()

    sort_cols, ascending = _selection_sort_spec(trades, selection_rule)

    ordered = trades.sort_values(sort_cols, ascending=ascending,
                                 na_position="last").reset_index(drop=True)
    # 同日内の採用優先順位（1=最優先）。選択理由の説明に使う。
    ordered["selection_rank"] = ordered.groupby("entry_date").cumcount() + 1
    open_positions: list[dict[str, object]] = []
    accepted: list[dict[str, object]] = []
    cash = portfolio.initial_capital

    for row in ordered.to_dict("records"):
        entry_date = pd.Timestamp(row["entry_date"])
        still_open = []
        for pos in open_positions:
            if pd.Timestamp(pos["exit_date"]) < entry_date:
                cash += float(pos["entry_amount"]) + float(pos["pnl_yen"])
            else:
                still_open.append(pos)
        open_positions = still_open

        code = str(row.get("code", ""))
        if any(str(pos["code"]) == code for pos in open_positions):
            continue
        if len(open_positions) >= portfolio.max_positions:
            continue

        entry_price = float(row["entry_price"])
        shares = int(portfolio.slot_capital // (entry_price * portfolio.round_lot)) * portfolio.round_lot
        if shares <= 0:
            continue
        entry_amount = shares * entry_price
        if entry_amount > portfolio.slot_capital:
            continue
        if entry_amount > cash:
            continue

        exit_price = float(row["exit_price"])
        pnl_yen = (exit_price - entry_price) * shares
        exit_date = pd.Timestamp(row["exit_date"])
        cash -= entry_amount
        accepted_row = dict(row)
        rank = int(row.get("selection_rank", 1))
        tv = row.get("turnover_20d_avg", row.get("turnover_20d"))
        dm = row.get("dist_to_ma25_pct", row.get("ma25_gap_pct"))
        sc = row.get("score")
        if tv is not None and pd.notna(tv):
            reason = (f"売買代金{float(tv)/1e8:.1f}億"
                      + (f"・MA25乖離{float(dm):.1f}%" if dm is not None and pd.notna(dm) else "")
                      + (f"・score{float(sc):.0f}" if sc is not None and pd.notna(sc) else "")
                      + f"（同日{rank}位）")
        else:
            reason = f"先着順（同日{rank}位）"
        accepted_row.update(
            {
                "shares": shares,
                "entry_amount": float(entry_amount),
                "pnl_yen": float(pnl_yen),
                "available_cash_after_entry": float(cash),
                "capital": float(portfolio.initial_capital),
                "slot_capital": float(portfolio.slot_capital),
                "max_positions": int(portfolio.max_positions),
                "selection_rank": rank,
                "selection_reason": reason,
            }
        )
        open_positions.append(
            {
                "code": code,
                "exit_date": exit_date,
                "entry_amount": entry_amount,
                "pnl_yen": pnl_yen,
                "row_index": len(accepted),
            }
        )
        accepted.append(accepted_row)

    if not accepted:
        return pd.DataFrame()

    accepted_df = pd.DataFrame(accepted).sort_values(["exit_date", "entry_date", "code"]).reset_index(drop=True)
    realized = portfolio.initial_capital
    ending_equity = []
    for row in accepted_df.itertuples(index=False):
        realized += float(row.pnl_yen)
        ending_equity.append(realized)
    accepted_df["ending_equity"] = ending_equity
    return accepted_df


def _selection_sort_spec(trades: pd.DataFrame, selection_rule: str) -> tuple[list[str], list[bool]]:
    """同日候補の選抜順。entry_date は必ず最上位に置く。"""
    turnover_key = "turnover_20d_avg" if "turnover_20d_avg" in trades.columns else (
        "turnover_20d" if "turnover_20d" in trades.columns else None)
    ma25_key = "dist_to_ma25_pct" if "dist_to_ma25_pct" in trades.columns else (
        "ma25_gap_pct" if "ma25_gap_pct" in trades.columns else None)
    if selection_rule == "current":
        keys = [turnover_key, ma25_key, "score"]
        asc = [False, True, False]
    elif selection_rule == "ma25_first":
        keys = [ma25_key, "score", turnover_key]
        asc = [True, False, False]
    elif selection_rule == "score_first":
        keys = ["score", ma25_key, turnover_key]
        asc = [False, True, False]
    else:
        raise ValueError(f"unknown selection_rule: {selection_rule}")

    sort_cols: list[str] = ["entry_date"]
    ascending: list[bool] = [True]
    for key, is_asc in zip(keys, asc):
        if key and key not in sort_cols and key in trades.columns:
            sort_cols.append(key)
            ascending.append(is_asc)
    sort_cols.append("code")
    ascending.append(True)
    if "exit_idx" in trades.columns:
        sort_cols.append("exit_idx")
        ascending.append(True)
    return sort_cols, ascending


def run(
    years: int = 5,
    limit: int | None = None,
    params: BTParams = BTParams(),
    portfolio: PortfolioParams = PortfolioParams(),
    use_cache: bool = True,
    resume: bool = False,
    no_marketcap: bool = False,
    batch_size: int = 0,
) -> dict:
    """Mac専用: JPXユニバース×5年ヒストリーで全銘柄バックテスト。

    高速化オプション:
      use_cache   = OHLCを cache/ohlc/ に parquet 保存し、次回以降は通信せず再利用。
      resume      = checkpoint_trades.jsonl から処理済み銘柄を読み飛ばして続行。
      no_marketcap= 時価総額(.info)取得を省略（最も遅い処理をスキップ）。取得時もキャッシュする。
      batch_size  = >0 なら未キャッシュ銘柄を yf.download でまとめて先読みしHTTP往復を削減。
    """
    import yfinance as yf  # Mac専用（クラウドは通信不可）
    from scanner.universe import UniverseConfig, load_jpx_listed

    universe_config = UniverseConfig()
    run_meta = _build_run_meta(years, limit, params, portfolio, universe_config)
    run_hash = _run_hash(run_meta)
    checkpoint_path, checkpoint_meta_path = _checkpoint_paths(run_hash)
    if resume:
        _validate_checkpoint_meta(run_meta, checkpoint_meta_path)
    else:
        _reset_checkpoint(checkpoint_path, checkpoint_meta_path)
        _write_checkpoint_meta(run_meta, checkpoint_meta_path)

    universe = load_jpx_listed(universe_config)
    if limit:
        universe = universe.head(limit)

    period = f"{years + 1}y"  # MA200・52週分の助走を確保
    benchmarks = _load_benchmarks(yf, period)
    mc_cache = _load_marketcap_cache()

    # 途中再開: 既存チェックポイントから処理済み銘柄とトレードを復元
    candidate_trades: list = []
    done_tickers: set = set()
    if resume:
        done_tickers, prior = _load_checkpoint(checkpoint_path)
        candidate_trades.extend(prior)
        print(
            f"[resume] hash={run_hash} 復元: 処理済み{len(done_tickers)}銘柄 / トレード{len(prior)}件",
            flush=True,
        )

    # バッチ先読み（未キャッシュのみ・Mac）
    if batch_size and batch_size > 0:
        pending = [row.ticker for row in universe.itertuples(index=False)
                   if str(row.ticker) not in done_tickers]
        _batch_prefetch_ohlc(yf, pending, period, batch_size=batch_size, use_cache=use_cache)

    total = len(universe)
    for i, row in enumerate(universe.itertuples(index=False), start=1):
        if str(row.ticker) in done_tickers:
            print(f"[{i}/{total}] {row.ticker} skip(済)", flush=True)
            continue
        print(f"[{i}/{total}] {row.ticker} {row.name}", flush=True)
        try:
            raw = _load_cached_ohlc(yf, row.ticker, period, use_cache=use_cache)
            if raw is None or raw.empty:
                _append_checkpoint(row.ticker, [], checkpoint_path)  # 空でも処理済みとして記録（再開で再取得しない）
                continue
            meta = {"code": row.code, "name": row.name, "sector": row.sector}
            trades = backtest_symbol(raw, meta, params, benchmarks)
            # 時価総額（取れる時だけ・キャッシュ優先・--no-marketcapで省略）
            mktcap = _marketcap_with_cache(yf, row.ticker, mc_cache, no_marketcap=no_marketcap)
            for t in trades:
                t["mktcap"] = mktcap
                t["market_cap"] = mktcap
            candidate_trades.extend(trades)
            _append_checkpoint(row.ticker, trades, checkpoint_path)
        except Exception as exc:  # noqa
            print(f"  skip {row.ticker}: {exc}", flush=True)

    _save_marketcap_cache(mc_cache)
    candidates_df = pd.DataFrame(candidate_trades)
    trades_df = apply_portfolio_constraints(candidates_df, portfolio, selection_rule=params.selection_rule)
    if not trades_df.empty:
        trades_df = _bucket_columns(trades_df)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "years": years,
        "universe_size": int(total),
        "params": params.__dict__,
        "portfolio": portfolio.__dict__,
        "candidate_trades": int(len(candidates_df)),
        "accepted_trades": int(len(trades_df)),
        "metrics": compute_metrics(trades_df),
        "analyses": run_analyses(trades_df),
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if not candidates_df.empty:
        candidates_df.to_csv(OUTPUT_DIR / f"backtest_candidate_trades_{stamp}.csv", index=False, encoding="utf-8-sig")
    if not trades_df.empty:
        trades_df.to_csv(OUTPUT_DIR / f"backtest_trades_{stamp}.csv", index=False, encoding="utf-8-sig")
    (OUTPUT_DIR / f"backtest_report_{stamp}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    print(f"\n保存: outputs/backtest_report_{stamp}.json")
    return report


def _load_benchmarks(yf, period: str) -> dict:
    out = {}
    for key, tickers in BENCHMARKS.items():
        for tk in tickers:
            try:
                d = yf.download(tk, period=period, interval="1d", auto_adjust=True, progress=False)
                if d is not None and not d.empty:
                    if isinstance(d.columns, pd.MultiIndex):
                        d = d.droplevel(-1, axis=1)
                    out[key] = d["Close"].astype(float)
                    break
            except Exception:  # noqa
                continue
    return out


def _safe_marketcap(yf, ticker: str):
    try:
        info = yf.Ticker(ticker).fast_info
        mc = getattr(info, "market_cap", None)
        if mc is None and isinstance(info, dict):
            mc = info.get("market_cap") or info.get("marketCap")
        return float(mc) if mc else np.nan
    except Exception:  # noqa
        return np.nan


# ───────────────────────── 高速化: OHLCキャッシュ / チェックポイント / marketcapキャッシュ ─────────────────────────
# ファイル入出力は純粋（通信不要）＝クラウドで self-test 可能。実 yfinance 取得のみ Mac 専用。

def _ohlc_cache_path(ticker: str, period: str) -> Path:
    safe = str(ticker).replace("/", "_").replace("\\", "_")
    return OHLC_CACHE_DIR / f"{safe}__{period}.parquet"


def _save_ohlc_cache(df: pd.DataFrame, path: Path) -> None:
    """parquet で保存。pyarrow/fastparquet が無ければ pickle にフォールバック。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(path)
    except Exception:  # noqa（parquetエンジン未導入など）
        df.to_pickle(path.with_suffix(".pkl"))


def _read_ohlc_cache(path: Path):
    """キャッシュがあれば読む（parquet優先・無ければpkl）。無ければ None。"""
    try:
        if path.exists():
            return pd.read_parquet(path)
        pkl = path.with_suffix(".pkl")
        if pkl.exists():
            return pd.read_pickle(pkl)
    except Exception:  # noqa
        return None
    return None


def _load_cached_ohlc(yf, ticker: str, period: str, use_cache: bool = True):
    """OHLCをキャッシュ優先で取得。キャッシュヒット時は通信しない（Mac/yfinance）。"""
    path = _ohlc_cache_path(ticker, period)
    if use_cache:
        cached = _read_ohlc_cache(path)
        if cached is not None and not cached.empty:
            return cached
    raw = yf.download(ticker, period=period, interval="1d", auto_adjust=True, progress=False)
    if raw is None or raw.empty:
        return raw
    if isinstance(raw.columns, pd.MultiIndex):
        raw = raw.droplevel(-1, axis=1)
    if use_cache:
        _save_ohlc_cache(raw, path)
    return raw


def _batch_prefetch_ohlc(yf, tickers: list, period: str, batch_size: int = 50,
                         use_cache: bool = True) -> None:
    """未キャッシュのティッカーだけをまとめてダウンロードし、銘柄ごとにキャッシュ保存（Mac）。
    HTTP往復を減らす。キャッシュ済みは通信しない。"""
    todo = [t for t in tickers
            if not (use_cache and _read_ohlc_cache(_ohlc_cache_path(t, period)) is not None)]
    for k in range(0, len(todo), max(1, batch_size)):
        chunk = todo[k:k + batch_size]
        try:
            data = yf.download(chunk, period=period, interval="1d", auto_adjust=True,
                               progress=False, group_by="ticker", threads=True)
        except Exception as exc:  # noqa
            print(f"  batch skip {chunk[:3]}...: {exc}", flush=True)
            continue
        for t in chunk:
            try:
                if isinstance(data.columns, pd.MultiIndex) and t in data.columns.get_level_values(0):
                    sub = data[t].dropna(how="all")
                elif len(chunk) == 1:
                    sub = data
                else:
                    continue
                if sub is not None and not sub.empty:
                    _save_ohlc_cache(sub, _ohlc_cache_path(t, period))
            except Exception:  # noqa
                continue


def _build_run_meta(years: int, limit: int | None, params: BTParams,
                    portfolio: PortfolioParams, universe_config) -> dict:
    """checkpointを安全に分離するための実行条件メタデータ。"""
    return {
        "schema": 1,
        "years": int(years),
        "period": f"{years + 1}y",
        "limit": "ALL" if limit is None else int(limit),
        "params": {
            "timeout_bdays": int(params.timeout_bdays),
            "exit_mode": str(params.exit_mode),
            "trail_giveback": float(params.trail_giveback),
            "stop_loss_pct": float(params.stop_loss_pct),
            "min_turnover_20d": float(params.min_turnover_20d),
            "min_ma25_gap_pct": params.min_ma25_gap_pct,
            "max_ma25_gap_pct": params.max_ma25_gap_pct,
            "min_52w_dist_pct": params.min_52w_dist_pct,
            "max_52w_dist_pct": params.max_52w_dist_pct,
            "min_volume_ratio_5d_20d": params.min_volume_ratio_5d_20d,
            "max_volume_ratio_5d_20d": params.max_volume_ratio_5d_20d,
            "electric_volume_min": params.electric_volume_min,
            "selection_rule": params.selection_rule,
        },
        "portfolio": {
            "initial_capital": float(portfolio.initial_capital),
            "slot_capital": float(portfolio.slot_capital),
            "max_positions": int(portfolio.max_positions),
            "round_lot": int(portfolio.round_lot),
        },
        "universe": {
            "markets": list(getattr(universe_config, "markets", ())),
            "source": "jpx_listed",
        },
    }


def _normalize_run_meta(meta: dict) -> dict:
    """checkpoint互換のため、旧メタ欠落分をデフォルトで埋める。"""
    normalized = json.loads(json.dumps(meta, ensure_ascii=False))
    params = normalized.setdefault("params", {})
    params.setdefault("electric_volume_min", None)
    params.setdefault("selection_rule", "current")
    return normalized


def _run_hash(meta: dict) -> str:
    payload = json.dumps(_normalize_run_meta(meta), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _checkpoint_paths(run_hash: str) -> tuple[Path, Path]:
    stem = f"backtest_{run_hash}"
    return CHECKPOINT_DIR / f"{stem}.jsonl", CHECKPOINT_DIR / f"{stem}.meta.json"


def _write_checkpoint_meta(meta: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _validate_checkpoint_meta(expected: dict, path: Path) -> None:
    if not path.exists():
        raise RuntimeError(f"ERROR: checkpoint meta not found: {path}")
    try:
        actual = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ERROR: checkpoint meta is broken: {path}") from exc
    if _normalize_run_meta(actual) != _normalize_run_meta(expected):
        raise RuntimeError(
            "ERROR: checkpoint meta mismatch. "
            "Do not use --resume with different years/timeout/exit/portfolio/universe conditions."
        )


def _reset_checkpoint(checkpoint_path: Path, meta_path: Path) -> None:
    for path in (checkpoint_path, meta_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _load_checkpoint(path: Path):
    """チェックポイント(JSONL)を読み、(処理済みティッカー集合, トレードのlist) を返す。"""
    done: set = set()
    trades: list = []
    if not path.exists():
        return done, trades
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        done.add(str(rec.get("ticker", "")))
        for t in rec.get("trades", []):
            trades.append(t)
    return done, trades


def _append_checkpoint(ticker: str, trades: list, path: Path) -> None:
    """1銘柄分の処理結果を追記（途中再開用）。NaN は JSON 化できないため None に変換。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_trades = []
    for t in trades:
        safe_trades.append({k: (None if (isinstance(v, float) and not np.isfinite(v)) else v)
                            for k, v in t.items()})
    rec = {"ticker": str(ticker), "trades": safe_trades}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _load_marketcap_cache(path: Path = MARKETCAP_CACHE_PATH) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_marketcap_cache(cache: dict, path: Path = MARKETCAP_CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def _marketcap_with_cache(yf, ticker: str, cache: dict, no_marketcap: bool = False) -> float:
    """marketcap をキャッシュ優先で取得。--no-marketcap なら取得せず NaN。"""
    if no_marketcap:
        return float("nan")
    if ticker in cache:
        v = cache[ticker]
        return float(v) if v is not None else float("nan")
    mc = _safe_marketcap(yf, ticker)
    cache[ticker] = None if (isinstance(mc, float) and not np.isfinite(mc)) else mc
    return mc


# ───────────────────────── self-test（純関数のみ・通信不要） ─────────────────────────

def self_test() -> None:
    # 1) 指標フレーム
    dates = pd.bdate_range("2021-01-01", periods=400)
    base = np.linspace(1000, 1600, 400)
    close = pd.Series(base, index=dates)
    hist = pd.DataFrame({
        "Open": close, "High": close * 1.01, "Low": close * 0.99,
        "Close": close, "Volume": np.linspace(1e6, 2e6, 400),
    }, index=dates)
    ind = compute_indicator_frame(hist)
    assert {"ma25", "ma75", "ma200", "ma25_slope", "high_52w", "turnover_20d", "ma25_gap_pct", "volume_ratio_5d_20d"} <= set(ind.columns)
    assert ind["ma25_slope"].iloc[-1] > 0  # 上昇基調なので上向き

    # 2) エントリーシグナル: 上昇トレンド終盤は条件成立
    sig = entry_signals(ind)
    assert sig.iloc[-1] == True  # noqa: E712
    assert entry_signals(ind, BTParams(min_ma25_gap_pct=0, max_ma25_gap_pct=7)).iloc[-1] == True  # noqa: E712
    assert entry_signals(ind, BTParams(max_ma25_gap_pct=0.1)).iloc[-1] == False  # noqa: E712
    assert entry_signals(ind, BTParams(min_52w_dist_pct=1, max_52w_dist_pct=7)).iloc[-1] == False  # noqa: E712

    filtered = ind.copy()
    last = filtered.index[-1]
    filtered.loc[last, "ma25_gap_pct"] = 5.0
    filtered.loc[last, "dist_52w_high_pct"] = 3.0
    filtered.loc[last, "volume_ratio_5d_20d"] = 1.2
    filtered.loc[last, "turnover_20d"] = 1_200_000_000.0
    params_filtered = BTParams(
        min_ma25_gap_pct=0,
        max_ma25_gap_pct=7,
        min_52w_dist_pct=1,
        max_52w_dist_pct=7,
        min_volume_ratio_5d_20d=0.9,
        max_volume_ratio_5d_20d=1.5,
        min_turnover_20d=1_000_000_000.0,
    )
    assert entry_signals(filtered, params_filtered).iloc[-1] == True  # noqa: E712
    filtered.loc[last, "volume_ratio_5d_20d"] = 0.8
    assert entry_signals(filtered, params_filtered).iloc[-1] == False  # noqa: E712
    filtered.loc[last, "volume_ratio_5d_20d"] = 1.2
    filtered.loc[last, "volume_ratio_5d_20d"] = 1.8
    assert entry_signals(filtered, params_filtered).iloc[-1] == False  # noqa: E712
    filtered.loc[last, "volume_ratio_5d_20d"] = 1.2
    filtered.loc[last, "turnover_20d"] = 500_000_000.0
    assert entry_signals(filtered, params_filtered).iloc[-1] == False  # noqa: E712
    sector_params = BTParams(electric_volume_min=1.1)
    assert _passes_sector_volume_rule("電気機器", 1.1, sector_params) is True
    assert _passes_sector_volume_rule("電気機器", 1.0, sector_params) is False
    assert _passes_sector_volume_rule("卸売業", 1.0, sector_params) is True
    assert _selection_sort_spec(pd.DataFrame(columns=["entry_date", "turnover_20d_avg", "ma25_gap_pct", "score"]), "current")[0][:4] == ["entry_date", "turnover_20d_avg", "ma25_gap_pct", "score"]
    assert _selection_sort_spec(pd.DataFrame(columns=["entry_date", "turnover_20d_avg", "ma25_gap_pct", "score"]), "ma25_first")[0][:4] == ["entry_date", "ma25_gap_pct", "score", "turnover_20d_avg"]
    assert _selection_sort_spec(pd.DataFrame(columns=["entry_date", "turnover_20d_avg", "ma25_gap_pct", "score"]), "score_first")[0][:4] == ["entry_date", "score", "ma25_gap_pct", "turnover_20d_avg"]

    # 3) simulate_trade: 利益方向（+20%到達→トレーリング or timeout で勝ち）
    res = simulate_trade(ind, len(ind) - 60)
    assert res is not None and res["pnl_pct"] > 0 and res["win"] is True

    # 4) 損切りの検証: 急落データで -7% 付近で stop
    n = 60
    d2 = pd.bdate_range("2022-01-01", periods=n)
    fall = pd.Series(np.linspace(1000, 600, n), index=d2)
    ohlc2 = pd.DataFrame({"Open": fall, "High": fall * 1.005, "Low": fall * 0.99,
                          "Close": fall, "Volume": 1e6, "ma25": fall}, index=d2)
    r2 = simulate_trade(ohlc2, 0)
    assert r2 is not None and r2["pnl_pct"] <= -6.0 and r2["exit_reason"] in ("stop7", "trail")

    # 5) compute_metrics
    trades = pd.DataFrame({
        "pnl_pct": [10.0, -7.0, 25.0, -7.0, 5.0],
        "win": [True, False, True, False, True],
        "hold_days": [8, 3, 15, 2, 6],
        "ret_5d_pct": [4.0, -3.0, 6.0, -2.0, 1.0],
        "ret_10d_pct": [8.0, -5.0, 12.0, -7.0, 3.0],
        "ret_20d_pct": [10.0, -7.0, 22.0, -7.0, 5.0],
        "ret_40d_pct": [9.0, -7.0, 30.0, -7.0, 4.0],
    })
    m = compute_metrics(trades)
    assert m["n_trades"] == 5
    assert abs(m["win_rate_pct"] - 60.0) < 1e-6
    assert m["profit_factor"] is not None and m["profit_factor"] > 1.0
    assert "5d" in m["per_hold_period"] and m["per_hold_period"]["5d"]["n"] == 5
    assert m["max_drawdown_pct"] <= 0.0

    # 6) run_analyses: 軸が無くてもデータ無しで落ちない
    trades2 = trades.assign(
        dist_52w_high_pct=[1, 10, 2, 12, 5],
        volume_ratio_5d_20d=[2.0, 1.1, 1.8, 1.0, 1.3],
        ma25_gap_pct=[3, 1, 4, 0.5, 2],
        ma200_gap_pct=[8, 2, 10, 1, 5],
        exit_reason=["trail", "stop7", "trail", "stop7", "timeout"],
        sector=["半導体", "銀行", "半導体", "銀行", "防衛"],
    )
    a = run_analyses(trades2)
    assert "win_pattern" in a and "loss_pattern" in a and "best_conditions" in a
    assert a["benchmark_correlation"]["nikkei"] == "データ無し"

    # 7) ポートフォリオ制約: 最大3銘柄、100万円枠、同一銘柄の重複保有禁止、資金不足時は不可
    candidates = pd.DataFrame(
        [
            {"code": "1111", "name": "A", "entry_date": "2024-01-02", "exit_date": "2024-01-10", "entry_price": 1000, "exit_price": 930, "pnl_pct": -7, "win": False, "exit_idx": 10},
            {"code": "2222", "name": "B", "entry_date": "2024-01-02", "exit_date": "2024-01-10", "entry_price": 1000, "exit_price": 930, "pnl_pct": -7, "win": False, "exit_idx": 10},
            {"code": "3333", "name": "C", "entry_date": "2024-01-02", "exit_date": "2024-01-10", "entry_price": 1000, "exit_price": 930, "pnl_pct": -7, "win": False, "exit_idx": 10},
            {"code": "4444", "name": "D", "entry_date": "2024-01-02", "exit_date": "2024-01-10", "entry_price": 1000, "exit_price": 1030, "pnl_pct": 3, "win": True, "exit_idx": 10},
            {"code": "1111", "name": "A2", "entry_date": "2024-01-05", "exit_date": "2024-01-12", "entry_price": 1000, "exit_price": 1100, "pnl_pct": 10, "win": True, "exit_idx": 12},
            {"code": "5555", "name": "E", "entry_date": "2024-01-11", "exit_date": "2024-01-20", "entry_price": 20000, "exit_price": 21000, "pnl_pct": 5, "win": True, "exit_idx": 20},
            {"code": "6666", "name": "F", "entry_date": "2024-01-11", "exit_date": "2024-01-20", "entry_price": 1000, "exit_price": 1100, "pnl_pct": 10, "win": True, "exit_idx": 20},
            {"code": "7777", "name": "G", "entry_date": "2024-01-11", "exit_date": "2024-01-20", "entry_price": 1000, "exit_price": 1100, "pnl_pct": 10, "win": True, "exit_idx": 20},
            {"code": "8888", "name": "H", "entry_date": "2024-01-11", "exit_date": "2024-01-20", "entry_price": 1000, "exit_price": 1100, "pnl_pct": 10, "win": True, "exit_idx": 20},
        ]
    )
    constrained = apply_portfolio_constraints(candidates)
    assert len(constrained) == 5
    assert constrained["entry_amount"].max() <= 1_000_000
    assert set(constrained["code"]) == {"1111", "2222", "3333", "6666", "7777"}
    assert len(constrained[constrained["entry_date"] == "2024-01-11"]) == 2
    assert constrained["available_cash_after_entry"].min() >= 0
    assert "ending_equity" in constrained.columns
    constrained_metrics = compute_metrics(constrained)
    assert constrained_metrics["total_pnl_yen"] == -10_000
    assert constrained_metrics["ending_equity_yen"] == 2_990_000
    assert constrained_metrics["avg_entry_amount_yen"] == 1_000_000
    assert constrained_metrics["max_drawdown_pct"] < 0

    # 9) 空データ安全
    assert compute_metrics(pd.DataFrame())["n_trades"] == 0
    assert "note" in run_analyses(pd.DataFrame())

    # 10) _entry_score: #4の発見に沿った単調性
    s_near = _entry_score(1.0, 5e8, 1.2, 2.0, True)
    s_far = _entry_score(12.0, 5e8, 1.2, 2.0, True)
    assert s_near > s_far                       # 高値に近い方が高い
    s_liquid = _entry_score(2.0, 30e8, 1.2, 2.0, True)
    s_illiquid = _entry_score(2.0, 1e8, 1.2, 2.0, True)
    assert s_liquid > s_illiquid                # 流動性が高い方が高い
    s_shallow = _entry_score(2.0, 5e8, 1.2, 1.0, True)
    s_extended = _entry_score(2.0, 5e8, 1.2, 12.0, True)
    assert s_shallow > s_extended               # 浅い押し（MA25乖離小）の方が高い
    assert _entry_score(2.0, 5e8, 1.2, 2.0, True) > _entry_score(2.0, 5e8, 1.2, 2.0, False)  # PO加点

    # 10) exit_mode: timeout は途中手仕舞いせずタイムアウト / ma25 は終値<MA25で即手仕舞い
    flat = pd.bdate_range("2023-01-01", periods=12)
    df_to = pd.DataFrame({"Open": 1000.0, "High": 1005.0, "Low": 998.0, "Close": 1000.0,
                          "ma25": 950.0}, index=flat)  # MA25は株価より下＝割れていない
    r_to = simulate_trade(df_to, 0, BTParams(exit_mode="timeout", timeout_bdays=8))
    assert r_to is not None and r_to["exit_reason"] == "timeout"
    df_ma = pd.DataFrame({"Open": 1000.0, "High": 1005.0, "Low": 998.0, "Close": 1000.0,
                          "ma25": 1050.0}, index=flat)  # 終値<MA25
    r_ma = simulate_trade(df_ma, 0, BTParams(exit_mode="ma25", timeout_bdays=8))
    assert r_ma is not None and r_ma["exit_reason"] == "ma25_exit"

    # 11) 3枠選択: 同日競合は売買代金が大きい順に採用され selection_rank/reason が付く
    sel = pd.DataFrame([
        {"code": "A", "name": "A", "entry_date": "2024-02-01", "exit_date": "2024-02-09",
         "entry_price": 1000, "exit_price": 1010, "pnl_pct": 1, "win": True, "exit_idx": 9,
         "turnover_20d_avg": 5e8, "dist_to_ma25_pct": 3.0, "score": 50},
        {"code": "B", "name": "B", "entry_date": "2024-02-01", "exit_date": "2024-02-09",
         "entry_price": 1000, "exit_price": 1010, "pnl_pct": 1, "win": True, "exit_idx": 9,
         "turnover_20d_avg": 9e8, "dist_to_ma25_pct": 4.0, "score": 40},
        {"code": "C", "name": "C", "entry_date": "2024-02-01", "exit_date": "2024-02-09",
         "entry_price": 1000, "exit_price": 1010, "pnl_pct": 1, "win": True, "exit_idx": 9,
         "turnover_20d_avg": 2e8, "dist_to_ma25_pct": 1.0, "score": 90},
        {"code": "D", "name": "D", "entry_date": "2024-02-01", "exit_date": "2024-02-09",
         "entry_price": 1000, "exit_price": 1010, "pnl_pct": 1, "win": True, "exit_idx": 9,
         "turnover_20d_avg": 7e8, "dist_to_ma25_pct": 2.0, "score": 60},
    ])
    sc = apply_portfolio_constraints(sel)
    assert set(sc["code"]) == {"B", "D", "A"}              # 売買代金 上位3（B9>D7>A5、C2は落選）
    top = sc[sc["selection_rank"] == 1].iloc[0]
    assert top["code"] == "B"                              # 第1キー=売買代金最大
    assert "売買代金" in str(top["selection_reason"])

    # 12) キャッシュ / チェックポイント / marketcap キャッシュ（ファイルI/O・通信不要）
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        sample = pd.DataFrame({"Open": [1, 2], "High": [1, 2], "Low": [1, 2],
                               "Close": [1, 2], "Volume": [10, 20]})
        cpath = tdp / "x__6y.parquet"
        _save_ohlc_cache(sample, cpath)
        back = _read_ohlc_cache(cpath)
        assert back is not None and len(back) == 2
        ck = tdp / "ckpt.jsonl"
        _append_checkpoint("7203.T", [{"code": "7203", "pnl_pct": 3.0, "bad": float("nan")}], ck)
        _append_checkpoint("6758.T", [], ck)
        done, tr = _load_checkpoint(ck)
        assert done == {"7203.T", "6758.T"} and len(tr) == 1 and tr[0]["bad"] is None
        meta = _build_run_meta(5, None, BTParams(timeout_bdays=20), PortfolioParams(), type("U", (), {"markets": ("prime", "growth")})())
        rh = _run_hash(meta)
        cp, mp = _checkpoint_paths(rh)
        cp = tdp / cp.name
        mp = tdp / mp.name
        old_meta = {
            "schema": 1,
            "years": 5,
            "period": "6y",
            "limit": "ALL",
            "params": {
                "timeout_bdays": 20,
                "exit_mode": "trail",
                "trail_giveback": 0.08,
                "stop_loss_pct": 0.07,
                "min_turnover_20d": 100000000.0,
                "min_ma25_gap_pct": None,
                "max_ma25_gap_pct": None,
                "min_52w_dist_pct": None,
                "max_52w_dist_pct": None,
                "min_volume_ratio_5d_20d": None,
                "max_volume_ratio_5d_20d": None,
            },
            "portfolio": {
                "initial_capital": 3000000.0,
                "slot_capital": 1000000.0,
                "max_positions": 3,
                "round_lot": 100,
            },
            "universe": {
                "markets": ["prime", "growth"],
                "source": "jpx_listed",
            },
        }
        _write_checkpoint_meta(old_meta, mp)
        _validate_checkpoint_meta(meta, mp)
        changed = _build_run_meta(5, None, BTParams(timeout_bdays=40), PortfolioParams(), type("U", (), {"markets": ("prime", "growth")})())
        changed_filter = _build_run_meta(5, None, BTParams(max_ma25_gap_pct=7), PortfolioParams(), type("U", (), {"markets": ("prime", "growth")})())
        changed_sector_filter = _build_run_meta(5, None, BTParams(electric_volume_min=1.1), PortfolioParams(), type("U", (), {"markets": ("prime", "growth")})())
        changed_selection_rule = _build_run_meta(5, None, BTParams(selection_rule="ma25_first"), PortfolioParams(), type("U", (), {"markets": ("prime", "growth")})())
        assert _run_hash(meta) != _run_hash(changed_filter)
        assert _run_hash(meta) != _run_hash(changed_sector_filter)
        assert _run_hash(meta) != _run_hash(changed_selection_rule)
        try:
            _validate_checkpoint_meta(changed, mp)
            raise AssertionError("checkpoint meta mismatch must fail")
        except RuntimeError as exc:
            assert "checkpoint meta mismatch" in str(exc)
        mcp = tdp / "mc.json"
        _save_marketcap_cache({"7203.T": 3.5e13, "6758.T": None}, mcp)
        loaded = _load_marketcap_cache(mcp)
        assert loaded["7203.T"] == 3.5e13 and loaded["6758.T"] is None

    print("backtest self-test: OK")


def main() -> None:
    p = argparse.ArgumentParser(description="統合戦略バックテスト（過去5年）")
    p.add_argument("--self-test", action="store_true", help="純関数の検証（通信不要）")
    p.add_argument("--run", action="store_true", help="Mac: 実データでバックテスト")
    p.add_argument("--years", type=int, default=5)
    p.add_argument("--limit", type=int, default=None, help="先頭N銘柄で動作確認")
    p.add_argument("--timeout-bdays", type=int, default=20, help="最長保有営業日（20/30/40/60で比較）")
    p.add_argument("--exit-mode", choices=["timeout", "ma25", "trail"], default="trail",
                   help="手仕舞いモード: timeout=損切り+タイムアウトのみ / ma25=25日線終値割れ / trail=トレーリング")
    p.add_argument("--trail-giveback", type=float, default=0.08,
                   help="trailモードの高値からの戻し幅（0.10/0.15で比較）")
    p.add_argument("--min-ma25-gap-pct", type=float, default=None, help="MA25乖離の下限%")
    p.add_argument("--max-ma25-gap-pct", type=float, default=None, help="MA25乖離の上限%")
    p.add_argument("--min-52w-dist-pct", type=float, default=None, help="52週高値距離の下限%")
    p.add_argument("--max-52w-dist-pct", type=float, default=None, help="52週高値距離の上限%")
    p.add_argument("--min-volume-ratio-5d-20d", type=float, default=None, help="5日平均出来高/20日平均出来高の下限")
    p.add_argument("--max-volume-ratio-5d-20d", type=float, default=None, help="5日平均出来高/20日平均出来高の上限")
    p.add_argument("--electric-volume-min", type=float, default=None, help="電気機器セクターだけに適用する出来高倍率下限")
    p.add_argument("--selection-rule", choices=["current", "ma25_first", "score_first"], default="current",
                   help="同日候補の選抜順")
    p.add_argument("--min-turnover-20d", type=float, default=100_000_000.0, help="20日平均売買代金の下限")
    p.add_argument("--no-cache", action="store_true", help="OHLCキャッシュを使わない")
    p.add_argument("--resume", action="store_true", help="checkpointから途中再開")
    p.add_argument("--no-marketcap", action="store_true", help="時価総額取得を省略（高速化）")
    p.add_argument("--batch-size", type=int, default=0, help=">0でyfinanceバッチ先読み")
    args = p.parse_args()

    if args.self_test:
        self_test()
        return
    if args.run:
        run(years=args.years, limit=args.limit,
            params=BTParams(timeout_bdays=args.timeout_bdays,
                            exit_mode=args.exit_mode,
                            trail_giveback=args.trail_giveback,
                            min_turnover_20d=args.min_turnover_20d,
                            min_ma25_gap_pct=args.min_ma25_gap_pct,
                            max_ma25_gap_pct=args.max_ma25_gap_pct,
                            min_52w_dist_pct=args.min_52w_dist_pct,
                            max_52w_dist_pct=args.max_52w_dist_pct,
                            min_volume_ratio_5d_20d=args.min_volume_ratio_5d_20d,
                            max_volume_ratio_5d_20d=args.max_volume_ratio_5d_20d,
                            electric_volume_min=args.electric_volume_min,
                            selection_rule=args.selection_rule),
            use_cache=not args.no_cache,
            resume=args.resume,
            no_marketcap=args.no_marketcap,
            batch_size=args.batch_size)
        return
    p.print_help()


if __name__ == "__main__":
    main()
