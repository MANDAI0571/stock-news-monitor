#!/usr/bin/env python3
"""高値更新の銘柄の「注文が溜まっていそうな価格帯」を毎日記録し、そのあと実際にどちらへ先に動いたかを確かめる
（2026-10-25 持ち主「今後はデータを残し調査する」）。

きっかけは、動画「流動性という概念」の考え方（株価は損切り注文の溜まり＝直近の押し安値・戻り高値に引き寄せられる）。
過去5年の日足では、溜まりの位置から方向を当てる根拠は見つからなかった（stock-volume-screener/review/STATUS.md、
2026-10-25）。これからの実際の動きでも確かめるため、ランキング記事を作るときに記録だけをする。記事やメールには出さない。

記録するもの（その日はじめて高値を更新した銘柄すべて。SS・S とそれ以外を比べられるように）:
  below1, below2  直近60営業日の押し安値（前後3本より低い安値）のうち、終値より下の近い2つ（買った人の損切りの溜まり）
  above           直近250営業日の戻り高値（前後3本より高い高値）のうち、終値より上でいちばん近いもの（無ければ null）
  low20           20日安値
  poc, val, vah   直近60営業日の日足の出来高を、その日の高値〜安値に均して積んだときの最多の帯と、7割が入る帯
判定（記録した日の翌営業日から20営業日）:
  sym   below1 と、同じ距離だけ上のどちらに先に触れたか（up / down / both / none）。偏りがなければ半々
  pool  above がある銘柄で、above と below1 のどちらに先に触れたか
  poc   POC の値段に触れたか、何日目か
  ret20 20営業日後の終値の騰落率

記録は liquidity_track/records.jsonl（公開リポジトリなのでコミットしない。Actions のキャッシュに置く）。
実行ログ（公開）には件数と割合だけを出し、銘柄は出さない。
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TRACK = ROOT / "liquidity_track" / "records.jsonl"
K = 3              # 押し安値・戻り高値: 前後3本
HORIZON = 20       # 判定する営業日数
BIN = 0.005        # 出来高の帯の幅（終値の0.5%）


def _profile(hi, lo, vo, price):
    width = price * BIN
    if width <= 0:
        return None
    bins = defaultdict(float)
    for h, l, v in zip(hi, lo, vo):
        if not v or h is None or l is None:
            continue
        a, b = int(l // width), int(h // width)
        share = float(v) / (b - a + 1)
        for k in range(a, b + 1):
            bins[k] += share
    if not bins:
        return None
    poc = max(bins, key=bins.get)
    kmin, kmax, total = min(bins), max(bins), sum(bins.values())
    lo_k = hi_k = poc
    acc = bins[poc]
    while acc < 0.7 * total and (lo_k > kmin or hi_k < kmax):
        nl = bins.get(lo_k - 1, 0) if lo_k > kmin else -1
        nh = bins.get(hi_k + 1, 0) if hi_k < kmax else -1
        if nh >= nl:
            hi_k += 1
            acc += nh
        else:
            lo_k -= 1
            acc += nl
    return round((poc + 0.5) * width, 4), round(lo_k * width, 4), round((hi_k + 1) * width, 4)


def levels(df) -> dict | None:
    """日足（High/Low/Close/Volume）の最後の足の時点の溜まり。足りなければ None。"""
    df = df[["High", "Low", "Close", "Volume"]].dropna()
    hi, lo = [float(x) for x in df["High"]], [float(x) for x in df["Low"]]
    cl, vo = [float(x) for x in df["Close"]], [float(x) for x in df["Volume"]]
    n = len(cl) - 1
    if n < 70:
        return None
    price = cl[n]
    lows = [lo[i] for i in range(max(K, n - 60), n - K + 1) if lo[i] == min(lo[i - K:i + K + 1]) and lo[i] < price]
    highs = [hi[i] for i in range(max(K, n - 250), n - K + 1) if hi[i] == max(hi[i - K:i + K + 1]) and hi[i] > price]
    below = sorted(lows, reverse=True)
    prof = _profile(hi[n - 59:n + 1], lo[n - 59:n + 1], vo[n - 59:n + 1], price)
    return {"close": price, "below1": below[0] if below else None, "below2": below[1] if len(below) > 1 else None,
            "above": min(highs) if highs else None, "low20": min(lo[n - 19:n + 1]),
            "poc": prof[0] if prof else None, "val": prof[1] if prof else None, "vah": prof[2] if prof else None}


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _write(path: Path, recs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
    tmp.replace(path)


def _ticker(code: str) -> str:
    return f"{code}.T"


def record(day: dt.date, alerts: list, fetch, path: Path = TRACK) -> int:
    """その日はじめて高値を更新した銘柄の溜まりを足す。同じ日・同じ銘柄は二重に足さない。足した件数を返す。"""
    recs = _read(path)
    have = {(r["date"], r["code"]) for r in recs}
    picked = {}
    for a in alerts:                     # 52週高値更新を直近高値ブレイクより優先（同じ銘柄が両方に出ることがある）
        if not getattr(a, "is_fresh_break", True):
            continue
        cur = picked.get(a.code)
        if cur is None or (a.alert_type == "52週高値更新" and cur.alert_type != "52週高値更新"):
            picked[a.code] = a
    added = 0
    for code, a in picked.items():
        if (day.isoformat(), code) in have:
            continue
        try:
            df = fetch(_ticker(code))
        except Exception:                                   # noqa: BLE001
            continue
        if df is None or len(df) == 0 or df.index[-1].date() != day:
            continue                     # 記事の日の足でなければ記録しない（古い足で溜まりを測らない）
        lv = levels(df)
        if lv is None:
            continue
        recs.append({"date": day.isoformat(), "code": code, "grade": a.grade or "", "kind": a.alert_type,
                     "market": a.market or "", **lv, "final": False})
        added += 1
    if added:
        _write(path, recs)
    return added


def _judge(rec: dict, df) -> dict:
    """記録した日の翌営業日から HORIZON 本で判定する。"""
    day = dt.date.fromisoformat(rec["date"])
    df = df[["High", "Low", "Close"]].dropna()
    after = df[[d.date() > day for d in df.index]].head(HORIZON)
    hi, lo, cl = list(after["High"]), list(after["Low"]), list(after["Close"])
    c0, b1, ab, poc = rec["close"], rec.get("below1"), rec.get("above"), rec.get("poc")
    out = {"bars": len(cl)}

    def first(up, down):
        for i, (h, l) in enumerate(zip(hi, lo), 1):
            u, d = up is not None and h >= up, down is not None and l <= down
            if u and d:
                return "both", i
            if u:
                return "up", i
            if d:
                return "down", i
        return ("none" if len(cl) >= HORIZON else None), None

    if b1:
        out["sym"], out["sym_day"] = first(c0 * (1 + (c0 - b1) / c0), b1)
    if b1 and ab:
        out["pool"], out["pool_day"] = first(ab, b1)
    if poc:
        days = [i for i, (h, l) in enumerate(zip(hi, lo), 1) if l <= poc <= h]
        out["poc_day"] = days[0] if days else None
    if len(cl) >= HORIZON:
        out["ret20"] = round(cl[HORIZON - 1] / c0 - 1, 5)
        out["final"] = True
    return out


def evaluate(fetch, path: Path = TRACK) -> int:
    """判定が終わっていない記録を、今ある足で判定し直す。判定を更新した件数を返す。"""
    recs = _read(path)
    todo = defaultdict(list)
    for i, r in enumerate(recs):
        if not r.get("final"):
            todo[r["code"]].append(i)
    changed = 0
    for code, idx in todo.items():
        try:
            df = fetch(_ticker(code))
        except Exception:                                   # noqa: BLE001
            continue
        if df is None or len(df) == 0:
            continue
        for i in idx:
            recs[i].update(_judge(recs[i], df))
            changed += 1
    if changed:
        _write(path, recs)
    return changed


def summary_lines(path: Path = TRACK) -> list[str]:
    """公開ログに出す集計（銘柄は出さない）。"""
    recs = _read(path)
    if not recs:
        return ["liquidity_track records=0"]
    out = [f"liquidity_track records={len(recs)} days={len({r['date'] for r in recs})} "
           f"final={sum(1 for r in recs if r.get('final'))} first={min(r['date'] for r in recs)}"]
    for name, sel in (("SS・S", lambda r: r.get("grade") in ("SS", "S")),
                      ("それ以外", lambda r: r.get("grade") not in ("SS", "S"))):
        g = [r for r in recs if sel(r)]
        up = sum(1 for r in g if r.get("sym") == "up")
        dn = sum(1 for r in g if r.get("sym") == "down")
        pu = sum(1 for r in g if r.get("pool") == "up")
        pd_ = sum(1 for r in g if r.get("pool") == "down")
        fin = [r for r in g if r.get("final")]
        poc = sum(1 for r in fin if r.get("poc_day"))
        ret = [r["ret20"] for r in fin if r.get("ret20") is not None]
        out.append(f"liquidity_track {name}: 記録{len(g)} 同じ距離で上が先{_pct(up, up + dn)}（{up + dn}件で決着） "
                   f"上の溜まりが先{_pct(pu, pu + pd_)}（{pu + pd_}件） 20日でPOCに触れた{_pct(poc, len(fin))}（{len(fin)}件） "
                   f"20日後の平均{(100 * sum(ret) / len(ret)):+.2f}%" if ret else
                   f"liquidity_track {name}: 記録{len(g)} 同じ距離で上が先{_pct(up, up + dn)}（{up + dn}件で決着） 20日後はまだ")
    return out


def _pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "-"
