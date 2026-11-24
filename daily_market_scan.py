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
        "pos52": _pos52(price, h, low),
        "hanging": is_hanging_shape(float(o.iloc[-1]), float(h.iloc[-1]), float(low.iloc[-1]), price),
        "harami": harami_kind(float(o.iloc[-2]), float(c.iloc[-2]), float(o.iloc[-1]), price),
    }


def _pos52(price: float, h: pd.Series, low: pd.Series) -> float | None:
    """52週（252本）の安値〜高値の幅の中で、終値がどこにいるか（0＝安値、1＝高値）。足りなければ None。"""
    if len(h) < 252:
        return None
    top, bottom = float(h.iloc[-252:].max()), float(low.iloc[-252:].min())
    return (price - bottom) / (top - bottom) if top > bottom else None


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


LARGE_CAP_YEN = 1e12             # 大型株の形: 時価総額1兆円以上の東証プライム
HIGH_ZONE, LOW_ZONE = 0.8, 0.2   # 52週の幅の上2割＝高値圏、下2割＝安値圏
LARGE_LIST_LIMIT = 3             # 2026-11-24 持ち主「TOP10は多すぎるのTOP3で」
# 首吊り線（2026-11-24 持ち主、ちょる子さんの「指標2」）: 実体が小さく、長い下ヒゲ、上ヒゲはほとんど無い足が高値圏で出たもの
HANG_BODY_RATIO = 0.30           # 実体 ≤ 幅 × 0.3
HANG_LOWER_RATIO = 0.60          # 下ヒゲ ≥ 幅 × 0.6 かつ 実体の2倍以上
HANG_UPPER_RATIO = 0.10          # 上ヒゲ ≤ 幅 × 0.1


def is_hanging_shape(o: float, h: float, low: float, c: float) -> bool:
    """首吊り線（下ヒゲの長い小さな足）の形か。位置（高値圏か）は見ない。幅は終値の1%以上。"""
    rng = h - low
    if not (rng > 0 and c > 0 and rng / c >= DOJI_MIN_RANGE):
        return False
    body, lower, upper = abs(c - o), min(o, c) - low, h - max(o, c)
    return body <= HANG_BODY_RATIO * rng and lower >= HANG_LOWER_RATIO * rng and lower >= 2 * body \
        and upper <= HANG_UPPER_RATIO * rng


# はらみ線（2026-11-24 持ち主「次はハラミ線で同じ感じで」。ちょる子さんの「指標3」）: 前日の大きな実体の中に、本日の小さな実体が収まる2本の足
HARAMI_PREV_BODY = 0.015         # 前日の実体が前日終値の1.5%以上（大きな足）
HARAMI_BODY_RATIO = 0.5          # 本日の実体 ≤ 前日の実体 × 0.5


def harami_kind(po: float, pc: float, o: float, c: float) -> str:
    """はらみ線なら "after_down"（大きな陰線のあと）か "after_up"（大きな陽線のあと）。違えば空。
    本日の実体（始値〜終値）が前日の実体の内側に収まり、前日の実体の半分以下。前日の実体は前日終値の1.5%以上。"""
    prev_body = abs(pc - po)
    if not (pc > 0 and prev_body >= HARAMI_PREV_BODY * pc):
        return ""
    if not (min(po, pc) <= min(o, c) and max(o, c) <= max(po, pc) and abs(c - o) <= HARAMI_BODY_RATIO * prev_body):
        return ""
    return "after_down" if pc < po else "after_up"


HARAMI_WORDS = {"after_down": "大きな陰線のあと", "after_up": "大きな陽線のあと"}


def zone_word(pos: float | None) -> str:
    if pos is None:
        return "位置は不明"
    return "高値圏" if pos >= HIGH_ZONE else "安値圏" if pos <= LOW_ZONE else "中ほど"


def _large(r: dict) -> bool:
    return "プライム" in str(r.get("market", "")) and (market_cap.market_cap_yen(r["code"], r["close"]) or 0) >= LARGE_CAP_YEN


def _large_rows_lines(rows: list[dict], extra=None) -> list[str]:
    out = []
    for i, r in enumerate(rows[:LARGE_LIST_LIMIT], 1):
        pos = r.get("pos52")
        tag = zone_word(pos) + (f"・{extra(r)}" if extra else "")
        where = f"【{tag}】" + (f"52週の幅の{pos * 100:.0f}%の位置" if pos is not None else "")
        out.append(f"{i}. {where} {r['code']} {r['name']}（終値 {r['close']:,.0f}円{_cap(r)}・前日比 {r['ret1'] * 100:+.1f}%）")
    if not rows:
        out.append("本日は該当なし。")
    if len(rows) > LARGE_LIST_LIMIT:
        out.append(f"（ほか{len(rows) - LARGE_LIST_LIMIT}件）")
    return out + [""]


def large_doji_lines(facts: list[dict]) -> list[str]:
    """大型株の十字線 TOP3（2026-11-24 持ち主「十字線をスクリーニング大型株で時価総額順に…300万運用の記事に」、
    同日「TOP10は多すぎるのTOP3で」。ママさん投資家ちょる子さんの判断基準: 十字線は単体ではなく、株価がどの位置で
    出たかと合わせて見る。大型株ほど効く）。時価総額1兆円以上の東証プライムで本日の足が十字線の銘柄を、時価総額の大きい順に。"""
    rows = _by_cap([r for r in facts if r.get("doji") and _large(r)])
    out = [f"## 大型株の十字線：{len(rows)}件（時価総額の大きい順に{LARGE_LIST_LIMIT}件まで）", "",
           "ママさん投資家のちょる子さんが判断の軸にしている形の1つ「十字線」を、大型株（時価総額1兆円以上の東証プライム）から"
           "機械的に拾いました。十字線は、始値と終値がほぼ同じで上下にヒゲだけが伸びた足で、買いと売りの力が釣り合ったことを表します。"
           "ちょる子さんは、十字線をそれだけで見ず「いま株価がどの位置にあるか」と合わせて見るそうです。"
           "高値圏で出れば買いの勢いが衰えてきた合図、安値圏で出れば売りが一巡して下げ止まりかけた合図とされ、"
           "多くの投資家が同じチャートを見ている大型株ほど意識されやすい、とのことです。", "",
           "ここでは日足で、始値と終値の差が高値と安値の幅の1割以下（幅は終値の1%以上）の足を十字線とし、"
           f"位置は52週の安値〜高値の幅の上{round((1 - HIGH_ZONE) * 100)}%を高値圏、下{round(LOW_ZONE * 100)}%を安値圏としました。"
           "この運用の売買の決まりとは別に、参考として載せています（この運用はこの一覧では売買していません）。", ""]
    return out + _large_rows_lines(rows)


def large_hanging_lines(facts: list[dict]) -> list[str]:
    """大型株の首吊り線 TOP3（2026-11-24 持ち主「次はこれ同じ感じで…TOP3で」。ちょる子さんの「指標2：首吊り線」:
    下ヒゲは普通は買いの合図とされるが、高値圏で出た小さな実体・長い下ヒゲの足は、上昇の終わりを知らせる形として見る）。
    時価総額1兆円以上の東証プライムで、本日の足が首吊り線の形で、終値が52週の幅の高値圏（上2割）にある銘柄。"""
    rows = _by_cap([r for r in facts if r.get("hanging") and _large(r)
                    and r.get("pos52") is not None and r["pos52"] >= HIGH_ZONE])
    out = [f"## 大型株の首吊り線：{len(rows)}件（時価総額の大きい順に{LARGE_LIST_LIMIT}件まで）", "",
           "ちょる子さんが判断の軸にしている形の2つ目「首吊り線」です。実体が小さく、長い下ヒゲがあり、上ヒゲはほとんど無い足です。"
           "下ヒゲは普通「安いところで買う人が現れた」しるしとされますが、高値圏で出たときに限っては意味が逆になり、"
           "その日のうちに大きく売られる場面があり、買い戻されても高値を更新する勢いは無かった、"
           "つまり上昇の勢いが尽きかけている合図として見るそうです。"
           "ちょる子さんは、大型株が力強く上がった後にこの形が出たら、手じまいの準備を始めるとのことです。", "",
           f"ここでは日足で、実体が幅の{round(HANG_BODY_RATIO * 100)}%以下、下ヒゲが幅の{round(HANG_LOWER_RATIO * 100)}%以上かつ"
           f"実体の2倍以上、上ヒゲが幅の{round(HANG_UPPER_RATIO * 100)}%以下（幅は終値の1%以上）の足で、"
           f"終値が52週の安値〜高値の幅の上{round((1 - HIGH_ZONE) * 100)}%（高値圏）にあるものを拾いました。"
           "この運用の売買の決まりとは別に、参考として載せています（この運用はこの一覧では売買していません）。", ""]
    return out + _large_rows_lines(rows)


def large_harami_lines(facts: list[dict]) -> list[str]:
    """大型株のはらみ線 TOP3（2026-11-24 持ち主「次はハラミ線で同じ感じで」。ちょる子さんの「指標3：はらみ線」）。
    時価総額1兆円以上の東証プライムで、本日の足がはらみ線の銘柄を時価総額の大きい順に。位置と、前日が陰線か陽線かを付ける。"""
    rows = _by_cap([r for r in facts if r.get("harami") and _large(r)])
    out = [f"## 大型株のはらみ線：{len(rows)}件（時価総額の大きい順に{LARGE_LIST_LIMIT}件まで）", "",
           "ちょる子さんが取り上げている形の3つ目「はらみ線」です。前日の大きな足の実体の中に、本日の小さな足の実体がすっぽり"
           "収まった2本の組み合わせで、前日の勢いが本日は続かず、いったん止まったことを表す形です。"
           "一般には、安値圏で大きな陰線のあとに出れば下げの勢いが弱まった合図、高値圏で大きな陽線のあとに出れば"
           "上げの勢いが弱まった合図として見られます。十字線・首吊り線と同じく、どの位置で出たかと合わせて見る形です。", "",
           f"ここでは日足で、前日の実体（始値〜終値）が前日終値の{HARAMI_PREV_BODY * 100:.1f}%以上あり、本日の実体が前日の実体の内側に"
           f"収まって前日の実体の{round(HARAMI_BODY_RATIO * 100)}%以下のものを拾いました。行の先頭に、位置（52週の幅の上"
           f"{round((1 - HIGH_ZONE) * 100)}%＝高値圏、下{round(LOW_ZONE * 100)}%＝安値圏）と、前日が大きな陰線か陽線かを付けています。"
           "この運用の売買の決まりとは別に、参考として載せています（この運用はこの一覧では売買していません）。", ""]
    return out + _large_rows_lines(rows, lambda r: HARAMI_WORDS.get(r.get("harami"), ""))


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
