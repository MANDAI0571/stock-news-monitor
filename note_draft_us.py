"""米株のnote記事を作る。52週新高値と、押し目（25MA・200MA・240MAタッチ）。

日本株の note_draft.py とは別ファイルにしてある。あちらは2,500行あり、円建ての表記と
日本語の社名前提が全体に染みているので、混ぜると日本株側を壊す危険が高い。
記事の骨組み（何をどの順で書くか）は同じにして、米株の書き方に直したものを新しく書く。

2026-09-06 までに日本株側で分かったことは、最初から入れてある。
  ・note.com はマークダウンを一切解釈しない。表（|）も見出し（##）も文字として出る。
    → 表は使わない。最初から「1行目=銘柄名、2行目=項目 値」の並びで書く。
  ・リンクは裸のURLで書く（[文言](URL) は note では押せない文字列になるため）。
    裸のURLが note で自動リンクになるかは未確認。ならなければ貼った後に手でリンク設定する。
  ・読者に何も伝えない行は書かない（値が無い項目はその行ごと落とす）。
  ・同じ銘柄を1つの記事に何度も出さない。まとめて1回にする。

OpenWork は日本のサービスなので米株の記事には入れない（無理に別のサービスを当てはめない）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"

US_TITLES = {
    "highs": "米国株 52週新高値｜到達・接近銘柄",
    "pullback": "米国株 押し目候補｜新高値ライン戻り・25MA・200MA・240MAタッチ",
}

DISCLAIMER_LINES = [
    "## 注意書き",
    "",
    "- 本記事は情報提供を目的としたもので、特定銘柄の売買を推奨するものではありません。",
    "- 数値は取得済みデータに基づく機械集計です。取得できなかった項目は載せていません。",
    "- 米国株は為替の影響を受けます。円換算の損益は株価の動きと一致しません。",
    "- 本記事は投資助言ではありません。売買判断はご自身の責任でお願いします。",
]

# 1記事あたりの掲載数。多すぎると読めないので絞る（日本株で学んだ）。
LIST_CAP = 20
DETAIL_CAP = 3
PULLBACK_CARD_CAP = 6

MA_BUCKETS = (
    ("ma25_touch", "25MAタッチ", "25日線"),
    ("ma200_touch", "200MAタッチ", "200日線"),
    ("ma240_touch", "240MAタッチ", "240日線"),
)


def chart_url(ticker: str) -> str:
    """米株のチャート。裸のURLで書く（[文言](URL) は note では押せない文字列になるため）。"""
    return f"https://finance.yahoo.com/quote/{str(ticker).strip().upper()}/chart"


def fmt_usd(value: object, digits: int = 2) -> str:
    number = _num(value)
    return "" if number is None else f"${number:,.{digits}f}"


def fmt_turnover(value: object) -> str:
    """売買代金を読みやすい単位にする。10億ドル以上はB、100万ドル以上はM。"""
    number = _num(value)
    if number is None or number <= 0:
        return ""
    if number >= 1_000_000_000:
        return f"${number / 1_000_000_000:,.1f}B"
    if number >= 1_000_000:
        return f"${number / 1_000_000:,.0f}M"
    return f"${number:,.0f}"


def fmt_pct(value: object, signed: bool = False) -> str:
    number = _num(value)
    if number is None:
        return ""
    return f"{number:+.2f}%" if signed else f"{number:.2f}%"


def _num(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _text(row: pd.Series, key: str) -> str:
    value = row.get(key)
    text = "" if value is None else str(value).strip()
    return "" if text.lower() in ("nan", "none", "null") else text


def _stock_lines(row: pd.Series) -> list[str]:
    """1銘柄を2行で書く。表は使わない（noteが表を解釈しないため）。"""
    ticker = _text(row, "ticker") or _text(row, "code")
    name = _text(row, "name")
    head = f"{ticker} {name}".strip()
    change = fmt_pct(row.get("change_pct"), signed=True)
    price = fmt_usd(row.get("current_price"))
    if price:
        head += f"  {price}"
    if change:
        head += f"（{change}）"

    parts: list[str] = []
    dist = _num(row.get("dist_to_high_pct"))
    if dist is not None:
        parts.append("高値更新" if dist <= 0 else f"高値まで{dist:.2f}%")
    turnover = fmt_turnover(row.get("turnover_20d"))
    if turnover:
        parts.append(f"売買代金 {turnover}")
    ratio = _num(row.get("volume_ratio_5d_20d"))
    if ratio is not None:
        parts.append(f"出来高比 {ratio:.2f}倍")
    sector = _text(row, "sector")
    if sector and sector != "-":
        parts.append(sector)
    lines = [head]
    if parts:
        lines.append("　" + " ／ ".join(parts))
    return lines


def _detail_lines(row: pd.Series, rank: int) -> list[str]:
    """上位銘柄の詳細。取れなかった項目は書かない。"""
    ticker = _text(row, "ticker") or _text(row, "code")
    name = _text(row, "name")
    lines = [f"### {rank}. {name}（{ticker}）", ""]

    def add(label: str, value: str) -> None:
        if value:
            lines.append(f"{label}{value}")

    add("株価：", fmt_usd(row.get("current_price")))
    add("前日比：", fmt_pct(row.get("change_pct"), signed=True))
    dist = _num(row.get("dist_to_high_pct"))
    if dist is not None:
        add("52週高値まで：", "更新済み" if dist <= 0 else f"{dist:.2f}%")
    add("売買代金（20日平均）：", fmt_turnover(row.get("turnover_20d")))
    ratio = _num(row.get("volume_ratio_5d_20d"))
    if ratio is not None:
        add("出来高倍率（当日/20日平均）：", f"{ratio:.2f}倍")
    add("セクター：", _text(row, "sector"))
    add("市場：", _text(row, "market"))
    add("次回決算：", _text(row, "earnings_date"))
    flags = _text(row, "note_flags")
    if flags:
        add("フラグ：", flags)
    lines.append("")
    lines.append(f"📈 チャート: {chart_url(ticker)}")
    lines.append("")
    return lines


def _sector_line(df: pd.DataFrame) -> str:
    """候補が多いセクターを3つ。数えられなければ空（捏造しない）。"""
    if df.empty or "sector" not in df.columns:
        return ""
    counts = (
        df["sector"].astype(str).str.strip()
        .replace({"": None, "nan": None, "-": None}).dropna().value_counts()
    )
    if counts.empty:
        return ""
    top = counts.head(3)
    body = "、".join(f"{name}（{int(n)}銘柄）" for name, n in top.items())
    return f"セクター別に数えると、{body}に集中しました（当スクリーニング内の集計）。"


def build_us_highs_note(highs: pd.DataFrame, target_date: str) -> str:
    """52週新高値の記事。到達と接近を分けて書く。"""
    lines = [f"# {US_TITLES['highs']} {target_date}", ""]
    if highs is None or highs.empty:
        lines += ["> データ不足：本日の米株スクリーニング出力が空のため、候補を表示できません。", ""]
        lines += DISCLAIMER_LINES
        return "\n".join(lines) + "\n"

    kind = highs.get("high_type", pd.Series(dtype=str)).astype(str)
    reached = highs[kind == "52W_NEW_HIGH"]
    near = highs[kind == "52W_NEAR_HIGH"]

    lines.append(
        f"{target_date} の米国株（S&P500 と NASDAQ の大型株）で、52週新高値に到達した銘柄は"
        f"{len(reached)}銘柄、新高値まで3%以内に接近した銘柄は{len(near)}銘柄でした。"
    )
    sector_line = _sector_line(highs)
    if sector_line:
        lines.append(sector_line)
    lines += [
        "",
        "この記事は毎営業日、同じ基準で機械的に抽出しています。基準がぶれないことがこの記事の価値です。",
        "",
    ]

    for label, part in (("【A】52週新高値に到達した銘柄", reached),
                        ("【B】52週新高値まで3%以内に接近している銘柄", near)):
        lines += [f"## {label}", ""]
        if part.empty:
            lines += ["- 該当なし", ""]
            continue
        ordered = _sort_by_turnover(part)
        for _, row in ordered.head(LIST_CAP).iterrows():
            lines += _stock_lines(row)
        if len(ordered) > LIST_CAP:
            lines += ["", f"※ この分類は全{len(ordered)}銘柄です。売買代金の大きい{LIST_CAP}銘柄を載せています。"]
        lines.append("")
        lines += ["### 銘柄詳細（売買代金の大きい順）", ""]
        for rank, (_, row) in enumerate(ordered.head(DETAIL_CAP).iterrows(), start=1):
            lines += _detail_lines(row, rank)

    lines += ["## 対象営業日の集計", "",
              f"- 候補数（合計）：{len(highs)}銘柄",
              f"- 52週新高値 到達：{len(reached)}銘柄",
              f"- 3%以内 接近：{len(near)}銘柄", ""]
    lines += DISCLAIMER_LINES
    return "\n".join(lines) + "\n"


def build_us_pullback_note(pullback: pd.DataFrame, target_date: str) -> str:
    """押し目の記事。同じ銘柄を何度も出さない（日本株で直したのと同じ扱い）。"""
    lines = [f"# {US_TITLES['pullback']} {target_date}", ""]
    if pullback is None or pullback.empty:
        lines += ["> データ不足：本日の米株スクリーニング出力が空のため、候補を表示できません。", ""]
        lines += DISCLAIMER_LINES
        return "\n".join(lines) + "\n"

    retest = _flag_rows(pullback, "retest_52w")
    lines.append(
        "強い銘柄を高値で追いかけるのではなく、強い銘柄が休んだところを狙うのがこの記事のテーマです。"
        "移動平均線が上向きのままのタッチだけを拾うので、下落トレンドの「落ちるナイフ」は含みません。"
    )
    sector_line = _sector_line(pullback)
    if sector_line:
        lines.append(sector_line)
    lines += ["", "## この記事の読み方", "",
              "- 52週新高値後リテスト：一度新高値を取った銘柄が、その高値ラインまで戻ってきたところです。",
              "- 25MA／200MA／240MAタッチ：株価が25日／200日／240日移動平均線に触れたところです。",
              "- どれも「買い推奨」ではありません。押し目で反発するかは、翌日以降の値動きと出来高で確かめてください。",
              ""]

    lines += ["## 【52週新高値後リテスト】", ""]
    if retest.empty:
        lines += ["- 該当なし", ""]
    else:
        for _, row in _sort_by_turnover(retest).head(PULLBACK_CARD_CAP).iterrows():
            lines += _stock_lines(row) + [f"　📈 チャート: {chart_url(_text(row, 'ticker'))}", ""]

    # 同じ銘柄が複数の線に触れていても、最初の分類で1回だけ載せる。
    touched: dict[str, list[str]] = {}
    for flag, _, label in MA_BUCKETS:
        for ticker in _flag_rows(pullback, flag).get("ticker", pd.Series(dtype=str)).astype(str):
            touched.setdefault(ticker.strip(), []).append(label)
    shown: set[str] = set()

    for flag, title, label in MA_BUCKETS:
        lines += [f"## 【{title}】", ""]
        bucket = _flag_rows(pullback, flag)
        if not bucket.empty:
            bucket = bucket[~bucket["ticker"].astype(str).str.strip().isin(shown)]
        if bucket.empty:
            lines += ["- 該当なし（ほかの分類で掲載ずみ）" if shown else "- 該当なし", ""]
            continue
        ordered = _sort_by_turnover(bucket).head(PULLBACK_CARD_CAP)
        shown.update(ordered["ticker"].astype(str).str.strip())
        for _, row in ordered.iterrows():
            ticker = _text(row, "ticker")
            also = [x for x in touched.get(ticker, []) if x != label]
            stock = _stock_lines(row)
            if also:
                stock[0] += f"　🔁 {'・'.join(also)}にも同時タッチ"
            lines += stock + [f"　📈 チャート: {chart_url(ticker)}", ""]

    lines += DISCLAIMER_LINES
    return "\n".join(lines) + "\n"


def _flag_rows(df: pd.DataFrame, column: str) -> pd.DataFrame:
    if df is None or df.empty or column not in df.columns:
        return df.iloc[0:0] if df is not None and not df.empty else pd.DataFrame()
    mask = df[column].astype(str).str.lower().isin(["true", "1", "1.0"])
    return df[mask]


def _sort_by_turnover(df: pd.DataFrame) -> pd.DataFrame:
    if "turnover_20d" not in df.columns:
        return df
    ordered = df.copy()
    ordered["_t"] = pd.to_numeric(ordered["turnover_20d"], errors="coerce").fillna(0)
    return ordered.sort_values("_t", ascending=False).drop(columns=["_t"]).reset_index(drop=True)


def latest_csv(output_dir: Path, prefix: str) -> Path | None:
    files = sorted(Path(output_dir).glob(f"{prefix}_*.csv"))
    return files[-1] if files else None


def _read(path: Path | None) -> pd.DataFrame:
    if path is None or not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype=str).fillna("")
    except Exception:
        return pd.DataFrame()


def build_us_notes(output_dir: Path | str = OUTPUT_DIR, target_date: str | None = None) -> dict[str, Path]:
    """スクリーニングCSVから記事2本を書き出す。"""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    if target_date is None:
        from run_screening_us import us_target_date

        target_date = us_target_date()

    highs = _read(latest_csv(out, "screening_us_highs"))
    pullback = _read(latest_csv(out, "screening_us_pullback"))

    written: dict[str, Path] = {}
    for key, text in (
        ("highs", build_us_highs_note(highs, target_date)),
        ("pullback", build_us_pullback_note(pullback, target_date)),
    ):
        path = out / f"note_us_{key}.md"
        path.write_text(text, encoding="utf-8")
        (out / f"note_us_{key}_title.txt").write_text(
            f"{US_TITLES[key]} {target_date}", encoding="utf-8"
        )
        written[key] = path
        print(f"saved={path} chars={len(text):,}", flush=True)
    return written


def main() -> None:
    build_us_notes()


if __name__ == "__main__":
    main()
