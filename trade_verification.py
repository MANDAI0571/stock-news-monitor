from __future__ import annotations

"""
trade_verification.py — BUY3銘柄の自動検証システム

decision_engine.py が出した BUY / WATCH を毎日 data/trade_history.csv に記録し、
翌営業日以降の実際の値動きで「もし買っていたらどうなったか」を検証する。

やること:
  1. record : outputs/decision_result.csv の BUY / WATCH 銘柄をその日のシグナルとして保存
              （WATCH も保存するのは BUY との成績比較のため）
  2. update : yfinance で株価を取得し、未確定シグナルを評価
              - 翌営業日の寄付き(entry_open)・引け(entry_close)
              - 2/3/5/10営業日後の終値
              - 期間内の最大上昇率・最大下落率
              - 損切り(-7%) / 利確(+15%) 到達判定（寄付き基準・先に触れた方で決済）
              - 10営業日経過なら引けで時間切れ決済
  3. report : 勝率・平均損益・プロフィットファクター・最大ドローダウンを
              BUY / WATCH 別に集計し outputs/performance_report.md を更新

ルールは decision_engine.py / paper_portfolio_discipline.py と同一:
  損切 -7% / 利確 +15% / 保有 最大10営業日。

判定できないものは判定しない（データが無い日は OPEN / PENDING のまま。捏造しない）。
yfinance は update 時にだけ遅延 import する（record / report / テストはオフラインで動く）。
"""

import argparse
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pandas as pd

from decision_engine import STOP_LOSS_PCT, TAKE_PROFIT_PCT, HOLD_MAX_BDAYS

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DECISION = PROJECT_ROOT / "outputs" / "decision_result.csv"
DEFAULT_HISTORY = PROJECT_ROOT / "data" / "trade_history.csv"
DEFAULT_REPORT = PROJECT_ROOT / "outputs" / "performance_report.md"

HORIZONS = (2, 3, 5, 10)  # 終値を記録する営業日オフセット（翌営業日=1日目）

HISTORY_COLUMNS: List[str] = [
    "signal_date", "code", "name", "decision", "rank", "score", "confidence",
    "screen_type", "strategy", "high_type", "high_label",
    "current_price", "lot_value_100", "dist_52w_high_pct", "dist_25ma_pct", "dist_200ma_pct",
    "volume_ratio_5d_20d", "turnover_20d",
    "buy_reason",
    "near_high", "vol_up",              # entry_reason から抽出した条件フラグ（検証用）
    "entry_date", "entry_open", "entry_close",
    "close_d2", "close_d3", "close_d5", "close_d10",
    "max_gain_pct", "max_drop_pct",
    "stop_hit", "tp_hit", "first_hit",
    "exit_date", "exit_price", "exit_return_pct", "exit_reason",
    "status",                            # PENDING(翌営業日待ち) / OPEN(観測中) / CLOSED(確定)
    "last_updated",
]

# 型: fetcher(code) -> OHLC DataFrame（index=DatetimeIndex, 列 Open/High/Low/Close）
PriceFetcher = Callable[[str], Optional[pd.DataFrame]]


# ─────────────────────────────────────────
# ユーティリティ
# ─────────────────────────────────────────
def _safe_text(v) -> str:
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    t = str(v).strip()
    return "" if t.lower() in {"nan", "none", "<na>", "nat"} else t


def _num(v) -> Optional[float]:
    t = _safe_text(v).replace(",", "").replace("%", "").replace("円", "")
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _code(v) -> str:
    t = _safe_text(v)
    return t[:-2] if t.endswith(".0") else t


def _parse_date(v) -> Optional[date]:
    t = _safe_text(v)
    if not t:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def load_history(path: Path = DEFAULT_HISTORY) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    df = pd.read_csv(path, dtype=str).fillna("")
    for col in HISTORY_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[HISTORY_COLUMNS]


def save_history(df: pd.DataFrame, path: Path = DEFAULT_HISTORY) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df[HISTORY_COLUMNS].to_csv(path, index=False, encoding="utf-8-sig")


# ─────────────────────────────────────────
# 1) record — 当日の BUY / WATCH を trade_history.csv に追記
# ─────────────────────────────────────────
def record_signals(decision_path: Path = DEFAULT_DECISION,
                   history_path: Path = DEFAULT_HISTORY,
                   run_date: Optional[str] = None) -> Dict[str, int]:
    run_date = run_date or date.today().isoformat()
    decision_path = Path(decision_path)
    history = load_history(history_path)

    if not decision_path.exists():
        save_history(history, history_path)
        return {"appended": 0, "total": len(history)}

    dec = pd.read_csv(decision_path, dtype=str).fillna("")
    dec = dec[dec.get("decision", "").isin(["BUY", "WATCH"])]

    existing = set(zip(history["signal_date"].astype(str), history["code"].astype(str)))
    rows: List[Dict[str, object]] = []
    for _, r in dec.iterrows():
        code = _code(r.get("code"))
        if not code or (run_date, code) in existing:
            continue
        reason = _safe_text(r.get("entry_reason")) + " " + _safe_text(r.get("skip_reason"))
        rows.append({
            "signal_date": run_date,
            "code": code,
            "name": _safe_text(r.get("name")),
            "decision": _safe_text(r.get("decision")),
            "rank": _safe_text(r.get("rank")),
            "score": _safe_text(r.get("score")),
            "confidence": _safe_text(r.get("confidence")),
            "screen_type": _safe_text(r.get("screen_type")),
            "strategy": _safe_text(r.get("strategy")),
            "high_type": _safe_text(r.get("high_type")),
            "high_label": _safe_text(r.get("high_label")),
            "current_price": _safe_text(r.get("current_price")),
            "lot_value_100": _safe_text(r.get("lot_value_100")),
            "dist_52w_high_pct": _safe_text(r.get("dist_52w_high_pct")),
            "dist_25ma_pct": _safe_text(r.get("dist_25ma_pct")),
            "dist_200ma_pct": _safe_text(r.get("dist_200ma_pct")),
            "volume_ratio_5d_20d": _safe_text(r.get("volume_ratio_5d_20d")),
            "turnover_20d": _safe_text(r.get("turnover_20d")),
            "buy_reason": _safe_text(r.get("buy_reason")),
            "near_high": "（近い）" in reason,
            "vol_up": "（増加）" in reason,
            "status": "PENDING",
            "last_updated": run_date,
        })

    if rows:
        new = pd.DataFrame(rows)
        for col in HISTORY_COLUMNS:
            if col not in new.columns:
                new[col] = ""
        history = pd.concat([history[HISTORY_COLUMNS], new[HISTORY_COLUMNS]],
                            ignore_index=True)
        history = history.drop_duplicates(subset=["signal_date", "code"], keep="first")
        history = history.sort_values(["signal_date", "decision", "code"],
                                      kind="stable").reset_index(drop=True)
    save_history(history, history_path)
    return {"appended": len(rows), "total": len(history)}


# ─────────────────────────────────────────
# 2) update — 株価で評価して埋める
# ─────────────────────────────────────────
def _default_fetcher(code: str) -> Optional[pd.DataFrame]:
    """yfinance で東証銘柄の日足を取得する（update 時のみネットワークを使う）。"""
    try:
        import yfinance as yf
        df = yf.Ticker(f"{code}.T").history(period="3mo", auto_adjust=False)
        if df is None or df.empty:
            return None
        df = df[["Open", "High", "Low", "Close"]].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df
    except Exception:
        return None


def evaluate_signal(signal_date: date, prices: pd.DataFrame) -> Dict[str, object]:
    """シグナル日以降の値動きから1シグナル分の検証結果を計算する。

    翌営業日の寄付きで買った想定。損切/利確はザラ場の高安で判定し、
    同日に両方触れた場合は保守的に損切りを先とみなす。
    """
    out: Dict[str, object] = {"status": "PENDING"}
    if prices is None or prices.empty:
        return out
    after = prices[prices.index.normalize() > pd.Timestamp(signal_date)].sort_index()
    if after.empty:
        return out  # まだ翌営業日が来ていない

    day1 = after.iloc[0]
    entry_open = float(day1["Open"])
    if not entry_open or math.isnan(entry_open) or entry_open <= 0:
        return out
    out["entry_date"] = after.index[0].date().isoformat()
    out["entry_open"] = round(entry_open, 1)
    out["entry_close"] = round(float(day1["Close"]), 1)

    window = after.iloc[:HOLD_MAX_BDAYS]
    for n in HORIZONS:
        if len(after) >= n:
            out[f"close_d{n}"] = round(float(after.iloc[n - 1]["Close"]), 1)

    out["max_gain_pct"] = round((float(window["High"].max()) / entry_open - 1) * 100, 2)
    out["max_drop_pct"] = round((float(window["Low"].min()) / entry_open - 1) * 100, 2)

    stop_px = entry_open * (1 - STOP_LOSS_PCT)
    tp_px = entry_open * (1 + TAKE_PROFIT_PCT)
    stop_hit = tp_hit = False
    first_hit = ""
    exit_date = exit_price = exit_return = exit_reason = None

    for ts, row in window.iterrows():
        day_stop = float(row["Low"]) <= stop_px
        day_tp = float(row["High"]) >= tp_px
        stop_hit = stop_hit or day_stop
        tp_hit = tp_hit or day_tp
        if not first_hit and (day_stop or day_tp):
            # 同日両到達はザラ場の順序が分からないため保守的に損切り扱い
            first_hit = "STOP" if day_stop else "TP"
            exit_date = ts.date().isoformat()
            if first_hit == "STOP":
                exit_price, exit_return, exit_reason = stop_px, -STOP_LOSS_PCT * 100, "損切り(-7%)"
            else:
                exit_price, exit_return, exit_reason = tp_px, TAKE_PROFIT_PCT * 100, "利確(+15%)"

    out["stop_hit"] = stop_hit
    out["tp_hit"] = tp_hit
    out["first_hit"] = first_hit

    if first_hit:
        out.update(status="CLOSED", exit_date=exit_date,
                   exit_price=round(float(exit_price), 1),
                   exit_return_pct=round(float(exit_return), 2),
                   exit_reason=exit_reason)
    elif len(after) >= HOLD_MAX_BDAYS:
        last = after.iloc[HOLD_MAX_BDAYS - 1]
        out.update(status="CLOSED",
                   exit_date=after.index[HOLD_MAX_BDAYS - 1].date().isoformat(),
                   exit_price=round(float(last["Close"]), 1),
                   exit_return_pct=round((float(last["Close"]) / entry_open - 1) * 100, 2),
                   exit_reason=f"時間切れ({HOLD_MAX_BDAYS}営業日)")
    else:
        out["status"] = "OPEN"
    return out


def update_history(history_path: Path = DEFAULT_HISTORY,
                   fetcher: Optional[PriceFetcher] = None,
                   today: Optional[date] = None) -> Dict[str, int]:
    fetcher = fetcher or _default_fetcher
    today = today or date.today()
    history = load_history(history_path)
    if history.empty:
        return {"updated": 0, "closed": 0, "open": 0, "pending": 0}

    price_cache: Dict[str, Optional[pd.DataFrame]] = {}
    updated = 0
    for idx, row in history.iterrows():
        if _safe_text(row["status"]) == "CLOSED":
            continue
        sd = _parse_date(row["signal_date"])
        code = _code(row["code"])
        if sd is None or not code or sd >= today:
            continue
        if code not in price_cache:
            price_cache[code] = fetcher(code)
        prices = price_cache[code]
        if prices is None or prices.empty:
            continue
        res = evaluate_signal(sd, prices)
        for k, v in res.items():
            history.at[idx, k] = v
        history.at[idx, "last_updated"] = today.isoformat()
        updated += 1

    save_history(history, history_path)
    counts = history["status"].value_counts()
    return {"updated": updated,
            "closed": int(counts.get("CLOSED", 0)),
            "open": int(counts.get("OPEN", 0)),
            "pending": int(counts.get("PENDING", 0))}


# ─────────────────────────────────────────
# 3) report — 集計とレポート
# ─────────────────────────────────────────
def aggregate(history: pd.DataFrame) -> pd.DataFrame:
    """CLOSED トレードを decision 別に集計する（勝率・平均損益・PF・最大DD）。"""
    if history is None or history.empty:
        return pd.DataFrame()
    df = history.copy()
    df["ret"] = df["exit_return_pct"].map(_num)
    closed = df[(df["status"] == "CLOSED") & df["ret"].notna()].copy()

    rows = []
    for decision, sub in closed.groupby("decision"):
        sub = sub.sort_values(["exit_date", "code"], kind="stable")
        rets = sub["ret"].astype(float)
        wins, losses = rets[rets > 0], rets[rets <= 0]
        pf = float(wins.sum() / abs(losses.sum())) if losses.sum() < 0 else float("inf")
        cum = rets.cumsum()
        dd = float((cum.cummax() - cum).max()) if not cum.empty else 0.0
        rows.append({
            "decision": decision,
            "trades": len(rets),
            "wins": len(wins),
            "win_rate_pct": round(100 * len(wins) / len(rets), 1) if len(rets) else "",
            "avg_return_pct": round(float(rets.mean()), 2),
            "avg_win_pct": round(float(wins.mean()), 2) if len(wins) else "",
            "avg_loss_pct": round(float(losses.mean()), 2) if len(losses) else "",
            "profit_factor": (round(pf, 2) if math.isfinite(pf) else "∞"),
            "max_drawdown_pct": round(dd, 2),
            "stop_hits": int(sub["stop_hit"].astype(str).str.lower().eq("true").sum()),
            "tp_hits": int(sub["tp_hit"].astype(str).str.lower().eq("true").sum()),
        })
    return pd.DataFrame(rows)


def _fmt(v) -> str:
    return "—" if v is None or v == "" else str(v)


def _mean_of(sub: pd.DataFrame, col: str, base_col: str = "entry_open") -> str:
    base = sub[base_col].map(_num)
    val = sub[col].map(_num)
    mask = base.notna() & val.notna() & (base > 0)
    if not mask.any():
        return "—"
    pct = (val[mask] / base[mask] - 1) * 100
    return f"{pct.mean():+.2f}%"


def build_performance_report(history: pd.DataFrame,
                             today: Optional[date] = None) -> str:
    today = today or date.today()
    stats = aggregate(history)
    n_all = len(history) if history is not None else 0

    lines: List[str] = []
    lines.append(f"# BUY銘柄 検証レポート（{today.isoformat()} 更新）")
    lines.append("")
    lines.append(f"- ルール: 翌営業日寄付きで買い想定 / 損切 -{int(STOP_LOSS_PCT*100)}% / "
                 f"利確 +{int(TAKE_PROFIT_PCT*100)}% / 最大{HOLD_MAX_BDAYS}営業日で時間切れ決済")
    lines.append(f"- 記録シグナル数: {n_all}（BUY と WATCH を比較用に両方記録）")
    lines.append("")

    lines.append("## 成績サマリー（確定トレードのみ）")
    lines.append("")
    if stats.empty:
        lines.append("確定トレードがまだありません（データが貯まるまで待機。捏造しない）。")
        lines.append("")
    else:
        lines.append("| 判定 | 件数 | 勝率 | 平均損益 | 平均利益 | 平均損失 | PF | 最大DD | 損切到達 | 利確到達 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for _, s in stats.sort_values("decision").iterrows():
            lines.append(
                f"| {s['decision']} | {s['trades']} | {_fmt(s['win_rate_pct'])}% | "
                f"{s['avg_return_pct']:+.2f}% | {_fmt(s['avg_win_pct'])}% | {_fmt(s['avg_loss_pct'])}% | "
                f"{s['profit_factor']} | {s['max_drawdown_pct']:.2f}% | "
                f"{s['stop_hits']} | {s['tp_hits']} |")
        lines.append("")

    if history is not None and not history.empty:
        evaluated = history[history["entry_open"].map(_num).notna()]
        if not evaluated.empty:
            lines.append("## 保有日数別の平均リターン（寄付き比・評価済み全シグナル）")
            lines.append("")
            lines.append("| 判定 | 翌日引け | 2日後 | 3日後 | 5日後 | 10日後 | 最大上昇(平均) | 最大下落(平均) |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for decision, sub in evaluated.groupby("decision"):
                mg = sub["max_gain_pct"].map(_num).dropna()
                md = sub["max_drop_pct"].map(_num).dropna()
                lines.append(
                    f"| {decision} | {_mean_of(sub, 'entry_close')} | {_mean_of(sub, 'close_d2')} | "
                    f"{_mean_of(sub, 'close_d3')} | {_mean_of(sub, 'close_d5')} | {_mean_of(sub, 'close_d10')} | "
                    f"{(f'{mg.mean():+.2f}%' if len(mg) else '—')} | "
                    f"{(f'{md.mean():+.2f}%' if len(md) else '—')} |")
            lines.append("")

            # どの条件が利益につながったか（条件フラグ別・確定のみ）
            closed = evaluated[(evaluated["status"] == "CLOSED")].copy()
            closed["ret"] = closed["exit_return_pct"].map(_num)
            closed = closed[closed["ret"].notna()]
            if not closed.empty:
                lines.append("## 条件別の平均損益（確定トレード・スコア/フィルター改善用）")
                lines.append("")
                lines.append("| 条件 | 該当 | 平均損益 | 非該当 | 平均損益 |")
                lines.append("|---|---|---|---|---|")
                for col, label in (("near_high", "52週高値に近い"), ("vol_up", "出来高増加")):
                    flag = closed[col].astype(str).str.lower().eq("true")
                    yes, no = closed[flag]["ret"], closed[~flag]["ret"]
                    lines.append(
                        f"| {label} | {len(yes)}件 | "
                        f"{(f'{yes.mean():+.2f}%' if len(yes) else '—')} | {len(no)}件 | "
                        f"{(f'{no.mean():+.2f}%' if len(no) else '—')} |")
                lines.append("")

                for col, title in (("strategy", "戦略別"), ("high_type", "高値タイプ別")):
                    if col in closed.columns and closed[col].astype(str).str.strip().ne("").any():
                        lines.append(f"## {title}の平均損益（確定トレード）")
                        lines.append("")
                        lines.append("| 分類 | 件数 | 勝率 | 平均損益 |")
                        lines.append("|---|---:|---:|---:|")
                        for key, sub in closed.groupby(closed[col].fillna("").astype(str)):
                            if not key:
                                continue
                            ret = sub["ret"].astype(float)
                            wins = int((ret > 0).sum())
                            lines.append(
                                f"| {key} | {len(sub)} | {100 * wins / len(sub):.1f}% | {ret.mean():+.2f}% |"
                            )
                        lines.append("")

        # 直近の確定トレード
        closed_all = history[history["status"] == "CLOSED"]
        if not closed_all.empty:
            lines.append("## 直近の確定トレード（新しい順・最大20件）")
            lines.append("")
            lines.append("| シグナル日 | コード | 銘柄 | 判定 | 建値(寄) | 決済 | 損益 | 理由 |")
            lines.append("|---|---|---|---|---|---|---|---|")
            recent = closed_all.sort_values("exit_date", ascending=False).head(20)
            for _, r in recent.iterrows():
                ret = _num(r["exit_return_pct"])
                lines.append(
                    f"| {r['signal_date']} | {r['code']} | {r['name']} | {r['decision']} | "
                    f"{_fmt(r['entry_open'])} | {_fmt(r['exit_price'])} | "
                    f"{(f'{ret:+.2f}%' if ret is not None else '—')} | {_fmt(r['exit_reason'])} |")
            lines.append("")

    lines.append("---")
    lines.append("集計は「翌営業日寄付きで買った場合」の机上検証です（実際の約定・手数料は含みません）。")
    lines.append("")
    return "\n".join(lines)


def write_report(history_path: Path = DEFAULT_HISTORY,
                 report_path: Path = DEFAULT_REPORT,
                 today: Optional[date] = None) -> Path:
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    history = load_history(history_path)
    report_path.write_text(build_performance_report(history, today=today),
                           encoding="utf-8")
    return report_path


# ─────────────────────────────────────────
# CLI
# ─────────────────────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="BUY銘柄の自動検証（記録→株価評価→レポート）")
    p.add_argument("--decision", default=str(DEFAULT_DECISION))
    p.add_argument("--history", default=str(DEFAULT_HISTORY))
    p.add_argument("--report-path", default=str(DEFAULT_REPORT))
    p.add_argument("--date", default=None, help="記録日 YYYY-MM-DD（省略時は今日）")
    p.add_argument("--record", action="store_true", help="当日のBUY/WATCHを記録")
    p.add_argument("--update", action="store_true", help="株価を取得して評価")
    p.add_argument("--report", action="store_true", help="レポートを更新")
    args = p.parse_args(argv)

    do_all = not (args.record or args.update or args.report)
    history_path = Path(args.history)

    if args.record or do_all:
        rec = record_signals(Path(args.decision), history_path, run_date=args.date)
        print(f"record: appended={rec['appended']} total={rec['total']}")
    if args.update or do_all:
        upd = update_history(history_path)
        print(f"update: updated={upd['updated']} closed={upd['closed']} "
              f"open={upd['open']} pending={upd['pending']}")
    if args.report or do_all:
        path = write_report(history_path, Path(args.report_path))
        print(f"report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
