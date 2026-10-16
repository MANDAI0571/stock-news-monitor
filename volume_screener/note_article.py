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


def _oku(v) -> str:
    try:
        return f"{float(v) / 1e8:.1f}億円"
    except (TypeError, ValueError):
        return "-"


def article_lines(result: dict, track_note: str = "") -> tuple[str, list[str]]:
    rows = result.get("rows") or []
    when = f"{result.get('date')} {result.get('cutoff')}"
    title = f"出来高急増×下降トレンド｜{when}時点 {len(rows)}件"
    lines = [f"東証プライム全銘柄（約1,550銘柄）から、下降トレンドの途中で出来高が急に増えた銘柄を"
             f"機械的に探した結果です（{when}時点の値）。",
             "**売買をすすめるもの（投資助言）ではありません。** 個別のご相談・ご質問にはお答えしていません。", "",
             "## 条件", "",
             "- 出来高が過去20日平均の2倍以上（当日のその時刻までの出来高）",
             "- 現在値 < 25日線 < 75日線、25日線が5本前より低い（下降トレンド）",
             "- 20日平均の売買代金が1億円以上",
             "- 並び順は25日線から深い順（過去の検証で、この順に差が出たため）", "",
             f"## 本日の件数：{len(rows)}件", ""]
    if track_note:
        lines += ["## この一覧に出た銘柄のその後", "", track_note, ""]
    lines += ["---", "", PAID_LINE, "", "## 銘柄の一覧", ""]
    if not rows:
        lines.append("本日は該当なし。")
    for i, r in enumerate(rows, 1):
        lines.append(f"**{i}. {r['code']} {r['name']}**")
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
    except (OSError, ValueError, ImportError):
        track_note = ""
    title, lines = article_lines(result, track_note)
    write_outputs(Path(a.out), [("volume", title, lines)])
    print(f"vs_note=built rows={len(result.get('rows') or [])}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
