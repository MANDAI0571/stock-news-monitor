"""カブタン照合ツール（スクリーニング検証運用）。

T-J(2026-07-10): カブタンの52週高値スクリーニングと当システムの結果の
「ズレた銘柄コード」を受け取り、1銘柄ずつ当システムの判定ロジックに
通して原因を診断する。全銘柄スクリーニングは行わないので数分で終わる。

使い方（GitHub Actions の kabutan_check.yml から実行するのが基本）:
    python3 kabutan_check.py --codes "7203, 6758 9984"

- codes にはカブタン側にあって当方に無い銘柄（見逃し疑い）と、
  当方にあってカブタンに無い銘柄（過剰ヒット疑い）を混ぜて渡してよい。
- outputs/screening_highs_*.csv が同じ実行内にあれば突き合わせて
  「当方リストに載っているか」も表示する。
- 結果は標準出力と outputs/kabutan_check.md に出す。

判定の前提（既知のズレ要因）:
- 当方の価格は yfinance の配当調整済み。カブタンは無調整のため、
  配当落ち銘柄では高値の絶対値が数%ズレることがある（判定境界付近で影響）。
- 当方のユニバースは JPX 東証上場の「4桁数字コード」のみ。130A のような
  英字入りコードは現状対象外（カブタンには載る）。
- 当方は「更新」と「接近(乖離3%以内)」の両方を拾う。カブタンの
  「52週高値更新」リストと比べる場合、接近銘柄は当方だけに出るのが正常。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

from scanner.highs import HIGH_LABELS, high_quality_flags, window_high_profile
from scanner.prices import fetch_price_history

PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "outputs"
REPORT_PATH = OUTPUT_DIR / "kabutan_check.md"


def parse_codes(raw: str) -> list[str]:
    """カンマ・空白・改行・読点区切りのコード列を正規化して返す。"""
    tokens = re.split(r"[\s,、，/]+", raw.strip())
    codes: list[str] = []
    for token in tokens:
        code = token.strip().upper()
        if not code:
            continue
        code = code.removesuffix(".T")
        if code and code not in codes:
            codes.append(code)
    return codes


def latest_highs_df(output_dir: Path = OUTPUT_DIR) -> pd.DataFrame | None:
    files = sorted(output_dir.glob("screening_highs_*.csv"))
    if not files:
        return None
    try:
        return pd.read_csv(files[-1], dtype={"code": str})
    except Exception:
        return None


def _fmt(value: object, suffix: str = "") -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}{suffix}"
    return f"{value}{suffix}"


def diagnose_code(code: str, highs: pd.DataFrame | None) -> dict[str, object]:
    """1銘柄をロジックに通して判定と理由を返す。捏造しない: 取得不可は取得不可と言う。"""
    result: dict[str, object] = {"code": code, "in_our_list": "-", "verdict": "", "reason": ""}

    if highs is not None and "code" in highs.columns:
        row = highs[highs["code"].astype(str).str.upper() == code]
        if not row.empty:
            r = row.iloc[0]
            flags = str(r.get("note_flags", "") or "")
            result["in_our_list"] = f"掲載あり（{r.get('high_type', '?')}{'/' + flags if flags else ''}）"
        else:
            result["in_our_list"] = "掲載なし"

    if not code.isdigit() or len(code) != 4:
        result["verdict"] = "対象外（ユニバース）"
        result["reason"] = "英字入り/4桁以外のコードは現行ユニバース対象外（既知の制限）"
        return result

    ticker = f"{code}.T"
    try:
        history = fetch_price_history(ticker)
    except Exception as exc:  # noqa: BLE001
        result["verdict"] = "診断不能"
        result["reason"] = f"価格取得エラー: {exc}"
        return result

    if history is None or history.empty or "Close" not in history.columns:
        result["verdict"] = "診断不能"
        result["reason"] = "yfinance から価格データを取得できず（上場直後・コード違い・データ欠落の可能性）"
        return result

    n = len(history)
    if n < 60:
        result["verdict"] = "対象外（日数不足）"
        result["reason"] = f"データ{n}日分（60日未満は判定しない仕様）"
        return result

    profile = window_high_profile(history, 252)
    quality = high_quality_flags(history)
    close = history["Close"].astype(float)
    high = history["High"].astype(float) if "High" in history.columns else close
    current = float(close.iloc[-1])
    prior_high = float(high.iloc[:-1].tail(251).max()) if n > 1 else current
    dist = (prior_high - current) / prior_high * 100 if prior_high > 0 else 999.0

    flag_bits = []
    if quality.get("first_break_60d"):
        flag_bits.append("初回ブレイク")
    breaks_20d = quality.get("breaks_20d")
    if isinstance(breaks_20d, (int, float)) and breaks_20d >= 5:
        flag_bits.append(f"連日更新{int(breaks_20d)}回/20日")
    if quality.get("inago_suspect"):
        flag_bits.append("イナゴ疑い")
    if quality.get("tob_suspect"):
        flag_bits.append("TOB疑い")
    flags_text = " / ".join(flag_bits) if flag_bits else "-"

    if profile is None:
        result["verdict"] = "対象外（乖離>3%）"
        result["reason"] = (
            f"直近終値{current:.1f} / 窓内高値{prior_high:.1f} / 乖離{dist:.2f}%。"
            "3%超のため接近にも該当せず。カブタン側と価格がズレる場合は配当調整の影響の可能性"
        )
        result["flags"] = flags_text
        return result

    label = HIGH_LABELS.get(profile.high_type, profile.high_type)
    result["verdict"] = f"該当（{label}）"
    result["reason"] = (
        f"高値{_fmt(profile.high_price)}（{profile.high_date}） / "
        f"乖離{_fmt(profile.dist_to_high_pct, '%')} / データ{n}日"
    )
    result["flags"] = flags_text
    return result


def build_report(codes: list[str], highs: pd.DataFrame | None) -> list[str]:
    lines = [
        "# カブタン照合レポート",
        "",
        f"- 診断対象: {len(codes)}銘柄",
        "",
        "| コード | 当システム判定 | 本日リスト掲載 | フラグ | 詳細 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for code in codes:
        d = diagnose_code(code, highs)
        lines.append(
            f"| {d['code']} | {d['verdict']} | {d['in_our_list']} | {d.get('flags', '-')} | {d['reason']} |"
        )
    lines.extend(
        [
            "",
            "## 読み方",
            "",
            "- **該当なのに本日リスト掲載なし** → ユニバース・実行タイミング・データ取得失敗のいずれか。要調査。",
            "- **対象外（乖離>3%）でカブタンには載っている** → 配当調整済み価格と無調整価格の差が境界にかかった可能性。",
            "- **対象外（ユニバース）** → 英字入りコードは現行仕様の既知の制限。",
            "- 当方は「更新」に加えて「接近(3%以内)」も拾うため、接近銘柄が当方だけに出るのは正常。",
            "",
        ]
    )
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="カブタン照合（ズレ銘柄の診断）")
    parser.add_argument("--codes", default="", help="銘柄コード（カンマ/空白区切り）")
    parser.add_argument("--codes-file", default="", help="コード一覧ファイル")
    parser.add_argument("--output", default=str(REPORT_PATH), help="レポート出力先")
    args = parser.parse_args()

    raw = args.codes
    if args.codes_file:
        raw += "\n" + Path(args.codes_file).read_text(encoding="utf-8")
    codes = parse_codes(raw)
    if not codes:
        print("コードが指定されていません（--codes '7203, 6758' の形式で指定）", file=sys.stderr)
        sys.exit(2)

    highs = latest_highs_df()
    lines = build_report(codes, highs)
    text = "\n".join(lines)
    print(text)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"kabutan_check: レポート -> {out}")


if __name__ == "__main__":
    main()
