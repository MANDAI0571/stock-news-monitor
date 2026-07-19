#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
メトロン日次KPIサマリー生成（フェーズ1・Claude/Fable）

metron_daily.csv（スキーマは phase1/metron/kpi_min_schema.md）を読み、
指定日の日次サマリー(.md)を生成する。

原則:
- 実測のみ。空欄の指標は「未記録」と表示し、0や推定で埋めない（捏造禁止）。
- 前日比は、当日と前日の両方に実測値がある指標だけ計算する。
- 秘密情報はCSVにもサマリーにも書かない。

使い方:
  python3 generate_metron_summary.py --csv metron_daily.csv --date YYYY-MM-DD [--out out.md]
  （--date 省略時はCSV内の最新日を対象）
"""

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, timedelta

NUM_INT = ("posts_sent", "impressions", "clicks", "note_views", "signups", "conversions")


def to_int(v):
    v = (v or "").strip()
    if v == "":
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def load(csv_path):
    rows = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            r = {k: (v.strip() if isinstance(v, str) else v) for k, v in r.items()}
            rows.append(r)
    return rows


def day_totals(rows, date_str):
    """その日の合計と、utm_content別・channel別の登録数を返す。"""
    day = [r for r in rows if r.get("date") == date_str]
    tot = {k: None for k in NUM_INT}
    by_content = defaultdict(lambda: None)
    by_channel = defaultdict(int)
    is_dummy = any((r.get("note") or "").find("サンプル") >= 0 or (r.get("note") or "").find("ダミー") >= 0 for r in day)
    for r in day:
        for k in NUM_INT:
            v = to_int(r.get(k))
            if v is not None:
                tot[k] = (tot[k] or 0) + v
        s = to_int(r.get("signups"))
        if s is not None:
            c = r.get("utm_content") or "(未設定)"
            by_content[c] = (by_content[c] or 0) + s
            ch = r.get("channel") or "(未設定)"
            by_channel[ch] += s
    return day, tot, dict(by_content), dict(by_channel), is_dummy


def ctr_of(tot):
    if tot.get("clicks") is not None and tot.get("impressions"):
        return tot["clicks"] / tot["impressions"]
    return None


def fmt_int(v):
    return "未記録" if v is None else f"{v:,}"


def fmt_ctr(v):
    return "未記録" if v is None else f"{v*100:.1f}%"


def diff_int(cur, prev):
    if cur is None or prev is None:
        return ""
    d = cur - prev
    sign = "+" if d >= 0 else ""
    return f"（前日比 {sign}{d:,}）"


def diff_ctr(cur, prev):
    if cur is None or prev is None:
        return ""
    d = (cur - prev) * 100
    sign = "+" if d >= 0 else ""
    return f"（前日比 {sign}{d:.1f}pt）"


def prev_date_str(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def build(rows, date_str):
    day, tot, by_content, by_channel, is_dummy = day_totals(rows, date_str)
    if not day:
        raise ValueError(f"指定日 {date_str} のデータがありません。")
    _, ptot, _, _, _ = day_totals(rows, prev_date_str(date_str))
    ctr, pctr = ctr_of(tot), ctr_of(ptot)

    dt = datetime.strptime(date_str, "%Y-%m-%d")
    date_jp = f"{dt.year}年{dt.month}月{dt.day}日"

    lines = [f"# メトロン日次KPI {date_jp}", ""]
    if is_dummy:
        lines += ["> ※これはサンプル入力（動作確認用ダミー）に対する出力です。実データではありません。", ""]

    lines += ["## 主要3指標"]
    lines.append(f"- 登録数：{fmt_int(tot['signups'])} {diff_int(tot['signups'], ptot['signups'])}".rstrip())
    lines.append(f"- クリック数／クリック率：{fmt_int(tot['clicks'])}／{fmt_ctr(ctr)} {diff_ctr(ctr, pctr)}".rstrip())
    lines.append(f"- note閲覧→登録 転換数：{fmt_int(tot['conversions'])} {diff_int(tot['conversions'], ptot['conversions'])}".rstrip())
    lines.append("")

    lines += ["## 補助"]
    lines.append(f"- 配信数：{fmt_int(tot['posts_sent'])}本")
    if by_channel:
        naiwake = "／".join(f"{ch} {n}" for ch, n in sorted(by_channel.items()))
        lines.append(f"- 経路別 登録数：{naiwake}")
    lines.append("")

    if by_content:
        lines += ["## 記事別（utm_content）登録数"]
        for c, n in sorted(by_content.items(), key=lambda kv: -(kv[1] or 0)):
            lines.append(f"- {c}：{n}")
        lines.append("")

    lines += ["## 所見（1〜2行・手入力）", "- ", ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--date", help="YYYY-MM-DD（省略時はCSV最新日）")
    ap.add_argument("--out")
    args = ap.parse_args()

    rows = load(args.csv)
    if not rows:
        sys.stderr.write("[中止] CSVに行がありません。\n")
        sys.exit(2)

    date_str = args.date
    if not date_str:
        dates = sorted({r.get("date") for r in rows if r.get("date")})
        if not dates:
            sys.stderr.write("[中止] date列が空です。\n")
            sys.exit(2)
        date_str = dates[-1]

    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        md = build(rows, date_str)
    except ValueError as exc:
        sys.stderr.write(f"[中止] {exc}\n")
        sys.exit(2)
    out = args.out or f"メトロン日次KPI_{date_str.replace('-', '')}.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"生成: {out}（対象日 {date_str}）")


if __name__ == "__main__":
    main()
