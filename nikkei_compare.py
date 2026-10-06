"""個別銘柄と日経平均の値動きを比べた数字を作る。

なぜリンクではなく数字なのか（2026-10-06）
  Yahoo!ファイナンスの比較チャートURL（compare=998407.O）は、スマートフォンで
  開くとパラメータごと捨てられる。実際に iPhone 相当の画面で開いて確かめた。
    要求 https://finance.yahoo.co.jp/quote/8392.T/chart?frm=dly&trm=6m&compare=998407.O
    着地 https://finance.yahoo.co.jp/quote/8392.T/chart?trm=6m&styl=cndl&ovrIndctr=sma%2Cmma
  日経平均の線は出ず、移動平均だけが表示される。リンクでは実現できないので、
  こちらで計算してメール本文に数字で出す。

  ここから分かるのは「日経平均に対して強いか弱いか」だけで、
  PER・PBR のような意味での割安・割高ではない。文言にも必ずそう書く。
"""

from __future__ import annotations

import pandas as pd

from scanner.prices import fetch_price_history

NIKKEI_TICKER = "^N225"

# 比べる期間（営業日）。1か月・3か月・6か月のつもり。
WINDOWS = ((20, "1か月"), (60, "3か月"), (120, "6か月"))

_NIKKEI_CACHE: pd.Series | None = None
_NIKKEI_TRIED = False


def nikkei_closes(period: str = "1y") -> pd.Series | None:
    """日経平均の終値。1回のスキャンで何度も取りに行かないよう覚えておく。"""
    global _NIKKEI_CACHE, _NIKKEI_TRIED
    if _NIKKEI_CACHE is not None or _NIKKEI_TRIED:
        return _NIKKEI_CACHE
    _NIKKEI_TRIED = True
    try:
        hist = fetch_price_history(NIKKEI_TICKER, period=period)
    except Exception as error:                        # noqa: BLE001
        print(f"nikkei_compare=fetch_failed err={error}", flush=True)
        return None
    if hist is None or getattr(hist, "empty", True) or "Close" not in hist:
        print("nikkei_compare=no_data", flush=True)
        return None
    _NIKKEI_CACHE = pd.to_numeric(hist["Close"], errors="coerce").dropna()
    return _NIKKEI_CACHE


def _ret(series: pd.Series, bars: int) -> float | None:
    """bars 本前から今までの騰落率(%)。足りなければ None。"""
    if series is None or len(series) <= bars:
        return None
    now = float(series.iloc[-1])
    then = float(series.iloc[-1 - bars])
    if not then:
        return None
    return (now / then - 1) * 100


def relative(history) -> list[dict[str, float | str]]:
    """銘柄と日経平均の騰落率、およびその差を期間ごとに返す。

    戻り値の各要素: {"label": "3か月", "stock": 12.3, "index": 4.5, "diff": 7.8}
    どちらかが足りない期間は返さない（埋めない）。
    """
    if history is None or getattr(history, "empty", True) or "Close" not in history:
        return []
    stock = pd.to_numeric(history["Close"], errors="coerce").dropna()
    index = nikkei_closes()
    if index is None:
        return []
    out = []
    for bars, label in WINDOWS:
        s = _ret(stock, bars)
        i = _ret(index, bars)
        if s is None or i is None:
            continue
        out.append({"label": label, "stock": s, "index": i, "diff": s - i})
    return out


def text_lines(rel: list[dict]) -> list[str]:
    """プレーンテキスト用。"""
    if not rel:
        return ["  📊 日経平均との比較:数字を取れませんでした"]
    head = "  📊 日経平均との比較（銘柄 / 日経平均 / 差）"
    body = [f"     {r['label']}: {r['stock']:+.1f}% / {r['index']:+.1f}%"
            f" / {r['diff']:+.1f}ポイント" for r in rel]
    return [head] + body


def html_block(rel: list[dict]) -> str:
    """HTML用。メールアプリが落とさないよう、表ではなく行で組む。"""
    if not rel:
        return ('<div style="font-size:13px;color:#777777;line-height:1.7;">'
                '📊 日経平均との比較:数字を取れませんでした</div>')
    rows = "".join(
        f'<div style="font-size:13px;line-height:1.8;color:#333333;">'
        f'{r["label"]}　銘柄 <b>{r["stock"]:+.1f}%</b>'
        f'／日経平均 {r["index"]:+.1f}%'
        f'／差 <b>{r["diff"]:+.1f}ポイント</b></div>'
        for r in rel)
    return ('<div style="background:#f4f6fb;border:1px solid #d8def0;'
            'border-radius:8px;padding:10px 12px;margin:8px 0;">'
            '<div style="font-size:13px;font-weight:bold;color:#1a1a1a;'
            'padding:0 0 4px 0;">📊 日経平均との比較</div>' + rows +
            '<div style="font-size:11px;color:#888888;line-height:1.6;'
            'padding:4px 0 0 0;">日経平均に対して強いか弱いかを示す数字です。'
            '割安・割高を意味するものではありません。</div></div>')
