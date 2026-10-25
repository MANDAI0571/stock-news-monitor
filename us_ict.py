#!/usr/bin/env python3
"""米株・日本株の「押し目の箱（オーダーブロック）」の形（ICT の考え方を機械的に決められる形にしたもの。2026-10-25 追加）。

持ち主が共有した動画（ICT 手法の簡略版）の手順を日足に当てはめる:
  ① 直前の戻り高値（前後3本より高い高値）を終値で上抜いた日を節目とする。
     レンジ = 上昇の始まりの安値（その戻り高値から上抜けまでの最安値）〜 上抜け後の最高値。
     箱 = 始まりの安値の日か、それより前（10本以内）の最後の陰線（終値 < 始値）の高値〜安値。
     箱の上端がレンジの真ん中（50%線）以下＝割安ゾーンのものだけ。
  ② 株価が箱まで戻る（安値が箱の上端以下）のを待つ。終値がレンジの安値を割ったら、その形は終わり。
  ③ 箱に入ってから10営業日以内に、直近3日の安値を割った日があり、その後に直近3日の高値を終値で上抜いた。
     （動画は1時間足で見る。ここでは日足で代わりに見る）
  目安: 損切り = 割った安値（③が出ていなければ箱の下端）の0.2%下、利確 = レンジの高値。
        利幅が損切り幅の2倍未満のものは出さない（動画の決まり）。

検証（stock-volume-screener/backtest/us_ict_study.py・us_ict_survivor.py・us_ict_hourly.py。翌営業日の寄付で買い、
損切り・利確・40営業日で手じまい、コスト往復0.1%）:
  2021年9月時点の S&P500 で株価が取れた454銘柄（途中で外れた44銘柄を含む）、日足5年、①②③ 1,562回:
    損益比 1.35 / 2.33 / 2.59 / 3.00（2022 / 2023 / 2024 / 2025-26）。同じ損切り・2倍の利確でいつでも入ると 0.79 / 1.20 / 1.13 / 1.14。
  ③を1時間足で見た約2年（2023-12〜2026-10）: 4.15 / 2.67 / 2.61、いつでも 1.33 / 1.28 / 1.13。
  限界: 買収・上場廃止で株価が取れなかった51銘柄は入っていない。条件は動画のとおりで、こちらで選び直してはいない。
  日本株（東証プライム・売買代金1億円以上、日足5年、コスト往復0.2%。backtest/jp_ict_study.py）: 3,923回、損益比
    1.16 / 3.18 / 2.05 / 3.12、いつでも 0.74 / 1.26 / 0.97 / 1.34。2026-10-25 にランキング記事にも節を足した（note_two.py）。
売買をすすめるものではない。
"""
from __future__ import annotations

K, CONFIRM_DAYS, OB_BACK = 3, 10, 10
PF_DAILY = (1.35, 2.33, 2.59, 3.00)
PF_ANY = (0.79, 1.20, 1.13, 1.14)
PF_HOURLY = (4.15, 2.67, 2.61)
PF_HOURLY_ANY = (1.33, 1.28, 1.13)


def latest_setup(op, hi, lo, cl):
    """最後の足の時点で生きている（終わっていない）いちばん新しい形。無ければ None。"""
    n = len(cl)
    piv_h = [i for i in range(K, n - K) if hi[i] == max(hi[i - K:i + K + 1])]
    last_ph, pi, t, found = None, 0, K, None
    while t < n:
        while pi < len(piv_h) and piv_h[pi] + K <= t - 1:
            last_ph = piv_h[pi]
            pi += 1
        if last_ph is not None and cl[t] > hi[last_ph] and cl[t - 1] <= hi[last_ph]:
            s, b = last_ph, t
            l0 = min(range(s, b + 1), key=lambda i: lo[i])
            ob = next((i for i in range(l0, max(l0 - OB_BACK, 0) - 1, -1) if cl[i] < op[i]), None)
            if ob is None:
                t += 1
                continue
            st = {"b": b, "rng_lo": lo[l0], "rng_hi": hi[b], "box_lo": lo[ob], "box_hi": hi[ob],
                  "touch": None, "end": None}
            for j in range(b + 1, n):
                if st["touch"] is None and hi[j] > st["rng_hi"]:
                    st["rng_hi"] = hi[j]
                mid = (st["rng_lo"] + st["rng_hi"]) / 2
                if cl[j] < st["rng_lo"]:
                    st["end"] = j
                    break
                if st["touch"] is None and lo[j] <= st["box_hi"]:
                    if st["box_hi"] <= mid:
                        st["touch"] = j
                    else:
                        st["end"] = j
                        break
            found = st
            t = (st["touch"] or st["end"] or b) + 1
            continue
        t += 1
    return found if found and found["end"] is None else None


def confirm(hi, lo, cl, touch):
    """箱に入ってから CONFIRM_DAYS 以内の「直近3日の安値割れ → 直近3日の高値を終値で上抜け」。(その日, 割った安値)。"""
    swept = None
    for j in range(max(touch, 3), min(len(cl), touch + CONFIRM_DAYS + 1)):
        if lo[j] < min(lo[j - 3:j]):
            swept = lo[j] if swept is None else min(swept, lo[j])
        elif swept is not None and cl[j] > max(hi[j - 3:j]):
            return j, swept
    return None, None


def state(df, max_age=120):
    """日足（Open/High/Low/Close）の最後の足の時点の状態。当てはまらなければ None。
    返す dict: kind（"③" / "②"）, close, rng_lo, rng_hi, box_lo, box_hi, stop, target, rr"""
    df = df[["Open", "High", "Low", "Close"]].dropna()
    op, hi = [float(x) for x in df["Open"]], [float(x) for x in df["High"]]
    lo, cl = [float(x) for x in df["Low"]], [float(x) for x in df["Close"]]
    n = len(cl)
    if n < 60:
        return None
    s = latest_setup(op, hi, lo, cl)
    L = n - 1
    if s is None or L - s["b"] > max_age or s["touch"] is None:
        return None
    c = cl[L]
    if c >= s["rng_hi"] or s["box_hi"] > (s["rng_lo"] + s["rng_hi"]) / 2:
        return None
    j, swept = confirm(hi, lo, cl, s["touch"])
    if j is not None:
        stop = swept * 0.998
        if L - j > 5 or min(lo[j + 1:], default=float("inf")) <= stop or max(hi[j + 1:], default=0) >= s["rng_hi"]:
            return None                       # 5営業日より前の成立、またはもう損切り・利確に触れた
        kind = "③"
    elif L - s["touch"] <= CONFIRM_DAYS:
        stop, kind = s["box_lo"] * 0.998, "②"
    else:
        return None
    if c <= stop:
        return None
    rr = (s["rng_hi"] - c) / (c - stop)
    if rr < 2:
        return None                           # 利幅が損切り幅の2倍未満は出さない（動画の決まり）
    return {"kind": kind, "close": c, "rng_lo": s["rng_lo"], "rng_hi": s["rng_hi"], "box_lo": s["box_lo"],
            "box_hi": s["box_hi"], "stop": stop, "target": s["rng_hi"], "rr": rr}


PF_JP = (1.16, 3.18, 2.05, 3.12)
PF_JP_ANY = (0.74, 1.26, 0.97, 1.34)


def section_lines(rows: list[dict], limit: int = 15, market: str = "us") -> list[str]:
    """一覧記事の節。rows は state() の結果に ticker / name / turnover を足したもの。
    market="jp" は日本株のランキング記事用（円で書き、日本株の過去の数字を出す。2026-10-25）。"""
    pf = lambda xs: " / ".join(f"{x:.2f}" for x in xs)  # noqa: E731
    import re

    rows = sorted(rows, key=lambda x: (x["kind"] != "③", -x.get("turnover", 0)))
    # 同じ会社の別の株（Alphabet の Class A と Class C など）は、売買代金の大きい方の1行だけにする（2026-10-26）
    seen, uniq = set(), []
    for x in rows:
        key = re.sub(r"\s*\(Class [A-Z]\)", "", str(x.get("name", ""))).strip() or x["ticker"]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(x)
    rows = uniq
    out = [f"## 押し目の箱（オーダーブロック）まで戻った銘柄（{len(rows)}件・③が先、その中は売買代金の大きい順）", "",
           "上昇の節目（直前の戻り高値を終値で上抜け）のあと、上昇が始まる直前の最後の陰線（箱）まで戻ってきた銘柄です。"
           "箱がレンジ（上昇の始まりの安値〜その後の高値）の真ん中より下にあるものだけを出しています。"
           "③＝箱に入ったあと、直近3日の安値を一度割り、その後に直近3日の高値を終値で上抜いた。②＝箱の中で③待ち。", ""]
    m = (lambda v: f"{v:,.1f}円") if market == "jp" else (lambda v: f"${v:,.2f}")  # noqa: E731
    for i, x in enumerate(rows[:limit], 1):
        out.append(f"{i}. {x['kind']} {x['ticker']} {x['name']}（終値{m(x['close'])} / 箱 {m(x['box_lo'])}〜{m(x['box_hi'])} / "
                   f"損切りの目安 {m(x['stop'])}（{(x['stop'] / x['close'] - 1) * 100:+.1f}%） / "
                   f"利確の目安 {m(x['target'])}（{(x['target'] / x['close'] - 1) * 100:+.1f}%） / 利幅は損切り幅の{x['rr']:.1f}倍）")
    if not rows:
        out.append("本日は該当なし。")
    if len(rows) > limit:
        out.append(f"（ほか{len(rows) - limit}件）")
    rule = "損切りの目安＝割った安値（②は箱の下端）の少し下、利確の目安＝レンジの高値。利幅が損切り幅の2倍未満のものは出していません。"
    if market == "jp":
        facts = (f"過去の数字（東証プライム・売買代金1億円以上・2022〜2026年、③が出た翌営業日の寄付で買い、損切り・利確・40営業日で手じまい、"
                 f"コスト往復0.2%）: 損益比 {pf(PF_JP)}（2022 / 2023 / 2024 / 2025-26）、同じ損切り・2倍の利確でいつでも入った場合 {pf(PF_JP_ANY)}。"
                 "上場廃止になった銘柄は入っていません（数字は良い方に寄ります）。過去の集計で、今後を保証するものではありません。")
    else:
        facts = (f"過去の数字（2021年9月時点のS&P500で株価が取れた454銘柄・2022〜2026年、③が出た翌営業日の寄付で買い、損切り・利確・40営業日で手じまい、"
                 f"コスト往復0.1%）: 損益比 {pf(PF_DAILY)}（2022 / 2023 / 2024 / 2025-26）、同じ損切り・2倍の利確でいつでも入った場合 {pf(PF_ANY)}。"
                 f"③を1時間足で見た約2年では {pf(PF_HOURLY)}（いつでも {pf(PF_HOURLY_ANY)}）。"
                 "買収・上場廃止で株価が取れなかった銘柄は入っていません。過去の集計で、今後を保証するものではありません。")
    out += ["", rule + facts, ""]
    return out
