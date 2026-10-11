#!/usr/bin/env python3
"""ザラ場アラートの「過去の成績ランク」を、実際のアラートのその後で確かめ続ける。2026-10-12 追加。

ランク（alert_grade.py）は過去5年の検証で決めた。条件は全期間を見て選んだので、
別の期間で確かめた数字ではない。そこで、実際にメールに出たアラートを記録し、
検証と同じ決まりで売買したらどうなったかを等級ごとに数え続ける。

決まり（alert_rank_research.sim と同じ）: アラートの日の引けで決め、翌営業日の寄付で買う。
以後の終値が -5% 以下か +12% 以上、または15営業日で、その翌営業日の寄付で手じまう。
売買コスト0.2%を引く。売買をすすめるものではない。

流れ（.github/workflows/alert_grade_track.yml が毎営業日の夕方に動かす。遅れて届いてもよい）
  1. --download DIR : 巡回の成果物 intraday-high-alerts（90日残る）から CSV を取り出す
  2. --collect DIR  : その日はじめてメールに出たアラート（is_new）で、等級の付いたものを
                      data/alert_grade_record.csv に足す（日付と銘柄で1件）
  3. --settle       : 手じまいまで終わった場面の損益を、日足（yfinance）で埋める
  4. 等級ごとの集計を data/alert_grade_forward.json に書く（メールの末尾が読む）
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path

import pandas as pd

from alert_rank_research import COST, STOP, TAKE, TIMEOUT, pf

ROOT = Path(__file__).resolve().parent
RECORD_PATH = ROOT / "data" / "alert_grade_record.csv"
SUMMARY_PATH = ROOT / "data" / "alert_grade_forward.json"
COLUMNS = ["date", "time", "code", "name", "alert_type", "price", "grade", "grade_score",
           "entry_date", "exit_date", "ret_pct", "status"]
GRADES = ("SS", "S", "A", "B", "C")
# この日数たっても日足が取れない場面は「取れない」として数えるのをやめる（上場廃止など）。
GIVE_UP_DAYS = 60
_STAMP = re.compile(r"intraday_high_alerts_(\d{8})_(\d{6})\.csv$")


def sim_detail(op: list, cl: list, t: int):
    """alert_rank_research.sim と同じ決まりで、(損益, 買った足, 手じまった足) を返す。
    決着していなければ None。手じまいは翌営業日の寄付なので、その足が無いうちは決着とみなさない
    （研究側は最後の終値で代用するが、それは過去データの端だけの扱い）。"""
    n = len(cl)
    if t + 1 >= n or not op[t + 1]:
        return None
    entry = op[t + 1]
    for k in range(t + 1, n):
        pnl = cl[k] / entry - 1
        if pnl <= STOP or pnl >= TAKE or k - t >= TIMEOUT:
            if k + 1 >= n or not op[k + 1]:
                return None
            return op[k + 1] / entry - 1 - COST, t + 1, k + 1
    return None


def load_record(path: Path = RECORD_PATH) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = ""
    return df[COLUMNS]


def collect(paths: list[Path]) -> pd.DataFrame:
    """巡回の CSV から、その日はじめて出たアラートで等級の付いたものを集める。"""
    rows = []
    for p in paths:
        m = _STAMP.search(p.name)
        if not m:
            continue
        day = dt.datetime.strptime(m.group(1), "%Y%m%d").date().isoformat()
        hhmm = f"{m.group(2)[:2]}:{m.group(2)[2:4]}"
        try:
            df = pd.read_csv(p, dtype=str, keep_default_na=False, encoding="utf-8-sig")
        except Exception as exc:                      # noqa: BLE001
            print(f"skip {p.name}: {exc}", flush=True)
            continue
        if "grade" not in df.columns or "is_new" not in df.columns:
            continue
        for r in df.to_dict("records"):
            if str(r.get("is_new")).strip().lower() != "true" or r.get("grade") not in GRADES:
                continue
            rows.append({"date": day, "time": hhmm, "code": str(r["code"]).strip().upper(),
                         "name": r.get("name", ""), "alert_type": r.get("alert_type", ""),
                         "price": r.get("current_price", ""), "grade": r["grade"],
                         "grade_score": r.get("grade_score", ""), "entry_date": "",
                         "exit_date": "", "ret_pct": "", "status": ""})
    out = pd.DataFrame(rows, columns=COLUMNS)
    if out.empty:
        return out
    # 同じ日に同じ銘柄が何度出ても（種別違い・別の巡回）、最初の1件だけを数える。
    out = out.sort_values(["date", "time"]).drop_duplicates(["date", "code"], keep="first")
    return out.reset_index(drop=True)


def merge(record: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """記録済みの行（損益を埋めたもの）を優先して足す。"""
    if new.empty:
        return record
    both = pd.concat([record, new], ignore_index=True)
    both = both.drop_duplicates(["date", "code"], keep="first")
    return both.sort_values(["date", "time", "code"]).reset_index(drop=True)


def fetch_daily(codes: list[str], start: dt.date) -> dict[str, pd.DataFrame]:
    """yfinance で日足（調整なし）を取る。今日の足は途中の値なので使わない。"""
    import yfinance as yf

    today = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)).date()
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(codes), 100):
        chunk = codes[i:i + 100]
        tickers = [f"{c}.T" for c in chunk]
        data = yf.download(tickers, start=start.isoformat(),
                           end=(today + dt.timedelta(days=1)).isoformat(), interval="1d",
                           auto_adjust=False, group_by="ticker", threads=True, progress=False)
        for c, t in zip(chunk, tickers):
            try:
                df = data
                if isinstance(df.columns, pd.MultiIndex):
                    if t not in df.columns.get_level_values(0):
                        continue
                    df = df[t]
                df = df[["Open", "Close"]].dropna()
                df = df[[d.date() < today for d in df.index]]
                if not df.empty:
                    out[c] = df
            except Exception as exc:                  # noqa: BLE001
                print(f"no data {c}: {exc}", flush=True)
    return out


def settle(record: pd.DataFrame, fetch=fetch_daily, today: dt.date | None = None) -> pd.DataFrame:
    """決着していない場面の損益を埋める。"""
    today = today or (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)).date()
    pending = record[record["status"] == ""]
    if pending.empty:
        return record
    start = dt.date.fromisoformat(min(pending["date"])) - dt.timedelta(days=7)
    bars = fetch(sorted(set(pending["code"])), start)
    for i, r in pending.iterrows():
        day = dt.date.fromisoformat(r["date"])
        df = bars.get(r["code"])
        res = None
        if df is not None:
            dates = [d.date() if hasattr(d, "date") else d for d in df.index]
            if day in dates:
                t = dates.index(day)
                res = sim_detail([float(x) for x in df["Open"]], [float(x) for x in df["Close"]], t)
        if res:
            ret, a, b = res
            record.at[i, "entry_date"] = dates[a].isoformat()
            record.at[i, "exit_date"] = dates[b].isoformat()
            record.at[i, "ret_pct"] = f"{ret * 100:.3f}"
            record.at[i, "status"] = "done"
        elif (today - day).days > GIVE_UP_DAYS:
            record.at[i, "status"] = "no_data"
    return record


def summarize(record: pd.DataFrame, now: dt.datetime | None = None) -> dict:
    now = now or (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9))
    by = {}
    for g in GRADES:
        sub = record[record["grade"] == g]
        rets = [float(x) / 100 for x in sub.loc[sub["status"] == "done", "ret_pct"]]
        p = pf(rets) if rets else None
        by[g] = {"done": len(rets), "pending": int((sub["status"] == "").sum()),
                 "pf": round(p, 2) if p is not None else None,
                 "win": round(sum(1 for x in rets if x > 0) / len(rets), 3) if rets else None,
                 "avg_pct": round(sum(rets) / len(rets) * 100, 2) if rets else None}
    return {"since": min(record["date"]) if len(record) else None,
            "updated": now.strftime("%Y-%m-%d %H:%M"),
            "recorded": int(len(record)),
            "no_data": int((record["status"] == "no_data").sum()),
            "grades": by}


def download_artifacts(dest: Path, days: int) -> list[Path]:
    """成果物 intraday-high-alerts のうち、直近 days 日ぶんの CSV を取り出す。"""
    import requests

    repo = os.environ["GITHUB_REPOSITORY"]
    head = {"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json"}
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    dest.mkdir(parents=True, exist_ok=True)
    got, page = [], 1
    while True:
        r = requests.get(f"https://api.github.com/repos/{repo}/actions/artifacts",
                         params={"name": "intraday-high-alerts", "per_page": 100, "page": page},
                         headers=head, timeout=30)
        r.raise_for_status()
        arts = r.json().get("artifacts", [])
        if not arts:
            break
        for a in arts:
            created = dt.datetime.fromisoformat(a["created_at"].replace("Z", "+00:00"))
            if a.get("expired") or created < since:
                continue
            z = requests.get(a["archive_download_url"], headers=head, timeout=60)
            z.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
                for n in zf.namelist():
                    if _STAMP.search(Path(n).name):
                        p = dest / Path(n).name
                        p.write_bytes(zf.read(n))
                        got.append(p)
        if len(arts) < 100:
            break
        page += 1
    print(f"downloaded {len(got)} CSV files", flush=True)
    return got


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--download", type=Path, help="成果物から CSV を取り出す先")
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--collect", type=Path, help="巡回の CSV があるフォルダ")
    ap.add_argument("--settle", action="store_true")
    a = ap.parse_args(argv)
    if a.download:
        download_artifacts(a.download, a.days)
    record = load_record()
    if a.collect:
        new = collect(sorted(a.collect.glob("intraday_high_alerts_*.csv")))
        before = len(record)
        record = merge(record, new)
        print(f"collected {len(new)} / added {len(record) - before}", flush=True)
    if a.settle:
        record = settle(record)
    RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    record.to_csv(RECORD_PATH, index=False)
    summary = summarize(record)
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
