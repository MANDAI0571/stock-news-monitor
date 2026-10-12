"""データ取得の共通部品。

データの出所（2026-09-21 にすべて実アクセスで到達を確認）
  株価     : Yahoo Finance chart API (query1.finance.yahoo.com)
  業績     : 株探 kabutan.jp /stock/finance
  銘柄一覧 : JPX 上場銘柄一覧 data_j.xlsx
  祝日     : 内閣府 syukujitsu.csv
"""

import csv
import datetime as dt
import io
import json
import os
import re
import html
import time
import subprocess
import sys

JST = dt.timezone(dt.timedelta(hours=9))
UA = "Mozilla/5.0"   # 長いChrome風のUAだと Yahoo が 429 を返す（2026-09-21 実測）。短いUAだと通る。

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "cache")
os.makedirs(CACHE, exist_ok=True)


# curl が使う証明書の束（/etc/ssl/cert.pem）に無いルート証明書を足す。
# 内閣府（www8.cao.go.jp）は 2026-09-15 に GlobalSign Root R46 配下の証明書に
# 替わり、この束では検証できず curl (60) で失敗するようになった
# （2026-10-08/09 に確認。祝日CSVが更新できず古いキャッシュで動いていた）。
# Root R46 は macOS 自身の信頼済みルート（SystemRootCertificates.keychain）から
# 書き出したもの。SHA-256 4F:A3:12:6D:...:3C:40:C9、期限 2046-03-20。
CERTS = os.path.join(BASE, "certs")
SYSTEM_CA = "/etc/ssl/cert.pem"


_CA_MEMO = None


def _ca_bundle():
    """システムの束＋certs/ の追加分をまとめたファイルの場所。作れなければ None
    （その場合は curl の既定の束を使う。追加分が無いだけで、ほかは変わらない）。

    毎回、元の束の中身から作り直したものと比べて、違えば書き直す。更新日時で
    比べていたが、macOS は /etc/ssl/cert.pem の日付を OS のビルド日にするので、
    OS を更新しても古い束を使い続けた。空や壊れた束が残った場合も同じだった
    （どちらも2026-10-09の見直しで判明）。プロセスの中では1回だけ作る。
    """
    global _CA_MEMO
    if _CA_MEMO is not None:
        return _CA_MEMO or None
    _CA_MEMO = ""
    try:
        extra = sorted(os.path.join(CERTS, f) for f in os.listdir(CERTS)
                       if f.endswith(".pem"))
        if not extra or not os.path.exists(SYSTEM_CA):
            return None
        parts = [open(x, "rb").read().strip() for x in [SYSTEM_CA] + extra]
        if not all(b"BEGIN CERTIFICATE" in x for x in parts):
            return None                 # 元の束が壊れているなら足さない
        data = b"\n".join(parts) + b"\n"
        out = os.path.join(CACHE, "ca-bundle.pem")
        try:
            same = open(out, "rb").read() == data
        except OSError:
            same = False
        if not same:
            try:
                tmp = f"{out}.{os.getpid()}.new"
                with open(tmp, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, out)
            except OSError:
                # 書き直せない（cache/ に書けない等）ときは、今ある束が使えれば
                # それを使う。捨てると R46 が無い既定の束に戻り、祝日CSVが
                # 黙って取れなくなる（2026-10-09の見直しで判明）。
                try:
                    if b"BEGIN CERTIFICATE" in open(out, "rb").read():
                        _CA_MEMO = out
                        return out
                except OSError:
                    pass
                return None
        _CA_MEMO = out
        return out
    except OSError:
        return None


def _get(url, timeout=25, binary=False):
    """curl 経由で取得する。

    株探はチャンク転送の切り方が Python 標準の http.client と相性が悪く
    IncompleteRead で落ちる（2026-09-21 に実際に発生）。curl は同じURLを
    毎回きちんと読み切れたので、取得は curl に一本化している。
    """
    ca = _ca_bundle()
    p = subprocess.run(
        ["curl", "-sSL", "--fail", "--compressed", "--max-time", str(timeout),
         "-A", UA, "-H", "Accept-Language: ja,en;q=0.8"]
        + (["--cacert", ca] if ca else []) + [url],
        capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"curl exit {p.returncode}: "
                           f"{p.stderr.decode('utf-8', 'replace')[:200]}")
    if not p.stdout:
        raise RuntimeError("curl returned empty body")
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


def get_with_retry(url, tries=4, timeout=25, binary=False, pause=1.2):
    last = None
    for i in range(tries):
        try:
            return _get(url, timeout=timeout, binary=binary)
        except Exception as e:                      # noqa: BLE001
            last = e
            if i < tries - 1:
                time.sleep(pause * (i + 1))
    raise last


# ---------------------------------------------------------------- 営業日判定

def _cached_path(name):
    return os.path.join(CACHE, name)


# 連続性の検査で1日ずつ呼ぶので、読み直しを避けて覚えておく。
# ただしファイルの更新日時で持つ。ただ覚えるだけだと、同じプロセスの中で
# CSVが更新されても古いものを返し続ける（2026-10-05の見直しで判明）。
_HOLIDAY_CACHE = None       # (mtime, 祝日の集合)

# 取得に失敗して古いキャッシュで続けたもの。{"prime_list": 日数, "holidays": 日数}
# 標準エラーにしか出していなかったので、一覧が何か月も古いまま気づけなかった
# （2026-10-09の見直しで判明）。daily_run がメールの但し書きに使う。
STALE = {}


def holidays(max_age_days=7):
    """内閣府の国民の祝日CSVを取得（キャッシュ付き）。date の set を返す。

    取得に失敗しても、古いキャッシュがあればそれを使う。
    祝日は年単位でしか変わらないので、数日古くても判定は変わらない。
    ここで例外を投げると営業日判定ごと落ちてメールが出せなくなる
    （2026-09-29 04:40 に実際に発生。キャッシュが8日目で期限切れ、
    Mac起動直後でネットワーク未確立、curl (28) タイムアウト）。
    """
    global _HOLIDAY_CACHE
    p = _cached_path("syukujitsu.csv")
    if _HOLIDAY_CACHE is not None and os.path.exists(p) \
            and _HOLIDAY_CACHE[0] == os.path.getmtime(p):
        return _HOLIDAY_CACHE[1]
    fresh = os.path.exists(p) and (time.time() - os.path.getmtime(p)) < max_age_days * 86400
    if not fresh:
        try:
            raw = get_with_retry(
                "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv",
                binary=True, timeout=45, tries=5, pause=3.0)
            # 中身を確かめてから置き換える。直接書き込むと、途中で失敗した
            # ときに「今まで使えていたキャッシュ」を壊したうえで、それを
            # 「祝日0件」として読んでしまう。祝日に動いてメールを送る状態に
            # なり、しかも7日間そのままになる（2026-10-05の見直しで判明）。
            if not _holidays_sane(_parse_holidays(raw)):
                raise RuntimeError("祝日CSVの中身が足りません（今年ぶんが無い等）")
            tmp = p + ".new"
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, p)
        except Exception as e:                       # noqa: BLE001
            if not os.path.exists(p):
                raise
            age = (time.time() - os.path.getmtime(p)) / 86400
            STALE["holidays"] = age
            print(f"祝日CSVの更新に失敗（{e}）。{age:.0f}日前のキャッシュを使います。",
                  file=sys.stderr)
    out = _parse_holidays(open(p, "rb").read())
    # 読んだキャッシュ自体も確かめる。壊れたキャッシュを「祝日0件」として
    # 読むと、祝日に動いてメールを送る（2026-10-05の見直しで判明）。
    # 件数だけだと、1960年代までで切れたCSVが素通りする。今年と前年の
    # 祝日が入っているかを見る（2026-10-05の見直しで判明）。
    if not _holidays_sane(out):
        y = dt.date.today().year
        raise RuntimeError(
            f"祝日の一覧が{len(out)}件（今年{sum(1 for d in out if d.year == y)}件）"
            f"しかありません。キャッシュが壊れている可能性があるため、"
            f"休場日の判定ができません。")
    _HOLIDAY_CACHE = (os.path.getmtime(p), out)
    return out


def _holidays_sane(out):
    """祝日の一覧がまともか。合計だけ見ると、今年ぶんが丸ごと欠けた
    CSVが素通りする。今年と前年をそれぞれ確かめる。"""
    y = dt.date.today().year
    return (len(out) >= 100
            and sum(1 for d in out if d.year == y) >= 10
            and sum(1 for d in out if d.year == y - 1) >= 10)


def _parse_holidays(raw):
    text = raw.decode("shift_jis", "replace")
    out = set()
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2 or "/" not in row[0]:
            continue
        try:
            y, m, d = (int(x) for x in row[0].split("/"))
            out.add(dt.date(y, m, d))
        except ValueError:
            continue
    return out


def is_business_day(d, hol=None):
    """JPXの営業日か。土日・国民の祝日・12/31〜1/3 を休場とする。"""
    if d.weekday() >= 5:
        return False, "土日"
    if (d.month, d.day) in {(12, 31), (1, 1), (1, 2), (1, 3)}:
        return False, "年末年始"
    hol = holidays() if hol is None else hol
    if d in hol:
        return False, "祝日"
    return True, ""


# ---------------------------------------------------------------- 銘柄一覧

def prime_list(max_age_days=20):
    """JPXの上場銘柄一覧からプライム（内国株式）を返す。[(code, name), ...]"""
    import openpyxl

    p = _cached_path("data_j.xlsx")
    fresh = os.path.exists(p) and (time.time() - os.path.getmtime(p)) < max_age_days * 86400
    if not fresh:
        # HTMLのエラーページが返ってきても curl は成功扱いにしうる。
        # xlsxとして開けることを確かめてから置き換える。壊れたものを
        # 置くと、更新日時だけ新しいまま20日間ずっと同じ失敗を繰り返す。
        # 取得そのものの失敗も、確かめの失敗と同じく前の一覧で続ける。
        # 取得だけ try の外にあったので、JPX に届かない日は使える一覧が
        # あっても1日まるごと失敗していた（2026-10-09の見直しで判明）。
        tmp = p + ".new.xlsx"      # openpyxl は拡張子を見る。".new" だと必ず失敗する
        try:
            raw = get_with_retry(
                "https://www.jpx.co.jp/markets/statistics-equities/misc/"
                "tvdivq0000001vg2-att/data_j.xlsx", binary=True, timeout=60)
            with open(tmp, "wb") as f:
                f.write(raw)
            openpyxl.load_workbook(tmp, read_only=True).close()
        except Exception as e:                       # noqa: BLE001
            if os.path.exists(tmp):
                os.remove(tmp)
            if not os.path.exists(p):
                raise
            age = (time.time() - os.path.getmtime(p)) / 86400
            STALE["prime_list"] = age
            print(f"銘柄一覧の更新に失敗（{e}）。{age:.0f}日前のキャッシュを使います。",
                  file=sys.stderr)
        else:
            os.replace(tmp, p)
    # 注意: 鮮度は「落とした日」で見ている。JPXの一覧は公開時点で既に
    # 1か月ほど古いので、実質7週間遅れた一覧を使うことがある。
    wb = openpyxl.load_workbook(p, read_only=True)
    ws = wb.active
    out, asof = [], None   # asof はファイル自身の基準日
    for i, row in enumerate(ws.values):
        if i == 0:
            continue
        if asof is None:
            asof = row[0]
        if row[3] and "プライム" in str(row[3]) and "内国株式" in str(row[3]):
            out.append((str(row[1]).strip(), str(row[2]).strip()))
    wb.close()
    return out, asof


# ---------------------------------------------------------------- 株価

def chart(code, rng, interval):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.T"
           f"?range={rng}&interval={interval}")
    d = json.loads(get_with_retry(url))
    res = d.get("chart", {}).get("result")
    if not res:
        err = d.get("chart", {}).get("error")
        raise RuntimeError(f"{code}: {err}")
    r = res[0]
    q = r["indicators"]["quote"][0]
    return {
        "ts": r.get("timestamp") or [],
        "open": q.get("open") or [],
        "high": q.get("high") or [],
        "low": q.get("low") or [],
        "close": q.get("close") or [],
        "volume": q.get("volume") or [],
        "meta": r.get("meta", {}),
    }


# 分割・併合でよくある比率。
# 実際に東証であった分割・併合の比率。1/6・1/7・1/8・1/15 が抜けていて、
# 6:1や15:1の分割が調整されずに残っていた（2026-10-05の見直し）。
# 1/20・1/25 も抜けていた。NTT（9432）の 1:25（2023-06-28）が実例で、調整も目印も
# 付かず、約21営業日は「乖離が現実的でない」で隠れ、その後は-70%近い作られた乖離で
# 一覧の先頭に出た（2026-10-12の見直しで実測）。値幅制限の何倍もの段差なので、
# 本物の値動きと取り違えることはない。1/30 と併合側（20・25・30）も入れておく。
_SPLIT_RATIOS = (1 / 30, 1 / 25, 1 / 20, 1 / 15, 0.1, 1 / 8, 1 / 7, 1 / 6, 0.2, 0.25, 1 / 3,
                 0.4, 0.5, 2 / 3, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0,
                 6.0, 7.0, 8.0, 10.0, 15.0, 20.0, 25.0, 30.0)

# 東証の値幅制限（基準値段の上限, 制限値幅）。1日でこの幅を超える変動は
# 株価の動きとしては起こりえないので、超えていれば分割・併合などの
# 「株価以外の理由」と判断できる。
_PRICE_LIMITS = (
    (100, 30), (200, 50), (500, 80), (700, 100), (1000, 150),
    (1500, 300), (2000, 400), (3000, 500), (5000, 700), (7000, 1000),
    (10000, 1500), (15000, 3000), (20000, 4000), (30000, 5000),
    (50000, 7000), (70000, 10000), (100000, 15000), (150000, 30000),
    (200000, 40000), (300000, 50000), (500000, 70000), (700000, 100000),
    (1000000, 150000), (1500000, 300000), (2000000, 400000), (3000000, 500000),
    (5000000, 700000), (7000000, 1000000), (10000000, 1500000),
)


def _price_limit(base):
    """その値段での1日の値幅制限（円）。表の外は直近の幅を使う。"""
    for upper, width in _PRICE_LIMITS:
        if base < upper:
            return width
    return max(3000000, base * 0.15)     # 表の外は値段の15%を目安にする


def _corporate_action(prev_close, close, prev_vol=None, vol=None, adjacent=True):
    """1日の変化が分割・併合によるものなら、その比率を返す。違えば None。

    判定の軸は東証の値幅制限。制限の内側の動きは、どれだけ大きく見えても
    実際の売買で起きた値動きなので触らない。制限を超えていれば株価の動き
    ではないので、比率が分割によくある値に近いときだけ遡って直す。

    2026-09-24 の 8377 ほくほくFG が実例。1:10分割で 8,604円 -> 835.8円に
    なったが、Yahooの日足は分割前の株価を調整しないまま返してきた。
    そのままだと25日線が6,207円のままになり、「25日線から-85.9%」という
    実在しない下落として、分割後およそ25営業日のあいだ該当し続ける。

    最初は「株価と出来高が逆向きに動いたか」で見分けようとしたが、これは
    誤りだった（2026-10-04の見直しで判明）。暴落も株価↓・出来高↑で
    逆向きになるため、出来高を伴う急落を分割と誤判定する。
    値幅制限なら、6740 ジャパンディスプレイの 27円 -> 52円（制限は±30円
    なので内側＝実際の急騰）と、8377 の分割（制限±1,500円を大きく超える）を
    取り違えずに分けられる。
    """
    if not prev_close or not close:
        return None
    lim = _price_limit(prev_close)
    move = abs(close - prev_close)
    if move <= lim:
        return None                      # 値幅制限の内側＝実際の値動き
    if not adjacent:
        # 間に別の立会日をはさんでいると、複数日ぶんの変化を1日の制限と
        # 比べることになる。売買停止明けの暴落を分割と取り違えるので触らない。
        return None
    # 東証は連続ストップのときに制限幅を広げる。2倍までとは限らず、
    # さらに続くと4倍まで広がる。4倍を超えていれば値動きではまず届かない。
    # 許容は狭く保つ。広げると本物の暴落を分割に書き換えてしまう
    # （3498が2024-08-05のブラックマンデーで実際にそうなった）。
    # 原因は、値幅制限が「実際に取引された値段」で決まるのに、Yahooの終値は
    # 後の分割で既に縮尺されていること。縮尺のぶん制限を超えて見えるため、
    # 「制限を超えた＝値動きではない」が成り立たない場合がある。
    #
    # 取りこぼした分割への備えは、stage_a の「25日線からの乖離が現実的でない」
    # （-75%より深いものを出さない）だけ。これが拾えるのは1:4以上の分割の
    # 直後の数日だけで、次のものは拾えない（2026-10-05の見直しで実測。直していない）。
    #   ・値幅制限の内側に収まる細かい分割（1:1.1、1:1.2）。値段だけでは
    #     本物の値動きと見分けられない
    #   ・窓の中ほどに残った段差。25日線は正常で、75日線だけが汚れる
    # 以前は「説明のつかない段差」を別に数えて弾いていたが、本物の急騰
    # （4062の2026-08-05、+24.5%）まで弾いたので2026-10-05にやめた。
    # 許容を広げる前に、この穴が残っていることを前提に考えること。
    tol = 0.06 if move > lim * 4 else 0.04
    r = close / prev_close
    # 比率が近い候補が複数あることがある（1/6と1/7など）。最も近いものを
    # 機械的に採ると、2:1の分割を0.4と読んで20%の誤差を焼き付ける。
    # 許容に入る候補の中から、売買代金が最も連続するものを選ぶ。
    cands = [x for x in _SPLIT_RATIOS if abs(r / x - 1) <= tol]
    if not cands:
        return None
    if not prev_vol or not vol:
        return None                      # 確かめられないなら触らない
    # 候補が複数あるときは比率が最も近いものを選ぶ。出来高の増え方で
    # 選ぶ案も試したが、実データ12件中9件しか当たらず、素直に比率で
    # 選ぶ方（12件中12件）が良かった（2026-10-05の検証）。
    best = min(cands, key=lambda x: abs(r / x - 1))
    # 分割・併合は1株を割る（まとめる）だけなので、売買代金（株価×出来高）は
    # だいたい続く。株価が1.5倍でも出来高が176倍なら、それは併合ではなく
    # 実際の急騰（6191が2026-09-14にこの形で誤判定された）。
    if not prev_vol or not vol:
        return None                      # 確かめられないなら触らない
    turnover = (close * vol) / (prev_close * prev_vol)
    if not 0.1 <= turnover <= 10.0:
        return None
    return best


def _has_business_day_between(a, b):
    """a と b のあいだに、足の無い立会日があるか。"""
    hol = holidays()
    d = a + dt.timedelta(days=1)
    while d < b:
        if is_business_day(d, hol)[0]:
            return True
        d += dt.timedelta(days=1)
    return False


def daily(code):
    """日足。(dates, closes, volumes, meta) を返す。

    最後の1本は、立会中に取ると当日の途中経過になる。
    チャート用の四本値は meta["ohlc"] に入れて返す。
    """
    c = chart(code, "1y", "1d")
    dates, closes, vols = [], [], []
    op, hi, lo = [], [], []
    n = len(c["ts"])
    # Yahooは引け直後などに、最終足を「出来高あり・終値None」で返すことがある
    # （2026-10-01 のトヨタ等で実際に発生）。その足を捨てると当日の判定が
    # 丸ごとできなくなるので、meta の現在値で補う。
    live = c["meta"].get("regularMarketPrice")
    for i, (t, cl, v) in enumerate(zip(c["ts"], c["close"], c["volume"])):
        if cl is None:
            if i == n - 1 and live:
                cl = float(live)
            else:
                continue
        if v is None:
            # 出来高が無い足は値段だけあっても判定に使えない。0として扱うと
            # 「出来高0の実在する日」になり、全銘柄が出来高倍率0で落ちて
            # 「該当なし」のメールが出てしまう（2026-10-04の見直しで判明）。
            # 足ごと捨てれば、当日ぶんが無いことが段階Aの検査で数えられる。
            continue
        if not v and (not closes or closes[-1] <= 0
                      or abs(float(cl) / closes[-1] - 1) > 0.001):
            # 出来高0の足は、直前の足と同じ値段なら「値段の変わらない日」として
            # 残す。9508・6486・7240 の 2026-09-29/30、5202 の 09-28〜30 が
            # この形（2026-10-06 に実データで確認）で、
            # 捨てると欠けた立会日が増え、75日線の範囲で弾かれる銘柄が出る。
            # 値段が違うものは中身が壊れているので捨てる。8303 は上場前の
            # 2025-11-17〜12-17 に、終値55,319,998,464円・出来高0の足が11本ある
            # （2026-10-06 に実データで確認）。残すと移動平均に入り、75日線が
            # 何億円にもなって「現在値<25日線<75日線」を作り物の数字で満たす
            # （2026-10-05の見直しで判明）。
            continue
        dates.append(dt.datetime.fromtimestamp(t, JST).date())
        closes.append(float(cl))
        vols.append(float(v))
        # 四本値が欠けている本は終値で埋める（描画が飛ばないように）
        op.append(float(c["open"][i] if c["open"][i] is not None else cl))
        hi.append(float(c["high"][i] if c["high"][i] is not None else cl))
        lo.append(float(c["low"][i] if c["low"][i] is not None else cl))
    # 分割・併合がまだ調整されていない場合、その日より前をさかのぼって直す。
    # 直さないと移動平均が分割前の株価のままになり、実在しない下落になる。
    actions = []
    for i in range(len(closes) - 1, 0, -1):
        if not vols[i]:
            continue                  # 出来高0の足は値段として使えない
        # 比較相手は「直前の、売買のあった足」。すぐ前が出来高0の偽の足だと
        # 判定できず、分割を見落として実在しない下落が残る
        # （2026-10-04 の人工データで再現。8377の分割と9508の偽足が重なる形）。
        j = i - 1
        while j >= 0 and not vols[j]:
            j -= 1
        if j < 0:
            continue
        # 配列の位置ではなく日付で見る。出来高0やnullの足を飛ばしている
        # ので、位置が隣でも実際には数営業日あいていることがある。
        # 注意（2026-10-12の見直しで実測。直していない）: 分割の直前に出来高0の
        # 足（配信の遅れた偽の足）があると、ここで「隣でない」となり調整されない。
        # 調整されなかった1:2〜1:4は suspect_split の目印で知らせるが、併合（2:1等）は
        # 印が付かず、本物の該当が最長75日ほど消えることがある。隣とみなすと、
        # 数日分の本物の暴落を分割に書き換える危険があるので、今はこのままにしている。
        # また 1/3 と 0.4 のように近い比率を取り違え、分割の日の本物の値動きを
        # 消してしまうことがある（5年で該当しうる未調整の分割は2件）。
        gap_ok = not _has_business_day_between(dates[j], dates[i])
        ratio = _corporate_action(closes[j], closes[i], vols[j], vols[i],
                                  adjacent=gap_ok)
        if not ratio:
            continue
        actions.append((dates[i], ratio))
        # 遡って直すのは「比較相手の足まで」。間にある出来高0の足は、
        # すでに分割後の値段で配信されているので掛けてはいけない
        # （掛けると系列の中に実在しない90%安の足ができる）。
        for j in range(j + 1):
            closes[j] *= ratio
            op[j] *= ratio
            hi[j] *= ratio
            lo[j] *= ratio
            vols[j] /= ratio          # 株数は逆向きに増える
    if actions:
        for d, r in actions:
            print(f"split_adjusted[{code}] {d} ratio={r:g}", flush=True)

    meta = dict(c["meta"])
    meta["ohlc"] = {"open": op, "high": hi, "low": lo}
    meta["split_adjustments"] = [(d.isoformat(), r) for d, r in actions]
    return dates, closes, vols, meta


def fold_cum(c):
    """5分足のチャートを日付ごとに (時刻, その時点までの累計出来高) に畳む。

    戻り値: {date: [(datetime_jst, cum_volume), ...]}
    """
    by_day = {}
    for t, v in zip(c["ts"], c["volume"]):
        tm = dt.datetime.fromtimestamp(t, JST)
        # null は「出来高0」ではない。0に読み替えると、配信障害の日が
        # 「出来高が増えなかった日」に化けて静かに該当なしになる（日足側と同じ穴）。
        if v is None:
            continue
        by_day.setdefault(tm.date(), []).append((tm, float(v)))
    out = {}
    for d, rows in by_day.items():
        rows.sort()
        cum, acc = [], 0.0
        for tm, v in rows:
            acc += v
            cum.append((tm, acc))
        out[d] = cum
    return out


def cum_vol_at(cum_rows, cutoff_time):
    """その日の cutoff_time（time）までの累計出来高。"""
    acc = 0.0
    for tm, c in cum_rows:
        if tm.time() <= cutoff_time:
            acc = c
        else:
            break
    return acc


# ---------------------------------------------------------------- 業績（株探）

_NUM = re.compile(r"^[+-]?[\d,]+(\.\d+)?$")
# 決算期の表記。「26.04-06」も「25.10-26.06」も拾う
_PERIOD = r"\d{2}\.\d{2}-(?:\d{2}\.)?\d{2}"


def _tables(html_text):
    out = []
    for m in re.finditer(r"<table[^>]*>.*?</table>", html_text, flags=re.S):
        rows = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(0), flags=re.S):
            cells = [html.unescape(re.sub(r"<[^>]+>", "", x))
                     .replace("\xa0", " ").replace("　", " ").strip()
                     for x in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, flags=re.S)]
            if cells:
                rows.append(cells)
        if rows:
            out.append(rows)
    return out


def _num(s):
    """株探の数値セルを float にする。

    伸び率が大きいと株探は「％」ではなく「2.5倍」と書く（例: 7974 任天堂の
    2026.04-06 営業益 前年同期比 = 2.5倍）。これを読めずに None にしていたため、
    利益が2倍以上に伸びた銘柄ほど落ちていた（2026-09-29 に発見・修正）。
    「黒転」「赤転」は前年が赤字で伸び率が定義できないので None のままにする。
    """
    s = (s or "").replace(",", "").replace("％", "").strip()
    if not s or s in {"－", "-", "―", "--"}:
        return None
    m = re.match(r"^([0-9.]+)倍$", s)
    if m:
        try:
            return (float(m.group(1)) - 1.0) * 100.0
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def fundamentals(code, max_age_days=5, offline=False):
    """株探の決算ページから必要な数字を取る。結果はキャッシュする。

    返す辞書:
      rev_yoy, op_yoy  直近の四半期累計決算の 売上高／営業益 の前年同期比(%)
      period           その決算の期間（例 26.04-06）
      announced        発表日
      roe              直近「実績」通期のROE(%)
      roe_period       そのROEの決算期
      equity_ratio     直近の自己資本比率(%)
      eq_period        その自己資本比率の決算期
    """
    fdir = os.path.join(CACHE, "fund")
    os.makedirs(fdir, exist_ok=True)
    p = os.path.join(fdir, f"{code}.json")
    if os.path.exists(p) and (time.time() - os.path.getmtime(p)) < max_age_days * 86400:
        with open(p, encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("period") or cached.get("roe") is not None \
                or cached.get("equity_ratio") is not None:
            return cached
    if offline:
        # 株探に届かない日（呼び出し側が続けて失敗したと判断した日、または
        # この銘柄の取得に失敗した直後）は取りに行かない。古いキャッシュが
        # あればそれを使い、何日前のものかを付けて返す（カードに出す）。
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                out = json.load(f)
            if out.get("period") or out.get("roe") is not None \
                    or out.get("equity_ratio") is not None:
                out["cache_age_days"] = (time.time() - os.path.getmtime(p)) / 86400
                return out
        raise RuntimeError("株探に届かなかったため、取得していません")

    text = get_with_retry(f"https://kabutan.jp/stock/finance?code={code}", timeout=25)
    tabs = _tables(text)
    out = {"code": code, "rev_yoy": None, "op_yoy": None, "period": None,
           "announced": None, "period_kind": None,
           "rev_yoy_raw": "", "op_yoy_raw": "",
           "roe": None, "roe_period": None,
           "equity_ratio": None, "eq_period": None, "error": None}

    # --- 直近決算の前年同期比
    #
    # 2026-09-29 に25銘柄で実地確認して整理した。株探の決算ページには
    # 「決算期」で始まる表が複数あり、取り違えると別の期の数字を読む。
    #   ・四半期累計   ヘッダーに「進捗率」がある（対上期/対通期）。これが第一候補。
    #   ・3ヵ月決算     ヘッダーに「売上営業損益率」がある。累計表が無い会社
    #                  （直近が通期決算でkabutanが累計表を作らない。例: 4385 メルカリ）
    #                  のための第二候補。
    #   ・上期業績     ヘッダーが「修正1株配」。上期だけの表で、累計ではない。使わない。
    #   ・修正履歴     ヘッダーに「修正日」。使わない。
    # どの表でも「予」の付く行は予想なので実績として読まない
    # （予想だけの表が先に出てくる会社があり、発表日を誤って拾っていた）。
    def _pick_yoy(rows):
        yoy = next((r for r in rows if r and r[0] == "前年同期比"), None)
        if not yoy:
            return None
        data = [r for r in rows[1:]
                if len(r) >= 3 and re.search(_PERIOD, r[0]) and "予" not in r[0]]
        if not data:
            return None
        return yoy, data[-1]

    cumulative = None
    three_month = None
    for rows in tabs:
        head = rows[0]
        if not head or head[0] != "決算期" or "修正日" in head:
            continue
        got = _pick_yoy(rows)
        if not got:
            continue
        if "進捗率" in " ".join(head) and cumulative is None:
            cumulative = got
        elif "売上営業損益率" in head and three_month is None:
            three_month = got

    picked = cumulative or three_month
    if picked:
        yoy, last = picked
        # 「黒転」（前年赤字→今年黒字）は伸び率が定義できず数値にならないが、
        # 業績としては明確なプラス。判定側で拾えるよう生の表記も残す。
        out["rev_yoy_raw"] = yoy[1].strip() if len(yoy) > 1 else ""
        out["op_yoy_raw"] = yoy[2].strip() if len(yoy) > 2 else ""
        out["rev_yoy"] = _num(yoy[1]) if len(yoy) > 1 else None
        out["op_yoy"] = _num(yoy[2]) if len(yoy) > 2 else None
        out["period"] = re.search(_PERIOD, last[0]).group(0)
        out["announced"] = last[-1]
        out["period_kind"] = "累計" if cumulative else "3ヵ月"

    # --- ROE（成長性・収益性の表。「予」は使わず直近の実績を採る）
    for rows in tabs:
        head = rows[0]
        if not head or "ＲＯＥ" not in head:
            continue
        i = head.index("ＲＯＥ")
        act = [r for r in rows[1:]
               if len(r) > i and re.search(r"\d{4}\.\d{2}", r[0]) and "予" not in r[0]]
        if act:
            out["roe"] = _num(act[-1][i])
            out["roe_period"] = re.search(r"\d{4}\.\d{2}", act[-1][0]).group(0)
        break

    # --- 自己資本比率（財務の表。四半期の行があればそれを使う）
    for rows in tabs:
        head = rows[0]
        if not head or "自己資本比率" not in head:
            continue
        i = head.index("自己資本比率")
        act = [r for r in rows[1:]
               if len(r) > i and re.search(r"\d{2,4}\.\d{2}", r[0]) and "予" not in r[0]]
        if act:
            out["equity_ratio"] = _num(act[-1][i])
            m = re.search(_PERIOD + r"|\d{4}\.\d{2}", act[-1][0])
            out["eq_period"] = m.group(0) if m else None
        break

    # 何も読めなかったページは、取得の失敗として扱う。株探に個別ページの無い
    # コードは検索ページへ転送され、混んでいるときは中身の無いページが返る
    # （2026-09-29 に普通の4桁の銘柄28件が空のまま残っていた）。成功として
    # 返すと、前に取った数字も使われず「取得失敗 0」と出ていた（2026-10-11の見直し）。
    if out["period"] is None and out["roe"] is None and out["equity_ratio"] is None:
        raise RuntimeError("株探のページから業績を読めませんでした")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out
