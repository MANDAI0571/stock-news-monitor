#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compliance_check.py  ―  AI社員「シールド（コンプラ／校閲）」の実体
------------------------------------------------------------------
note公開前の下書き(Markdown/テキスト)を機械的に校閲し、
金融商品取引法に触れやすい表現・禁止トークン・免責の有無を検査する。

方針:
  - 「断定」「推奨・助言」「利益保証・煽り」の3カテゴリで危険表現を検出
  - 「未取得 / OpenWork / 残業 / 有給 / nan / None / null / inf」等の禁止トークンを検出
  - 未置換のプレースホルダ（{{...}}）を検出
  - 免責文（投資助言でない旨・自己責任）が入っているかを確認
  - 合格なら exit code 0、要修正なら 1 を返す（公開ゲートとして使える）
  - 校閲レポート(Markdown)を出力できる

使い方:
  python3 compliance_check.py --input 本日のnote下書き_YYYYMMDD.md
  python3 compliance_check.py --input draft.md --report 校閲レポート.md

注意: これは自社note原稿の表現チェック補助であり、法的助言ではありません。
      最終判断は高重さん（および必要に応じて専門家）が行ってください。
"""
import argparse
import re
import sys

# ── 禁止トークン（データ不備の生出力を防ぐ） ──────────────
FORBIDDEN_SUBSTR = ("未取得", "OpenWork", "残業", "有給")
FORBIDDEN_WORD = ("nan", "None", "null", "inf", "NaN", "NULL")
PLACEHOLDER_RE = re.compile(r"\{\{[^{}\n]+\}\}")

# ── 金商法リスク表現の辞書（カテゴリ, 重大度, 正規表現, 言い換え例） ──
# severity: "high" = 公開ゲートで不合格 / "warn" = 要注意（人の確認を促す）
RISK_RULES = [
    # 断定・予測の言い切り
    ("断定", "high", r"必ず(上が|下が|儲か|勝て|利益)", "「〜しやすい局面」「〜傾向がみられる」に言い換える"),
    ("断定", "high", r"確実に(上が|下が|儲か|勝て|利益|稼)", "「確実」を避け「過去の傾向では」等に"),
    ("断定", "high", r"絶対(に)?(上が|下が|儲か|勝|安全|大丈夫)", "「絶対」を削除し観察事実のみ述べる"),
    ("断定", "high", r"間違いなく(上が|上昇|下が|儲)", "「〜の可能性がある」に"),
    ("断定", "high", r"(急騰|暴騰|爆上げ)(は必至|確定|間違いなし|する)", "値動きは事実のみ・予測断定は避ける"),
    ("断定", "warn", r"(天井|底値)(を打った|確定|です)", "「〜のようにも見える」と含みを持たせる"),
    ("断定", "warn", r"(上がり続け|下がらない|下げ止まった)", "断定を避け条件付きの表現に"),
    # 推奨・投資助言
    ("推奨・助言", "high", r"(買い|売り|エントリー|仕込み)推奨", "「注目している」「観察対象」に"),
    ("推奨・助言", "high", r"(今すぐ|今が|絶好の)買い時", "時期の断定を避ける"),
    ("推奨・助言", "high", r"(買うべき|売るべき|仕込むべき|買え|売れ)", "「〜という見方もある」に"),
    ("推奨・助言", "high", r"全力(買い|で買)", "ポジションの断定的指示を避ける"),
    ("推奨・助言", "warn", r"(狙い目|買い場|妙味|おすすめ銘柄)", "主観的推奨語。観察表現に置換を検討"),
    # 利益保証・煽り
    ("利益保証・煽り", "high", r"(必ず)?儲か(る|り)ます", "利益の保証表現は不可"),
    ("利益保証・煽り", "high", r"(元本保証|リスクなし|ノーリスク|損しない|負けない)", "保証・無リスク表現は不可"),
    ("利益保証・煽り", "high", r"(テンバガー|10倍|億り人|資産\d+倍)(確定|確実|間違いなし)", "確定的な利益予測は不可"),
    ("利益保証・煽り", "warn", r"(爆益|一攫千金|必勝|鉄板)", "煽り表現。トーンを中立に"),
]

# 免責の合格条件（下記のいずれかの語を含めば免責ありと判定）
DISCLAIMER_MARKERS = (
    "推奨するものではありません",
    "投資判断はご自身の責任",
    "投資助言ではありません",
    "売買を推奨するものではありません",
)

# 免責行そのものは推奨語の誤検出対象から除外する
DISCLAIMER_LINE_HINTS = ("推奨するものではありません", "投資判断はご自身の責任", "投資助言")


def find_forbidden(text):
    hits = []
    for line_no, line in enumerate(text.splitlines(), 1):
        for t in FORBIDDEN_SUBSTR:
            if t in line:
                hits.append((line_no, t, line.strip()))
        for t in FORBIDDEN_WORD:
            if re.search(rf"(?<![A-Za-z]){re.escape(t)}(?![A-Za-z])", line):
                hits.append((line_no, t, line.strip()))
        for match in PLACEHOLDER_RE.finditer(line):
            hits.append((line_no, match.group(0), line.strip()))
    return hits


def scan_risk(text):
    hits = []  # (category, severity, matched, suggestion, line_no, line)
    for line_no, line in enumerate(text.splitlines(), 1):
        if any(h in line for h in DISCLAIMER_LINE_HINTS):
            continue  # 免責行は対象外
        for cat, sev, pat, sug in RISK_RULES:
            for m in re.finditer(pat, line):
                hits.append((cat, sev, m.group(0), sug, line_no, line.strip()))
    return hits


def has_disclaimer(text):
    return any(mk in text for mk in DISCLAIMER_MARKERS)


def build_report(text):
    forbidden = find_forbidden(text)
    risks = scan_risk(text)
    disc = has_disclaimer(text)
    high = [r for r in risks if r[1] == "high"]
    warn = [r for r in risks if r[1] == "warn"]

    # 合否: 禁止トークンあり or high危険表現あり or 免責なし → 不合格
    passed = (not forbidden) and (not high) and disc

    lines = []
    lines.append("# シールド校閲レポート")
    lines.append("")
    lines.append(f"判定: {'✅ 合格（公開可）' if passed else '⚠️ 要修正（公開ゲート不合格）'}")
    lines.append("")
    lines.append("## サマリー")
    lines.append("")
    lines.append(f"- 禁止トークン: {len(forbidden)}件")
    lines.append(f"- 断定・推奨・保証の危険表現(重大): {len(high)}件")
    lines.append(f"- 要注意表現(警告): {len(warn)}件")
    lines.append(f"- 免責文: {'あり ✅' if disc else 'なし ❌（必須）'}")
    lines.append("")

    if forbidden:
        lines.append("## ❌ 禁止トークン（データ不備の生出力）")
        lines.append("")
        for ln, tok, src in forbidden:
            lines.append(f"- L{ln}: 「{tok}」 → 該当項目を省略するか実数に差し替え")
            lines.append(f"    > {src}")
        lines.append("")

    if high:
        lines.append("## ❌ 重大: 金商法リスク表現（要修正）")
        lines.append("")
        for cat, sev, matched, sug, ln, src in high:
            lines.append(f"- L{ln}【{cat}】「{matched}」 → {sug}")
            lines.append(f"    > {src}")
        lines.append("")

    if warn:
        lines.append("## ⚠️ 警告: 要注意表現（人の目で確認）")
        lines.append("")
        for cat, sev, matched, sug, ln, src in warn:
            lines.append(f"- L{ln}【{cat}】「{matched}」 → {sug}")
            lines.append(f"    > {src}")
        lines.append("")

    if not disc:
        lines.append("## ❌ 免責文が見つかりません")
        lines.append("")
        lines.append("次のいずれかを必ず末尾に入れてください:")
        lines.append("「本記事は個人的な相場観察の記録であり、特定銘柄の売買を推奨するものではありません。"
                     "投資判断はご自身の責任でお願いします。」")
        lines.append("")

    if passed:
        lines.append("## 所見")
        lines.append("")
        lines.append("重大な問題は検出されませんでした。上記の警告があれば人の目で確認のうえ公開してください。")
        lines.append("")

    lines.append("---")
    lines.append("※本チェックは表現の機械的スクリーニングであり、法的助言ではありません。")
    return "\n".join(lines), passed


def main():
    ap = argparse.ArgumentParser(description="note下書きの金商法・表現コンプラ校閲")
    ap.add_argument("--input", required=True, help="校閲する下書き(.md/.txt)")
    ap.add_argument("--report", default=None, help="校閲レポートの出力先(.md)。省略時は標準出力")
    ap.add_argument("--quiet", action="store_true", help="レポートを表示せず終了コードだけ返す")
    a = ap.parse_args()
    with open(a.input, encoding="utf-8") as f:
        text = f.read()
    report, passed = build_report(text)
    if a.report:
        with open(a.report, "w", encoding="utf-8") as f:
            f.write(report)
    if not a.quiet:
        if a.report:
            print(f"[校閲レポート] {a.report} を出力しました")
        print(report if not a.report else report.splitlines()[2])  # 判定行
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
