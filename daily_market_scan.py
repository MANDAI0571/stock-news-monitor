"""日本株のランク記事に足す4つの節（2026-11-13 持ち主の「これやって」のメモ）。

  - PPP: 「PPP 移動平均線が短期、中期、長期の全てが（上向きにそろった）状態」
        5日線 > 25日線 > 75日線 で、3本とも前の日より上向き。記事には「本日そろった銘柄」を出す。
  - 下げトレンドの十字線: 「下げトレンドで十字線 スクリーニング」
        終値 < 25日線 < 75日線、25日線が5本前より低い（出来高急増×下降と同じ下降の決まり）で、
        その日の足が十字線（始値と終値の差が、高値と安値の幅の1割以下。幅は終値の1%以上）。
  - 盛り上がっている業種と業種No.1: 「テーマは何か？ 出来高は上がっているか？ セクター全体が盛り上がっている
        木野内栄治 業界No.1の企業」。業種（東証33業種）ごとに、5日の値上がり・その日に上げた銘柄の割合・
        売買代金の増え方（その日 ÷ 前の20日の平均）を出し、業種の時価総額1位の会社を添える。
        テーマ（ニュースの話題）までは判定しない（値段と出来高だけ）。
  - 循環物色: 「循環物色はあるか？」 60日で出遅れていた業種が5日で上位に来ているか（とその逆）を並べる。断定しない。

どの一覧も時価総額の大きい順（2026-11-13 持ち主「スクリーニングした銘柄はすべて時価総額順に並べる」）。
売買をすすめるものではない。公開リポジトリのログには銘柄を出さない（件数だけ）。
"""
from __future__ import annotations

import statistics as st

import pandas as pd

import market_cap

MIN_TURNOVER = 1e8              # 20日平均の売買代金（一覧に出す銘柄）
DOJI_BODY_RATIO = 0.10          # 十字線: |終値-始値| ≤ 幅 × 0.1
DOJI_MIN_RANGE = 0.01           # 十字線: 幅が終値の1%以上（値動きの無い足を除く）
SECTOR_MIN_STOCKS = 5
LIST_LIMIT = 10


def stock_facts(df: pd.DataFrame | None) -> dict | None:
    """日足（Open/High/Low/Close/Volume）から、節に使う数字を出す。80本未満は None。"""
    if df is None or len(df) < 80:
        return None
    try:
        o, h, low, c = (df[k].astype(float) for k in ("Open", "High", "Low", "Close"))
        v = df["Volume"].fillna(0).astype(float)
    except (KeyError, ValueError):
        return None
    ma5, ma25, ma75 = c.rolling(5).mean(), c.rolling(25).mean(), c.rolling(75).mean()

    def ppp(i: int) -> bool:
        return bool(ma5.iloc[i] > ma25.iloc[i] > ma75.iloc[i] and ma5.iloc[i] > ma5.iloc[i - 1]
                    and ma25.iloc[i] > ma25.iloc[i - 1] and ma75.iloc[i] > ma75.iloc[i - 1])

    price, rng = float(c.iloc[-1]), float(h.iloc[-1] - low.iloc[-1])
    turnover = c * v
    base = float(turnover.iloc[-21:-1].mean())
    return {
        "close": price,
        "turnover20": float(turnover.iloc[-20:].mean()),
        "turnover_today": float(turnover.iloc[-1]),
        "turnover_base": base,
        "ret1": price / float(c.iloc[-2]) - 1,
        "ret5": price / float(c.iloc[-6]) - 1,
        "ret20": price / float(c.iloc[-21]) - 1,
        "ret60": price / float(c.iloc[-61]) - 1,
        "ppp": ppp(-1),
        "ppp_new": ppp(-1) and not ppp(-2),
        "downtrend": bool(price < ma25.iloc[-1] < ma75.iloc[-1] and ma25.iloc[-1] < ma25.iloc[-6]),
        "doji": bool(rng > 0 and rng / price >= DOJI_MIN_RANGE
                     and abs(price - float(o.iloc[-1])) <= DOJI_BODY_RATIO * rng),
        "dev25": price / float(ma25.iloc[-1]) - 1,
        "high52": bool(float(h.iloc[-1]) > float(h.iloc[-253:-1].max())) if len(h) >= 253 else False,
    }


def _by_cap(rows: list[dict]) -> list[dict]:
    rows = sorted(rows, key=lambda r: -r["turnover20"])
    return market_cap.sort_by_market_cap(rows, code=lambda r: r["code"], price=lambda r: r["close"])


def _cap(r: dict) -> str:
    text = market_cap.cap_text(market_cap.market_cap_yen(r["code"], r["close"]))
    return f"・{text}" if text else ""


def ppp_lines(facts: list[dict]) -> list[str]:
    liquid = [r for r in facts if r["turnover20"] >= MIN_TURNOVER]
    new = _by_cap([r for r in liquid if r["ppp_new"]])
    n_all = sum(1 for r in liquid if r["ppp"])
    out = [f"## PPP（移動平均線が上向きにそろった）銘柄：本日そろった{len(new)}件（時価総額の大きい順）", "",
           "5日線 > 25日線 > 75日線 の順に並び、3本とも前の日より上を向いた形です。"
           f"この形の銘柄は全部で{n_all}件あり、そのうち本日この形になった銘柄を出しています（売買代金1億円以上）。", ""]
    for i, r in enumerate(new[:LIST_LIMIT], 1):
        out.append(f"{i}. {r['code']} {r['name']}（終値 {r['close']:,.0f}円{_cap(r)}・5日 {r['ret5'] * 100:+.1f}%）")
    if not new:
        out.append("本日は該当なし。")
    if len(new) > LIST_LIMIT:
        out.append(f"（ほか{len(new) - LIST_LIMIT}件）")
    return out + [""]


def doji_lines(facts: list[dict]) -> list[str]:
    rows = _by_cap([r for r in facts if r["turnover20"] >= MIN_TURNOVER and r["downtrend"] and r["doji"]])
    out = [f"## 下げトレンドの十字線：{len(rows)}件（時価総額の大きい順）", "",
           "終値 < 25日線 < 75日線 で25日線が5本前より低い（下げトレンド）銘柄のうち、本日の足が十字線"
           "（始値と終値の差が、高値と安値の幅の1割以下）になったものです。売り買いが釣り合った形で、"
           "下げが止まるかどうかはまだ分かりません。", ""]
    for i, r in enumerate(rows[:LIST_LIMIT], 1):
        out.append(f"{i}. {r['code']} {r['name']}（終値 {r['close']:,.0f}円{_cap(r)}・25日線から {r['dev25'] * 100:+.1f}%）")
    if not rows:
        out.append("本日は該当なし。")
    if len(rows) > LIST_LIMIT:
        out.append(f"（ほか{len(rows) - LIST_LIMIT}件）")
    return out + [""]


def sector_table(facts: list[dict]) -> list[dict]:
    """業種ごとの数字（5銘柄以上の業種）。No.1 は業種の中で時価総額がいちばん大きい会社。"""
    groups: dict[str, list[dict]] = {}
    for r in facts:
        if r.get("sector"):
            groups.setdefault(r["sector"], []).append(r)
    table = []
    for sector, rows in groups.items():
        if len(rows) < SECTOR_MIN_STOCKS:
            continue
        base = sum(r["turnover_base"] for r in rows)
        top = _by_cap(rows)[0]
        table.append({
            "sector": sector, "n": len(rows),
            "ret5": st.fmean(r["ret5"] for r in rows), "ret60": st.fmean(r["ret60"] for r in rows),
            "up_share": sum(1 for r in rows if r["ret1"] > 0) / len(rows),
            "vol_ratio": (sum(r["turnover_today"] for r in rows) / base) if base else 0.0,
            "top": top if market_cap.market_cap_yen(top["code"], top["close"]) else None,
        })
    return table


def sector_lines(facts: list[dict], show: int = 3) -> list[str]:
    table = sector_table(facts)
    if len(table) < 6:
        return []
    # 盛り上がり: 5日の値上がり・上げた銘柄の割合・売買代金の増え方の順位の合計（大きいほど上）
    for key in ("ret5", "up_share", "vol_ratio"):
        for rank, row in enumerate(sorted(table, key=lambda x: x[key]), 1):
            row[f"_{key}"] = rank
    hot = sorted(table, key=lambda x: -(x["_ret5"] + x["_up_share"] + x["_vol_ratio"]))[:show]
    out = ["## 盛り上がっている業種と、業種No.1（時価総額1位）の会社", "",
           "東証33業種ごとに、5日の値上がりの平均・本日上げた銘柄の割合・売買代金の増え方（本日 ÷ 前の20日の平均）を出し、"
           "3つの順位の合計が大きい業種を並べました。業種全体が盛り上がっているか、出来高が増えているかの目安です。"
           "テーマ（ニュースの話題）までは判定していません。", ""]
    for i, row in enumerate(hot, 1):
        top = row["top"]
        lead = (f"業種No.1：{top['code']} {top['name']}（{market_cap.cap_text(market_cap.market_cap_yen(top['code'], top['close']))}・"
                f"5日 {top['ret5'] * 100:+.1f}%{'・本日52週新高値' if top['high52'] else ''}）") if top else "業種No.1：時価総額を取れず"
        out.append(f"{i}. **{row['sector']}**（{row['n']}銘柄）：5日 {row['ret5'] * 100:+.1f}%・本日上げた銘柄 {row['up_share'] * 100:.0f}%・"
                   f"売買代金 {row['vol_ratio']:.2f}倍｜{lead}")
    out.append("")
    # 循環物色: 60日で下半分だった業種が5日で上位5つに入ったか、60日で上位5つの業種が5日で下位5つに入ったか
    by60 = sorted(table, key=lambda x: -x["ret60"])
    by5 = sorted(table, key=lambda x: -x["ret5"])
    lower60 = {r["sector"] for r in by60[len(by60) // 2:]}
    top60 = {r["sector"] for r in by60[:5]}
    catch_up = [r for r in by5[:5] if r["sector"] in lower60]
    cooling = [r for r in by5[-5:] if r["sector"] in top60]
    out += ["## 循環物色はあるか", "",
            "60日で出遅れていた業種（60日の値上がりが下半分）が5日で上位5つに入っていれば、出遅れた業種に資金が回っている形、"
            "60日で上位5つだった業種が5日で下位5つに入っていれば、先に上げた業種が一服している形です。"
            "形を並べただけで、循環物色かどうかは断定できません。", ""]
    out.append("- 出遅れから上位へ：" + ("、".join(f"{r['sector']}（60日 {r['ret60'] * 100:+.1f}% → 5日 {r['ret5'] * 100:+.1f}%）"
                                                   for r in catch_up) or "なし"))
    out.append("- 先に上げて一服：" + ("、".join(f"{r['sector']}（60日 {r['ret60'] * 100:+.1f}% → 5日 {r['ret5'] * 100:+.1f}%）"
                                                 for r in cooling) or "なし"))
    if catch_up and cooling:
        out.append("本日は両方が出ていて、資金の向きが入れ替わっている形です。")
    elif catch_up or cooling:
        out.append("片方だけが出ています。")
    else:
        out.append("本日はどちらも出ておらず、業種の順番は大きくは入れ替わっていません。")
    return out + [""]
