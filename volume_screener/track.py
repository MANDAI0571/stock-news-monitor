"""一覧に出た銘柄の、その後の成績。2026-10-12 追加。

メール末尾の損益比は過去の検証の数字で、実際にメールに出た銘柄の成績ではない。
out/history/日付.json（その日の14:10ごろの本番の一覧）に残った銘柄を、末尾の検証と
同じ決まりで数え続ける。

決まり: その日の引けで決め、翌営業日の寄付で買う。以後の終値が -5% 以下か +12% 以上、
または15営業日で、その翌営業日の寄付で手じまう。売買コスト0.2%を引く。
（検証の btlib.sim と同じ。決着の翌日の寄付がまだ無いものは「途中」として待つ）

結果は out/track.json に残し、決着したものは取り直さない。売買をすすめるものではない。
"""
import concurrent.futures as cf
import datetime as dt
import json
import os
import time

import fetchlib as F

STOP, TAKE, TIMEOUT, COST = -0.05, 0.12, 15, 0.002
# この日数たっても日足で決着できない場面は数えない（上場廃止・売買停止など）
GIVE_UP_DAYS = 60


def sim(dates, opens, closes, day):
    """day の引けで決めた場面の (損益, 手じまいの日)。決着していなければ None。"""
    if day not in dates:
        return None
    t = dates.index(day)
    n = len(closes)
    if t + 1 >= n or not opens[t + 1]:
        return None
    entry = opens[t + 1]
    for k in range(t + 1, n):
        pnl = closes[k] / entry - 1
        if pnl <= STOP or pnl >= TAKE or k - t >= TIMEOUT:
            if k + 1 >= n or not opens[k + 1]:
                return None
            return opens[k + 1] / entry - 1 - COST, dates[k + 1]
    return None


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def update(out_dir, today, budget_s=60, workers=4, fetch=None):
    """out/history の銘柄で、決着していないものを取り直して out/track.json を更新し、集計を返す。
    今日の足（途中の値）は使わない。時間切れや取得失敗の銘柄は次の回に回す。"""
    fetch = fetch or F.daily
    path = os.path.join(out_dir, "track.json")
    state = _load(path)
    done = state.get("done", {})                       # "日付|コード" -> {...}
    hist = os.path.join(out_dir, "history")
    pending = []
    try:
        names = sorted(os.listdir(hist))
    except OSError:
        names = []
    for f in names:
        if not f.endswith(".json"):
            continue
        rec = _load(os.path.join(hist, f))
        day = rec.get("date")
        if not day or day >= today.isoformat():
            continue
        for r in rec.get("rows") or []:
            key = f"{day}|{r.get('code')}"
            if r.get("code") and key not in done:
                pending.append((day, r["code"], r.get("name", ""), r.get("mark", "")))
    codes = sorted({p[1] for p in pending})
    bars, errored, deadline = {}, set(), time.time() + budget_s

    def one(code):
        dates, closes, _vols, meta = fetch(code)
        opens = (meta or {}).get("ohlc", {}).get("open") or []
        keep = [i for i, d in enumerate(dates) if d < today]   # 今日の途中の足は使わない
        return ([dates[i] for i in keep], [opens[i] for i in keep] if opens else [],
                [closes[i] for i in keep])

    if codes:
        ex = cf.ThreadPoolExecutor(max_workers=workers)
        futs = {ex.submit(one, c): c for c in codes}
        try:
            for fu in cf.as_completed(futs, timeout=max(1, deadline - time.time())):
                try:
                    bars[futs[fu]] = fu.result()
                except Exception:                     # noqa: BLE001
                    errored.add(futs[fu])
        except cf.TimeoutError:
            pass
        ex.shutdown(wait=False, cancel_futures=True)
    open_n = 0
    for day, code, name, mark in pending:
        b = bars.get(code)
        res = None
        if b and len(b[1]) == len(b[0]):
            res = sim(b[0], b[1], b[2], dt.date.fromisoformat(day))
        if res:
            done[f"{day}|{code}"] = {"ret": round(res[0], 6), "exit": res[1].isoformat(),
                                     "name": name, "mark": mark}
        elif ((b or code in errored)
              and (today - dt.date.fromisoformat(day)).days > GIVE_UP_DAYS):
            # 取りに行けて、それでも決着できないときだけ打ち切る。時間切れで
            # 取れなかっただけの場面まで打ち切っていた（2026-10-12、作成時の確認で判明）。
            done[f"{day}|{code}"] = {"ret": None, "exit": None, "name": name, "mark": mark}
        else:
            open_n += 1
    state = {"done": done, "updated": dt.datetime.now(F.JST).strftime("%Y-%m-%d %H:%M"),
             "since": min([k[:10] for k in done] + [p[0] for p in pending], default=None)}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return summary(state, open_n)


def _stats(rets):
    win = [x for x in rets if x > 0]
    loss = [x for x in rets if x <= 0]
    pf = (sum(win) / -sum(loss)) if loss and sum(loss) else None
    return {"n": len(rets), "pf": pf, "win": (len(win) / len(rets)) if rets else None,
            "avg": (sum(rets) / len(rets)) if rets else None}


def summary(state, open_n):
    done = state.get("done", {}).values()
    rets = [v["ret"] for v in done if v.get("ret") is not None]
    out = {"since": state.get("since"), "open": open_n, **_stats(rets)}
    # ランク（◎）ごと。印を記録し始めた 2026-10-23 以降の分だけ（それより前は印が無いので数えない）
    marked = [v for v in done if v.get("ret") is not None and "mark" in v]
    out["by_mark"] = {m: _stats([v["ret"] for v in marked if v["mark"] == m]) for m in ("◎", "")}
    return out


def _cell(x):
    if not x.get("n"):
        return "0件"
    pf = "-" if x["pf"] is None else f"{x['pf']:.2f}"
    return f"{x['n']}件・損益比{pf}・勝率{x['win'] * 100:.0f}%"


def mark_line(s):
    """ランク（◎）ごとの実際の成績（1文）。◎ の決着が1件も無ければ空。"""
    bm = (s or {}).get("by_mark") or {}
    a, b = bm.get("◎") or {}, bm.get("") or {}
    if not a.get("n"):
        return ""
    return (f"ランク別（印を付け始めた2026-10-23以降に手じまいまで終わった分）：◎ {_cell(a)}、"
            f"それ以外 {_cell(b)}。件数が少ないうちは偶然の差が大きい数字です。")


def note(s):
    """メール末尾に出す文（1段落）。記録が無ければ空。"""
    if not s or not s.get("since"):
        return ""
    head = f"{s['since']}以降にこの一覧に出た銘柄を、上と同じ決まり（売買コスト0.2%込み）で数えると、"
    if not s["n"]:
        return head + f"まだ手じまいまで終わった場面がありません（途中 {s['open']}件）。"
    pf = "-" if s["pf"] is None else f"{s['pf']:.2f}"
    text = (head + f"手じまいまで終わった{s['n']}件で、損益比{pf}・勝率{s['win'] * 100:.0f}%・"
            f"1回あたり平均{s['avg'] * 100:+.1f}%でした（途中 {s['open']}件）。")
    if s["n"] < 30:
        text += "30件に満たないうちは偶然の差が大きく、上の検証の数字と食い違っても不思議ではありません。"
    ml = mark_line(s)
    return text + (" " + ml if ml else "")
