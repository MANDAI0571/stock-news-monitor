"""個別銘柄が指数に対してどの辺にいるかを、比率だけから数字にする。

なぜ作るか:
  (1)で入れた比較チャートは「日経平均に対して上か下か」を目で見せるだけで、
  どれくらいかは読む人の目分量になる。ここでは同じことを数字にする。

作法（chart_images.compute_series() と同じ）:
  ・通信しない。純関数。標準ライブラリだけ。合成データで自己テストできる。
  ・数字はすべて「個別終値 ÷ 指数終値」の比率から出す。株価そのものからは出さない。
  ・足りない数字は埋めない。出せないときは None を返して、呼ぶ側が行ごと落とす。

言えること・言えないこと（高重さんとの取り決め 2026-09-13）:
  ・言えるのは「日経平均に対する位置」だけ。必ず「日経平均に対して」を付ける。
  ・割安・割高（本来の価値に対して安いか高いか）は、この数字からは一切言えない。
  ・断定しない。PER/PBR のような別の物差しは混ぜない。
"""
from __future__ import annotations

# 比率を見るのに必要な最低本数。1年レンジ(252)とMA200の両方を満たす本数。
MIN_BARS = 252
RANGE_DAYS = 252   # 「1年レンジ」＝直近252営業日
MA_SHORT = 25      # 比率の25日移動平均
MA_LONG = 200      # 比率の200日移動平均
TREND_DAYS = 20    # 方向を見る営業日数
FLAT_PCT = 1.0     # ±1%未満は「横ばい」と呼ぶ

DEFAULT_INDEX_LABEL = "日経平均"

# 出力に混ぜてはいけない言葉。断定と、別の物差し（PER/PBR）。
# 自己テストがこの語を出力に含まないことを見張る。
FORBIDDEN_WORDS: tuple[str, ...] = (
    "割安", "割高", "買い時", "売り時", "おすすめ", "推奨", "狙い目", "PER", "PBR",
)


def _finite_positive(values) -> list[float] | None:
    """数として読めて、有限で、正の値だけの列に直す。ひとつでも駄目なら None。

    比率を取るので 0 や負の値は使えない。欠測を 0 で埋めたりはしない。
    """
    out: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number != number:            # NaN
            return None
        if number in (float("inf"), float("-inf")):
            return None
        if number <= 0:
            return None
        out.append(number)
    return out


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def compute_relative(closes, index_closes) -> dict | None:
    """終値の列と指数の終値の列（同じ日付で並んでいること）から、位置の数字を出す。

    返り値のキー:
      ratio          最新の比率（個別終値 ÷ 指数終値）
      range_pos_pct  直近252営業日の比率レンジの中での位置（0〜100）
      range_low/high そのレンジの下端・上端
      ma25_gap_pct   比率の25日移動平均からの乖離（%）
      ma200_gap_pct  比率の200日移動平均からの乖離（%）
      trend_20d_pct  直近20営業日の比率の変化（%）＝指数に対する方向
      index_20d_pct  直近20営業日の指数自体の変化（%）
      bars           使った本数

    出せないときは None を返す（推測で埋めない）。None になるのは:
      ・どちらかの列に数でない値・NaN・0以下がある
      ・2つの列の長さが違う（日付が揃っていない疑いがある）
      ・本数が MIN_BARS に足りない
      ・1年レンジの上端と下端が同じ（位置を定義できない）
    """
    prices = _finite_positive(closes)
    index = _finite_positive(index_closes)
    if prices is None or index is None:
        return None
    if len(prices) != len(index):
        return None
    if len(prices) < MIN_BARS:
        return None

    ratio = [price / level for price, level in zip(prices, index)]
    last = ratio[-1]

    window = ratio[-RANGE_DAYS:]
    low = min(window)
    high = max(window)
    if high <= low:
        return None

    ma_short = _mean(ratio[-MA_SHORT:])
    ma_long = _mean(ratio[-MA_LONG:])
    if ma_short <= 0 or ma_long <= 0:
        return None

    ratio_prev = ratio[-(TREND_DAYS + 1)]
    index_prev = index[-(TREND_DAYS + 1)]

    return {
        "ratio": last,
        "range_pos_pct": (last - low) / (high - low) * 100.0,
        "range_low": low,
        "range_high": high,
        "ma25_gap_pct": (last - ma_short) / ma_short * 100.0,
        "ma200_gap_pct": (last - ma_long) / ma_long * 100.0,
        "trend_20d_pct": (last / ratio_prev - 1.0) * 100.0,
        "index_20d_pct": (index[-1] / index_prev - 1.0) * 100.0,
        "bars": len(prices),
    }


def _signed_pct(value: float, digits: int = 1) -> str:
    return f"{value:+.{digits}f}%"


def _direction(pct: float) -> str:
    """方向の言い方。±FLAT_PCT 未満は横ばいと呼ぶ。"""
    if pct > FLAT_PCT:
        return "上向き"
    if pct < -FLAT_PCT:
        return "下向き"
    return "横ばい"


def format_relative_line(rel: dict, index_label: str = DEFAULT_INDEX_LABEL) -> str:
    """位置の数字を、断定しない一行にする。

    「〜に対して」を必ず頭に付け、最後に「位置であって価値の評価ではない」と断る。
    """
    trend = rel["trend_20d_pct"]
    index_trend = rel["index_20d_pct"]
    return (
        f"{index_label}に対して：1年レンジの下から{rel['range_pos_pct']:.0f}%"
        f"／比率は25日線 {_signed_pct(rel['ma25_gap_pct'])}"
        f"・200日線 {_signed_pct(rel['ma200_gap_pct'])}"
        f"／直近{TREND_DAYS}営業日は{_direction(trend)}（{_signed_pct(trend)}）。"
        f"{index_label}自体は{_direction(index_trend)}（{_signed_pct(index_trend)}）。"
        f"位置を測った数字で、企業価値の評価ではありません。"
    )


def relative_line(
    closes,
    index_closes,
    index_label: str = DEFAULT_INDEX_LABEL,
) -> str | None:
    """終値の列2本から一行を作る。出せないときは None（呼ぶ側は行ごと落とす）。"""
    rel = compute_relative(closes, index_closes)
    if rel is None:
        return None
    return format_relative_line(rel, index_label)


def align_closes(stock_pairs, index_pairs):
    """(2026-09-13 fix65) 個別と指数の (日付, 終値) を、同じ日付の日だけに揃える。

    比率は「同じ日の個別終値 ÷ 同じ日の指数終値」でなければ意味が無い。
    個別銘柄と指数では休みの日がずれることがあるので、日付の文字列が
    完全に一致する日だけを残す。前後の日で埋めることはしない。

    返り値は (個別の終値リスト, 指数の終値リスト) を日付の昇順で。
    揃った日が MIN_BARS に足りないときは (None, None)（呼ぶ側は行ごと落とす）。
    """
    index_map: dict[str, object] = {}
    for day, value in index_pairs:
        index_map[str(day)] = value

    merged: list[tuple[str, object, object]] = []
    seen: set[str] = set()
    for day, value in stock_pairs:
        key = str(day)
        if key in seen or key not in index_map:
            continue
        seen.add(key)
        merged.append((key, value, index_map[key]))

    if len(merged) < MIN_BARS:
        return None, None
    merged.sort(key=lambda item: item[0])
    return [item[1] for item in merged], [item[2] for item in merged]
