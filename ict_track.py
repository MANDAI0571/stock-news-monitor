#!/usr/bin/env python3
"""押し目の箱（オーダーブロック。us_ict.py）で記事に出した銘柄の「その後」を記録して判定する
（2026-10-25 持ち主「今後はデータを残し調査する」）。過去5年の検証と同じ決まりで、これからの実際の成績を数える。

決まり（backtest/us_ict_study.py と同じ）: 記事の日の翌営業日の寄付で買う。損切り = 記事の損切りの目安、
利確 = 記事の利確の目安（レンジの高値）。40営業日たったらその日の終値で手じまう。寄付の時点で損切りより下、
または利幅が損切り幅の2倍未満になっていたら買わない（見送りとして数える）。コストは日本株 往復0.2%、米株 0.1%。
同じ日に損切りと利確の両方に触れたら損切りとみなす（不利な側）。

記録は銘柄が入るのでコミットしない（Actions のキャッシュ）。日本株 liquidity_track/ict_records.jsonl、
米株 us_track/ict_records.jsonl。公開ログには件数と割合だけを出す。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TRACK_JP = ROOT / "liquidity_track" / "ict_records.jsonl"
TRACK_US = ROOT / "us_track" / "ict_records.jsonl"
COST = {"jp": 0.002, "us": 0.001}
HOLD = 40


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
    (path.parent / ".changed").write_text("1\n", encoding="utf-8")   # この回で書き換えた印（キャッシュに残すかの判断）


def record(day: dt.date, market: str, rows: list[dict] | None, path: Path) -> int:
    """記事に出した銘柄を足す。同じ日・同じ銘柄は二重に足さない。足した件数。"""
    if not rows:
        return 0
    recs = _read(path)
    have = {(r["date"], r["ticker"]) for r in recs}
    added = 0
    for x in rows:
        key = (day.isoformat(), str(x["ticker"]))
        if key in have:
            continue
        recs.append({"date": key[0], "market": market, "ticker": key[1], "kind": x["kind"],
                     "close": round(float(x["close"]), 4), "stop": round(float(x["stop"]), 4),
                     "target": round(float(x["target"]), 4), "rr": round(float(x["rr"]), 3), "final": False})
        have.add(key)
        added += 1
    if added:
        _write(path, recs)
    return added


def judge(rec: dict, df, market: str) -> dict:
    """日足（Open/High/Low/Close）で判定する。まだ決着していなければ final=False のまま。"""
    day = dt.date.fromisoformat(rec["date"])
    df = df[["Open", "High", "Low", "Close"]].dropna()
    after = df[[(d.date() if hasattr(d, "date") else d) > day for d in df.index]].head(HOLD)
    if len(after) == 0:
        return {}
    op, hi = [float(x) for x in after["Open"]], [float(x) for x in after["High"]]
    lo, cl = [float(x) for x in after["Low"]], [float(x) for x in after["Close"]]
    e, stop, target = op[0], rec["stop"], rec["target"]
    if e <= stop or (target - e) < 2 * (e - stop):
        return {"entry": e, "result": "見送り", "final": True}
    cost = COST.get(market, 0.002)
    for j in range(len(cl)):
        if lo[j] <= stop:
            px = min(op[j], stop) if j > 0 else stop
            return {"entry": e, "exit": px, "result": "損切り", "days": j + 1, "ret": round(px / e - 1 - cost, 5), "final": True}
        if hi[j] >= target:
            px = max(op[j], target) if j > 0 else target
            return {"entry": e, "exit": px, "result": "利確", "days": j + 1, "ret": round(px / e - 1 - cost, 5), "final": True}
    if len(cl) >= HOLD:
        return {"entry": e, "exit": cl[-1], "result": "時間切れ", "days": HOLD, "ret": round(cl[-1] / e - 1 - cost, 5), "final": True}
    return {"entry": e, "days": len(cl), "open_ret": round(cl[-1] / e - 1 - cost, 5), "final": False}


def evaluate(fetch, path: Path, market: str) -> int:
    """決着していない記録を判定し直す。判定を更新した件数。fetch(ticker) は日足を返す（日本株は 'コード.T'）。"""
    recs = _read(path)
    changed = 0
    for r in recs:
        if r.get("final"):
            continue
        t = f"{r['ticker']}.T" if market == "jp" else r["ticker"]
        try:
            df = fetch(t)
        except Exception:                                   # noqa: BLE001
            continue
        if df is None or len(df) == 0:
            continue
        upd = judge(r, df, market)
        if upd:
            r.update(upd)
            changed += 1
    if changed:
        _write(path, recs)
    return changed


def summary_lines(path: Path, market: str) -> list[str]:
    """公開ログに出す集計（銘柄は出さない）。"""
    recs = [r for r in _read(path) if r.get("market") == market]
    if not recs:
        return [f"ict_track {market} records=0"]
    out = [f"ict_track {market} records={len(recs)} days={len({r['date'] for r in recs})} first={min(r['date'] for r in recs)}"]
    for kind in ("③", "②"):
        g = [r for r in recs if r["kind"] == kind]
        done = [r for r in g if r.get("final") and r.get("ret") is not None]
        skip = sum(1 for r in g if r.get("result") == "見送り")
        pend = sum(1 for r in g if not r.get("final"))
        if done:
            w = sum(r["ret"] for r in done if r["ret"] > 0)
            l = -sum(r["ret"] for r in done if r["ret"] <= 0)
            pf = f"{w / l:.2f}" if l else "-"
            out.append(f"ict_track {market} {kind}: 記録{len(g)} 決着{len(done)} 勝率{100 * sum(1 for r in done if r['ret'] > 0) / len(done):.0f}% "
                       f"平均{100 * sum(r['ret'] for r in done) / len(done):+.2f}% 損益比{pf} 見送り{skip} 途中{pend}")
        else:
            out.append(f"ict_track {market} {kind}: 記録{len(g)} 決着0 見送り{skip} 途中{pend}")
    return out
