"""出来高急増 × 下降トレンド の東証プライム銘柄を探す。

判定条件（初期値）
  出来高   当日の出来高が過去20日平均の2.0倍以上
  トレンド 現在値 < 25日線 < 75日線、かつ25日線が5本前の足の時点より低い
           （足が飛んだ日があると5営業日前とは限らない）
  流動性   20日平均売買代金が1億円以上
  業績     絞り込みには使わない。売上高の前年同期比とROEを「参考条件」として
           銘柄ごとに表示するだけ（2026-10-04に変更）。営業益の前年同期比と
           自己資本比率も数字は出すが、条件には入れていない。
"""

import os
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import statistics
import sys
import threading
import time

import fetchlib as F

JST = F.JST

VOL_RATIO_MIN = 2.0
ROE_MIN = 8.0
# 自己資本比率は2026-10-04に条件から外した。表示だけに使うのでしきい値は持たない。
TURNOVER_MIN = 1e8          # 20日平均売買代金 1億円
DAILY_FAIL_STREAK = 30      # 日足の取得がこの回数続けて失敗したら、待ってからやり直す
DAILY_COOLDOWNS = (60, 120, 180)   # その待ち時間（秒）。使い切ったら残りは取りに行かない
# 日足を取り終える時間の上限（秒）。これを過ぎたら残りは取りに行かない。
# 定期タスクは約40分で止められ、止められると理由が残らない。連続した失敗だけを
# 数えていたので、ときどき応答しない Yahoo では遮断が働かず、何時間も
# かかっていた（2026-10-11の見直しで実測。半分が応答しないと3時間超）。
# 普段は1分ほどで終わる（2026-10-06〜09 の実行で53〜68秒）。
STAGE_A_BUDGET = 12 * 60
FUND_BUDGET = 20 * 60       # 実行開始からこの時間を過ぎたら、業績は前に取った数字だけで出す
SKIPPED = "SKIP:株価の取得が続けて失敗したため、取りに行っていません"
PROBE_CODES = ("7203", "6758", "8306", "9984", "8035")
FUND_FAIL_STREAK = 5        # 株探も同じ（残りはキャッシュだけ）
PREFILTER_SLACK = 1.02      # 概算段階では境界付近を落とさないよう少し緩める


def ma(xs, n):
    return statistics.fmean(xs[-n:]) if len(xs) >= n else None


NO_BAR = "対象日の日足なし"

# 段階Aで「データが足りない」ために落ちた理由。数えないと、全銘柄が
# 履歴不足で消えた日が平常の「該当なし」になる（2026-10-05の見直しで判明）。
STAGE_A_DATA_MISSING = (
    "日足が80日に満たない", "移動平均を出せない",
    "売買代金の計算に必要な20日分（売買のあった日）がない",
    "直前の立会日の足がない", "移動平均に使う75営業日ぶんの足がそろっていない",
)

# 乖離が現実的でない（調整されていない分割の疑い）として除いた理由。データが
# 無いのではないので欠損には数えず、別に数えて内訳に出す。欠損に数えていた
# ため、本物の急騰（4392 の 2026-05-21 等）が「当日の日足なし」と書かれていた
# （2026-10-09の見直しで判明）。
IMPLAUSIBLE = "25日線からの乖離が現実的でない"

# 出来高段階で「データが無い」ために落ちた理由。処理の失敗と同じ扱いで数える。
# 数えていなかったため、5分足が全滅した日が「該当なし」として送られていた
# （2026-10-04の見直しで判明）。
DATA_MISSING = (
    "当日の5分足がない", "前日の5分足がない", "前日の同時刻までの出来高が0",
    "比較対象の出来高が0", "当日の出来高が0", "直前の立会日の足がない",
    "移動平均に使う75営業日ぶんの足がそろっていない", "5分足が対象時刻まで来ていない",
)


def _business_days_between(a, b):
    """a の翌日から b の前日までにある立会日の数。"""
    hol = F.holidays()
    n, d = 0, a + dt.timedelta(days=1)
    while d < b:
        if F.is_business_day(d, hol)[0]:
            n += 1
        d += dt.timedelta(days=1)
    return n


def _gap_has_business_day(a, b):
    """a と b のあいだに、足の無い立会日があるか。"""
    return _business_days_between(a, b) > 0


def _missing_bars(dates, n):
    """直近 n 本の範囲で、足の無い立会日が何日あるか。

    立会日の数と足の数を比べる。「間に立会日があるか」を見るだけだと、
    範囲が広いときに必ず真になってしまう（2026-10-05に一度そう書いて、
    全銘柄が弾かれた）。
    """
    span = _business_days_between(dates[-n], dates[-1])
    return max(0, span - (n - 2))


# 目印にする分割・併合の比率。1:2より細かい（3:2、1:1.2 など）は、本物の
# 急落・急騰と見分けられないので対象にしない（見逃す。直していない）。
_SUSPECT_RATIOS = tuple(x for x in F._SPLIT_RATIOS if x <= 0.5 or x >= 2.0)


def suspect_split(dates, closes, vols, window=80):
    """直近 window 本の中で、分割・併合が調整されずに残っていそうな段差。

    見つかれば (日付, 比率) を、無ければ None を返す。表示に使うだけで、
    銘柄を落とすことはしない（本物の暴落を消さないため）。

    2026-10-05 に「説明のつかない段差」で落とす仕組みをやめてから、調整に
    失敗した分割（比率の許容4%の外、出来高0の足をはさむ等）は、そのまま
    一覧の先頭に出るようになった（2026-10-09の見直しで実測。4507 を1:2の
    未調整にすると -53% で1位に出た）。Yahoo は実際に分割を何か月も調整
    しないことがある（8227 の1:3、8377 の1:10）。

    目印にする条件（すべてを満たすもの）。比べる相手は直前の売買のあった足で、
    間に出来高0の足や欠けた日があっても日数で割り引かない（2026-10-09の
    見直しで、日数で割り引くと出来高0の足をはさんだ1:2を必ず見逃すと判明）:
      ・変化が1日の値幅制限を超える
      ・比率が 1:2 以上の分割・併合の比率（_SUSPECT_RATIOS）の±15%以内
      ・売買代金（株価×出来高）が続いている（0.02〜50倍）。分割は株数が
        増えるだけで売買代金は大きく変わらない。本物の急騰・急落は売買代金が
        跳ねる（4392 の 2026-05-12 は +71% で売買代金263倍。以前は誤って印を付けた）
    5年分（1,556銘柄）で試すと、印が付いたのは本当に調整されていなかった
    8227（2026-02-18、1:3）と 8377（2026-09-24、1:10）の2件だけだった。
    """
    start = max(1, len(closes) - window)
    found = None
    for i in range(start, len(closes)):
        if not vols[i]:
            continue
        j = i - 1
        while j >= 0 and not vols[j]:
            j -= 1
        if j < 0 or not closes[j] or not closes[i] or not vols[j]:
            continue
        if abs(closes[i] - closes[j]) <= F._price_limit(closes[j]):
            continue
        r = closes[i] / closes[j]
        best = min(_SUSPECT_RATIOS, key=lambda x: abs(r / x - 1))
        if abs(r / best - 1) > 0.15:
            continue
        # 幅は調整側（0.1〜10）より広くする。同じ幅だと、売買代金で調整から外れた
        # 分割を目印でも見逃す（2026-10-11の見直しで判明。8848 の1:5で12倍だった）。
        # 0.02〜50 にしても5年分で印が増えないことを確かめた。4392（263倍）は外れる。
        turnover = (closes[i] * vols[i]) / (closes[j] * vols[j])
        if not 0.02 <= turnover <= 50:
            continue
        found = (dates[i], r)
    return found


def stage_a(code, name, target):
    """日足だけで、トレンドと売買代金の足切りをかける（概算）。

    target は判定したい日（過去日再現ならその日、本番なら当日）。
    日足の最終日が target と一致しない銘柄は必ず弾く。
    ここを本番で素通りさせると、前営業日の数字を「本日14:00時点」として
    配信してしまい、全銘柄が同じだけずれるので気づけない。
    """
    dates, closes, vols, meta = F.daily(code)
    keep = [i for i, d in enumerate(dates) if d <= target]
    if not keep or dates[keep[-1]] != target:
        return None, NO_BAR
    i = keep[-1] + 1
    dates, closes, vols = dates[:i], closes[:i], vols[:i]
    if len(closes) < 80:
        return None, "日足が80日に満たない"

    # 出来高の無い足を落としているので、添字の1本前が必ずしも直前の立会日とは
    # 限らない。前日比・前日の出来高・5本前の25日線はすべて添字で取るので、
    # 連続していなければ名乗らずに弾く（2026-10-05の見直しで判明した副作用）。
    # 移動平均は80本ぶんを添字で数えるので、その範囲に欠けた立会日が
    # あると平均が静かにずれる。最後の6本だけを見ていたのでは足りない
    # （2026-10-05の見直しで判明）。
    # 直前の足が「前営業日」でないことはある。取引所の臨時休場なら全銘柄で
    # 起きるので、ここで弾くとスクリーニングが丸ごと止まる。2営業日までは
    # 通し、何日あいているかを持ち回って表示側で断る（2026-10-05の見直し）。
    # 上限は下の75本の検査と同じ2にそろえる。あいた日は75本の範囲の欠けにも
    # 数えられるので、3にしても3日あいた銘柄は下の検査で別の理由で落ちていた
    # （2026-10-05の見直しで判明）。
    prev_gap = _business_days_between(dates[-2], dates[-1])
    if prev_gap > 2:
        return None, "直前の立会日の足がない"
    # 窓は75本（75日線が使う範囲）。1本の欠けは取引所全体の臨時休場でも
    # 起きるので、それだけで全銘柄を落とすと screen が丸ごと止まる。
    # 2本までは許し、それ以上を欠損として扱う（2026-10-05の見直し）。
    if _missing_bars(dates, 75) > 2:
        return None, "移動平均に使う75営業日ぶんの足がそろっていない"

    price = closes[-1]
    ma25 = ma(closes, 25)
    ma75 = ma(closes, 75)
    ma25_prev = ma(closes[:-5], 25)
    if None in (ma25, ma75, ma25_prev):
        return None, "移動平均を出せない"

    # 20日平均売買代金は「終わった日」だけで計算する（当日の途中は混ぜない）。
    # 出来高0の足は実際には売買のなかった日なので混ぜない。混ぜると平均が
    # 下がり、出来高倍率が実際より大きく出る（2026-10-04の見直しで判明）。
    done = [(c, v) for c, v in zip(closes[:-1], vols[:-1]) if v][-20:]
    if len(done) < 20:
        return None, "売買代金の計算に必要な20日分（売買のあった日）がない"
    done_c = [c for c, _ in done]
    done_v = [v for _, v in done]
    turnover20 = statistics.fmean([c * v for c, v in zip(done_c, done_v)])
    vol_avg20 = statistics.fmean(done_v)

    # 調整されていない分割が残っていると、実在しない大きな乖離になる
    # （8377は1:10分割の未調整で-85.9%と出た）。株価を憶測で書き換えるより、
    # 現実にありえない乖離の銘柄を出さない方を選ぶ。
    #
    # 境界は実測で決める。5年・1,556銘柄・12,965件の該当日を調べたところ、
    # 正当な該当で最も深いのは -48.94%（7033、2022-06-21）だった。
    # 連続ストップ安でちょうど -50.00% に達した例もある（4565、2023-06-29）。
    # 当初 -50% にしたが、それでは本物の暴落を消す（2026-10-05の見直しで判明。
    # 「最も深いのは-18%程度」と書いていたのは31ポイントの誤り）。
    # -75% に境界を置いても、これで除けるのは 1:5 以上の未調整の分割の直後
    # だけ（比率が大きいほど長く、1:25 では約21営業日。2026-10-12の見直しで実測）。1:4 は分割の日でも -74% ほどで内側に入り、1:2・1:3 も
    # 調整に失敗すれば（比率の許容4%の外、出来高0の足をはさむ等）ここを
    # 通る（2026-10-09の見直しで実測）。それらは suspect_split の目印で
    # メールに出す。この境界は本物の該当を消さないことだけを確かめてある
    # （5年で正当な該当はすべて内側。外れたのは+200%超の急騰4件だけ）。
    if not (-0.75 < price / ma25 - 1 < 2.0):
        return None, "25日線からの乖離が現実的でない"

    if turnover20 < TURNOVER_MIN:
        return None, "売買代金が足りない"
    if not (ma25 < ma25_prev):
        return None, "25日線が下向きでない"
    if not (price < ma25 * PREFILTER_SLACK and ma25 < ma75):
        return None, "現在値<25日線<75日線 でない"

    return {
        "code": code, "name": name,
        "date": dates[-1], "dates": dates, "closes": closes, "vols": vols,
        "ohlc": meta.get("ohlc"),
        "price_daily": price, "prev_close": closes[-2],
        "ma25": ma25, "ma75": ma75, "ma25_prev": ma25_prev,
        "turnover20": turnover20, "vol_avg20": vol_avg20,
        "prev_gap": prev_gap,
        "vol_today_daily": vols[-1], "vol_prev_daily": vols[-2],
        # Yahoo が最後に約定を記録した時刻（UNIX秒）。配信は実行時刻より
        # 遅れている（2026-10-06 12:47 の取得で中央値15.6分遅れを実測）。
        "market_time": meta.get("regularMarketTime"),
    }, None


TURNED_PROFIT = "黒転"   # 前年赤字 → 今年黒字。伸び率は出ないが明確なプラス


LOSS_WORDS = {"黒転": "黒字転換", "赤転": "赤字転落",
              "赤縮": "赤字縮小", "赤拡": "赤字拡大"}


def _yoy_text(value, raw):
    """前年同期比を人が読める形にする。"""
    if raw in LOSS_WORDS:
        return LOSS_WORDS[raw]
    return "不明" if value is None else f"{value:+.1f}%"


def _yoy_positive(value, raw):
    """前年同期比がプラスか。「黒転」は数値にならないがプラス扱いにする。"""
    if raw == TURNED_PROFIT:
        return True
    return value is not None and value > 0


def _fund_known(f):
    """参考条件を判定できるだけの数字がそろっているか。

    そろっていないものを「満たさない」と書かないために、表示側
    （report._fund_state）と同じ考え方をここにも置く。
    """
    rev_known = (f.get("rev_yoy") is not None
                 or f.get("rev_yoy_raw", "") in LOSS_WORDS
                 or f.get("rev_yoy_raw", "") == TURNED_PROFIT)
    return rev_known and f.get("roe") is not None


def fundamentals_ok(f):
    """業績の「参考条件」を満たすか。満たさない場合は未達の条件を全部返す。

    2026-10-04 に、業績は絞り込み（仕分け）に使うのをやめた。

    数字は必ず3つに分けて読む。「判定できなかった」を「満たさない」に混ぜると
    結論が逆に見える。最初はそれで誤った説明を書いていた（2026-10-05に訂正）。
    さらに2026-10-05の見直しで、業績キャッシュの135件が空のまま使われていたこと、
    決算発表当日の数字を先読みしていたこと、条件を判定できない期間まで集計に
    入れていたことが分かり、全部直して測り直した。

    下降×出来高2倍の該当銘柄、2026-02-06〜2026-09-30に決着した売買:
      参考条件を満たす     753件  損益比1.07（コスト0.2%後 0.99）
      満たさない           778件  損益比1.03（同 0.96）
      業績が判定できない     65件  損益比0.86（同 0.81）
    売買代金1億円以上の全銘柄日で、条件ごとに同じ3分割をすると:
      売上高 前年同期比プラス   満たす1.10 / 満たさない1.12 / 判定できない0.80
      ROE 8%以上               満たす1.06 / 満たさない1.15 / 判定できない1.35
    つまり業績は、条件としても目印としても成績をほとんど動かさない。
    数字を出すのは自分で見て判断するためであって、満たす方が良いという
    意味ではない。メールの末尾にも同じ断りを入れてある。

    この数字の限界: 期間が約8か月しかない（株探に残る決算が8四半期ぶんで、
    それ以前は条件を判定できない）。母集団が今日のプライム一覧なので、
    途中で上場廃止や市場変更になった銘柄が入っておらず、数字は良い方に偏る。

    営業益の前年同期比と自己資本比率は条件から外したが、数字は出し続ける。
    """
    ng = []
    if not _yoy_positive(f.get("rev_yoy"), f.get("rev_yoy_raw", "")):
        ng.append(f"売上高 {_yoy_text(f.get('rev_yoy'), f.get('rev_yoy_raw', ''))}")
    roe = f.get("roe")
    if roe is None or roe < ROE_MIN:
        ng.append(f"ROE {'不明' if roe is None else format(roe, '.1f') + '%'}"
                  f"（{ROE_MIN:.0f}%以上）")
    return (not ng), " / ".join(ng)


def stage_c_daily(row, cutoff, volmode):
    """日足だけで出来高倍率を出す。

    分足を使わない理由: Yahoo の分足は寄り付き・引けの気配寄せ等を取りこぼし、
    1日の出来高の33〜84%しか積み上がらない（2026-09-21 実測）。日足の出来高は
    株探（東証の正式な売買高）と完全一致したので、こちらだけを使う。
    """
    # 段階Aは境界付近を落とさないよう少し緩めているので、ここで厳密に見直す
    price = row["price_daily"]
    if not (row["ma25"] < row["ma25_prev"] and price < row["ma25"] < row["ma75"]):
        return None, "現在値<25日線<75日線 でない"

    v_today = row["vol_today_daily"]
    # 0は「売買がなかった日」ではなく「取れていない」とみなす。倍率0で
    # 静かに落ちると、配信障害の日が平常の「該当なし」になる。
    if not v_today:
        return None, "当日の出来高が0"
    if volmode == "prevday":
        base = row["vol_prev_daily"]
        # 直前の足が前営業日でない日（段階Aで2営業日まで通している）に
        # 「前日1日」と書くと、何日も前の出来高を前日のものと名乗る
        # （2026-10-05の見直しで判明）。
        base_label = ("前日1日" if not row.get("prev_gap")
                      else f"{row['prev_gap'] + 1}営業日前の1日")
    else:                                        # avg20
        base = row["vol_avg20"]
        base_label = "過去20日平均"
    if not base:
        return None, "比較対象の出来高が0"
    ratio = v_today / base
    if ratio < VOL_RATIO_MIN:
        return None, f"出来高倍率が{VOL_RATIO_MIN}倍未満"

    row = dict(row)
    row.update({
        "price": price, "vol_today": v_today, "vol_base": base,
        "base_label": base_label, "ratio": ratio,
        "chg_pct": (price / row["prev_close"] - 1) * 100,
        "dev25_pct": (price / row["ma25"] - 1) * 100,
    })
    return row, None


def stage_c(row, cutoff, asof):
    """5分足で、cutoff時点の値段と出来高を正確に出す。"""
    c5 = F.chart(row["code"], "5d", "5m")       # 5分足はここで1回だけ取る
    cum = F.fold_cum(c5)
    today = row["date"]
    days = sorted(d for d in cum if d <= today)
    if today not in cum:
        return None, "当日の5分足がない"
    if len(days) < 2:
        return None, "前日の5分足がない"
    prev = days[-2]

    v_today = F.cum_vol_at(cum[today], cutoff)
    v_prev = F.cum_vol_at(cum[prev], cutoff)
    if v_prev <= 0:
        return None, "前日の同時刻までの出来高が0"

    # cutoff 時点の値段（5分足の終値）。取れなければ日足の値で代用する。
    price = None
    for t, cl in zip(c5["ts"], c5["close"]):
        tm = dt.datetime.fromtimestamp(t, JST)
        if tm.date() == today and tm.time() <= cutoff and cl is not None:
            price = float(cl)
    if price is None:
        price = row["price_daily"]

    closes = row["closes"][:-1] + [price]
    ma25 = ma(closes, 25)
    ma75 = ma(closes, 75)
    ma25_prev = ma(closes[:-5], 25)
    if not (ma25 < ma25_prev and price < ma25 < ma75):
        return None, "cutoff時点の値段で見ると下降トレンドの条件を満たさない"

    last_today = cum[today][-1][0].time() if cum.get(today) else None
    if last_today is None or (
            dt.datetime.combine(dt.date.today(), cutoff)
            - dt.datetime.combine(dt.date.today(), last_today)).total_seconds() > 900:
        # 配信が途中で止まった日に「前日の同時刻まで」と比べると、当日だけ
        # 短い時間の出来高になり、全銘柄の倍率が一律に小さく出る。
        return None, "5分足が対象時刻まで来ていない"

    ratio = v_today / v_prev
    if ratio < VOL_RATIO_MIN:
        return None, f"出来高倍率が{VOL_RATIO_MIN}倍未満"

    row = dict(row)
    row.update({
        "price": price, "ma25": ma25, "ma75": ma75, "ma25_prev": ma25_prev,
        "vol_today": v_today, "vol_prev_same": v_prev, "prev_day": prev,
        # 表示側（report.card / fmt / 記録）はこの2つを必ず読む。
        # ここで入れていなかったため --volmode sametime は常に KeyError で
        # 失敗メールになっていた（2026-10-04 の見直しで判明）。
        "vol_base": v_prev,
        # 比べた日が前営業日でなければ、その日付を書く（2026-10-11の見直し）
        "base_label": ("前日の同時刻まで" if _business_days_between(prev, today) == 0
                       else f"{prev}の同時刻まで"),
        "ratio": ratio,
        "ratio_vs_prev_full": (v_today / row["vol_prev_daily"]
                               if row["vol_prev_daily"] else None),
        "chg_pct": (price / row["prev_close"] - 1) * 100,
        "dev25_pct": (price / ma25 - 1) * 100,
    })
    return row, None


def pmap(fn, items, workers, label):
    out = []
    done = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, it): it for it in items}
        for fu in cf.as_completed(futs):
            done += 1
            if done % 200 == 0:
                print(f"  {label}: {done}/{len(items)}", file=sys.stderr, flush=True)
            out.append((futs[fu], fu.result()))
    return out


def run(asof=None, cutoff=dt.time(14, 0), limit=None, workers=8, volmode="prevday"):
    """判定して (一覧, 経過メモ, 失敗件数) を返す。

    2026-10-02 に判定の順番を変えた。以前は 日足 → 業績 → 出来高 の順で、
    300銘柄ぶんの業績を毎回取りに行っていた。出来高で先に十数銘柄まで絞れるので、
    日足 → 出来高 → 業績 に変更した（取得量が大幅に減り、速くなる）。

    2026-10-04 に業績での仕分けをやめ、1つの一覧にした。業績は絞り込みにも
    並べ替えにも使わず、銘柄ごとの目印（fund_ok）としてだけ付ける。
    """
    t0 = time.time()
    target = asof or dt.datetime.now(JST).date()
    errors = {"daily": 0, "fund": 0, "intraday": 0, "no_bar": 0, "universe": 0}
    notes = []

    names, asof_list = F.prime_list()
    if limit:
        names = names[:limit]
    errors["universe"] = len(names)
    notes.append(f"対象: 東証プライム（内国株式）{len(names)}銘柄 / JPX一覧 基準日 {asof_list}")

    # --- 段階A: 日足でトレンドと売買代金
    # 取得が続けて失敗したら、少し待ってからやり直す。1銘柄あたり「25秒待って
    # 4回やり直す」ので、Yahoo が止まったまま全銘柄を回ると何時間もかかり、
    # 定期タスクの時間切れで理由を残さず止められる（2026-10-09の見直しで判明）。
    # 続けて30回失敗したら、60秒・120秒・180秒と待ってからやり直す。それでも
    # 失敗が続けば、残りは取りに行かない（「取りに行っていない」として数える）。
    # 一度で諦めると、40秒ほどの一時的な不調で1日分を失っていた（同じ見直し）。
    brk = {"streak": 0, "until": 0.0, "trips": 0, "giveup": False}
    brk_lock = threading.Lock()

    deadline = t0 + STAGE_A_BUDGET

    def a(item):
        code, name = item
        with brk_lock:
            if time.time() > deadline:
                errors["stage_a_timeout"] = True
                return None, SKIPPED
            if brk["giveup"]:
                return None, SKIPPED
            wait = min(brk["until"], deadline) - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            res = stage_a(code, name, target)
        except Exception as e:                       # noqa: BLE001
            with brk_lock:
                errors.setdefault("first_error", f"{code}: {e}"[:200])
                brk["streak"] += 1
                if brk["streak"] >= DAILY_FAIL_STREAK:
                    brk["streak"] = 0
                    brk["trips"] += 1
                    if brk["trips"] > len(DAILY_COOLDOWNS):
                        brk["giveup"] = True
                    else:
                        brk["until"] = time.time() + DAILY_COOLDOWNS[brk["trips"] - 1]
            return None, f"ERR:{e}"
        with brk_lock:
            brk["streak"] = 0
        return res

    res_a = pmap(a, names, workers, "日足")
    passed_a = []
    for item, (row, why) in res_a:
        if row:
            passed_a.append(row)
        elif why == NO_BAR:
            errors["no_bar"] += 1
        elif why in DATA_MISSING or why in STAGE_A_DATA_MISSING:
            errors["no_bar"] += 1
        elif why == IMPLAUSIBLE:
            errors["implausible"] = errors.get("implausible", 0) + 1
        elif why == SKIPPED:
            errors["not_attempted"] = errors.get("not_attempted", 0) + 1
        elif why and why.startswith("ERR:"):
            errors["daily"] += 1
    notes.append(f"判定した日: {target}（日足の最終日がこの日でない銘柄は除外）")
    if errors.get("implausible"):
        notes.append(f"25日線からの乖離が現実的でない（-75%より深い・+200%より高い）"
                     f"ため除いた銘柄: {errors['implausible']}"
                     f"（分割が調整されていない疑い。本物の急騰のこともある）")

    # --- 配信の具合を測る（判定には使わない。daily_run が但し書きに使う）
    # 出来高が全体に過少な日や、配信が止まった日は、全銘柄が静かに
    # 「出来高倍率が2倍未満」で落ちて平常の「該当なし」に見える
    # （2026-10-05の見直しで判明）。段階Aを通った銘柄で中央値を取っておく。
    ratios = [r["vol_today_daily"] / r["vol_avg20"]
              for r in passed_a if r["vol_avg20"]]
    # 10銘柄あれば中央値を出す。30にしていたため、残りが少ない日は出来高の
    # 目安が黙って使われなかった（2026-10-12の見直し）。
    errors["vol_median"] = statistics.median(ratios) if len(ratios) >= 10 else None
    errors["vol_median_n"] = len(ratios)
    times = sorted(r["market_time"] for r in passed_a if r.get("market_time"))
    if not times and asof is None and time.time() < deadline:
        # 段階Aを通った銘柄が無いと時刻が取れず、「Yahooから時刻が取れない」と
        # 書いて配信の止まりも確かめていなかった（2026-10-11の見直しで判明）。
        # 売買の多い数銘柄で確かめる。過去日の再現では使わないので取りに行かない。
        # 1回・10秒だけ試す（取得が止まっている日に時間を使わないため）。
        for code in PROBE_CODES:
            try:
                d = json.loads(F.get_with_retry(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.T"
                    f"?range=5d&interval=1d", tries=1, timeout=10))
                mtp = d["chart"]["result"][0]["meta"].get("regularMarketTime")
            except Exception:                        # noqa: BLE001
                mtp = None
            if mtp:
                times.append(mtp)
        times.sort()
        errors["market_time_from_probe"] = bool(times)
        errors["market_time_probe_n"] = len(times)
        errors["market_time_probe_why"] = "no_survivors" if not passed_a else "no_time"
    errors["market_time"] = times[len(times) // 2] if times else None
    # 下位2割の時刻。一部の銘柄だけ配信が止まった日を見分ける（2026-10-09の見直し）。
    errors["market_time_p20"] = times[len(times) // 5] if times else None
    notes.append(f"下降トレンド＋売買代金で絞り込み後: {len(passed_a)}銘柄"
                 f"（取得失敗 {errors['daily']} / 当日の日足なし・履歴不足 {errors['no_bar']}"
                 + (f" / 取りに行っていない {errors['not_attempted']}"
                    if errors.get("not_attempted") else "") + "）")

    # --- 段階B: 出来高倍率
    def b(row):
        try:
            if volmode == "sametime":
                return stage_c(row, cutoff, asof)
            return stage_c_daily(row, cutoff, volmode)
        except Exception as e:                       # noqa: BLE001
            return None, f"ERR:{e}"

    res_b = pmap(b, passed_a, workers, "出来高")
    vol_ok = []
    for row, (out, why) in res_b:
        if out:
            vol_ok.append(out)
        elif why and (why.startswith("ERR:") or why in DATA_MISSING):
            errors["intraday"] += 1
    errors["stage_b"] = len(passed_a)
    notes.append(f"出来高倍率で絞り込み後: {len(vol_ok)}銘柄"
                 f"（データが無くて判定できず {errors['intraday']}銘柄"
                 f" / 段階Aを通ったのは {len(passed_a)}銘柄）")

    # --- 段階C: 業績で仕分ける（落とさない）
    # 株探も同じ。続けて届かなければ、残りはキャッシュだけで済ませる。
    fbrk = {"streak": 0}

    def c(row):
        if os.environ.get("VS_NO_FUNDAMENTALS") == "1":
            return {"error": "業績は取得しない（GitHub 版）"}
        with brk_lock:
            offline = (fbrk["streak"] >= FUND_FAIL_STREAK
                       or time.time() - t0 > FUND_BUDGET)
        try:
            f = F.fundamentals(row["code"], offline=offline)
        except Exception as e:                       # noqa: BLE001
            if not offline:
                with brk_lock:
                    fbrk["streak"] += 1
                # 取れなかったときも、前に取った数字があればそれを出す（古さを
                # 付ける）。止まる前に失敗した銘柄だけ「取得できませんでした」に
                # なっていた（2026-10-09の見直しで判明）。
                try:
                    return F.fundamentals(row["code"], offline=True)
                except Exception:                    # noqa: BLE001
                    pass
            return {"error": str(e)}
        if not offline:
            with brk_lock:
                fbrk["streak"] = 0
        return f

    for row in vol_ok:
        try:
            x = suspect_split(row["dates"], row["closes"], row["vols"])
            # 日付は文字列にしておく。date のままだと result.json に書けず、
            # 正しい結果が失敗メールに置き換わっていた（2026-10-09 に試して判明）。
            row["suspect_split"] = (x[0].isoformat(), x[1]) if x else None
        except Exception:                            # noqa: BLE001
            row["suspect_split"] = None

    res_c = pmap(c, vol_ok, max(4, workers // 2), "業績")
    rows = []
    for row, f in res_c:
        row = dict(row)
        if f.get("cache_age_days") is not None:
            # 前に取った数字で出したもの。成功に数えると、株探に届かなかった日でも
            # 「業績の取得失敗 0」と出ていた（2026-10-11の見直しで判明）。
            errors["fund_stale"] = errors.get("fund_stale", 0) + 1
            errors["fund_stale_max_days"] = max(errors.get("fund_stale_max_days", 0),
                                                f["cache_age_days"])
        if f.get("error"):
            errors["fund"] += 1
            row["fund"] = {}
            row["fund_ok"] = False
            row["fund_reason"] = "業績データを取得できませんでした"
        else:
            row["fund"] = f
            ok, why = fundamentals_ok(f)
            row["fund_ok"] = ok
            row["fund_reason"] = "" if ok else why
        rows.append(row)
    # カードの表示と同じ3つに分けて数える。判定できなかったものを
    # 「満たさない」に混ぜると、カードの「不明」と食い違う。
    n_ok = sum(1 for r in rows if r["fund_ok"])
    n_unknown = sum(1 for r in rows if not _fund_known(r.get("fund") or {}))
    n_ng = len(rows) - n_ok - n_unknown
    if os.environ.get("VS_NO_FUNDAMENTALS") == "1":
        notes.append("業績: GitHub からは株探（業績の出所）に届かないため、出していません")
    else:
        notes.append(f"うち業績の参考条件を満たすもの: {n_ok}銘柄"
                     f" / 満たさないもの: {n_ng}銘柄"
                     f" / 業績が取れず判定できないもの: {n_unknown}銘柄"
                     f"（業績の取得失敗 {errors['fund']}）")

    # 並び順は「25日線からの乖離が深い順」。2026-10-04 の過去検証で決めた。
    # 2022-01-31〜2026-09-30 の1,329,486銘柄日で、同じ条件の中では乖離が深いほど
    # 結果が良い傾向が段階的に出た（損益比、売買コスト0.2%控除後）。
    #   乖離の指定なし 1.49 / -5%より下 1.67 / -10%より下 2.14
    #   -15%より下 2.86 / -20%より下 3.60 / -5%以上（浅い）1.25
    # 乖離-10%より下は5年すべての年で1.0を超えた
    # （コスト後の損益比 2022:1.76 / 2023:1.12 / 2024:3.13 / 2025:3.67 / 2026:1.03）。なお下降×出来高2倍そのものの2026年は
    # コスト前0.97 / コスト後0.91。

    # 出来高倍率の大きい順に戻すときは key を -r["ratio"] にする。
    rows.sort(key=lambda r: r["dev25_pct"])
    notes.append("並び順: 25日線からの乖離が深い順（過去検証でこの順が最も差が出たため）")
    notes.append(f"所要時間: {time.time() - t0:.0f}秒")
    return rows, notes, errors


def fmt(rows, asof_date, cutoff, notes):
    def pct(v):
        return "—" if v is None else f"{v:+.1f}%"

    lines = []
    for i, r in enumerate(rows, 1):
        f = r.get("fund") or {}
        mark = ("参考条件を満たす" if r.get("fund_ok")
                else "参考条件は満たさない" if _fund_known(f)
                else "業績の数字が取れず判定できず")
        lines.append(
            f"{i}. {r['code']} {r['name']}  [{mark}]\n"
            f"   現在値 {r['price']:,.1f}円（前日比 {r['chg_pct']:+.1f}%）"
            f" / {asof_date} {cutoff.strftime('%H:%M')}時点\n"
            f"   出来高倍率 {r['ratio']:.2f}倍"
            f"（当日{r['vol_today']:,.0f}株 ÷ {r['base_label']}{r['vol_base']:,.0f}株）\n"
            f"   25日線 {r['ma25']:,.1f} / 75日線 {r['ma75']:,.1f}"
            f" → 現在値は25日線から {r['dev25_pct']:+.1f}%\n"
            f"   業績 売上高 {pct(f.get('rev_yoy'))} / 営業益 {pct(f.get('op_yoy'))}"
            f" ROE {'—' if f.get('roe') is None else format(f['roe'], '.1f') + '%'}"
            f" 自己資本比率 "
            f"{'—' if f.get('equity_ratio') is None else format(f['equity_ratio'], '.1f') + '%'}\n"
            f"   20日平均売買代金 {r['turnover20'] / 1e8:,.1f}億円")
    return "\n".join(lines) if lines else "（該当なし）"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", help="YYYY-MM-DD。過去の営業日を再現する")
    ap.add_argument("--cutoff", default="14:00")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--volmode", default="prevday",
                    choices=["prevday", "avg20", "sametime"],
                    help="出来高の比較対象: 前日1日 / 過去20日平均 / 前日同時刻(分足)")
    ap.add_argument("--out")
    a = ap.parse_args()

    asof = dt.date.fromisoformat(a.asof) if a.asof else None
    hh, mm = (int(x) for x in a.cutoff.split(":"))
    cutoff = dt.time(hh, mm)

    target = asof or dt.datetime.now(JST).date()
    ok, why = F.is_business_day(target)
    if not ok:
        print(f"{target} は休場（{why}）です。", file=sys.stderr)
        if not asof:
            sys.exit(2)

    rows, notes, errors = run(asof, cutoff, a.limit, a.workers, a.volmode)
    print("\n".join("# " + n for n in notes))
    print()
    print(fmt(rows, target, cutoff, notes))

    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"date": str(target), "cutoff": a.cutoff,
                       "notes": notes, "errors": errors,
                       "rows": [{k: (str(v) if isinstance(v, dt.date) else v)
                                 for k, v in h.items()
                                 if k not in ("dates", "closes", "vols", "ohlc")}
                                for h in rows]},
                      f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
