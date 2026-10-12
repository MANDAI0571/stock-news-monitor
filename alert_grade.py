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
（検証は大引けの値）。
2026-10-12: スタンダード（24,249場面）は2024〜2026年で並びがそろい、2022-23年はそろわなかった。
グロース（10,847場面）はそろったのが2025年だけなので、等級を付けない（GROWTH_NOTE）。
そのため、実際にメールに出たアラートのその後を alert_grade_track.py が毎営業日に集計し、
メールの末尾に等級ごとの実際の成績として出す（forward_lines）。
"""

from __future__ import annotations

import datetime as dt
import json
import statistics as st
from pathlib import Path

# 期間ごとの損益比（alert_rank_research / rank2 の実測。2026-10-12）
PERIODS = ("2022-23", "2024", "2025", "2026")
GRADE_PF = {                       # プライム（125,779場面）
    "SS": (1.10, 0.99, 1.84, 1.14),
    "S": (1.17, 0.98, 1.72, 1.23),
    "A": (1.08, 0.90, 1.44, 1.12),
    "B": (0.98, 0.87, 1.45, 1.07),
    "C": (0.92, 0.81, 0.99, 0.82),
}
# スタンダード（24,249場面。stock-volume-screener/backtest/fetch_sg.py の5年分で同じ計算。
# 2026-10-12）。2024〜2026年は C が最下位で A・B・C の順もそろったが、2022-23年はそろわない。
GRADE_PF_STANDARD = {
    "SS": (0.99, 1.02, 1.25, 1.12),
    "S": (0.95, 0.81, 1.23, 1.16),
    "A": (1.02, 0.94, 1.27, 1.18),
    "B": (0.92, 0.77, 1.17, 1.04),
    "C": (0.98, 0.59, 0.81, 0.61),
}
# グロース（10,847場面）は並びがそろったのが2025年だけで、等級全体でも損益比が1を
# 下回る期間が4期間中3期間あった。根拠のない順位を見せないため、等級を付けない。
GROWTH_NOTE = "グロース市場は過去の検証で並びがそろわなかったため付けていません"
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


def grade_of(facts: dict | None, sector_rel: float | None,
             market: str = "") -> tuple[str, int, list[str]]:
    """(等級, 点数, 理由の一覧)。数字が取れなければ ("", 0, [])。
    グロース市場は ("", 0, [GROWTH_NOTE])（等級なしと、その理由）。"""
    if "グロース" in (market or ""):
        return "", 0, [GROWTH_NOTE]
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


def pf_text(grade: str, table: dict | None = None) -> str:
    pf = (table or GRADE_PF).get(grade)
    if not pf:
        return ""
    return " / ".join(f"{p} {v:.2f}" for p, v in zip(PERIODS, pf))


def footer_lines() -> list[str]:
    """メールの末尾に付ける説明（プレーンテキスト）。"""
    lines = [
        "■ 過去の成績ランクについて",
        "似た形のアラートが過去にどうなったかで付けた目安です。売買をすすめるものではありません。",
        "2022〜2026年に、翌営業日の寄付で入り、終値で-5%か+12%、または15営業日で手じまった"
        "場合の損益比（売買コスト0.2%込み）:",
        "東証プライム（125,779場面）",
    ]
    for g in GRADES:
        lines.append(f"  {g}: {pf_text(g)}")
    lines.append("東証スタンダード（24,249場面）")
    for g in GRADES:
        lines.append(f"  {g}: {pf_text(g, GRADE_PF_STANDARD)}")
    lines += [
        "プライムは C が4つの期間とも最下位、A・B・C の並びは4期間でそろいました。スタンダードは"
        "2024〜2026年の3期間でそろい、2022-23年はそろいませんでした。SS と S はどちらの市場でも"
        "はっきりした差がありません。",
        "グロース市場の銘柄には付けていません（10,847場面で、並びがそろったのは2025年だけでした）。",
        "限界: 条件はプライムの全期間を見て選んだため、別の期間で確かめた数字ではありません。最初に"
        "試した多くの条件で作ったランクは、2025〜26年に順位が逆転しました。ザラ場の途中の値段で"
        "判定しています（検証は大引けの値）。",
    ]
    return lines + forward_lines()


# 実際のアラートのその後（alert_grade_track.py が毎営業日の夕方に書く）。2026-10-12 追加。
FORWARD_PATH = Path(__file__).resolve().parent / "data" / "alert_grade_forward.json"


def forward_lines(path: Path | None = None, today: dt.date | None = None) -> list[str]:
    """実際にメールに出たアラートを、上と同じ決まりで売買した場合の等級ごとの成績。
    集計がまだ無ければ何も出さない。"""
    try:
        s = json.loads((path or FORWARD_PATH).read_text(encoding="utf-8"))
        grades = s["grades"]
    except Exception:
        return []
    if not s.get("recorded"):
        return []
    lines = [f"■ 実際のアラートでの成績（{s.get('since') or '-'}〜、{s.get('updated', '-')} 集計。"
             f"上と同じ決まりで、手じまいまで終わった場面だけ）"]
    done = sum(grades[g]["done"] for g in GRADES if g in grades)
    if done == 0:
        lines.append(f"まだ手じまいまで終わった場面がありません（記録 {s.get('recorded', 0)}件。"
                     "決着には最長で約3週間かかります）。")
    else:
        for g in GRADES:
            v = grades.get(g) or {}
            if not v.get("done") and not v.get("pending"):
                continue
            pf = "-" if v.get("pf") is None else f"{v['pf']:.2f}"
            win = "-" if v.get("win") is None else f"{v['win'] * 100:.0f}%"
            lines.append(f"  {g}: 終わった {v.get('done', 0)}件 損益比 {pf} 勝率 {win}"
                         f"（途中 {v.get('pending', 0)}件）")
        if any(0 < (grades.get(g) or {}).get("done", 0) < 30 for g in GRADES):
            lines.append("30件に満たない等級の数字は偶然の差が大きく、上の5年の数字と食い違っても"
                         "不思議ではありません。")
    try:
        upd = dt.date.fromisoformat(str(s.get("updated", ""))[:10])
        today = today or (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)).date()
        if (today - upd).days > 4:
            lines.append(f"（この集計は {(today - upd).days}日更新されていません）")
    except ValueError:
        lines.append("（集計の日付が読めません）")
    return lines
