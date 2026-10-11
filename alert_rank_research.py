#!/usr/bin/env python3
"""ザラ場アラートの「過去の成績ランク」を作るための検証（2026-10-11 追加）。

アラートと同じ定義の場面を過去の日足で作り直し、その後の成績を出す。
ランクは「過去に似た形がどうなったか」の数字であって、売買をすすめるものではない。

場面の定義（intraday_high_alert.py / scanner/highs.py と同じ考え方。日足で近似）
  52W   当日の高値が、前日までの252本の高値の最大を上抜けた
  60D   52Wではなく、当日の高値が前日までの60本の高値の最大を上抜けた
  SWING 上の2つではなく、5〜30本前の「はっきりした山」（左右2本より高い）の
        最大を、当日の高値か終値が上抜け、その山の翌日から前日まで一度も
        超えていなかった（今日はじめて超えた）
  共通  20日平均売買代金（当日を含む）が5000万円以上（IH_MIN_TURNOVER と同じ）

成績（sim）: 当日の引けで決め、翌営業日の寄付で買う。以後の終値が -5% 以下か
+12% 以上、または15営業日で、その翌営業日の寄付で手じまう。売買コスト0.2%を引く。
stock-volume-screener/backtest/btlib.py の sim と同じ決まり。

  python3 alert_rank_research.py --pkl <history_5y.pkl> --n225 <n225.json> --out <dir>
"""

import argparse
import datetime as dt
import json
import math
import os
import pickle
import statistics as st

STOP, TAKE, TIMEOUT, COST = -0.05, 0.12, 15, 0.002
MIN_TURNOVER = 5e7


def price_limit(base):
    table = ((100, 30), (200, 50), (500, 80), (700, 100), (1000, 150), (1500, 300),
             (2000, 400), (3000, 500), (5000, 700), (7000, 1000), (10000, 1500),
             (15000, 3000), (20000, 4000), (30000, 5000), (50000, 7000),
             (70000, 10000), (100000, 15000))
    for upper, width in table:
        if base < upper:
            return width
    return base * 0.15


def sim(op, cl, t):
    """t の引けで決め、t+1 の寄付で買う。決着しなければ None。"""
    n = len(cl)
    if t + 1 >= n or not op[t + 1]:
        return None
    entry = op[t + 1]
    for k in range(t + 1, n):
        pnl = cl[k] / entry - 1
        if pnl <= STOP or pnl >= TAKE or k - t >= TIMEOUT:
            exit_px = op[k + 1] if k + 1 < n else cl[k]
            return exit_px / entry - 1 - COST
    return None


def load_n225(path):
    r = json.load(open(path))["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    jst = dt.timezone(dt.timedelta(hours=9))
    rows = [(dt.datetime.fromtimestamp(t, jst).date(), c)
            for t, c in zip(r["timestamp"], q["close"]) if c]
    out = {}
    closes = []
    for d, c in rows:
        closes.append(c)
        ma25 = st.fmean(closes[-25:]) if len(closes) >= 25 else None
        ret5 = (c / closes[-6] - 1) if len(closes) >= 6 else None
        out[d] = {"above25": (ma25 is not None and c > ma25), "ret5": ret5}
    return out


def bucket(v, edges, labels):
    for e, lab in zip(edges, labels):
        if v < e:
            return lab
    return labels[-1]


def events_for(code, r, n225):
    d = r["dates"]
    cl = [float(x) if x else 0.0 for x in r["closes"]]
    vo = [float(x) if x else 0.0 for x in r["vols"]]
    op = [float(x) if x else 0.0 for x in r["ohlc"]["open"]]
    hi = [float(x) if x else 0.0 for x in r["ohlc"]["high"]]
    lo = [float(x) if x else 0.0 for x in r["ohlc"]["low"]]
    n = len(cl)
    # 出来高0の足（配信の遅れた偽の足）は使わない: その日は場面にしない
    out = []
    turn = [c * v for c, v in zip(cl, vo)]
    for t in range(260, n - 1):
        if not vo[t] or not cl[t] or not hi[t]:
            continue
        t20 = st.fmean(turn[t - 19:t + 1])
        if t20 < MIN_TURNOVER:
            continue
        prev252 = max(hi[t - 252:t])
        prev60 = max(hi[t - 60:t])
        kind = None
        line = None
        if hi[t] > prev252 > 0:
            kind, line = "52W", prev252
        elif hi[t] > prev60 > 0:
            kind, line = "60D", prev60
        else:
            cands = []
            for p in range(t - 30, t - 4):
                h = hi[p]
                if h <= 0:
                    continue
                if all(hi[q] < h for q in range(p - 2, p + 3) if q != p):
                    cands.append((h, p))
            if cands:
                h, p = max(cands)
                trig = max(hi[t], cl[t])
                if trig > h and max(hi[p + 1:t], default=0) <= h:
                    kind, line = "SWING", h
        if kind is None:
            continue
        rng = hi[t] - lo[t]
        close_pos = (cl[t] - lo[t]) / rng if rng > 0 else 0.5
        v5 = st.fmean(vo[t - 4:t + 1])
        v20 = st.fmean(vo[t - 19:t + 1])
        ma25 = st.fmean(cl[t - 24:t + 1])
        ma75 = st.fmean(cl[t - 74:t + 1])
        ma200 = st.fmean(cl[t - 199:t + 1])
        prev = cl[t - 1]
        nk = n225.get(d[t], {})
        ev = {
            "code": code, "date": d[t], "kind": kind,
            "excess_pct": (hi[t] / line - 1) * 100,
            "close_above_line": cl[t] > line,
            "close_pos": close_pos,
            "vol_ratio": v5 / v20 if v20 else 0.0,
            "turnover20": t20,
            "chg_pct": (cl[t] / prev - 1) * 100 if prev else 0.0,
            "limit_hit": abs(cl[t] - prev) >= price_limit(prev) * 0.98 if prev else False,
            "dev25": (cl[t] / ma25 - 1) * 100,
            "trend": ("up" if cl[t] > ma75 > ma200 else
                      "down" if cl[t] < ma75 < ma200 else "mixed"),
            "n225_above25": nk.get("above25"),
            "ret": sim(op, cl, t),
        }
        out.append(ev)
    return out


FEATURES = {
    "kind": lambda e: e["kind"],
    "excess": lambda e: bucket(e["excess_pct"], [1, 3, 6], ["<1%", "1-3%", "3-6%", ">=6%"]),
    "close": lambda e: ("終値も上" if e["close_above_line"] else "高値だけ") + "/" +
                       bucket(e["close_pos"], [0.3, 0.7], ["引け弱", "引け中", "引け強"]),
    "vol": lambda e: bucket(e["vol_ratio"], [1.0, 1.5, 2.5], ["<1.0", "1.0-1.5", "1.5-2.5", ">=2.5"]),
    "turnover": lambda e: bucket(e["turnover20"], [1e8, 5e8, 2e9],
                                 ["0.5-1億", "1-5億", "5-20億", ">=20億"]),
    "dev25": lambda e: bucket(e["dev25"], [3, 8, 15], ["<3%", "3-8%", "8-15%", ">=15%"]),
    "trend": lambda e: e["trend"],
    "n225": lambda e: {True: "日経25日線上", False: "日経25日線下", None: "不明"}[e["n225_above25"]],
}


def pf(rets):
    w = sum(x for x in rets if x > 0)
    l = -sum(x for x in rets if x <= 0)
    return (w / l) if l > 0 else None


def stats(rets):
    if not rets:
        return {"n": 0}
    return {"n": len(rets), "win": sum(1 for x in rets if x > 0) / len(rets),
            "avg": st.fmean(rets), "pf": pf(rets)}


def fit_table(train, min_n=200, shrink=300):
    """特徴の区分ごとに、全体の損益比からの寄与（対数）を出す。件数の少ない区分は
    全体に引き寄せる（shrink 件ぶんの全体平均を混ぜる）。"""
    base_w = sum(e["ret"] for e in train if e["ret"] > 0)
    base_l = -sum(e["ret"] for e in train if e["ret"] <= 0)
    base_pf = base_w / base_l
    table = {}
    for name, f in FEATURES.items():
        groups = {}
        for e in train:
            groups.setdefault(f(e), []).append(e["ret"])
        table[name] = {}
        for k, rets in groups.items():
            w = sum(x for x in rets if x > 0)
            l = -sum(x for x in rets if x <= 0)
            n = len(rets)
            # 全体の勝ち・負けの平均的な額を shrink 件ぶん足して縮める
            avg_w, avg_l = base_w / len(train), base_l / len(train)
            pf_k = (w + shrink * avg_w) / (l + shrink * avg_l)
            table[name][k] = {"n": n, "pf_raw": (w / l if l else None),
                              "logratio": math.log(pf_k / base_pf) if n >= min_n else 0.0}
    return base_pf, table


def score(e, table):
    return sum(table[name].get(f(e), {"logratio": 0.0})["logratio"]
               for name, f in FEATURES.items())


GRADES = (("SS", 0.10), ("S", 0.15), ("A", 0.25), ("B", 0.25), ("C", 0.25))


def cutoffs(scores):
    s = sorted(scores, reverse=True)
    cuts, acc = [], 0.0
    for g, frac in GRADES[:-1]:
        acc += frac
        cuts.append((g, s[min(len(s) - 1, int(len(s) * acc))]))
    return cuts


def grade_of(sc, cuts):
    for g, c in cuts:
        if sc > c:
            return g
    return "C"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pkl", required=True)
    ap.add_argument("--n225", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--split", default="2025-01-01")
    a = ap.parse_args()
    data = pickle.load(open(a.pkl, "rb"))["data"]
    n225 = load_n225(a.n225)
    evs = []
    for code, r in data.items():
        evs.extend(events_for(code, r, n225))
    evs = [e for e in evs if e["ret"] is not None]
    split = dt.date.fromisoformat(a.split)
    train = [e for e in evs if e["date"] < split]
    test = [e for e in evs if e["date"] >= split]
    base_pf, table = fit_table(train)
    for e in evs:
        e["score"] = score(e, table)
    cuts = cutoffs([e["score"] for e in train])
    res = {"events": len(evs), "train": len(train), "test": len(test),
           "train_period": [str(min(e["date"] for e in train)), str(max(e["date"] for e in train))],
           "test_period": [str(min(e["date"] for e in test)), str(max(e["date"] for e in test))],
           "base_pf_train": base_pf, "cuts": cuts, "table": table,
           "all_train": stats([e["ret"] for e in train]), "all_test": stats([e["ret"] for e in test]),
           "by_grade": {}, "by_grade_year": {}}
    for part, rows in (("train", train), ("test", test)):
        for g, _ in GRADES:
            res["by_grade"].setdefault(g, {})[part] = stats(
                [e["ret"] for e in rows if grade_of(e["score"], cuts) == g])
    for e in evs:
        y = str(e["date"].year)
        res["by_grade_year"].setdefault(grade_of(e["score"], cuts), {}).setdefault(y, []).append(e["ret"])
    res["by_grade_year"] = {g: {y: stats(v) for y, v in ys.items()}
                            for g, ys in res["by_grade_year"].items()}
    os.makedirs(a.out, exist_ok=True)
    json.dump(res, open(os.path.join(a.out, "alert_rank_research.json"), "w"),
              ensure_ascii=False, indent=1, default=str)
    print(json.dumps({k: res[k] for k in ("events", "train", "test", "train_period", "test_period",
                                          "base_pf_train", "all_train", "all_test", "by_grade")},
                     ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
