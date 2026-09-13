"""指数と重ねて見る「比較チャート」のURL（Yahoo!ファイナンス）。

2026-09-13: 高重さんの指示「日経平均のチャートと個別銘柄のチャートを同じ画面で重ねて見たい」。

compare= に何を入れるかは推測せず、実際にブラウザで開いて確かめた（2026-09-13）。
  ・日本株 https://finance.yahoo.co.jp/quote/7011.T/chart?frm=dly&trm=6m&compare=998407.O
      → ページの「日経平均」が選択済みになり、個別銘柄と日経平均が重なることを確認。
      日経平均=998407.O。依頼にない指数は足さない。
  ・米株  finance.yahoo.com は comparisons= を無視した（AAPLの1本のままだった）。
      Yahoo!ファイナンス（日本）の米国株ページなら compare=%5EGSPC で S&P500 と重なる
      （AAPL と JKHY で確認）。そのため比較URLだけ日本のドメインを使う。

比較チャートに切り替わると Yahoo 側が自動で「線＋パフォーマンス（％）」表示にし、
ローソク足や移動平均の指定（scl= styl= evnts= ovrIndctr=）は落とす。
だから比較用のURLには最初から付けない。

※ここから分かるのは「指数に対して相対的に高い位置か安い位置か」だけで、
  PER・PBR のような意味での割安・割高ではない。文言にも必ず「日経平均に対して」を付ける。
"""

from __future__ import annotations

# 日経平均。依頼にない指数は足さない。
JP_COMPARE_CODES = "998407.O"
JP_COMPARE_LABEL = "日経平均と比較"

# S&P500（^GSPC）。URLでは「^」が %5E になる。
US_COMPARE_CODES = "%5EGSPC"
US_COMPARE_LABEL = "S&P500と比較"


def _clean_code(code: object) -> str:
    """CSV 由来の "7011.0" のような表記を "7011" に直す。"""
    text = str(code).strip()
    return text[:-2] if text.endswith(".0") else text


def compare_chart_url(code: object) -> str:
    """日本株を日経平均と重ねた6ヶ月チャート。"""
    return (
        f"https://finance.yahoo.co.jp/quote/{_clean_code(code)}.T/chart"
        f"?frm=dly&trm=6m&compare={JP_COMPARE_CODES}"
    )


def compare_chart_url_us(ticker: object) -> str:
    """米株をS&P500と重ねた6ヶ月チャート（Yahoo!ファイナンスの米国株ページ）。"""
    return (
        f"https://finance.yahoo.co.jp/quote/{str(ticker).strip().upper()}/chart"
        f"?frm=dly&trm=6m&compare={US_COMPARE_CODES}"
    )
