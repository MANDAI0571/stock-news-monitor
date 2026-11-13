#!/usr/bin/env python3
"""note の2本立て（2026-10-16 持ち主の指示）。毎営業日の引け後に2本を作り、note に下書き保存する。

  (A) 高値更新・押し目・セリングクライマックス ランキング（2026-11-13 から、どの一覧も時価総額の大きい順）
      - 52週高値更新の上位5件を、リアルタイムアラートのメールと同じ形で紹介
      - 52週高値更新・直近高値更新の TOP10、PPP、盛り上がっている業種と業種No.1、循環物色、
        25MA・200MA 押し目の TOP10（注意なし）、セリングクライマックス（◎印）、下げトレンドの十字線
  (B) Claudeが300万円運用｜運用状況（Claude 側だけ。Codex は 2026-10-16 に削除）

下書きの保存は note_autosave.py（manifest を読み、1回のログインで保存。公開はしない）。
押し目とセリングクライマックスの印の根拠は、5年の検証（2026-10-16、review/STATUS.md）:
  押し目: 注意なし 1.37 / 0.93 / 1.77 / 1.48、注意あり 0.97 / 0.82 / 1.34 / 1.04
  セリングクライマックス: 業種の勢いがとても弱い（全市場で測った業種の20日騰落の平均が中央値より4ポイント以上低い）
    2.65 / 6.52 / 9.87 / 1.38、それ以外 1.48 / 1.59 / 1.87 / 0.83（2026-10-22 に出来高急増のスクリーナーと同じ
    条件の10,776場面で測り直した。stock-volume-screener/backtest/vs_rank_research.py。以前の区切り
    （2ポイント）は、急落の13日を除くと2024年に逆になったので4ポイントにした）
どちらも SS〜C の5段階を支える差は無かったので、SS〜C は付けない。売買をすすめるものではない。
業種の勢い（全市場、その日の終値）は sector_strength/strength.json にも書く。次の営業日 13:50 の
出来高急増のスクリーナーが、ランク（◎）にこの前営業日の値を使う（volume_screener/rank.py）。

  python3 note_two.py --day 2026-10-15 --out outputs/note_two
    （先に run_screening.py --output-dir outputs/note_two と、
      volume_screener/daily_run.py --asof <day> を動かしておく）
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
from pathlib import Path

import pandas as pd

import alert_grade as G
from note_legal import COMPLIANCE_LINES

ROOT = Path(__file__).resolve().parent
JST = dt.timezone(dt.timedelta(hours=9))
CLIMAX_JSON = ROOT / "volume_screener" / "out_asof" / "result.json"
CLIMAX_MARK_REL = -0.04          # ◎ の区切り（volume_screener/rank.py の THRESHOLD と同じ）
STRENGTH_JSON = ROOT / "sector_strength" / "strength.json"


def write_strength(day: dt.date, strength: dict, path: Path = STRENGTH_JSON) -> Path:
    """全市場の業種の勢い（その日の終値）を書く。翌営業日 13:50 の出来高急増のランクに使う。
    業種名と数字だけ（銘柄は入らない）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": day.isoformat(), "universe": "prime,standard,growth",
                                "strength": {k: round(float(v), 6) for k, v in strength.items()}},
                               ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path
GRADE_ORDER = {g: i for i, g in enumerate(G.GRADES)}
# 押し目の注意（4期間とも成績が下だった条件）
PULLBACK_MOM60_LIMIT = 25.0
DISCLAIMER = "これは条件に当てはまった銘柄の一覧です。売買をすすめるものではありません。投資の判断はご自身でお願いします。"


def target_day(now: dt.datetime, business) -> dt.date | None:
    """記事にする立会日。引け後（15:50以降。終値は15分遅れで届く）ならその日、立会の前ならその前の
    立会日。立会中から引け直後（9:00〜15:50）は途中の値になりうるので None（作らない）。"""
    d = now.date()
    if business(d):
        if now.time() >= dt.time(15, 50):
            return d
        if now.time() >= dt.time(9, 0):
            return None
    d -= dt.timedelta(days=1)
    for _ in range(15):
        if business(d):
            return d
        d -= dt.timedelta(days=1)
    return None


def _grade_key(a) -> tuple:
    return (GRADE_ORDER.get(a.grade, len(GRADE_ORDER)), -a.turnover_20d)


CARD_SHOW = 5                       # 52週高値更新を、くわしい形で出す件数（時価総額の大きい順）


def _alerts_by_cap(rows: list) -> list:
    import market_cap

    return market_cap.sort_by_market_cap(sorted(rows, key=lambda a: -a.turnover_20d),
                                         code=lambda a: a.code, price=lambda a: a.current_price)


def _cap_of(code, price) -> str:
    import market_cap

    text = market_cap.cap_text(market_cap.market_cap_yen(code, price))
    return f"（{text}）" if text else ""


def ranking_lines(day: dt.date, alerts: list, pullback: pd.DataFrame, climax: list[dict] | None,
                  info: dict, strength: dict, mom60: dict, ict: list[dict] | None = None,
                  facts: list[dict] | None = None) -> tuple[str, list[str]]:
    """(A) のタイトルと本文（markdown 風の行）。alerts は今日はじめての高値更新（is_fresh_break）。

    2026-11-13 持ち主「スクリーニングした銘柄はすべて時価総額順に並べる」「時価総額順 過去の成績という言葉は入らない」:
    どの一覧も時価総額の大きい順にし、ランク（SS〜C）の節と成績の数字を外した。同日「SS S A は残して」で、
    高値更新の銘柄にはランクの文字（と点数の理由）を残す。facts（daily_market_scan.stock_facts に
    code・name・sector を足したもの）があれば、PPP・盛り上がっている業種と業種No.1・循環物色・下げトレンドの十字線の節を足す。
    """
    import daily_market_scan as S
    import intraday_high_alert as M
    import market_cap

    closes = {str(f["code"]): f["close"] for f in facts or []}
    hi52 = _alerts_by_cap([a for a in alerts if a.alert_type == "52週高値更新"])
    swing = _alerts_by_cap([a for a in alerts if a.alert_type != "52週高値更新"])
    lines = [f"{day}の終値で、条件に当てはまった銘柄を機械的に並べた一覧です。どの一覧も**時価総額の大きい順**です。",
             "**売買をすすめるものではありません。**", "",
             f"## 52週高値更新の銘柄（時価総額の大きい順に{CARD_SHOW}件・くわしく）", ""]
    if not hi52:
        lines += ["本日は該当なし。", ""]
    for a in hi52[:CARD_SHOW]:
        block = [x for x in M._format_alert(a) if "OpenWork" not in x]
        body = [x.strip().replace("現在値:", "終値:") for x in block[1:] if x.strip()]
        lines += [f"**{block[0]}**（{(a.market or '').replace('東証', '')}）"] + body + [""]

    def top10(title, rows):
        out = [f"## {title} TOP10（全{len(rows)}件・時価総額の大きい順）", ""]
        out += [f"{i}. {a.grade + '　' if a.grade else ''}{a.code} {a.name}{_cap_of(a.code, a.current_price)}"
                for i, a in enumerate(rows[:10], 1)]
        return out + ([] if rows else ["本日は該当なし。"]) + [""]

    lines += top10("52週高値更新", hi52) + top10("直近高値更新", swing)
    if facts:
        lines += S.ppp_lines(facts) + S.sector_lines(facts)
    pb = pullback.copy()
    if len(pb):
        pb = pb[(pb["ma25_touch"] == True) | (pb["ma200_touch"] == True)]  # noqa: E712
        pb["caution"] = [(info.get(str(c), ("", ""))[0] != "東証プライム")
                         or (mom60.get(str(c), 0.0) >= PULLBACK_MOM60_LIMIT) for c in pb["code"]]
        pb = pb.sort_values("turnover_20d", ascending=False)
        price = pd.to_numeric(pb["current_price"], errors="coerce") if "current_price" in pb.columns else pd.Series(float("nan"), index=pb.index)
        pb["current_price"] = [p if p == p else closes.get(str(c)) for c, p in zip(pb["code"], price)]
        pb = market_cap.sort_frame_by_market_cap(pb, "code", "current_price")
    ok = pb[~pb["caution"]] if len(pb) else pb
    lines += [f"## 25MA・200MA 押し目 TOP10（全{len(pb)}件のうち注意なし{len(ok)}件・時価総額の大きい順）", ""]
    for i, r in enumerate(ok.head(10).itertuples(), 1):
        which = "・".join(x for x, f in (("25MA", r.ma25_touch), ("200MA", r.ma200_touch)) if f == True)  # noqa: E712
        cap = market_cap.cap_text(getattr(r, "market_cap_yen", None))
        lines.append(f"{i}. {r.code} {r.name}（{which}{'・' + cap if cap else ''}）")
    if not len(ok):
        lines.append("本日は該当なし。")
    if len(pb) - len(ok):
        lines.append(f"（注意あり {len(pb) - len(ok)}件は省略：グロース・スタンダード、または60日で+25%以上上げた後）")
    lines.append("")
    if climax is None:
        lines += ["## セリングクライマックス（下降中の出来高急増）", "", "本日は数字を取得できませんでした。", ""]
    else:
        climax = market_cap.sort_by_market_cap(climax, code=lambda r: r["code"],
                                               price=lambda r: r.get("price") or closes.get(str(r["code"])))
        lines += [f"## セリングクライマックス（下降中の出来高急増）全{len(climax)}件（時価総額の大きい順・終値）", ""]
        for i, r in enumerate(climax[:10], 1):
            rel = strength.get(info.get(str(r["code"]), ("", ""))[1])
            mark = "◎ " if rel is not None and rel < CLIMAX_MARK_REL else ""
            cap = market_cap.cap_text(market_cap.market_cap_yen(r["code"], r.get("price") or closes.get(str(r["code"]))))
            lines.append(f"{i}. {mark}{r['code']} {r['name']}（25日線から{r['dev25_pct']:+.1f}%・出来高{r['ratio']:.1f}倍"
                         f"{'・' + cap if cap else ''}）")
        if not climax:
            lines.append("本日は該当なし。")
        if len(climax) > 10:
            lines.append(f"（ほか{len(climax) - 10}件）")
        lines += ["（◎＝業種の勢いがとても弱い：その業種の20日騰落の平均が、全銘柄の中央値より4ポイント以上低い）", ""]
    if facts:
        lines += S.doji_lines(facts)
    if ict is not None:
        # 2026-10-25: 押し目の箱（オーダーブロック。us_ict.py）。米株と同じ手法で、プライムの5年でも4期間そろって上だった
        import us_ict

        ict = market_cap.sort_by_market_cap(ict, code=lambda x: x["ticker"], price=lambda x: x["close"])
        lines += us_ict.section_lines(ict, market="jp", keep_order=True)
    lines += ["## 印について", "",
              "- 高値更新の🏷ランク（SS・S・A・B・C）：52週高値までの距離・当日の値幅のどこにいるか・業種の勢いで付けた"
              "点数の段階（SS がいちばん点数が高い）。グロース市場には付けていません。",
              "- 押し目の「注意あり」：グロース・スタンダードの銘柄、または60日で+25%以上上げた後の銘柄。"
              "2022〜2026年の日足で、同じ決まりの押し目はこの2つで4つの期間とも損益比が下でした"
              "（注意なし 1.37 / 0.93 / 1.77 / 1.48、注意あり 0.97 / 0.82 / 1.34 / 1.04。2022-23 / 2024 / 2025 / 2026）。",
              "- セリングクライマックスの ◎：業種の勢いがとても弱いもの。同じ期間で ◎ 2.65 / 6.52 / 9.87 / 1.38、"
              "それ以外 1.48 / 1.59 / 1.87 / 0.83 でした。",
              "数字は条件を全期間を見て選んだもので、別の期間で確かめたものではなく、今後を保証するものではありません。",
              "", "---", ""] + COMPLIANCE_LINES
    return f"高値更新・押し目・セリングクライマックス ランキング｜{day}", lines


# 2026-10-30 持ち主「300万運用はもう少しアクティブに運用し読者を惹きつけるように利益を出し損を減らすのも重要だ
# 記事の内容で惹きつけても良い」。ルールを 3.0.0 に変え、記事に「出しておく注文の値段」「次の候補」「市場との比較」
# 「ルールを変えた理由と、過去の値動きで試した結果（負けている点も）」を足した。
RULE_CHANGE_DAY = dt.date(2026, 10, 30)
RULE_CHANGE_SHOW_DAYS = 14          # ルール変更の節を出す日数（暦日）
# 2026-11-13 持ち主「300万運用は損切り 直近の安値を下回ったら 利食いも」「52週高値で買いに変える」
# 「物差し入らない」「保有銘柄5銘柄の後に5銘柄と損益通算とを並べる チャート」。ルールを 4.0.0 に変え、
# 記事から TOPIX の物差しの行を外し、保有 → 次の買い候補5件 → 損益通算（棒のグラフ）の順に並べた
RULE4_CHANGE_DAY = dt.date(2026, 11, 13)
CANDIDATE_SHOW = 5
EXIT_WORDS = {"STOP_LOSS": "損切り", "TAKE_PROFIT": "利確", "TIMEOUT": "期限"}
EXIT_WORDS_TRAILING = {"STOP_LOSS": "損切り", "TAKE_PROFIT": "利食い", "TIMEOUT": "期限"}
BAR_PCT_PER_BLOCK = 1.0             # 損益通算の棒: ■ 1つ＝1%（最大20個）
ORDER_STATUS = {"FILLED": "約定", "CANCELLED_GAP": "寄り付きで窓が開いたため見送り", "PENDING": "約定待ち",
                "DECLARED": "約定待ち", "CANCELLED_CAPITAL": "現金が足りず見送り",
                "CANCELLED_EXPIRED": "値段が取れないまま期限切れ", "CANCELLED_OPEN_UNAVAILABLE": "始値が取れず見送り",
                "CANCELLED_STRATEGY_VERSION_MISMATCH": "ルールの切り替えで取り消し",
                "CANCELLED_RULE_HASH_MISMATCH": "ルールの切り替えで取り消し"}


def _yen_num(text: str) -> float | None:
    try:
        return float(str(text).replace(",", "").replace("円", "").strip())
    except ValueError:
        return None


def _field(r, key: str, default=""):
    value = getattr(r, key, default)
    return default if value is None or (isinstance(value, float) and value != value) else value


def rule_change_lines() -> list[str]:
    """3.0.0 に変えた理由と、過去の値動きで試した結果（2026-10-30 に調べた数字。stock-volume-screener/review/STATUS.md）。"""
    return ["## ルールを変えました（2026-10-30から）", "",
            "これまでの損切りは「終値で-5%を下回ったら、翌朝の寄付で売る」形でした。そのため、9月末からの損切り3回は、"
            "どれも-5%では止まらず-7〜-8%で売っていました。もっと動かし、利益を伸ばし、損を決めた幅で止めるために、次のように変えました。", "",
            "- 枠：最大3銘柄（1枠60万円）→ **最大5銘柄（1枠55万円）**",
            "- 損切り：終値で-5%を判定して翌朝売る → **逆指値-7%を買った日から出しておく（日中に約定）**",
            "- 利確：終値で+12%を判定して翌朝売る → **指値+15%を買った日から出しておく（日中に約定）**",
            "- 期限：15営業日 → **20営業日（その日の大引けで売る）**",
            "- 候補の順番：スコアの順 → **売買代金の大きさと、52週高値からの距離（押し目の深さ）の順位の合計**",
            "- ブレーキ：口座が-5%で新規1銘柄、-10%で新規をやめる（戻らない）→ **評価額が最高値から-8%のあいだは、1日の新規を半分**", "",
            "東証プライムの過去4年（2022年10月〜2026年10月）の日足で、同じ形の銘柄を選ぶ代わりの条件を使って試しました"
            "（売買コスト片道0.1%、候補の順番の偶然を20通り変えた真ん中の値）。",
            "- 損益：旧ルール +25% → 新ルール +45%（前半2年 +2% → +13%、後半2年 +20% → +37%）",
            "- 月の売買：約4.6回 → 約5.9回。いちばん大きい下落：どちらも-13%前後",
            "- 損切り1回の平均：-6.6%（悪い方の5%は-11%）→ -7.3%（悪い方の5%は-9%）",
            "ただし、同じ4年間に東証プライムの全銘柄を同じ金額ずつ買って持ち続けた場合は約+116%"
            "（いまある銘柄だけで計算したので高めに出ます）で、**どちらのルールもこれを下回りました。**"
            "株を持っている割合が平均で旧ルール約5割・新ルール約6割で、残りは現金だったことも差の理由です。"
            "代わりの条件での試算で、これからの成績を示すものではありません。", ""]


def rule4_change_lines() -> list[str]:
    """4.0.0 に変えた理由と、過去4年の日足で試した結果（2026-11-13 に調べた数字。stock-volume-screener/review/STATUS.md）。"""
    from dual_300man_config import CONFIG

    n = CONFIG.trailing_low_days
    return ["## ルールを変えました（2026-11-13から）", "",
            "買い方と売り方を、次のように変えました。前から持っている銘柄も、11/13 の立会から新しい売り方に切り替えています。", "",
            "- 買い：52週高値の少し下まで押した銘柄 → **その日に52週新高値を付けた東証プライムの銘柄を、時価総額の大きい順に**",
            f"- 売り：逆指値-7%・指値+15%・20営業日 → **直近{n}営業日の安値に逆指値を出しておき、毎晩引き上げる（下げない）。"
            "損切りも利食いも、この安値を割ったときに売る。利確の指値と期限は無し**",
            "- 枠（最大5銘柄・1枠55万円）、寄付が判定の値段から±3%を超えたら見送り、評価額が最高値から-8%のあいだは新規を半分、は同じです。", "",
            "東証プライムの2022年10月〜2026年10月の日足で、同じ形を試しました（売買コスト片道0.1%。決算・ニュース・業種の重なりの"
            "確認は入れていません。時価総額はいまの株式数で計算）。",
            "- 損益：+112%（前半2年 +52%、後半2年 +70%）、いちばん大きい下落 -9%",
            "- 売買173回で勝ち46%、勝ちの平均 +14.1%、負けの平均 -4.8%、保有の真ん中は23営業日",
            "- 同じ買い方で、売りを前のやり方（-7%・+15%・20営業日）にすると +59%（いちばん大きい下落 -13%）",
            f"- 直近の安値を何日で測るかで結果は変わり、5日 +10%・10日 +112%・{n}日 +112%・20日 +84% でした（{n}日は下落がいちばん小さかった）",
            "ただし、同じ4年間に東証プライムの全銘柄を同じ金額ずつ持ち続けた場合も約+112%で、**新しいやり方はこれとほぼ同じで、上回ってはいません。**"
            "いまある銘柄だけで計算したので、どちらも高めに出ます。代わりの条件での試算で、これからの成績を示すものではありません。", ""]


def pnl_bar(pct: float) -> str:
    """損益通算の棒（■＝プラス、□＝マイナス。1つ＝BAR_PCT_PER_BLOCK%、最大20個。0.5%未満は「・」）。"""
    blocks = min(20, int(abs(pct) / BAR_PCT_PER_BLOCK + 0.5))
    if not blocks:
        return "・"
    return ("■" if pct > 0 else "□") * blocks


# 2026-10-30 持ち主「300万運用でXでリアルタイム情報流しますと言ってXのフォローを増やすよう考えて」。
# X は note のプロフィールにつながっている @Breakout_Scan（note API の socials で確認）。投稿の文は
# claude_300man_x_posts.py が持ち主にメールで届け、持ち主が投稿する（このリポジトリから X には何も送らない）
X_PROFILE_URL = "https://x.com/Breakout_Scan"
X_CTA = ("📣 **買いの注文は前の晩、約定は朝、出しておいた注文に届いた売りはその日の夜に、X（@Breakout_Scan）で"
         f"リアルタイムに流しています。** 注文は寄付の前に出すので、後から都合よく書き換えられません。→ {X_PROFILE_URL}")
X_CTA_END = ("この記事は1日1回のまとめです。注文・約定・売りは、そのつど X（@Breakout_Scan）で先に流しています。"
             f"負けた売買もそのまま出します。フォローしておくと、この記事より先に届きます。→ {X_PROFILE_URL}")


def portfolio_lines(day: dt.date, journal: pd.DataFrame, orders: pd.DataFrame, ledger_text: str,
                    closes: dict, ict: list[dict] | None = None, *, today_exits: dict | None = None,
                    candidates: pd.DataFrame | None = None, bench: tuple[float, float] | None = None,
                    stops: dict | None = None) -> tuple[str, list[str]]:
    """(B) のタイトルと本文。保有の評価は day の終値（closes）。取れなければ台帳の値。

    today_exits: 本日、出しておいた注文に届いた保有（code → claude_300man_exits.ExitEvent）。台帳には夜の処理で付く。
    candidates : 今夜の判定の買い候補（claude_300man_declare.claude_candidates の並び）。None なら節を出さない。
    bench      : 使わない（2026-11-13 持ち主「物差し入らない」で TOPIX の行を外した。呼び出しの形だけ残す）。
    stops      : 4.0.0 の、次の営業日に出しておく逆指値（code → 直近の安値）。
    """
    from claude_300man_exits import deadline, exit_levels
    from dual_300man_config import CONFIG
    from dual_300man_fill import business_days_between
    from intraday_high_alert import chart_url
    import market_cap

    words = EXIT_WORDS_TRAILING if CONFIG.trailing_low else EXIT_WORDS
    stops = stops or {}

    def pick(key):
        for line in ledger_text.splitlines():
            if line.startswith(f"- {key}:"):
                return line.split(":", 1)[1].strip()
        return "-"
    today_exits = today_exits or {}
    op = journal[journal["status"] == "OPEN"]
    cl = journal[journal["status"] == "CLOSED"]
    rows, sold, unreal, held_value, sold_value, sold_pnl = [], [], 0.0, 0.0, 0.0, 0.0
    bars = []                                      # 損益通算の棒（銘柄, 損益%, 損益円）
    for r in op.itertuples():
        code, shares, entry = str(r.code), int(float(r.shares)), float(r.entry_price)
        ev = today_exits.get(code)
        if ev is not None:
            pnl = (ev.price - entry) * shares
            sold_value += ev.price * shares
            sold_pnl += pnl
            when_sold = "" if ev.day == day else f"{ev.day}に"
            sold.append(f"- {code} {r.name}：{when_sold}{words.get(ev.exit_type, ev.exit_type)}。{ev.reason}。"
                        f"{entry:,.0f}円 → {ev.price:,.0f}円（{(ev.price / entry - 1) * 100:+.1f}%、{pnl:+,.0f}円）")
            continue
        px = closes.get(code)
        when = f"{day}終値" if px else f"{r.last_mark_date}終値"
        px = px or float(r.mark_price or r.entry_price)
        pnl = (px - entry) * shares
        unreal += pnl
        held_value += px * shares
        line = (f"- {code} {r.name}：{shares}株、{r.entry_date}に{entry:,.0f}円で買い → "
                f"{when} {px:,.0f}円（{(px / entry - 1) * 100:+.1f}%、{pnl:+,.0f}円）")
        bars.append((f"{code} {r.name}", (px / entry - 1) * 100, pnl))
        if CONFIG.trailing_low:
            stop = stops.get(code)
            line += (f"｜出しておく逆指値：{stop:,.0f}円（直近{CONFIG.trailing_low_days}営業日の安値・買値から"
                     f"{(stop / entry - 1) * 100:+.1f}%。毎晩引き上げ）" if stop else
                     f"｜出しておく逆指値：直近{CONFIG.trailing_low_days}営業日の安値（値段を取れませんでした）")
            line += f"｜チャート {chart_url(code)}"
        elif CONFIG.standing_exit_orders:
            stop, take = exit_levels(entry)
            try:
                end = deadline(dt.date.fromisoformat(str(r.entry_date)))
                left = business_days_between(day, end)
                line += (f"｜出しておく注文：逆指値 {stop:,.0f}円（{CONFIG.stop_loss_pct:+.0f}%）・"
                         f"指値 {take:,.0f}円（{CONFIG.take_profit_pct:+.0f}%）・期限 {end.month}/{end.day}の大引け（あと{left}営業日）")
            except ValueError:
                pass
        rows.append(line)
    cash = _yen_num(pick("現金残"))
    equity = (cash + sold_value + held_value) if cash is not None else None
    n_hold = len(op) - len(sold)
    title = f"Claudeが300万円運用｜運用状況 {day}"
    if equity is not None:
        title += f" 評価額{equity / 1e4:,.1f}万円（{(equity / 3_000_000 - 1) * 100:+.1f}%）"
    if sold:
        title += f"・本日{len(sold)}銘柄売り"
    lines = [f"Claude（AI）が、300万円の紙上運用（実際のお金は使っていません）を決まったルールで続けている記録です。{day}の終値時点。",
             "**売買をすすめるものではありません。**", "", X_CTA, "", "## 運用の状況", ""]
    if equity is not None:
        lines.append(f"- 評価額 {equity:,.0f}円（初めの3,000,000円から {equity - 3_000_000:+,.0f}円、"
                     f"{(equity / 3_000_000 - 1) * 100:+.1f}%）")
    lines += [f"- 初期資金 3,000,000円 → 現金 {pick('現金残')}" + (f"（本日の売り {sold_value:,.0f}円は夜に入ります）" if sold else ""),
              f"- 保有 {n_hold}銘柄／最大{CONFIG.max_positions}銘柄（含み損益 {unreal:+,.0f}円）",
              f"- 実現損益（累計）{pick('実現損益（累計）')}（{len(cl)}回の決済）"
              + (f"、本日の売りで {sold_pnl:+,.0f}円" if sold else ""), ""]
    if CONFIG.trailing_low:
        if day >= RULE4_CHANGE_DAY and (day - RULE4_CHANGE_DAY).days <= RULE_CHANGE_SHOW_DAYS:
            lines += rule4_change_lines()
    elif day >= RULE_CHANGE_DAY and (day - RULE_CHANGE_DAY).days <= RULE_CHANGE_SHOW_DAYS and CONFIG.standing_exit_orders:
        lines += rule_change_lines()
    if sold:
        lines += ["## 本日、出しておいた注文に届いた売り", ""] + sold + [""]
    lines += [f"## 保有銘柄（{n_hold}銘柄）と、出しておく注文", ""]
    lines += rows or ["- なし（現金のみ）"]
    if candidates is not None:
        free = max(0, CONFIG.max_positions - n_hold)
        order_word = "52週新高値・時価総額の大きい順" if CONFIG.trailing_low else "順番の上から"
        lines += ["", f"## 次の買い候補（{order_word}に{CANDIDATE_SHOW}件まで）", ""]
        shown = candidates.head(CANDIDATE_SHOW)
        num = lambda v: pd.to_numeric(pd.Series([v]), errors="coerce").iloc[0]  # noqa: E731
        # 列名が _ で始まる（_candidate_strategy）ので itertuples ではなく iterrows で読む
        for i, (_, r) in enumerate(shown.iterrows(), 1):
            strategy = str(r.get("_candidate_strategy", ""))
            kind = ("52週新高値" if strategy == "claude_new_high_52w_v1"
                    else "押し目" if strategy == "claude_pullback_combo_v1" else "順張り")
            to, dist, px = num(r.get("turnover_20d")), num(r.get("dist_52w_high_pct")), num(r.get("current_price"))
            facts = [kind]
            cap = market_cap.cap_text(num(r.get("market_cap_yen")) if "market_cap_yen" in r else None)
            if cap:
                facts.append(cap)
            if to == to:
                facts.append(f"売買代金 20日平均{to / 1e8:,.1f}億円")
            if dist == dist and strategy != "claude_new_high_52w_v1":
                facts.append(f"52週高値から-{dist:.1f}%")
            if px == px:
                facts.append(f"終値 {px:,.0f}円")
            chart = f"｜チャート {chart_url(str(r.get('code', '')))}" if CONFIG.trailing_low else ""
            lines.append(f"{i}. {r.get('code', '')} {r.get('name', '')}（{'・'.join(facts)}）{chart}")
        if not len(shown):
            # 2026-10-30: 「今夜は買いをお休みする見込み」と書いた夜に買いが出たので、言い切らない（夜にスクリーニングし直すため）
            if getattr(candidates, "attrs", {}).get("earnings_hidden", 0):
                lines.append("この記事を作った時点では、決算日を確かめられなかった銘柄を候補に入れられず、"
                             "条件に当てはまる銘柄はありませんでした。今夜の判定では決算日を取り直すので、買いが出ることがあります。")
            else:
                lines.append("この記事を作った時点のスクリーニングでは、条件に当てはまる銘柄はありませんでした。"
                             "今夜の判定ではスクリーニングをし直すので、買いが出ることもあります。")
        lines += ["", f"空いている枠は{free}つです。今夜（18:45ごろ）の判定で、この順に空き枠の数まで買いを決めます。"
                  "判定のときにスクリーニングをし直すので、顔ぶれが変わることがあります。"
                  "悪材料のニュース・現金・同じ業種の重なりで飛ばすことがあり、翌朝の寄付が判定の値段から±3%を超えたら見送ります。"
                  + ("買えた銘柄には、その日から直近の安値に逆指値を出しておきます。" if CONFIG.trailing_low
                     else "買えた銘柄には、その日から逆指値と指値を出しておきます。")]
    # 損益通算（確定した損益 + 保有の含み損益 + 本日の売り）と、棒のグラフ
    realized = float(pd.to_numeric(cl.get("realized_pnl", pd.Series(dtype=str)), errors="coerce").fillna(0).sum())
    total = (equity - 3_000_000) if equity is not None else realized + sold_pnl + unreal
    lines += ["", f"## 損益通算（{day}終値）", "",
              f"- 確定した損益（これまでの{len(cl)}回の決済）：{realized:+,.0f}円",
              f"- 保有中の含み損益（{len(bars)}銘柄）：{unreal:+,.0f}円"]
    if sold:
        lines.append(f"- 本日の売りの損益（台帳には夜に付きます）：{sold_pnl:+,.0f}円")
    lines += [f"- **通算 {total:+,.0f}円（初めの3,000,000円から {total / 3_000_000 * 100:+.1f}%）**", "",
              f"損益の棒（■＝プラス、□＝マイナス。1つ＝{BAR_PCT_PER_BLOCK:.0f}%）"]
    for label, pct, yen in bars:
        lines.append(f"- {label}　{pnl_bar(pct)} {pct:+.1f}%（{yen:+,.0f}円）")
    lines.append(f"- 通算　{pnl_bar(total / 3_000_000 * 100)} {total / 3_000_000 * 100:+.1f}%（{total:+,.0f}円）")
    lines += ["", "## これまでの決済", ""]
    for r in cl.sort_values("exit_date").itertuples():
        lines.append(f"- {r.exit_date} {r.code} {r.name}：{float(r.realized_pnl):+,.0f}円"
                     f"（{float(r.exit_return_pct):+.1f}%、{r.exit_reason}）")
    if not len(cl):
        lines.append("- まだありません")
    else:
        rets = pd.to_numeric(cl["exit_return_pct"], errors="coerce").dropna()
        wins, losses = rets[rets > 0], rets[rets <= 0]
        lines.append(f"- まとめ：{len(rets)}回で 勝ち{len(wins)}回・負け{len(losses)}回"
                     + (f"、勝ちの平均 {wins.mean():+.1f}%" if len(wins) else "")
                     + (f"、負けの平均 {losses.mean():+.1f}%" if len(losses) else ""))
    recent = orders[(orders["execution_date"] >= (day - dt.timedelta(days=7)).isoformat())]
    lines += ["", "## 直近の売買の予定と結果", ""]
    for r in recent.sort_values("execution_date").itertuples():
        side = "買い" if r.side == "BUY" else "売り"
        word = words.get(str(_field(r, "exit_type")), "")
        if r.side != "BUY" and r.decision_date == r.execution_date and word:
            lines.append(f"- {r.execution_date} 売り（{word}・出しておいた注文） {r.code} {r.name} {r.shares}株：{ORDER_STATUS.get(r.status, r.status)}")
            continue
        lines.append(f"- {r.decision_date}に判定 → {r.execution_date} {side} "
                     f"{r.code} {r.name} {r.shares}株：{ORDER_STATUS.get(r.status, r.status)}")
    if not (orders["decision_date"] == day.isoformat()).any():
        lines.append(f"- {day}の判定（次の営業日の買い）：この記事を作った時点では出ていません（夜に決まります）")
    if ict is not None:
        # 2026-10-26 持ち主「300万運用にICT手法の記事織り込んで」。運用の決まりは変えず、参考として上位5件
        import us_ict

        import market_cap

        by_cap = market_cap.sort_by_market_cap(ict, code=lambda x: x["ticker"], price=lambda x: x["close"])
        lines += [""] + us_ict.section_lines(by_cap, limit=5, market="jp", brief=True, keep_order=True)
    if CONFIG.trailing_low:
        n = CONFIG.trailing_low_days
        rule = (f"1銘柄あたり最大{CONFIG.slot_yen // 10_000}万円・最大{CONFIG.max_positions}銘柄。買いは、その日に52週新高値を付けた"
                "東証プライムの銘柄を時価総額の大きい順に、終値で判定して翌営業日の寄付で買います。"
                f"売りは、買った日から直近{n}営業日の安値に逆指値を出しておき、毎晩その日までの直近{n}営業日の安値まで"
                "引き上げます（下げません）。安値を割ったら、損切りも利食いもその値段で売ります（寄付で下回っていたら始値）。")
        if CONFIG.peak_drawdown_reduce_pct is not None:
            rule += f"評価額が最高値から{CONFIG.peak_drawdown_reduce_pct:.0f}%以下のあいだは、1日の新規を半分にします。"
    elif CONFIG.standing_exit_orders:
        rule = (f"1銘柄あたり最大{CONFIG.slot_yen // 10_000}万円・最大{CONFIG.max_positions}銘柄。買いは終値で判定し、翌営業日の寄付で買います。"
                f"売りは買った日から逆指値{CONFIG.stop_loss_pct:+.0f}%と指値{CONFIG.take_profit_pct:+.0f}%を出しておき（日中に約定）、"
                f"{CONFIG.timeout_days}営業日目は大引けで売ります。")
        if CONFIG.peak_drawdown_reduce_pct is not None:
            rule += f"評価額が最高値から{CONFIG.peak_drawdown_reduce_pct:.0f}%以下のあいだは、1日の新規を半分にします。"
    else:
        rule = (f"1銘柄あたり最大{CONFIG.slot_yen // 10_000}万円。終値で判定し、翌営業日の寄付で売買します。"
                f"損切りは{CONFIG.stop_loss_pct:.0f}%、利確は+{CONFIG.take_profit_pct:.0f}%、最長{CONFIG.timeout_days}営業日。")
    lines += ["", "## ルール", "", rule + "売買コストと税金は入れていません（紙上運用のため）。値段は Yahoo Finance の日足です。",
              "", "## Xでリアルタイムに流しています", "", X_CTA_END,
              "", "---", "", "紙上運用の記録です。実際のお金での売買ではありません。", ""] + COMPLIANCE_LINES
    return title, lines


def portfolio_extras(day: dt.date, journal: pd.DataFrame, out: Path, fetch):
    """(B) に足すもの: 本日の出しておいた注文に届いた売り（夜に台帳へ付く前の速報）、今夜の買い候補、
    次の営業日に出しておく逆指値（4.0.0）。どれも取れなければ None / 空にして、記事づくりは止めない。
    公開リポジトリのログなので銘柄は出さない。戻り値は (today_exits, candidates, stops)。
    2026-11-13 持ち主「物差し入らない」で TOPIX連動ETF（1306）の値段は取らなくなった。"""
    today_exits, candidates, stops = {}, None, {}
    try:
        import claude_300man_exits as X
        from dual_300man_config import CONFIG
        from jpx_calendar import add_jpx_business_days

        if CONFIG.standing_exit_orders:
            nxt = add_jpx_business_days(day, 1)
            for r in journal[journal["status"] == "OPEN"].itertuples():
                ticker = str(getattr(r, "ticker", "") or f"{r.code}.T")
                history = fetch(ticker)
                entry = dt.date.fromisoformat(str(r.entry_date))
                ev = X.find_exit(history, entry, float(r.entry_price), day)
                if ev is not None:                 # 夜の処理がまだ付けていない売り（ふつうは本日の分）
                    today_exits[str(r.code)] = ev
                elif CONFIG.trailing_low:
                    stop = X.trailing_stop(history, entry, nxt)
                    if stop:
                        stops[str(r.code)] = stop
    except Exception as error:                                     # noqa: BLE001
        print(f"note_two_exits=failed reason={type(error).__name__}", flush=True)
    try:
        import claude_300man_declare as D

        ranked, screening = D.claude_candidates(out, day)
        if not screening.empty:
            held = set(journal.loc[journal["status"] == "OPEN", "code"].astype(str))
            recent = D._recent_entry_codes(journal, day, D.REENTRY_COOLDOWN_DAYS)
            candidates = ranked[~ranked["code"].astype(str).isin(held | recent)].copy() if len(ranked) else ranked.copy()
            candidates.attrs.update(getattr(ranked, "attrs", {}))
            if D.NEW_HIGH_RULE:
                # 4.0.0: 決算日が分からなくて外れた数（夜の判定で取り直すと入ることがある）
                candidates.attrs["earnings_hidden"] = int(candidates.attrs.get("earnings_unknown", 0))
                print(f"note_two_candidates_check new_high_ok={len(candidates)} "
                      f"earnings_unknown={candidates.attrs.get('earnings_unknown', 0)} "
                      f"earnings_window={candidates.attrs.get('earnings_window', 0)}", flush=True)
            else:
                # 2026-10-30: 16時台の記事で候補0件（「お休みする見込み」）と書いた夜に、18:45 の判定で買いが1件出た。
                # 候補は決算日が「確認済」の銘柄だけで、決算日は Yahoo から1銘柄ずつ取るので、取れなかった銘柄は外れる（推定の原因）。
                status = screening.get("earnings_status", pd.Series("", index=screening.index)).astype(str)
                unknown = status.eq("未確認")
                if unknown.any():
                    known_all = screening.assign(earnings_status=status.where(~unknown, "確認済"))
                    alt = D._claude_candidates(known_all)
                    alt = alt[~alt["code"].astype(str).isin(held | recent)]
                    candidates.attrs["earnings_hidden"] = max(0, len(alt) - len(candidates))
                print(f"note_two_candidates_check earnings_known={int(status.eq('確認済').sum())} "
                      f"earnings_unknown={int(unknown.sum())} "
                      f"hidden_by_earnings={candidates.attrs.get('earnings_hidden', 0)}", flush=True)
    except Exception as error:                                     # noqa: BLE001
        print(f"note_two_candidates=failed reason={type(error).__name__}", flush=True)
    print(f"note_two_300man_extras exits={len(today_exits)} "
          f"candidates={'-' if candidates is None else len(candidates)} stops={len(stops)}", flush=True)
    return today_exits, candidates, stops


def body_html(lines: list[str]) -> str:
    """note の本文に貼る HTML（note_autosave は <body> の中身をそのまま入れる）。"""
    out, lst = [], None

    def close():
        nonlocal lst
        if lst:
            out.append(f"</{lst}>")
            lst = None

    def inline(t):
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(t))

    for line in lines:
        if line.startswith("## "):
            close(); out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("### "):
            # 小見出し（出来高急増の記事の52週新高値の節、2026-10-27）。<p> のままだと note に「###」が文字で残った
            close(); out.append(f"<h3>{inline(line[4:])}</h3>")
        elif re.match(r"^\d+\. ", line):
            if lst != "ol":
                close(); out.append("<ol>"); lst = "ol"
            out.append(f"<li>{inline(line.split('. ', 1)[1])}</li>")
        elif line.startswith("- "):
            if lst != "ul":
                close(); out.append("<ul>"); lst = "ul"
            out.append(f"<li>{inline(line[2:])}</li>")
        elif line.strip() == "---":
            close(); out.append("<hr>")
        elif not line.strip():
            close()
        else:
            close(); out.append(f"<p>{inline(line)}</p>")
    close()
    return "<!doctype html><html><head><meta charset=\"utf-8\"></head><body>\n" + "\n".join(out) + "\n</body></html>\n"


def market_facts(universe: pd.DataFrame, fetch, codes_for_close: set[str], day: dt.date | None = None):
    """(銘柄→(市場, 業種), 業種の勢い, 60日騰落%, 指定銘柄の最終終値, 節の数字の一覧)。価格は巡回の控え（キャッシュ）から。
    節の数字（daily_market_scan.stock_facts）は、最後の足が day の銘柄だけ（2026-11-13 追加）。"""
    import daily_market_scan as S

    info = {str(r.code): (str(r.market), str(r.sector)) for r in universe.itertuples()}
    ret20_by_sector, all_ret20, mom60, closes, facts = {}, [], {}, {}, []
    for r in universe.itertuples():
        try:
            df = fetch(str(r.ticker))
            c = df["Close"].dropna().astype(float)
        except Exception:                                   # noqa: BLE001
            continue
        if len(c) < 61:
            continue
        if day is not None and pd.Timestamp(df.index[-1]).date() == day:
            try:
                f = S.stock_facts(df.dropna(subset=["Close"]))
            except Exception:                               # noqa: BLE001
                f = None
            if f:
                facts.append({**f, "code": str(r.code), "name": str(r.name), "sector": str(r.sector),
                              "market": str(r.market)})
        r20 = c.iloc[-1] / c.iloc[-21] - 1
        ret20_by_sector.setdefault(str(r.sector), []).append(r20)
        all_ret20.append(r20)
        mom60[str(r.code)] = (c.iloc[-1] / c.iloc[-61] - 1) * 100
        if str(r.code) in codes_for_close:
            closes[str(r.code)] = float(c.iloc[-1])
    return info, G.sector_strength(ret20_by_sector, all_ret20), mom60, closes, facts


def ict_rows(universe: pd.DataFrame, fetch, day: dt.date) -> list[dict]:
    """東証プライム・20日平均売買代金1億円以上で、押し目の箱（us_ict.state）に当てはまる銘柄。最後の足が day の銘柄だけ。"""
    import us_ict

    rows = []
    for r in universe.itertuples():
        if str(r.market) != "東証プライム":
            continue
        try:
            df = fetch(str(r.ticker))
        except Exception:                                   # noqa: BLE001
            continue
        if df is None or len(df) < 60 or df.index[-1].date() != day:
            continue
        to20 = float((df["Close"].astype(float) * df["Volume"].fillna(0).astype(float)).iloc[-20:].mean())
        if to20 < 1e8:
            continue
        try:
            st = us_ict.state(df.iloc[-260:])
        except Exception:                                   # noqa: BLE001
            st = None
        if st:
            rows.append({**st, "ticker": str(r.code), "name": str(r.name), "turnover": to20})
    return rows


def write_outputs(out: Path, items: list[tuple[str, str, list[str]]]) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    manifest = []
    for key, title, lines in items:
        # 2026-10-22 持ち主「タグ画像もコピペワンタッチで」。note のハッシュタグは下書きに残せない（公開の設定で
        # 「投稿する」まで保存されない）が、この1行をコピーしてハッシュタグの欄に貼ると、1回で全部のタグになる
        # （2026-10-22 に確かめた）。記事の最後に置く。
        try:
            import note_assets

            lines = list(lines) + ["", note_assets.tag_line(key)]
        except ImportError:
            pass
        (out / f"note_{key}_title.txt").write_text(title + "\n", encoding="utf-8")
        (out / f"note_{key}.md").write_text(f"# {title}\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
        (out / f"note_{key}.html").write_text(body_html(lines), encoding="utf-8")
        manifest.append({"key": key, "title_file": f"note_{key}_title.txt", "html_file": f"note_{key}.html"})
    path = out / "note_drafts_manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="note の2本立て（ランク記事と Claude 300万円運用）")
    ap.add_argument("--day", required=True)
    ap.add_argument("--out", default=str(ROOT / "outputs" / "note_two"))
    ap.add_argument("--climax-json", default=str(CLIMAX_JSON))
    a = ap.parse_args(argv)
    day = dt.date.fromisoformat(a.day)
    out = Path(a.out)

    import intraday_high_alert as M
    from scanner.prices import fetch_price_history
    from scanner.universe import UniverseConfig, load_jpx_listed

    # 先に動く run_screening.py と同じ18か月分にそろえ、その控え（cache/prices）をそのまま使う。
    # 14か月分にしていたので、同じ株価を2回取りに行き、記事づくりに12分かかっていた（2026-10-18）。
    alerts = [x for x in M.scan(markets=("prime", "standard", "growth"), scope="break", period="18mo")
              if x.is_fresh_break]
    bars = {x.bar_date for x in alerts if x.bar_date}
    if bars and max(bars) != day.isoformat():
        raise SystemExit(f"note_two: 価格データの日付 {sorted(bars)} が記事の日 {day} と違うので作りません")
    universe = load_jpx_listed(UniverseConfig(markets=("prime", "standard", "growth")))
    journal = pd.read_csv(ROOT / "data" / "claude_300man_journal.csv", dtype=str)
    orders = pd.read_csv(ROOT / "data" / "claude_300man_orders.csv", dtype=str)
    held = set(journal.loc[journal["status"] == "OPEN", "code"].astype(str))
    info, strength, mom60, closes, facts = market_facts(
        universe, lambda t: fetch_price_history(t, period="18mo"), held, day)
    pbs = sorted(out.glob("screening_pullback_*.csv"))
    pullback = pd.read_csv(pbs[-1], dtype={"code": str}) if pbs else pd.DataFrame(
        columns=["code", "name", "ma25_touch", "ma200_touch", "turnover_20d"])
    climax = None
    try:
        cj = json.loads(Path(a.climax_json).read_text(encoding="utf-8"))
        if cj.get("date") == day.isoformat():
            climax = cj["rows"]
    except (OSError, ValueError, KeyError):
        pass
    try:
        ict = ict_rows(universe, lambda t: fetch_price_history(t, period="18mo"), day)
    except Exception as error:                                     # noqa: BLE001
        print(f"note_two_ict=failed reason={type(error).__name__}", flush=True)
        ict = None
    t_a, l_a = ranking_lines(day, alerts, pullback, climax, info, strength, mom60, ict, facts)
    try:
        write_strength(day, strength)
    except OSError as error:
        print(f"note_two_strength=failed reason={type(error).__name__}", flush=True)
    # 2026-10-25 持ち主「今後はデータを残し調査する」。高値更新の銘柄の「注文の溜まり」を記録し、その後どちらへ
    # 先に動いたかを判定する（liquidity_track.py）。記事には出さない。転んでも記事づくりは止めない。
    try:
        import liquidity_track as LT

        fetch18 = lambda t: fetch_price_history(t, period="18mo")  # noqa: E731
        added = LT.record(day, alerts, fetch18)
        judged = LT.evaluate(fetch18)
        print(f"liquidity_track_added={added} judged={judged}", flush=True)
        for line in LT.summary_lines():
            print(line, flush=True)
    except Exception as error:                                     # noqa: BLE001
        print(f"liquidity_track=failed reason={type(error).__name__}", flush=True)
    # 押し目の箱で記事に出した銘柄のその後（ict_track.py。過去5年の検証と同じ決まりで、これからの成績を数える）
    try:
        import ict_track as IT

        f18 = lambda t: fetch_price_history(t, period="18mo")  # noqa: E731
        added = IT.record(day, "jp", ict, IT.TRACK_JP) if ict is not None else 0
        judged = IT.evaluate(f18, IT.TRACK_JP, "jp")
        print(f"ict_track_added={added} judged={judged}", flush=True)
        for line in IT.summary_lines(IT.TRACK_JP, "jp"):
            print(line, flush=True)
    except Exception as error:                                     # noqa: BLE001
        print(f"ict_track=failed reason={type(error).__name__}", flush=True)
    ledger = (ROOT / "docs" / "claude_300man_ledger.md").read_text(encoding="utf-8")
    today_exits, candidates, stops = portfolio_extras(day, journal, out, lambda t: fetch_price_history(t, period="18mo"))
    t_b, l_b = portfolio_lines(day, journal, orders, ledger, closes, ict,
                               today_exits=today_exits, candidates=candidates, stops=stops)
    path = write_outputs(out, [("ranking", t_a, l_a), ("300man", t_b, l_b)])
    # 公開リポジトリの実行ログなので、銘柄は出さず件数だけ出す
    print(f"note_two_built day={day} alerts={len(alerts)} pullback={len(pullback)} facts={len(facts)} "
          f"climax={'-' if climax is None else len(climax)} ict={'-' if ict is None else len(ict)} manifest={path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
