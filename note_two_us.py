#!/usr/bin/env python3
"""米株の note 2本立て（2026-10-16 持ち主の指示「米株も同じようにして」）。

  (A) 米株 高値更新・押し目・セリングクライマックス 一覧（S&P500 ＋ NASDAQ大型）
  (B) Claudeが$20,000運用（us_portfolio.save_us_portfolio_note が作る記事をそのまま使う。
      台帳との突き合わせは validate_us_notes が済ませている）

日本株（note_two.py）と同じ4つの条件と、押し目の箱（us_ict.py、2026-10-25）で、直前の米国営業日の終値で並べる。
高値更新などには印を付けない:
日本株の印（高値更新の SS〜C、押し目の注意、セリングクライマックスの◎）を米株の5年
（511銘柄、2026-10-16）で同じ決まりで確かめると、期間ごとの並びがそろわなかった
（高値更新は 2022-23 年に C が最も良い 1.43 など）。米株だけで探しても、4期間でそろった差は
「セリングクライマックスで25日線に近い（浅い）もの」が下（0.82 / 0.83 / 0.68 / 0.89）だけで、
これは並べ方（25日線から深い順）と同じ向き。根拠のない順位を見せないため、ランクは付けない。

記事は note 貼り付け用のメールで持ち主に届ける（note_mail.py。2026-10-18 から）。run_us_daily.py の最後の手順から呼ぶ。
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

import alert_rank_research as R
import us_ict
from note_legal import COMPLIANCE_LINES
from note_two import body_html, write_outputs

ROOT = Path(__file__).resolve().parent
DISCLAIMER = "これは条件に当てはまった銘柄の一覧です。売買をすすめるものではありません。投資の判断はご自身でお願いします。"
MIN_TURNOVER_USD = 1e7      # 20日平均の売買代金（押し目・セリングクライマックス）


def _r(df: pd.DataFrame) -> dict:
    return {"dates": [d.date() if hasattr(d, "date") else d for d in df.index],
            "closes": df["Close"].astype(float).tolist(), "vols": df["Volume"].fillna(0).astype(float).tolist(),
            "ohlc": {k: df[K].astype(float).tolist() for k, K in (("open", "Open"), ("high", "High"), ("low", "Low"))}}


COLUMNS = ("Open", "High", "Low", "Close", "Volume")


def _card_facts(df: pd.DataFrame, vo) -> dict:
    """52週高値更新の紹介に使う数字（日本株の記事の SS・S の紹介と同じ並び）。最後の足が判定の日。"""
    hi = df["High"].astype(float).to_numpy()
    prev = df.iloc[-253:-1]
    k = int(np.argmax(prev["High"].astype(float).to_numpy()))
    line = float(prev["High"].astype(float).iloc[k])
    d = prev.index[k]
    va20 = float(np.mean(vo[-21:-1])) if len(vo) > 21 else 0.0
    close = float(df["Close"].astype(float).iloc[-1])
    prev_close = float(df["Close"].astype(float).iloc[-2])
    return {"close": close, "high": float(hi[-1]), "line": line,
            "line_date": d.date() if hasattr(d, "date") else d,
            "excess": (float(hi[-1]) / line - 1) * 100 if line else 0.0,
            "vol_ratio": float(vo[-1]) / va20 if va20 else 0.0,
            "chg": (close / prev_close - 1) * 100 if prev_close else 0.0}


def _usd(v: float) -> str:
    return f"${v / 1e9:.1f}B" if v >= 1e9 else f"${v / 1e6:.0f}M"


def card_lines(rows: list[dict], n: int = 5) -> list[str]:
    """日本株の記事の「過去の成績ランク SS・S の銘柄」と同じ形の紹介（ランクは付けない）。"""
    out = []
    for x in rows[:n]:
        out += [f"**{x['ticker']} {x['name']}**",
                f"終値:${x['close']:,.2f}（前日比 {x['chg']:+.2f}%） / 当日高値:${x['high']:,.2f} / 種別:52週高値更新",
                f"52週高値ライン:${x['line']:,.2f}（{x['line_date']}の高値） / 本日${x['high']:,.2f}まで"
                f" {x['excess']:+.2f}% 上抜け",
                f"出来高比:{x['vol_ratio']:.2f}倍（前日までの20日平均比） / 売買代金:{_usd(x['turnover'])}（20日平均）",
                f"📈 チャート:https://finance.yahoo.com/quote/{x['ticker']}/chart", ""]
    return out


def lists_for_day(day: dt.date, histories: dict[str, pd.DataFrame], names: dict[str, str],
                  stats: dict | None = None) -> dict[str, list[dict]]:
    """day の終値で、4つの条件に当てはまった銘柄（それぞれ並べ済み）。最後の足が day でない銘柄は使わない。
    株価が取れなかった銘柄（空の表・列の欠け）は飛ばす（2026-10-20 に、取れなかった銘柄の空の表で
    KeyError: ['Close'] となり、米株の記事が1本も作れなかった）。stats には使えた銘柄の数を入れる。"""
    hi52, swing, pb, sc, ict = [], [], [], [], []
    used = 0
    for t, df in histories.items():
        if df is None or df.empty or any(c not in df.columns for c in COLUMNS):
            continue
        df = df.dropna(subset=["Close"])
        df = df[[(d.date() if hasattr(d, "date") else d) <= day for d in df.index]]
        if len(df) < 260 or (df.index[-1].date() if hasattr(df.index[-1], "date") else df.index[-1]) != day:
            continue
        used += 1
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
                if e["kind"] == "52W":
                    hi52.append({**row, **_card_facts(df, vo)})
                else:
                    swing.append(row)
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
        # 押し目の箱（オーダーブロック）。2026-10-25 持ち主「この動画と同じトレードがしたい銘柄を米株から」（us_ict.py）
        try:
            st_ = us_ict.state(df.iloc[-260:])
        except Exception:                                   # noqa: BLE001
            st_ = None
        if st_:
            ict.append({**row, **st_})
        # セリングクライマックス（日本株のスクリーナーと同じ: 出来高が前日までの20日平均の2倍以上、
        # 現在値 < 25日線 < 75日線、25日線が5本前より低い）
        va20 = float(np.mean(vo[-21:-1]))
        if va20 and vo[-1] / va20 >= 2.0 and cl[-1] < ma[25][-1] < ma[75][-1] and ma[25][-1] < ma[25][-6]:
            sc.append({**row, "dev25": (cl[-1] / ma[25][-1] - 1) * 100, "ratio": vo[-1] / va20})
    if stats is not None:
        stats.update(used=used, total=len(histories))
    by_turnover = lambda L: sorted(L, key=lambda x: -x["turnover"])  # noqa: E731
    return {"hi52": by_turnover(hi52), "swing": by_turnover(swing), "pullback": by_turnover(pb),
            "climax": sorted(sc, key=lambda x: x["dev25"]), "ict": ict}


def ranking_lines(day: dt.date, L: dict[str, list[dict]], coverage: tuple[int, int] | None = None) -> tuple[str, list[str]]:
    def top10(title, rows, extra=lambda x: ""):
        out = [f"## {title} TOP10（全{len(rows)}件）", ""]
        out += [f"{i}. {x['ticker']} {x['name']}{extra(x)}" for i, x in enumerate(rows[:10], 1)]
        return out + ([] if rows else ["該当なし。"]) + [""]
    lines = [f"{day}（米国時間）の終値で、S&P500 と NASDAQ の大型株から、5つの条件に当てはまった銘柄を機械的に並べた一覧です。",
             "**売買をすすめるものではありません。**", ""]
    if coverage and coverage[1] and coverage[0] < coverage[1] * 0.95:
        # 取れなかった銘柄を黙って落とさない（件数が少なく見えるのはそのため、と分かるように）
        lines += [f"※ この日は {coverage[1]}銘柄のうち {coverage[0]}銘柄しか株価が取れませんでした。"
                  "取れなかった銘柄は、この一覧に入っていません。", ""]
    if L["hi52"]:
        # 2026-10-22 持ち主「米株も日本株と同じようにして」。日本株の記事の SS・S の紹介と同じ形で、
        # 売買代金の大きい上位5件を紹介する。ランクは付けない（下の「ランクを付けていない理由」）
        lines += [f"## 52週高値更新の上位{min(5, len(L['hi52']))}件（売買代金の大きい順）", ""] + card_lines(L["hi52"])
    lines += top10("52週高値更新（売買代金の大きい順）", L["hi52"])
    lines += top10("直近高値更新（売買代金の大きい順）", L["swing"])
    lines += top10("25MA・200MA 押し目（売買代金の大きい順）", L["pullback"], lambda x: f"（{x['kind']}）")
    lines += top10("セリングクライマックス（下降中の出来高急増。25日線から深い順）", L["climax"],
                   lambda x: f"（25日線から{x['dev25']:+.1f}%・出来高{x['ratio']:.1f}倍）")
    lines += us_ict.section_lines(L.get("ict", []))
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

        # 先に動く米株のスクリーニング（run_screening_us）と同じ18か月分にそろえ、その控え（cache/prices）を
        # そのまま使う。14か月分にしていたので同じ株価をもう一度取りに行き、2026-10-20 は Yahoo の一括取得が
        # すべて時間切れになった（日本株の note_two.py と同じ直し方）。
        tickers = list(names)
        prefetch_price_histories(tickers, period="18mo")
        histories = {}
        for t in tickers:
            try:
                histories[t] = fetch_price_history(t, period="18mo")
            except Exception:                                # noqa: BLE001
                continue
    stats: dict = {}
    L = lists_for_day(session, histories, names, stats)
    used, total = stats.get("used", 0), max(len(names), stats.get("total", 0))
    print(f"note_two_us_coverage used={used} total={total}", flush=True)
    if total and used < total * 0.5:
        # 半分も取れない日は、一覧として意味をなさないので作らない（呼び出し側が失敗として扱う）
        raise RuntimeError(f"株価が取れた銘柄が少ない（{used}/{total}）")
    t_a, l_a = ranking_lines(session, L, (used, total))
    # 押し目の箱で記事に出した銘柄のその後（ict_track.py）。転んでも記事づくりは止めない
    try:
        import ict_track as IT

        added = IT.record(session, "us", L.get("ict"), IT.TRACK_US)
        judged = IT.evaluate(lambda t: histories.get(t), IT.TRACK_US, "us")
        print(f"ict_track_added={added} judged={judged}", flush=True)
        for line in IT.summary_lines(IT.TRACK_US, "us"):
            print(f"::notice title=ict track::{line}", flush=True)
    except Exception as error:                                     # noqa: BLE001
        print(f"ict_track=failed reason={type(error).__name__}", flush=True)
    pmd = Path(output_dir) / "note_us_portfolio.md"
    ptitle = Path(output_dir) / "note_us_portfolio_title.txt"
    b_lines = [x for x in pmd.read_text(encoding="utf-8").splitlines() if not x.startswith("# ")]
    # 2026-10-26 持ち主「米株も…ICT手法の記事織り込んで」。運用の決まりは変えず、参考として上位5件
    b_lines += [""] + us_ict.section_lines(L.get("ict", []), limit=5, market="us", brief=True)
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
