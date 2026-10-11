"""ザラ場アラートの「過去の成績ランク」（SS / S / A / B / C）。2026-10-12 追加。

ランクは「過去5年で、似た形のアラートがその後どうなったか」の数字であって、
売買をすすめるものではない。決め方と数字の出所は alert_rank_research.py。

点数（すべて足す）
  +1  52週高値まで -5%〜0%（年初来の高値のすぐ下でのブレイク）
  +1  引けの位置が当日の値幅の下3割（高値から押し戻されている）
  -1  引けの位置が当日の値幅の上3割
  -1  業種の勢いが弱い（その業種の20日騰落の平均が、全銘柄の中央値より2ポイント以上低い）
  -1  52週高値まで -15%より下（直近高値は抜けたが、年初来の高値からは遠い）
等級  2以上=SS / 1=S / 0=A / -1=B / -2以下=C

検証（東証プライム、2022-2026年、125,779場面。翌営業日の寄付で入り、終値で-5%か+12%、
または15営業日で、その翌営業日の寄付で手じまい。売買コスト0.2%込み）での損益比:
  期間ごとに C は4期間とも最下位、A・B・C の並びは4期間でそろった。SS と S はほぼ同じ。
限界: 条件は全期間を見て選んだので、別の期間で確かめた数字ではない。最初に試した
多くの条件で作ったランクは、2025-26年に順位が逆転した。ザラ場の途中の値段で判定する
（検証は大引けの値）。プライム以外の市場は検証していない。
"""

from __future__ import annotations

import statistics as st

# 期間ごとの損益比（alert_rank_research / rank2 の実測。2026-10-12）
PERIODS = ("2022-23", "2024", "2025", "2026")
GRADE_PF = {
    "SS": (1.10, 0.99, 1.84, 1.14),
    "S": (1.17, 0.98, 1.72, 1.23),
    "A": (1.08, 0.90, 1.44, 1.12),
    "B": (0.98, 0.87, 1.45, 1.07),
    "C": (0.92, 0.81, 0.99, 0.82),
}
GRADES = ("SS", "S", "A", "B", "C")


def facts_from_history(history) -> dict | None:
    """日足（当日の途中の足を含む）から、等級に使う数字を出す。取れなければ None。"""
    try:
        if history is None or len(history) < 254:
            return None
        hi = history["High"].astype(float)
        lo = history["Low"].astype(float)
        cl = history["Close"].astype(float)
        price = float(cl.iloc[-1])
        prior52 = float(hi.iloc[-253:-1].max())
        rng = float(hi.iloc[-1]) - float(lo.iloc[-1])
        close_pos = (price - float(lo.iloc[-1])) / rng if rng > 0 else 0.5
        ret20 = price / float(cl.iloc[-21]) - 1 if float(cl.iloc[-21]) > 0 else None
        if prior52 <= 0 or price <= 0:
            return None
        return {"dist52_pct": (price / prior52 - 1) * 100, "close_pos": close_pos, "ret20": ret20}
    except Exception:
        return None


def sector_strength(ret20_by_sector: dict[str, list[float]], all_ret20: list[float]) -> dict[str, float]:
    """業種ごとの「20日騰落の平均 − 全銘柄の中央値」。5銘柄未満の業種は出さない。"""
    if not all_ret20:
        return {}
    market = st.median(all_ret20)
    return {s: st.fmean(v) - market for s, v in ret20_by_sector.items() if len(v) >= 5}


def grade_of(facts: dict | None, sector_rel: float | None) -> tuple[str, int, list[str]]:
    """(等級, 点数, 理由の一覧)。数字が取れなければ ("", 0, [])。"""
    if not facts:
        return "", 0, []
    score, why = 0, []
    d52 = facts["dist52_pct"]
    if -5 <= d52 < 0:
        score += 1
        why.append(f"52週高値のすぐ下（{d52:+.1f}%）+1")
    elif d52 < -15:
        score -= 1
        why.append(f"52週高値から遠い（{d52:+.1f}%）-1")
    pos = facts["close_pos"]
    if pos < 0.3:
        score += 1
        why.append("当日の値幅の下の方にいる +1")
    elif pos >= 0.7:
        score -= 1
        why.append("当日の値幅の上の方にいる -1")
    if sector_rel is not None and sector_rel < -0.02:
        score -= 1
        why.append(f"業種の勢いが弱い（市場比{sector_rel * 100:+.1f}ポイント）-1")
    grade = "SS" if score >= 2 else "S" if score == 1 else "A" if score == 0 else "B" if score == -1 else "C"
    return grade, score, why


def pf_text(grade: str) -> str:
    pf = GRADE_PF.get(grade)
    if not pf:
        return ""
    return " / ".join(f"{p} {v:.2f}" for p, v in zip(PERIODS, pf))


def footer_lines() -> list[str]:
    """メールの末尾に付ける説明（プレーンテキスト）。"""
    lines = [
        "■ 過去の成績ランクについて",
        "似た形のアラートが過去にどうなったかで付けた目安です。売買をすすめるものではありません。",
        "東証プライムの2022〜2026年・125,779場面で、翌営業日の寄付で入り、終値で-5%か+12%、"
        "または15営業日で手じまった場合の損益比（売買コスト0.2%込み）:",
    ]
    for g in GRADES:
        lines.append(f"  {g}: {pf_text(g)}")
    lines += [
        "C は4つの期間とも最下位、A・B・C の並びは4期間でそろいましたが、SS と S はほぼ同じです。",
        "限界: 条件は全期間を見て選んだため、別の期間で確かめた数字ではありません。最初に試した"
        "多くの条件で作ったランクは、2025〜26年に順位が逆転しました。ザラ場の途中の値段で判定"
        "しています（検証は大引けの値）。プライム以外の市場は検証していません。",
    ]
    return lines
