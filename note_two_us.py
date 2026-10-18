#!/usr/bin/env python3
"""米株の note 2本立て（2026-10-16 持ち主の指示「米株も同じようにして」）。

  (A) 米株 高値更新・押し目・セリングクライマックス 一覧（S&P500 ＋ NASDAQ大型）
  (B) Claudeが$20,000運用（us_portfolio.save_us_portfolio_note が作る記事をそのまま使う。
      台帳との突き合わせは validate_us_notes が済ませている）

日本株（note_two.py）と同じ4つの条件で、直前の米国営業日の終値で並べる。ただし印は付けない:
日本株の印（高値更新の SS〜C、押し目の注意、セリングクライマックスの◎）を米株の5年
（511銘柄、2026-10-16）で同じ決まりで確かめると、期間ごとの並びがそろわなかった
（高値更新は 2022-23 年に C が最も良い 1.43 など）。米株だけで探しても、4期間でそろった差は
「セリングクライマックスで25日線に近い（浅い）もの」が下（0.82 / 0.83 / 0.68 / 0.89）だけで、
これは並べ方（25日線から深い順）と同じ向き。根拠のない順位を見せないため、ランクは付けない。

下書きの保存は note_autosave.py（公開しない）。run_us_daily.py の最後の手順から呼ぶ。
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

import alert_rank_research as R
from note_legal import COMPLIANCE_LINES
from note_two import body_html, write_outputs

ROOT = Path(__file__).resolve().parent
DISCLAIMER = "これは条件に当てはまった銘柄の一覧です。売買をすすめるものではありません。投資の判断はご自身でお願いします。"
MIN_TURNOVER_USD = 1e7      # 20日平均の売買代金（押し目・セリングクライマックス）


def _r(df: pd.DataFrame) -> dict:
    return {"dates": [d.date() if hasattr(d, "date") else d for d in df.index],
            "closes": df["Close"].astype(float).tolist(), "vols": df["Volume"].fillna(0).astype(float).tolist(),
            "ohlc": {k: df[K].astype(float).tolist() for k, K in (("open", "Open"), ("high", "High"), ("low", "Low"))}}


def lists_for_day(day: dt.date, histories: dict[str, pd.DataFrame], names: dict[str, str]) -> dict[str, list[dict]]:
    """day の終値で、4つの条件に当てはまった銘柄（それぞれ並べ済み）。最後の足が day でない銘柄は使わない。"""
    hi52, swing, pb, sc = [], [], [], []
    for t, df in histories.items():
        df = df.dropna(subset=["Close"])
        df = df[[(d.date() if hasattr(d, "date") else d) <= day for d in df.index]]
        if len(df) < 260 or (df.index[-1].date() if hasattr(df.index[-1], "date") else df.index[-1]) != day:
            continue
        cl = df["Close"].astype(float).to_numpy()
        vo = df["Volume"].fillna(0).astype(float).to_numpy()
        to20 = float(np.mean(cl[-20:] * vo[-20:]))
        row = {"ticker": t, "name": names.get(t, t), "turnover": to20}
        # 高値更新（ザラ場アラートと同じ定義。alert_rank_research.events_for の最後の足）
        r = _r(df.iloc[-300:])
        # events_for は翌日の寄付で買う前提で最後の足を見ないので、仮の足を1本足して最後の足まで見せる
        # （判定はその日までの値だけで決まり、仮の足は損益の計算にしか使われない）
        for k in ("closes", "vols"):
            r[k].append(r[k][-1])
        for k in ("open", "high", "low"):
            r["ohlc"][k].append(r["ohlc"][k][-1])
        r["dates"].append(day + dt.timedelta(days=1))
        for e in R.events_for(t, r, {}):
            if e["date"] == day:
                (hi52 if e["kind"] == "52W" else swing).append(row)
        s = pd.Series(cl)
        ma = {k: s.rolling(k).mean().to_numpy() for k in (25, 75, 200, 240)}
        if to20 < MIN_TURNOVER_USD:
            continue
        # 押し目（日本株と同じ: 52週高値から1〜120日、25/200/240日線が上向き、上から3%以内）
        if all(ma[k][-1] - ma[k][-6] > 0 for k in (25, 200, 240)):
            w = cl[-252:]
            last = 251 - int(np.where(w == w.max())[0][-1])
            if 1 <= last <= 120:
                kinds = []
                for k in (25, 200):
                    gap = (cl - ma[k]) / ma[k] * 100
                    if abs(gap[-1]) <= 3 and np.nanmax(gap[-11:-1]) > 3:
                        kinds.append(f"{k}MA")
                if kinds:
                    pb.append({**row, "kind": "・".join(kinds)})
        # セリングクライマックス（日本株のスクリーナーと同じ: 出来高が前日までの20日平均の2倍以上、
        # 現在値 < 25日線 < 75日線、25日線が5本前より低い）
        va20 = float(np.mean(vo[-21:-1]))
        if va20 and vo[-1] / va20 >= 2.0 and cl[-1] < ma[25][-1] < ma[75][-1] and ma[25][-1] < ma[25][-6]:
            sc.append({**row, "dev25": (cl[-1] / ma[25][-1] - 1) * 100, "ratio": vo[-1] / va20})
    by_turnover = lambda L: sorted(L, key=lambda x: -x["turnover"])  # noqa: E731
    return {"hi52": by_turnover(hi52), "swing": by_turnover(swing), "pullback": by_turnover(pb),
            "climax": sorted(sc, key=lambda x: x["dev25"])}


def ranking_lines(day: dt.date, L: dict[str, list[dict]]) -> tuple[str, list[str]]:
    def top10(title, rows, extra=lambda x: ""):
        out = [f"## {title} TOP10（全{len(rows)}件）", ""]
        out += [f"{i}. {x['ticker']} {x['name']}{extra(x)}" for i, x in enumerate(rows[:10], 1)]
        return out + ([] if rows else ["該当なし。"]) + [""]
    lines = [f"{day}（米国時間）の終値で、S&P500 と NASDAQ の大型株から、4つの条件に当てはまった銘柄を機械的に並べた一覧です。",
             "**売買をすすめるものではありません。**", ""]
    lines += top10("52週高値更新（売買代金の大きい順）", L["hi52"])
    lines += top10("直近高値更新（売買代金の大きい順）", L["swing"])
    lines += top10("25MA・200MA 押し目（売買代金の大きい順）", L["pullback"], lambda x: f"（{x['kind']}）")
    lines += top10("セリングクライマックス（下降中の出来高急増。25日線から深い順）", L["climax"],
                   lambda x: f"（25日線から{x['dev25']:+.1f}%・出来高{x['ratio']:.1f}倍）")
    lines += ["## ランクを付けていない理由", "",
              "日本株の記事で使っている印（高値更新の SS〜C、押し目の注意、セリングクライマックスの◎）を、"
              "米株の過去5年（2022〜2026年）で同じ決まり（翌営業日の寄付で入り、終値で-5%か+12%、または15営業日で"
              "手じまい、売買コスト0.2%込み）で確かめると、期間ごとの並びがそろいませんでした。"
              "米株だけで探しても、4つの期間でそろった差はセリングクライマックスの「25日線に近い（浅い）ものは成績が下」"
              "だけでした（並べ方の「深い順」と同じ向きです）。そのため、米株にはランクを付けていません。",
              "", "---", ""] + COMPLIANCE_LINES
    return f"米株 高値更新・押し目・セリングクライマックス 一覧｜{day}", lines


def build(session: dt.date, output_dir: Path, histories: dict[str, pd.DataFrame] | None = None) -> Path:
    """2本を output_dir/note_two_us に書き、その場所を返す。"""
    from scanner.universe_us import load_us_universe

    uni = load_us_universe()
    names = dict(zip(uni["ticker"].astype(str), uni["name"].astype(str)))
    if histories is None:
        from scanner.prices import fetch_price_history, prefetch_price_histories

        tickers = list(names)
        prefetch_price_histories(tickers, period="14mo")
        histories = {}
        for t in tickers:
            try:
                histories[t] = fetch_price_history(t, period="14mo")
            except Exception:                                # noqa: BLE001
                continue
    L = lists_for_day(session, histories, names)
    t_a, l_a = ranking_lines(session, L)
    pmd = Path(output_dir) / "note_us_portfolio.md"
    ptitle = Path(output_dir) / "note_us_portfolio_title.txt"
    b_lines = [x for x in pmd.read_text(encoding="utf-8").splitlines() if not x.startswith("# ")]
    b_lines += ["", "---", ""] + COMPLIANCE_LINES
    t_b = ptitle.read_text(encoding="utf-8").strip()
    out = Path(output_dir) / "note_two_us"
    write_outputs(out, [("us_ranking", t_a, l_a), ("us_20k", t_b, b_lines)])
    print(f"note_two_us_built session={session} hi52={len(L['hi52'])} swing={len(L['swing'])} "
          f"pullback={len(L['pullback'])} climax={len(L['climax'])}", flush=True)
    return out


def save_drafts(note_dir: Path, session=None) -> bool:
    """2本を note 貼り付け用のメールで持ち主に届ける（2026-10-18 から。note は reCAPTCHA で
    自動ログインできないため、下書きへの自動保存はやめた）。名前は呼び出し側との互換のため残す。"""
    import note_mail

    day = f"{session} " if session else ""
    return note_mail.send(Path(note_dir), f"【note貼り付け用】{day}米株（一覧・Claudeの$20,000運用）")
