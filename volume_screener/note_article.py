#!/usr/bin/env python3
"""出来高急増×下降トレンドの結果を、note の有料記事の下書きにする（2026-10-16 持ち主の指示
「出来高急増メールを有料化してnoteに出したい」）。

前半（無料にする部分）: 日付・件数・条件・この一覧に出た銘柄のその後（track.json）。
後半（有料にする部分）: 銘柄の一覧。境目に「▼ここから有料▼」の線を入れる。
値段の設定と公開は持ち主が note の画面で行う。ここでは下書きを作るだけで、公開しない。
売買をすすめるものではない（記事にも書く）。

  python3 volume_screener/note_article.py --out outputs/vs_note
  （SCREENER_OUT の status.txt が send で、件名に今日の日付があるときだけ作る）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

DISCLAIMER = ("これは条件に当てはまった銘柄を機械的に並べた一覧です。売買をすすめるものではありません。"
              "投資の判断はご自身でお願いします。")
PAID_LINE = "▼ここから有料▼"
# 2026-11-06 持ち主「全部公開まで」「（有料の記事は）一週間無料で」: この期間の記事は無料で公開するので、
# 境目の線と前置きを「無料で公開している」とわかる文にする（値段はその後に持ち主が決める）
FREE_FROM, FREE_UNTIL = "2026-11-06", "2026-11-12"
FREE_LINE = "▼ここから下も、11/6〜11/12 の記事は無料で公開しています（お試しの期間）▼"


def free_week(result: dict) -> bool:
    return FREE_FROM <= str(result.get("date") or "") <= FREE_UNTIL


def _oku(v) -> str:
    try:
        return f"{float(v) / 1e8:.1f}億円"
    except (TypeError, ValueError):
        return "-"


H52_SHOW = 5    # 記事に載せる件数（種類ごと）。note は長い記事で編集画面が重くなるので、1銘柄1行で少しだけ


def _yen(v) -> str:
    v = float(v)
    return f"{v:,.0f}円" if v >= 1000 else f"{v:,.1f}円"


# 銘柄の行の先頭に付ける形の印（2026-10-27 持ち主「記事で銘柄紹介する時、この銘柄は52週新高値とか25MAとか
# 直近高値とかセリングクライマックスとか、わかるようにして」）
LABELS = {"new": "【52週新高値】", "recent": "【直近高値更新】", "ma25": "【52週新高値→25日線押し目】",
          "climax": "【セリングクライマックス】"}


def high52_lines(h: dict) -> list[str]:
    """52週新高値の節（2026-10-27 持ち主「出来高急増のところに52週新高値を織り込んで…25MAとかいくつか紹介して。
    文字数が多くなると note が落ちるので注意」）。無料の部分に、種類ごとに売買代金の大きい順に5件まで、1銘柄1行。"""
    md = lambda d: f"{int(d[5:7])}/{int(d[8:10])}" if len(d or "") >= 10 else "-"  # noqa: E731
    lines = ["## 52週新高値・直近高値の銘柄（売買代金の大きい順に5件まで）", "",
             "同じ東証プライムの銘柄（20日平均の売買代金1億円以上）から、高値に関わる3つの形を機械的に拾いました。"
             "行の先頭の【 】が形です。", ""]
    heads = (("new", "本日、52週高値を更新"), ("recent", "本日、直近高値（60営業日の高値）を更新"),
             ("ma25", "52週高値のあと、25日線まで押した"))
    for kind, head in heads:
        if f"n_{kind}" not in h:                      # 前の形の記録（直近高値なし）
            continue
        n, rows = int(h.get(f"n_{kind}") or 0), list(h.get(kind) or [])[:H52_SHOW]
        lines += [f"### {head}（全{n}件）", ""]
        if not rows:
            lines += ["当てはまる銘柄はありませんでした。", ""]
            continue
        for r in rows:
            head_ = f"- {LABELS[kind]}{r['code']} {r['name']}　現在 {_yen(r['price'])}"
            if kind == "new":
                lines.append(f"{head_}・52週高値 {_yen(r['line'])}（{md(r.get('line_date'))}）を当日高値で "
                             f"{float(r['excess_pct']):+.2f}% 上抜け")
            elif kind == "recent":
                lines.append(f"{head_}・直近高値 {_yen(r['line'])}（{md(r.get('line_date'))}）を当日高値で "
                             f"{float(r['excess_pct']):+.2f}% 上抜け")
            else:
                lines.append(f"{head_}・25日線から {float(r['dev25_pct']):+.1f}%"
                             f"・52週高値 {_yen(r['line'])}（{md(r.get('line_date'))}）から {float(r['from_high_pct']):+.1f}%")
        lines.append("")
    lines += ["※ 直近高値更新＝52週高値ではないが、前日までの60営業日の高値を本日の高値で上抜け、現在値が25日線より上。"
              "25日線押し目＝直近20営業日のうちに52週高値を付け、本日の安値が25日線の+1%以内まで下がり、"
              "現在値が25日線の-2%より上、25日線が上向き。値は記事の時点のもので、売買をすすめるものではありません。", ""]
    return lines


CWH_SHOW = 10   # カップ・ウィズ・ハンドルの記事に載せる件数（上抜け・形成中それぞれ）。1銘柄1行
CWH_VOL = 2.0   # 【出来高急増】の印を付ける出来高倍率（出来高急増×下降の条件と同じ2倍）


def cwh_rule_lines(c: dict) -> list[str]:
    """カップ・ウィズ・ハンドルの決まりと件数（無料の部分。銘柄は有料の部分）。
    2026-11-06 持ち主「カップウィズハンドルと出来高急増込みで有料か」。"""
    return ["## カップ・ウィズ・ハンドルの条件", "",
            "同じ東証プライムの銘柄（20日平均の売買代金1億円以上）の日足から、次の形を機械的に探しました。"
            "目で選んだものではありません。", "",
            "- カップ：左の高値（その前60営業日の高値より高い山）から底まで12〜35%下げ、右の高値が左の高値の"
            "90〜103%まで戻った。左の高値から右の高値まで30〜130営業日、底の前後はそれぞれ10営業日以上で、"
            "底はカップの左から2〜8割の位置（V字や片寄った形を除く）",
            "- 取っ手：右の高値（前日までの30営業日の高値）のあと5〜25営業日の、3〜15%の浅い下げ。"
            "取っ手の安値はカップの真ん中より上",
            "- 前の上昇：左の高値は、その前（最大120営業日）の安値の1.25倍以上",
            "- 上抜け：本日の高値が取っ手の高値（右の高値）を上回った。形成中：まだ上回っていないが、"
            "現在値が取っ手の高値の95%以上",
            "- 出来高急増：本日のその時刻までの出来高が過去20日平均の2倍以上（上抜けの日の出来高を見るため）", "",
            f"## カップ・ウィズ・ハンドルの本日の件数：上抜け{int(c.get('n_break') or 0)}件"
            f"（うち出来高急増{int(c.get('n_break_vol') or 0)}件）・形成中{int(c.get('n_near') or 0)}件", "",
            "※ この形の一覧に出た銘柄のその後の成績は、まだ数えていません（2026-11-06 に始めたため）。", ""]


def cwh_list_lines(c: dict) -> list[str]:
    """カップ・ウィズ・ハンドルの銘柄（有料の部分）。上抜けは出来高倍率の大きい順、形成中は取っ手の高値に近い順。"""
    md = lambda d: f"{int(d[5:7])}/{int(d[8:10])}" if len(d or "") >= 10 else "-"  # noqa: E731
    lines = ["## カップ・ウィズ・ハンドルの銘柄", "",
             "【CWH上抜け】＝本日、取っ手の高値を上抜け。【CWH形成中】＝取っ手の高値の95%以上まで来ている。"
             "【出来高急増】＝本日の出来高が過去20日平均の2倍以上。", ""]
    heads = (("break", "上抜け", "出来高倍率の大きい順"), ("near", "形成中", "取っ手の高値に近い順"))
    for kind, label, order in heads:
        n, rows = int(c.get(f"n_{kind}") or 0), list(c.get(kind) or [])[:CWH_SHOW]
        lines += [f"### {label}（全{n}件、{order}に{CWH_SHOW}件まで）", ""]
        if not rows:
            lines += ["当てはまる銘柄はありませんでした。", ""]
            continue
        for r in rows:
            vr = r.get("vol_ratio")
            surge = "【出来高急増】" if vr is not None and float(vr) >= CWH_VOL else ""
            pivot = float(r["pivot"])
            if kind == "break":
                where = f"を当日高値で {(float(r['today_high']) / pivot - 1) * 100:+.2f}% 上抜け"
            else:
                where = f"まで {float(r['to_pivot_pct']):+.1f}%"
            lines.append(f"- 【CWH{label}】{surge}{r['code']} {r['name']}　現在 {_yen(r['price'])}"
                         f"・取っ手の高値 {_yen(pivot)}（{md(r.get('pivot_date'))}）{where}"
                         f"・カップ -{float(r['cup_depth_pct']):.0f}%（{md(r.get('left_date'))}→{md(r.get('bottom_date'))}）"
                         f"・取っ手 {int(r['handle_days'])}日 -{float(r['handle_depth_pct']):.1f}%"
                         + (f"・出来高 {float(vr):.1f}倍" if vr is not None else ""))
        lines.append("")
    return lines


def article_lines(result: dict, track_note: str = "", warning: str = "") -> tuple[str, list[str]]:
    """warning はメールの先頭の注意書き（データの欠け・疑い・調べられなかった銘柄）。あれば
    無料部分と一覧の両方に出す。0件の日に「該当なし」とだけ書いていた（2026-10-19 の見直し）。"""
    rows = result.get("rows") or []
    when = f"{result.get('date')} {result.get('cutoff')}"
    # ランク（◎）。付けられなかった日は、その理由（screener の経過メモ）を書く（2026-10-22）
    rank_note = next((str(n) for n in result.get("notes") or []
                      if "ランク" in str(n) and not str(n).startswith("並び順")), "")
    ranked = rank_note.startswith("ランク:")
    n_mark = sum(1 for r in rows if r.get("mark") == "◎")
    h52 = result.get("high52") if isinstance(result.get("high52"), dict) else None
    cwh = result.get("cwh") if isinstance(result.get("cwh"), dict) else None
    paid = "後半の部分" if free_week(result) else "有料の部分"
    if cwh:
        # 2026-11-06 持ち主「カップウィズハンドルと出来高急増込みで有料か」
        title = (f"カップウィズハンドルと出来高急増・52週新高値｜{when}時点 上抜け{int(cwh.get('n_break') or 0)}件"
                 + (f"・新高値{int(h52.get('n_new') or 0)}件" if h52 else "") + f"・急増{len(rows)}件")
        intro = (f"東証プライム全銘柄（約1,550銘柄）から、カップ・ウィズ・ハンドルの形の銘柄と、下降トレンドの途中で"
                 f"出来高が急に増えた銘柄（どちらも{paid}）"
                 + ("、52週新高値の銘柄（無料の部分で一部を紹介）" if h52 else "")
                 + f"を機械的に探した結果です（{when}時点の値）。")
    elif h52:
        # 2026-10-27 持ち主「タイトルも52週新高値の文字を織り込んで」
        title = f"52週新高値と出来高急増×下降トレンド｜{when}時点 新高値{int(h52.get('n_new') or 0)}件・急増{len(rows)}件"
        intro = (f"東証プライム全銘柄（約1,550銘柄）から、52週新高値の銘柄（無料の部分で一部を紹介）と、下降トレンドの途中で"
                 f"出来高が急に増えた銘柄（{paid}）を機械的に探した結果です（{when}時点の値）。")
    else:
        title = f"出来高急増×下降トレンド｜{when}時点 {len(rows)}件"
        intro = (f"東証プライム全銘柄（約1,550銘柄）から、下降トレンドの途中で出来高が急に増えた銘柄を"
                 f"機械的に探した結果です（{when}時点の値）。")
    lines = [intro,
             "**売買をすすめるもの（投資助言）ではありません。** 個別のご相談・ご質問にはお答えしていません。", ""]
    # 52週新高値を先に（タイトルで来た人がすぐ読めるように）。そのあとが出来高急増×下降の説明
    if h52:
        lines += high52_lines(h52)
    if cwh:
        lines += cwh_rule_lines(cwh)
    vs = "出来高急増×下降トレンドの" if h52 or cwh else ""
    lines += [f"## {vs}条件", "",
              "- 出来高が過去20日平均の2倍以上（当日のその時刻までの出来高）",
              "- 現在値 < 25日線 < 75日線、25日線が5本前より低い（下降トレンド）",
              "- 20日平均の売買代金が1億円以上",
              "- 並び順はランク（◎）の銘柄が先、その中は25日線から深い順", "",
              f"## {vs}本日の件数：{len(rows)}件" + (f"（うち ◎ {n_mark}件）" if ranked and rows else ""), ""]
    try:
        import rank as RK

        lines += ["## ランク（◎）について", ""] + RK.explain_lines() + [""]
    except Exception:                                 # noqa: BLE001
        pass
    if rank_note and not ranked:
        lines += [f"※ 本日は、{rank_note}。", ""]
    if warning:
        lines += [f"**※ 本日のデータについての注意：** {warning}", ""]
    if track_note:
        lines += ["## この一覧に出た銘柄のその後", "", track_note, ""]
    lines += ["---", "", FREE_LINE if free_week(result) else PAID_LINE, ""]
    if cwh:
        lines += cwh_list_lines(cwh)
    lines += ["## 出来高急増×下降トレンドの銘柄の一覧" if vs else "## 銘柄の一覧", ""]
    if vs and rows:
        lines += ["【セリングクライマックス】＝下降トレンドの途中で出来高が急に増えた形（上の条件）。", ""]
    if not rows:
        lines.append("条件に当てはまった銘柄はありませんでした。" + (
            "ただし上の注意のとおり、データの欠け・疑いがあるため、見落としがあり得ます。" if warning else ""))
    elif warning:
        lines += ["※ 上の注意のとおり、データの欠け・疑いがあるため、この一覧には見落としがあり得ます。", ""]
    for i, r in enumerate(rows, 1):
        lines.append(f"**{i}. {'◎ ' if r.get('mark') == '◎' else ''}{LABELS['climax'] if vs else ''}"
                     f"{r['code']} {r['name']}**")
        if r.get("mark") == "◎":
            lines.append("- ランク ◎（業種の勢いがとても弱い）")
        lines.append(f"- {float(r['price']):,.1f}円（{'前日比' if not r.get('prev_gap') else '前回の足から'}"
                     f" {float(r['chg_pct']):+.2f}%）・25日線から {float(r['dev25_pct']):+.1f}%")
        lines.append(f"- 出来高 {float(r['ratio']):.2f}倍（{r.get('base_label', '過去20日平均')}比）・"
                     f"20日平均売買代金 {_oku(r.get('turnover20'))}")
        if r.get("suspect_split"):
            lines.append("- ※分割が調整されていない疑いがあり、乖離と順位は信用できません")
        lines.append(f"- 日足チャート https://kabutan.jp/stock/chart?code={r['code']}")
    from note_legal import COMPLIANCE_LINES
    lines += ["", "---", ""] + COMPLIANCE_LINES
    return title, lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="出来高急増×下降の note 下書き（有料記事）")
    ap.add_argument("--out", default="outputs/vs_note")
    ap.add_argument("--today", default="")
    a = ap.parse_args(argv)
    src = Path(os.environ.get("SCREENER_OUT", HERE / "out"))
    from jptime import jst_today
    from note_two import write_outputs

    today = a.today or jst_today().isoformat()
    status = (src / "status.txt").read_text(encoding="utf-8").strip() if (src / "status.txt").exists() else ""
    subject = (src / "subject.txt").read_text(encoding="utf-8") if (src / "subject.txt").exists() else ""
    if status != "send" or today not in subject or "【スクリーニング失敗】" in subject:
        print(f"vs_note=skipped status={status or '-'}", flush=True)
        return 0
    result = json.loads((src / "result.json").read_text(encoding="utf-8"))
    track_note = ""
    try:
        import track as TR
        sm = TR.summary(json.loads((src / "track.json").read_text(encoding="utf-8")), 0)
        if sm["n"]:
            pf = "-" if sm["pf"] is None else f"{sm['pf']:.2f}"
            track_note = (f"{sm['since']}以降にこの一覧に出た銘柄を、翌営業日の寄付で買い、終値で-5%か+12%、"
                          f"または15営業日で翌寄付に手じまう決まり（売買コスト0.2%込み）で数えると、"
                          f"手じまいまで終わった{sm['n']}件で、損益比{pf}・勝率{sm['win'] * 100:.0f}%・"
                          f"1回あたり平均{sm['avg'] * 100:+.1f}%でした。"
                          + ("30件に満たないうちは偶然の差が大きい数字です。" if sm["n"] < 30 else ""))
            ml = TR.mark_line(sm)
            track_note += (" " + ml) if ml else ""
    except (OSError, ValueError, ImportError):
        track_note = ""
    # メールの先頭の注意書き（「※ …: 」で始まる最初の行）。件名に印があるのに記事に無かった
    body = (src / "body.txt").read_text(encoding="utf-8") if (src / "body.txt").exists() else ""
    first = body.splitlines()[0] if body else ""
    warning = first.split(": ", 1)[1].strip() if first.startswith("※") and ": " in first else ""
    title, lines = article_lines(result, track_note, warning)
    write_outputs(Path(a.out), [("volume", title, lines)])
    print(f"vs_note=built rows={len(result.get('rows') or [])}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
