"""1銘柄ずつの投稿用の文とチャート（2026-10-30 持ち主「出来高急増急落銘柄もコピペできるようにして」）。

リアルタイムの高値メール（intraday_high_alert.py）と同じ仕組みにそろえる:
「📋 この銘柄の文とチャート画像」はコピー用のページ（docs/clip.html。GitHub Pages）を開き、
そこで文のコピー・チャート画像の保存や共有をする。文とチャートはリンクの「#」より後ろで運ぶので、
サーバーにもリポジトリにも銘柄は残らない。「𝕏に投稿（文だけ）」は X の投稿画面を文が入った状態で開く。

チャートの文字は clip.html の readChart・intraday_high_alert.read_chart_payload と同じ形
（"1,安値,高値,ライン,初日,最終日,本体"。本体は1本9文字＝四本値を4096段で2文字ずつ＋出来高を64段で1文字）。
Mac の控え（stock-volume-screener/screener）でも動くように、pandas は使わない。
"""

import base64
import re
from urllib.parse import quote

NOTE_URL = "https://note.com/breakout_scan"
CLIP_PAGE_URL = "https://mandai0571.github.io/stock-news-monitor/clip.html"
X_LIMIT = 280
CHART_BARS = 60
_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def x_length(text):
    """X（旧Twitter）の数え方での長さ。漢字・かなは2、英数字・記号は1、URL は23。"""
    n = 0
    for part in re.split(r"(https?://\S+)", text):
        if part.startswith("http"):
            n += 23
            continue
        for ch in part:
            o = ord(ch)
            n += 1 if (o <= 0x10FF or 0x2000 <= o <= 0x200D or 0x2010 <= o <= 0x201F or 0x2032 <= o <= 0x2037) else 2
    return n


def clip_url(text, chart=""):
    """コピー用のページのリンク。文は UTF-8 の base64url（b=）、チャートは chart_payload の文字（c=）。"""
    b = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")
    return CLIP_PAGE_URL + "#b=" + b + (f"&c={chart}" if chart else "")


def x_post_url(text):
    return "https://x.com/intent/post?text=" + quote(text, safe="")


def _num(x):
    return f"{x:.2f}".rstrip("0").rstrip(".")


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v > 0 else None


def chart_payload(r, bars=CHART_BARS):
    """1銘柄の直近 bars 本の日足を clip.html 用の短い文字にする（作れなければ空）。

    行（screener の hit）の dates・closes・vols・ohlc を使う。ohlc は日付より長いことがある
    （過去日を指定した実行。先頭からそろっている）ので、日付の本数で切る。
    最後の足は取得した時点の途中の足なので、終値をメールの現在値（r["price"]）に置き換え、
    高値・安値をその値まで広げる。ラインは25日線（現在値で計算したもの）。"""
    try:
        dates = list(r.get("dates") or [])
        ohlc = r.get("ohlc") or {}
        n = len(dates)
        closes = list(r.get("closes") or [])[:n]
        vols = list(r.get("vols") or [])[:n]
        opens = list(ohlc.get("open") or [])[:n]
        highs = list(ohlc.get("high") or [])[:n]
        lows = list(ohlc.get("low") or [])[:n]
    except (TypeError, AttributeError):
        return ""
    if n < 5 or not (len(closes) == len(opens) == len(highs) == len(lows) == n):
        return ""
    vols = vols + [0] * (n - len(vols))
    rows = []
    for d, o, h, lo, c, v in zip(dates, opens, highs, lows, closes, vols):
        o, h, lo, c = _f(o), _f(h), _f(lo), _f(c)
        if None in (o, h, lo, c):
            continue
        rows.append([d, o, max(h, o, c), min(lo, o, c), c, _f(v) or 0.0])
    price = _f(r.get("price"))
    if rows and price:
        last = rows[-1]
        last[4] = price
        last[2], last[3] = max(last[2], price), min(last[3], price)
    rows = rows[-bars:]
    if len(rows) < 5:
        return ""
    line = _f(r.get("ma25")) or 0.0
    lo_all = min(x[3] for x in rows)
    hi_all = max(x[2] for x in rows)
    if line > 0:
        lo_all, hi_all = min(lo_all, line), max(hi_all, line)
    if not hi_all > lo_all:
        return ""

    def q(p):
        v = max(0, min(4095, int(round((p - lo_all) / (hi_all - lo_all) * 4095))))
        return _B64[v >> 6] + _B64[v & 63]

    vmax = max(x[5] for x in rows)
    body = []
    for _, o, h, lo, c, v in rows:
        vq = max(0, min(63, int(round(v / vmax * 63)))) if vmax > 0 else 0
        body.append(q(o) + q(h) + q(lo) + q(c) + _B64[vq])

    def ymd(d):
        return str(d)[:10].replace("-", "")

    return ",".join(["1", _num(lo_all), _num(hi_all), _num(line), ymd(rows[0][0]), ymd(rows[-1][0]), "".join(body)])


def _when(label):
    """"2026-10-30 13:50" → "10/30 13:50"。形が違えばそのまま。"""
    m = re.match(r"\d{4}-(\d{2})-(\d{2})(.*)$", str(label or ""))
    return f"{int(m.group(1))}/{int(m.group(2))}{m.group(3)}" if m else str(label or "")


def post_text(r, chart_url="", limit=X_LIMIT):
    """1銘柄ぶんの投稿用の文。X の280字に収める。長いときは ランク → チャートのURL → タグ → note のURL の順に外す。
    売買をすすめる言葉は入れない。1行目・2〜3行目は clip.html の画像の見出しにもなる。"""
    head = f"【出来高急増×下降】{r['code']} {r['name']}"
    chg = "前回の足から" if r.get("prev_gap") else "前日比"
    when = _when(r.get("asof_label"))
    price = f"現在値 {r['price']:,.1f}円（{chg} {r['chg_pct']:+.2f}%）" + (f"{when}時点" if when else "")
    # 「25日線 X円」は clip.html が画像の黄色い点線（その日の25日線の値）の見出しに使う
    line = f"25日線 {r['ma25']:,.1f}円から {r['dev25_pct']:+.1f}%・出来高は{r['base_label']}の{r['ratio']:.2f}倍"
    rank = "◎ 業種の勢いがとても弱い" if r.get("mark") == "◎" else ""
    split = "※分割が調整されていない疑いあり（数字に注意）" if r.get("suspect_split") else ""
    tags = "#日本株 #株式投資 #出来高急増"
    nt = f"📝 note {NOTE_URL}"
    note = "※売買をすすめるものではありません"
    steps = ((rank, chart_url, tags, nt), ("", chart_url, tags, nt), ("", "", tags, nt), ("", "", "", nt), ("", "", "", ""))
    for rk, u, tg, n in steps:
        text = "\n".join([head, price, line] + [x for x in (rk, split, u, tg, n) if x] + [note])
        if x_length(text) <= limit:
            return text
    return "\n".join([head, line, note])


def links(r, chart_url=""):
    """(コピー用のページのリンク, X の投稿リンク, チャート画像があるか)。"""
    text = post_text(r, chart_url)
    chart = chart_payload(r)
    return clip_url(text, chart), x_post_url(text), bool(chart)


def read_chart_payload(payload):
    """chart_payload を読み戻す（self_test 用。clip.html の readChart と同じ読み方）。"""
    parts = payload.split(",")
    if len(parts) != 7 or parts[0] != "1" or len(parts[6]) % 9:
        return None
    lo, hi, line = (float(x) for x in parts[1:4])

    def val(a, b):
        return (_B64.index(a) * 64 + _B64.index(b)) / 4095 * (hi - lo) + lo

    out = []
    for i in range(0, len(parts[6]), 9):
        t = parts[6][i:i + 9]
        out.append((val(t[0], t[1]), val(t[2], t[3]), val(t[4], t[5]), val(t[6], t[7]), _B64.index(t[8]) / 63))
    return {"lo": lo, "hi": hi, "line": line, "d0": parts[4], "d1": parts[5], "bars": out}
