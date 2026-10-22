"""出来高急増×下降トレンドの「過去の成績ランク」（2026-10-22 持ち主の指示「出来高急増もランクつけて」）。

ランクは「過去5年で、似た形がその後どうなったか」の目安であって、売買をすすめるものではない。

検証（stock-volume-screener/backtest/vs_rank_research.py。東証プライム、2022-2026年、10,776場面。
screener.py と同じ条件を確定日足で再現。翌営業日の寄付で入り、終値で-5%か+12%、または15営業日で
翌寄付に手じまい。売買コスト0.2%込み）で、4つの期間（2022-23 / 2024 / 2025 / 2026）でそろって
差が出たのは次の1つだけだった:

  ◎  その銘柄の業種の勢いがとても弱い（全市場（プライム・スタンダード・グロース）で測った
     業種の20日騰落の平均が、全銘柄の中央値より4ポイント以上低い。前営業日の終値で測る）
       ◎ 3.98 / 5.74 / 8.28 / 1.37（534場面）、それ以外 1.48 / 1.59 / 1.91 / 0.84（10,242場面）
     1日に60件以上出た急落の13日を除いても ◎ 5.02 / 2.68 / 4.67 / 1.37、それ以外 1.30 / 0.93 / 1.31 / 0.84

ほかに調べたもの（25日線・75日線からの乖離、出来高倍率、前日比、引けの位置、5・20・60日騰落、
52週高値・安値からの距離、売買代金、窓、業種の勢いをプライムだけで測ったもの、同じ日の件数、
相場全体の騰落と25日線の下の銘柄の割合）は、4期間でそろわなかった（期間によって向きが逆になる）。
3〜5段階に分けると、どの分け方でも途中の段の順番が期間ごとに入れ替わった。そのため SS〜C の5段階
にはせず、◎ とそれ以外の2つにする（ランキング記事のセリングクライマックスの ◎ と同じ決め方）。
限界: 区切り（4ポイント）は全期間を見て選んだので、別の期間で確かめた数字ではない。

業種の勢いは、前営業日の引け後に note の記事づくり（note_two.py）が全市場の終値から出し、
sector_strength/strength.json に書く（Actions のキャッシュでこちらに渡す）。13:50 の判定には
前営業日の値を使う（検証も前営業日の値で測った）。古いときは付けない。
"""
from __future__ import annotations

import datetime as dt
import json
import os

import fetchlib as F

MARK = "◎"
THRESHOLD = -0.04
MARK_LABEL = "業種の勢いがとても弱い"
PERIODS = ("2022-23", "2024", "2025", "2026")
PF_MARK = (3.98, 5.74, 8.28, 1.37)
PF_OTHER = (1.48, 1.59, 1.91, 0.84)
N_MARK, N_OTHER = 534, 10242
_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(os.path.dirname(_HERE), "sector_strength", "strength.json")


def strength_path():
    return os.environ.get("VS_SECTOR_STRENGTH") or DEFAULT_PATH


def prev_business_day(d, hol=None):
    x = d - dt.timedelta(days=1)
    for _ in range(15):
        if F.is_business_day(x, hol)[0]:
            return x
        x -= dt.timedelta(days=1)
    return None


def load_strength(target, path=None, hol=None):
    """(業種→勢い, 理由)。前営業日の値でなければ (None, 理由)。"""
    path = path or strength_path()
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None, "業種の勢いの数字がないため、ランク（◎）は付けていません"
    want = prev_business_day(target, hol)
    if not want or d.get("date") != want.isoformat():
        return None, (f"業種の勢いの数字が前営業日のものでない（{d.get('date')}）ため、"
                      "ランク（◎）は付けていません")
    s = d.get("strength") or {}
    if not s:
        return None, "業種の勢いの数字が空のため、ランク（◎）は付けていません"
    return {str(k): float(v) for k, v in s.items()}, ""


def sector_map():
    """銘柄コード→33業種（JPXの一覧 data_j.xlsx。prime_list が落とした控えを読む）。"""
    import openpyxl

    p = F._cached_path("data_j.xlsx")
    out = {}
    wb = openpyxl.load_workbook(p, read_only=True)
    try:
        for i, row in enumerate(wb.active.values):
            if i == 0 or len(row) < 6:
                continue
            if row[1] is not None and row[5]:
                out[str(row[1]).strip()] = str(row[5]).strip()
    finally:
        wb.close()
    return out


def grade(rel):
    return MARK if rel is not None and rel < THRESHOLD else ""


def apply(rows, target, strength=None, sectors=None, hol=None):
    """各行に sector / sector_rel / mark を付け、◎ を先に（その中は25日線から深い順）並べ直す。
    戻り値はメールの経過メモに出す一文（付けられなかった理由。付けたら件数）。"""
    note = ""
    if strength is None:
        strength, note = load_strength(target, hol=hol)
    if strength is not None and sectors is None:
        try:
            sectors = sector_map()
        except Exception as e:                      # noqa: BLE001
            strength, note = None, f"業種の一覧を読めないため、ランク（◎）は付けていません（{type(e).__name__}）"
    for r in rows:
        sec = (sectors or {}).get(str(r["code"]))
        rel = strength.get(sec) if (strength is not None and sec) else None
        r["sector"] = sec or ""
        r["sector_rel"] = rel
        r["mark"] = grade(rel)
    rows.sort(key=lambda r: (r.get("mark") != MARK, r["dev25_pct"]))
    if strength is not None:
        n = sum(1 for r in rows if r.get("mark") == MARK)
        note = f"ランク: {MARK}（{MARK_LABEL}）{n}件 / それ以外 {len(rows) - n}件。{MARK}を先に、その中は25日線から深い順"
    return note


def explain_lines():
    """メールと記事の「ランクについて」。"""
    pf = lambda xs: " / ".join(f"{x:.2f}" for x in xs)  # noqa: E731
    return [
        f"{MARK}＝{MARK_LABEL}（その銘柄の業種の20日騰落の平均が、全市場の全銘柄の中央値より4ポイント以上低い。"
        "前営業日の終値で測ります）。",
        f"過去5年（2022〜2026年、東証プライム）に同じ条件に当てはまった場面を、翌営業日の寄付で入り、終値で-5%か+12%、"
        f"または15営業日で手じまう決まり（売買コスト0.2%込み）で数えた損益比は、{MARK} {pf(PF_MARK)}、"
        f"それ以外 {pf(PF_OTHER)}（{' / '.join(PERIODS)}）でした。",
        "25日線からの乖離・出来高倍率・業種の勢いの細かい段階など、ほかの分け方は期間によって順番が入れ替わったため、"
        f"SS〜C の5段階ではなく「{MARK}」と「それ以外」の2つにしています。条件は全期間を見て選んだので、"
        "別の期間で確かめた数字ではなく、今後を保証するものではありません。",
    ]
