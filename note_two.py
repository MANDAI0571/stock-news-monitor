#!/usr/bin/env python3
"""note の2本立て（2026-10-16 持ち主の指示）。毎営業日の引け後に2本を作り、note に下書き保存する。

  (A) 高値更新・押し目・セリングクライマックス ランキング
      - 過去の成績ランク SS・S の銘柄を、リアルタイムアラートのメールと同じ形で紹介
      - 52週高値更新・直近高値更新の TOP10（ランクの高い順）、25MA・200MA 押し目の TOP10
        （注意なしの中で売買代金の大きい順）、セリングクライマックス（25日線から深い順、◎印）
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


def ranking_lines(day: dt.date, alerts: list, pullback: pd.DataFrame, climax: list[dict] | None,
                  info: dict, strength: dict, mom60: dict, ict: list[dict] | None = None) -> tuple[str, list[str]]:
    """(A) のタイトルと本文（markdown 風の行）。alerts は今日はじめての高値更新（is_fresh_break）。"""
    import intraday_high_alert as M

    top = sorted([a for a in alerts if a.grade in ("SS", "S")], key=_grade_key)
    hi52 = sorted([a for a in alerts if a.alert_type == "52週高値更新"], key=_grade_key)
    swing = sorted([a for a in alerts if a.alert_type != "52週高値更新"], key=_grade_key)
    lines = [f"{day}の終値で、4つの条件に当てはまった銘柄を機械的に並べた一覧です。",
             "**売買をすすめるものではありません。** ランクは過去の似た形の成績から付けた目安で、今後を保証するものではありません。", "",
             f"## 過去の成績ランク SS・S の銘柄（SS {sum(a.grade == 'SS' for a in top)}件・S {sum(a.grade == 'S' for a in top)}件）", ""]
    if not top:
        lines += ["本日は該当なし。", ""]
    for a in top:
        block = [x for x in M._format_alert(a) if "OpenWork" not in x]
        body = [x.strip().replace("現在値:", "終値:") for x in block[1:] if x.strip()]
        lines += [f"**{a.grade}｜{block[0]}**（{(a.market or '').replace('東証', '')}）"] + body + [""]

    def top10(title, rows):
        out = [f"## {title} TOP10（全{len(rows)}件）", ""]
        out += [f"{i}. {a.grade or 'ランクなし'}　{a.code} {a.name}" for i, a in enumerate(rows[:10], 1)]
        return out + ([] if rows else ["本日は該当なし。"]) + [""]

    lines += top10("52週高値更新", hi52) + top10("直近高値更新", swing)
    pb = pullback.copy()
    if len(pb):
        pb = pb[(pb["ma25_touch"] == True) | (pb["ma200_touch"] == True)]  # noqa: E712
        pb["caution"] = [(info.get(str(c), ("", ""))[0] != "東証プライム")
                         or (mom60.get(str(c), 0.0) >= PULLBACK_MOM60_LIMIT) for c in pb["code"]]
        pb = pb.sort_values("turnover_20d", ascending=False)
    ok = pb[~pb["caution"]] if len(pb) else pb
    lines += [f"## 25MA・200MA 押し目 TOP10（全{len(pb)}件のうち注意なし{len(ok)}件・売買代金の大きい順）", ""]
    for i, r in enumerate(ok.head(10).itertuples(), 1):
        which = "・".join(x for x, f in (("25MA", r.ma25_touch), ("200MA", r.ma200_touch)) if f == True)  # noqa: E712
        lines.append(f"{i}. {r.code} {r.name}（{which}）")
    if not len(ok):
        lines.append("本日は該当なし。")
    if len(pb) - len(ok):
        lines.append(f"（注意あり {len(pb) - len(ok)}件は省略：グロース・スタンダード、または60日で+25%以上上げた後）")
    lines.append("")
    if climax is None:
        lines += ["## セリングクライマックス（下降中の出来高急増）", "", "本日は数字を取得できませんでした。", ""]
    else:
        lines += [f"## セリングクライマックス（下降中の出来高急増）全{len(climax)}件（25日線から深い順・終値）", ""]
        for i, r in enumerate(climax[:10], 1):
            rel = strength.get(info.get(str(r["code"]), ("", ""))[1])
            mark = "◎ " if rel is not None and rel < CLIMAX_MARK_REL else ""
            lines.append(f"{i}. {mark}{r['code']} {r['name']}（25日線から{r['dev25_pct']:+.1f}%・出来高{r['ratio']:.1f}倍）")
        if not climax:
            lines.append("本日は該当なし。")
        if len(climax) > 10:
            lines.append(f"（ほか{len(climax) - 10}件）")
        lines += ["（◎＝業種の勢いがとても弱い：その業種の20日騰落の平均が、全銘柄の中央値より4ポイント以上低い）", ""]
    if ict is not None:
        # 2026-10-25: 押し目の箱（オーダーブロック。us_ict.py）。米株と同じ手法で、プライムの5年でも4期間そろって上だった
        import us_ict

        lines += us_ict.section_lines(ict, market="jp")
    lines += ["## ランクについて", "",
              "似た形のアラートが過去5年（2022〜2026年）にどうなったかで付けた目安です（52週高値更新・直近高値更新だけ。"
              "プライムとスタンダード。グロースは過去の検証で並びがそろわなかったため付けていません）。"
              "翌営業日の寄付で入り、終値で-5%か+12%、または15営業日で手じまった場合の損益比（売買コスト0.2%込み）は、"
              f"プライムで S {' / '.join(f'{v:.2f}' for v in G.GRADE_PF['S'])}、"
              f"C {' / '.join(f'{v:.2f}' for v in G.GRADE_PF['C'])}"
              "（2022-23 / 2024 / 2025 / 2026）。条件は全期間を見て選んだので、別の期間で確かめた数字ではありません。"]
    fw = G.forward_lines()
    if fw:
        lines += [""] + fw
    lines += ["", "押し目とセリングクライマックスは、同じ決まりで過去5年を調べると、SS〜Cの5段階を支える差は見つからず、"
              "4つの期間でそろって差が出たのは次の2つだけでした（損益比。2022-23 / 2024 / 2025 / 2026）。",
              "- 押し目：注意なし 1.37 / 0.93 / 1.77 / 1.48、注意あり（グロース・スタンダード、または60日で+25%以上）0.97 / 0.82 / 1.34 / 1.04",
              "- セリングクライマックス：業種の勢いがとても弱い（◎。その業種の20日騰落の平均が全銘柄の中央値より4ポイント以上低い）"
              "2.65 / 6.52 / 9.87 / 1.38、それ以外 1.48 / 1.59 / 1.87 / 0.83（急落で1日に60件以上出た13日を除いても"
              " ◎ 2.97 / 3.32 / 4.21 / 1.38、それ以外 1.30 / 0.93 / 1.31 / 0.83）",
              "そのため、この2つには SS〜C ではなく「注意なし」「◎」の印だけを付けています。", "", "---", ""] + COMPLIANCE_LINES
    return f"高値更新・押し目・セリングクライマックス ランキング｜{day}", lines


def portfolio_lines(day: dt.date, journal: pd.DataFrame, orders: pd.DataFrame, ledger_text: str,
                    closes: dict) -> tuple[str, list[str]]:
    """(B) のタイトルと本文。保有の評価は day の終値（closes）。取れなければ台帳の値。"""
    def pick(key):
        for line in ledger_text.splitlines():
            if line.startswith(f"- {key}:"):
                return line.split(":", 1)[1].strip()
        return "-"
    op = journal[journal["status"] == "OPEN"]
    cl = journal[journal["status"] == "CLOSED"]
    rows, unreal = [], 0.0
    for r in op.itertuples():
        px = closes.get(str(r.code))
        when = f"{day}終値" if px else f"{r.last_mark_date}終値"
        px = px or float(r.mark_price or r.entry_price)
        pnl = (px - float(r.entry_price)) * int(float(r.shares))
        unreal += pnl
        rows.append(f"- {r.code} {r.name}：{int(float(r.shares))}株、{r.entry_date}に{float(r.entry_price):,.0f}円で買い → "
                    f"{when} {px:,.0f}円（{(px / float(r.entry_price) - 1) * 100:+.1f}%、{pnl:+,.0f}円）")
    lines = [f"Claude（AI）が、300万円の紙上運用（実際のお金は使っていません）を決まったルールで続けている記録です。{day}の終値時点。",
             "**売買をすすめるものではありません。**", "", "## 運用の状況", "",
             f"- 初期資金 3,000,000円 → 現金 {pick('現金残')}",
             f"- 保有 {len(op)}銘柄（含み損益 {unreal:+,.0f}円）",
             f"- 実現損益（累計）{pick('実現損益（累計）')}（{len(cl)}回の決済）", "", "## 保有銘柄", ""]
    lines += rows or ["- なし（現金のみ）"]
    lines += ["", "## これまでの決済", ""]
    for r in cl.sort_values("exit_date").itertuples():
        lines.append(f"- {r.exit_date} {r.code} {r.name}：{float(r.realized_pnl):+,.0f}円"
                     f"（{float(r.exit_return_pct):+.1f}%、{r.exit_reason}）")
    if not len(cl):
        lines.append("- まだありません")
    status = {"FILLED": "約定", "CANCELLED_GAP": "寄り付きで窓が開いたため見送り", "PENDING": "約定待ち"}
    recent = orders[(orders["execution_date"] >= (day - dt.timedelta(days=7)).isoformat())]
    lines += ["", "## 直近の売買の予定と結果", ""]
    for r in recent.sort_values("execution_date").itertuples():
        lines.append(f"- {r.decision_date}に判定 → {r.execution_date} {'買い' if r.side == 'BUY' else '売り'} "
                     f"{r.code} {r.name} {r.shares}株：{status.get(r.status, r.status)}")
    if not (orders["decision_date"] == day.isoformat()).any():
        lines.append(f"- {day}の判定（次の営業日の売買）：この記事を作った時点では出ていません（夜に決まります）")
    lines += ["", "## ルール", "",
              "1銘柄あたり最大60万円。終値で判定し、翌営業日の寄付で売買します。損切りは-5%、利確は+12%、最長15営業日。"
              "売買コストと税金は入れていません（紙上運用のため）。", "", "---", "",
              "紙上運用の記録です。実際のお金での売買ではありません。", ""] + COMPLIANCE_LINES
    return f"Claudeが300万円運用｜運用状況 {day}", lines


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


def market_facts(universe: pd.DataFrame, fetch, codes_for_close: set[str]):
    """(銘柄→(市場, 業種), 業種の勢い, 60日騰落%, 指定銘柄の最終終値)。価格は巡回の控え（キャッシュ）から。"""
    info = {str(r.code): (str(r.market), str(r.sector)) for r in universe.itertuples()}
    ret20_by_sector, all_ret20, mom60, closes = {}, [], {}, {}
    for r in universe.itertuples():
        try:
            c = fetch(str(r.ticker))["Close"].dropna().astype(float)
        except Exception:                                   # noqa: BLE001
            continue
        if len(c) < 61:
            continue
        r20 = c.iloc[-1] / c.iloc[-21] - 1
        ret20_by_sector.setdefault(str(r.sector), []).append(r20)
        all_ret20.append(r20)
        mom60[str(r.code)] = (c.iloc[-1] / c.iloc[-61] - 1) * 100
        if str(r.code) in codes_for_close:
            closes[str(r.code)] = float(c.iloc[-1])
    return info, G.sector_strength(ret20_by_sector, all_ret20), mom60, closes


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
    info, strength, mom60, closes = market_facts(
        universe, lambda t: fetch_price_history(t, period="18mo"), held)
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
    t_a, l_a = ranking_lines(day, alerts, pullback, climax, info, strength, mom60, ict)
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
    ledger = (ROOT / "docs" / "claude_300man_ledger.md").read_text(encoding="utf-8")
    t_b, l_b = portfolio_lines(day, journal, orders, ledger, closes)
    path = write_outputs(out, [("ranking", t_a, l_a), ("300man", t_b, l_b)])
    # 公開リポジトリの実行ログなので、銘柄は出さず件数だけ出す
    print(f"note_two_built day={day} alerts={len(alerts)} pullback={len(pullback)} "
          f"climax={'-' if climax is None else len(climax)} ict={'-' if ict is None else len(ict)} manifest={path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
