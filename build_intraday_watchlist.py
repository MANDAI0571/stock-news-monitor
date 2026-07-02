"""日中15分監視用の軽量ウォッチリスト生成（前日EODの結果だけで作る＝新規通信ゼロ）。

目的: ザラ場中の intraday_high_alert.py が全銘柄(約3,567)ではなく、
前日の run_screening.py が出した outputs/screening_result*.csv から選んだ
200〜500銘柄だけを監視できるようにする。SPEC_intraday_watchlist.md を参照。

選定（和集合・重複除去、優先順に上限まで埋める）:
  1. 前日候補: rank in {S, A}
  2. 52週高値接近: dist_52w_high_pct <= INTRADAY_NEAR_52W_PCT（既定 5.0%）
  3. 直近高値更新: high_type が新高値/ブレイク系
  4. 出来高増加: volume_ratio_5d_20d >= INTRADAY_VOL_MULT（既定 1.5、列があれば）
  5. 売買代金上位: turnover_20d 降順の上位 INTRADAY_TURNOVER_TOP 件（既定 200、列があれば）

出力: outputs/intraday_watchlist.csv（固定名）。
前日CSVが無い / 選定0件のときは書かない（警告のみ）＝intraday側は全銘柄にフォールバック（捏造しない）。

このスクリプトは通信しない（CSVを読むだけ）＝クラウドでも動く。
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs"
WATCHLIST_NAME = "intraday_watchlist.csv"

# 直近高値更新/ブレイクとみなす high_type（scanner.highs の分類に対応）。
HIGH_BREAK_TYPES = {
    "52W_NEW_HIGH",
    "RECENT_NEW_HIGH",
    "SWING_HIGH_BREAK",
    "YEAR_NEW_HIGH",
}

WATCHLIST_COLUMNS = [
    "code",
    "name",
    "market",
    "reason",
    "score",
    "rank",
    "dist_52w_high_pct",
    "turnover_20d",
    "volume_ratio_5d_20d",
]


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _norm_code(raw: object) -> str:
    """数字のみ→前ゼロ4桁、英数字4桁(285A等)→そのまま保持。空は空文字。"""
    s = str(raw).strip().upper()
    if re.fullmatch(r"[0-9A-Z]{4}", s):
        return s
    digits = re.sub(r"\D", "", s)
    return digits.zfill(4) if digits else ""


def select_watchlist(
    df: pd.DataFrame,
    *,
    max_symbols: int = 300,
    near_pct: float = 5.0,
    turnover_top: int = 200,
    vol_mult: float = 1.5,
    ranks: tuple[str, ...] = ("S", "A"),
) -> pd.DataFrame:
    """前日EODのDataFrameから日中監視対象を選ぶ純関数（通信なし＝テスト可）。

    優先順(tier): 1)前日候補(S/A) 2)52週高値接近 3)直近高値更新 4)出来高増加 5)売買代金上位。
    上限 max_symbols(200〜500にクリップ) まで、tierの高い順に埋める。reasonは該当した全条件。
    """
    cap = max(200, min(500, int(max_symbols)))
    empty = pd.DataFrame(columns=WATCHLIST_COLUMNS)
    if df is None or len(df) == 0 or "code" not in df.columns:
        return empty

    d = df.copy()
    d["code"] = d["code"].map(_norm_code)
    d = d[d["code"] != ""].drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)
    if d.empty:
        return empty

    for col in ("score", "dist_52w_high_pct", "turnover_20d", "volume_ratio_5d_20d"):
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")

    rank_col = d["rank"].astype(str).str.strip() if "rank" in d.columns else pd.Series([""] * len(d), index=d.index)
    high_col = d["high_type"].astype(str).str.strip() if "high_type" in d.columns else pd.Series([""] * len(d), index=d.index)

    # 各条件のマスク（列が無い条件はスキップ）。
    m_rank = rank_col.isin(ranks)
    m_near = d["dist_52w_high_pct"].le(near_pct) if "dist_52w_high_pct" in d.columns else pd.Series(False, index=d.index)
    m_high = high_col.isin(HIGH_BREAK_TYPES)
    m_vol = d["volume_ratio_5d_20d"].ge(vol_mult) if "volume_ratio_5d_20d" in d.columns else pd.Series(False, index=d.index)

    # 売買代金上位: turnover_20d 降順の上位N。
    m_turn = pd.Series(False, index=d.index)
    if "turnover_20d" in d.columns and d["turnover_20d"].notna().any():
        top_idx = d["turnover_20d"].fillna(-1).sort_values(ascending=False).head(max(0, int(turnover_top))).index
        m_turn.loc[top_idx] = True

    reasons: list[str] = []
    tiers: list[int] = []
    for i in d.index:
        tags: list[str] = []
        tier = 99
        if bool(m_rank.get(i, False)):
            tags.append(f"{rank_col.get(i, '')}候補")
            tier = min(tier, 1)
        if bool(m_near.get(i, False)):
            dist = d.at[i, "dist_52w_high_pct"] if "dist_52w_high_pct" in d.columns else None
            tags.append("52週高値接近" if pd.isna(dist) else f"52週-{float(dist):.1f}%")
            tier = min(tier, 2)
        if bool(m_high.get(i, False)):
            tags.append("直近高値更新")
            tier = min(tier, 3)
        if bool(m_vol.get(i, False)):
            vr = d.at[i, "volume_ratio_5d_20d"] if "volume_ratio_5d_20d" in d.columns else None
            tags.append("出来高増加" if pd.isna(vr) else f"出来高x{float(vr):.2f}")
            tier = min(tier, 4)
        if bool(m_turn.get(i, False)):
            tags.append("売買代金上位")
            tier = min(tier, 5)
        reasons.append(" / ".join(tags))
        tiers.append(tier)

    d["_reason"] = reasons
    d["_tier"] = tiers
    picked = d[d["_tier"] < 99].copy()
    if picked.empty:
        return empty

    sort_turn = picked["turnover_20d"] if "turnover_20d" in picked.columns else pd.Series(0.0, index=picked.index)
    sort_score = picked["score"] if "score" in picked.columns else pd.Series(0.0, index=picked.index)
    picked = picked.assign(_score=sort_score.fillna(0.0), _turn=sort_turn.fillna(0.0))
    picked = picked.sort_values(by=["_tier", "_score", "_turn"], ascending=[True, False, False]).head(cap)

    out = pd.DataFrame({
        "code": picked["code"].values,
        "name": picked["name"].values if "name" in picked.columns else "",
        "market": picked["market"].values if "market" in picked.columns else "",
        "reason": picked["_reason"].values,
        "score": picked["score"].values if "score" in picked.columns else "",
        "rank": picked["rank"].values if "rank" in picked.columns else "",
        "dist_52w_high_pct": picked["dist_52w_high_pct"].values if "dist_52w_high_pct" in picked.columns else "",
        "turnover_20d": picked["turnover_20d"].values if "turnover_20d" in picked.columns else "",
        "volume_ratio_5d_20d": picked["volume_ratio_5d_20d"].values if "volume_ratio_5d_20d" in picked.columns else "",
    })
    return out.reset_index(drop=True)


def find_latest_screening(output_dir: Path) -> Path | None:
    """outputs/ 配下から最新の screening_result*.csv を探す。
    固定名 screening_result.csv を最優先、無ければタイムスタンプ付きの最新。
    アーティファクト展開でサブフォルダに入るケースに備えて再帰探索する。"""
    output_dir = Path(output_dir)
    fixed = output_dir / "screening_result.csv"
    if fixed.exists() and fixed.stat().st_size > 0:
        return fixed
    candidates = [p for p in output_dir.rglob("screening_result*.csv") if p.stat().st_size > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def build(output_dir: Path, screening_path: Path | None = None) -> Path | None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    src = screening_path or find_latest_screening(output_dir)
    if src is None or not Path(src).exists():
        print("WARNING: screening_result*.csv が見つかりません。ウォッチリストは作らず、intradayは全銘柄にフォールバックします。", flush=True)
        return None

    df = pd.read_csv(src, dtype={"code": str}, encoding="utf-8-sig")
    watchlist = select_watchlist(
        df,
        max_symbols=_env_int("INTRADAY_WATCH_MAX", 300),
        near_pct=_env_float("INTRADAY_NEAR_52W_PCT", 5.0),
        turnover_top=_env_int("INTRADAY_TURNOVER_TOP", 200),
        vol_mult=_env_float("INTRADAY_VOL_MULT", 1.5),
    )
    if watchlist.empty:
        print(f"WARNING: 選定0件（source={src}）。ウォッチリストは書かず、intradayは全銘柄にフォールバックします。", flush=True)
        return None

    path = output_dir / WATCHLIST_NAME
    watchlist.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"保存しました（固定名）: {path}  銘柄数={len(watchlist)}  source={src}", flush=True)
    return path


def _self_test() -> int:
    print("build_intraday_watchlist self-test ...")
    rows = []
    # S/A候補
    rows.append({"code": "7203", "name": "トヨタ", "market": "東証プライム", "rank": "S", "score": 90,
                 "dist_52w_high_pct": 0.5, "turnover_20d": 5e10, "high_type": "52W_NEW_HIGH", "volume_ratio_5d_20d": 2.0})
    rows.append({"code": "6758", "name": "ソニー", "market": "東証プライム", "rank": "A", "score": 70,
                 "dist_52w_high_pct": 12.0, "turnover_20d": 3e10, "high_type": "OTHER", "volume_ratio_5d_20d": 1.0})
    # 52週高値接近のみ（rankは見送り）
    rows.append({"code": "285A", "name": "キオクシア", "market": "東証プライム", "rank": "見送り", "score": 40,
                 "dist_52w_high_pct": 2.0, "turnover_20d": 1e10, "high_type": "52W_NEAR_HIGH", "volume_ratio_5d_20d": 1.1})
    # 直近高値更新のみ
    rows.append({"code": "9984", "name": "SBG", "market": "東証プライム", "rank": "見送り", "score": 30,
                 "dist_52w_high_pct": 20.0, "turnover_20d": 8e9, "high_type": "RECENT_NEW_HIGH", "volume_ratio_5d_20d": 1.0})
    # 出来高増加のみ
    rows.append({"code": "1234", "name": "テスト増出来", "market": "東証スタンダード", "rank": "見送り", "score": 10,
                 "dist_52w_high_pct": 30.0, "turnover_20d": 2e8, "high_type": "OTHER", "volume_ratio_5d_20d": 2.5})
    # どの条件にも該当しない（除外される）
    rows.append({"code": "0000", "name": "除外", "market": "東証スタンダード", "rank": "見送り", "score": 5,
                 "dist_52w_high_pct": 50.0, "turnover_20d": 1e7, "high_type": "OTHER", "volume_ratio_5d_20d": 0.8})
    df = pd.DataFrame(rows)

    # turnover_top はデータ件数より小さくして「売買代金上位」で全件拾わないようにする（テスト用）。
    wl = select_watchlist(df, max_symbols=300, near_pct=5.0, turnover_top=2, vol_mult=1.5)
    codes = list(wl["code"])
    assert "7203" in codes and "6758" in codes, codes
    assert "285A" in codes, "英数字コードが落ちた"
    assert "9984" in codes and "1234" in codes, codes
    assert "0000" not in codes, "非該当が混入した"
    # 優先順: S候補(7203)が先頭
    assert codes[0] == "7203", codes
    # reason に条件が入る
    r0 = wl[wl["code"] == "7203"]["reason"].iloc[0]
    assert "S候補" in r0, r0
    r_near = wl[wl["code"] == "285A"]["reason"].iloc[0]
    assert "52週" in r_near, r_near

    # 上限クリップ（下限200）
    big = pd.DataFrame([
        {"code": f"{1000+i}", "name": f"n{i}", "market": "東証プライム", "rank": "S", "score": 100 - i,
         "dist_52w_high_pct": 0.1, "turnover_20d": 1e9, "high_type": "52W_NEW_HIGH", "volume_ratio_5d_20d": 1.6}
        for i in range(600)
    ])
    wl_big = select_watchlist(big, max_symbols=1000)  # 1000→500にクリップ
    assert len(wl_big) == 500, len(wl_big)
    wl_small_cap = select_watchlist(big, max_symbols=50)  # 50→200にクリップ
    assert len(wl_small_cap) == 200, len(wl_small_cap)

    # 空入力 → 空
    assert select_watchlist(pd.DataFrame()).empty
    # turnover列が無くても落ちない
    df2 = df.drop(columns=["turnover_20d", "volume_ratio_5d_20d"])
    wl2 = select_watchlist(df2)
    assert "7203" in list(wl2["code"]), "turnover列無しで落ちた"

    print("SELF_TEST_PASS")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="日中監視用ウォッチリスト生成（前日EODから200〜500銘柄）")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="出力先（screening_result*.csv の場所）")
    parser.add_argument("--screening", default=None, help="使用する screening_result CSV のパス（省略時は自動検出）")
    parser.add_argument("--self-test", action="store_true", help="純粋ロジックの自己テスト（ネット不要）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        return _self_test()
    path = build(Path(args.output_dir), Path(args.screening) if args.screening else None)
    return 0 if path is not None else 0  # フォールバック前提のため常に 0（ワークフローを止めない）


if __name__ == "__main__":
    raise SystemExit(main())
