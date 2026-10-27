#!/usr/bin/env python3
"""毎営業日14:00に動かす本体。メールの件名と本文をファイルに書き出す。

出力（out/ 以下）:
  status.txt   send / skip のどちらか。skip のときはメールを送らない
  subject.txt  メールの件名
  body.html    メール本文（HTML）
  body.txt     メール本文（プレーンテキスト）
  result.json  判定結果の生データ

使い方:
  python3 daily_run.py                    # 今日の14:00時点として実行
  python3 daily_run.py --asof 2026-09-18  # 過去の営業日を再現（動作確認用）
                                          # 出力は out_asof/ に書く（out/ は本番専用）
  SCREENER_OUT=/tmp/x python3 daily_run.py  # 本番と同じ動きを別の場所に書く（確認用）
"""

import argparse
import datetime as dt
import fcntl
import html
import json
import os
import sys
import traceback

# matplotlib は ~/.matplotlib に書けないと、毎回フォントの一覧を作り直す
# （定期タスクの実行記録に毎回「building the font cache」が出ていた）。
# 書ける場所をプロジェクトの中に用意する。chart を読み込む前に決めること。
os.environ.setdefault("MPLCONFIGDIR", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cache", "mpl"))

# 部品の読み込みに失敗しても、ここで止まらない。止まると out/ の前回の
# status.txt=send が残り、定期タスクが前日のメールを今日のものとして送る
# （2026-10-09の見直しで実測。matplotlib が読めないだけで起きた）。
# 読めなかったときは main() が理由付きの失敗通知を書く。
# chart（matplotlib）はメールに使っていない画像を作るだけなので、
# 使う場所で読む（読めなくても判定とメールは出す）。
try:
    import fetchlib as F
except Exception:                                     # noqa: BLE001
    F = report = S = None
    _IMPORT_ERROR = traceback.format_exc()
else:
    # fetchlib が読めれば、休場日の判定には使える（読めなかった日でも
    # 休場日に失敗メールを送らないため。2026-10-09の見直し）。
    try:
        import report
        import screener as S
        _IMPORT_ERROR = None
    except Exception:                                 # noqa: BLE001
        report = S = None
        _IMPORT_ERROR = traceback.format_exc()

# 出来高を何と比べたかの表示名。0件の日は結果から取れないのでここで持つ。
BASE_LABELS = {"prevday": "前日1日", "avg20": "過去20日平均",
               "sametime": "前日の同時刻まで"}

BASE = os.path.dirname(os.path.abspath(__file__))
# 本番の出力先。定期タスクはここだけを読む。
# 確かめるための実行（--asof や SCREENER_OUT 指定）は別の場所に書く。
# 同じ場所に書くと、14:10の本番と重なったときに本番の結果や鍵を壊す。
OUT = os.environ.get("SCREENER_OUT") or os.path.join(BASE, "out")
OUT_ASOF = os.path.join(BASE, "out_asof")
JST = dt.timezone(dt.timedelta(hours=9))     # fetchlib が読めないときにも使う

# 配信の具合の目安（2026-10-06に実測した値から決めた）。
# 当日÷過去20日平均の出来高の中央値（全銘柄）は、2026-10-02 の10:25ごろの
# 配信で0.25倍、2026-10-06 の12:32ごろ（昼休み）の配信で0.32倍だった。
# 段階Aを通った銘柄だけで取ると、同じ10-06 12:32で0.31倍、14:10ごろの配信
# （14:25に実行）で0.45倍（299銘柄）。
# 5年分の大引け後の値（全銘柄）は下位1%で0.56倍。14時台にこれらを大きく
# 下回るなら、配信が途中までしか来ていない可能性が高い。
# 14:10ごろの値は1日分（0.45倍）しか測れていない。普段の幅は、
# out/history に残す data_time と vol_median がたまれば測れる。
VOL_MEDIAN_FLOOR = 0.2
VOL_MEDIAN_FLOOR_CLOSE = 0.3   # 1日分（大引け後・過去日の再現）のとき
# 下降トレンドの条件を満たす銘柄の数の下限の目安（5年の普段の日の最少は45件）
STAGE_B_FLOOR = 30
# Yahoo の配信は実行時刻より遅れる。2026-10-06 12:47 の取得で、
# 1,556銘柄の最終約定時刻は中央値15.6分（上位10%で17.5分）遅れていた。
FEED_LAG_LIMIT_MIN = 40


def write(name, text):
    """書きかけのファイルを残さない。直接書くと、途中で落ちたときに
    半分だけのメール本文が「送ってよい状態」として残る。"""
    os.makedirs(OUT, exist_ok=True)
    fp = os.path.join(OUT, name)
    tmp = f"{fp}.{os.getpid()}.new"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, fp)


def _expected_data_time(now):
    """その時刻なら、Yahoo の配信でどこまでの数字が来ているはずか（目安）。

    普段の遅れは15分ほど（2026-10-06 12:47 の取得で中央値15.6分）。立会の外は
    最後の立会の終わりまで。寄り付き直後はまだ判定しない（None）。
    """
    t = now - dt.timedelta(minutes=15)
    day = now.date()

    def at(h, m):
        return dt.datetime.combine(day, dt.time(h, m), JST)
    if t < at(9, 5):
        return None
    if t <= at(11, 30):
        return t
    if t < at(12, 35):
        # 後場の寄り（12:30）の約定は15〜16分遅れて届く。12:45ちょうどの実行で
        # 「12:30まで来ているはず」として、止まっていると誤って警告した
        # （2026-10-12の見直し）。前場の寄りと同じく5分の余裕を見る。
        return at(11, 30)
    if t <= at(15, 30):
        return t
    return at(15, 30)


def _stale_feed(now, mtd):
    """配信が止まっていそうなら、その説明。普段どおりなら空。

    以前は立会中（〜15:30）しか見ておらず、引け後の遅れ実行では、11:30で止まった
    配信でも警告を出さずに「確定値です」と書いていた（2026-10-09の見直しで実測）。
    """
    exp = _expected_data_time(now)
    if exp is None:
        return ""
    when = mtd.strftime("%H:%M") if mtd.date() == now.date() else mtd.strftime("%m-%d %H:%M")
    if mtd.date() != now.date() or (exp - mtd).total_seconds() / 60 > FEED_LAG_LIMIT_MIN - 15:
        return (f"Yahooの株価の配信が{when}で止まっているようです"
                f"（{now.strftime('%H:%M')}の実行なら{exp.strftime('%H:%M')}ごろまでは"
                f"来ているはずです。普段の遅れは15分ほど）")
    return ""


def _missing_history_days(today, limit=5):
    """最後に記録（out/history）が残った日より後で、記録の無い営業日。
    (新しい順に最大 limit 日を古い順に並べたもの, それより前に抜けている日数) を返す。

    最後の記録より後だけを見る。記録を残し始めた日から全部を見ていたので、
    1日抜けると、その後のメールに毎日ずっと出続けた。記録が1つも無い
    （フォルダが無い・壊れている）ときは前営業日を名指しする（2026-10-12の見直し）。"""
    hist = os.path.join(OUT, "history")
    try:
        names = os.listdir(hist)
    except OSError:
        names = []
    days = []
    for f in names:
        try:
            d = dt.date.fromisoformat(f[:10])
        except ValueError:
            continue
        if f.endswith(".json") and d < today and _history_of(f[:10]):
            days.append(d)
    if not days:
        prev = _previous_business_day(today)
        return ([prev] if prev else []), 0
    out, d = [], today - dt.timedelta(days=1)
    while d > max(days):
        try:
            if F.is_business_day(d)[0]:
                out.append(d)
        except Exception:                             # noqa: BLE001
            break
        d -= dt.timedelta(days=1)
    return sorted(out[:limit]), max(0, len(out) - limit)


def _previous_business_day(day):
    """day より前の、直近の立会日。判定できなければ None。"""
    d = day - dt.timedelta(days=1)
    for _ in range(10):
        try:
            if F.is_business_day(d)[0]:
                return d
        except Exception:                             # noqa: BLE001
            return None
        d -= dt.timedelta(days=1)
    return None


def _history_of(day):
    """その日の本番の記録（out/history/日付.json）。無いか壊れていれば None。"""
    fp = os.path.join(OUT, "history", f"{day}.json")
    try:
        with open(fp, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    # dict でない JSON（[1] など）を記録として返すと、立会前の経路が
    # rec.get で落ち、何も書かずに終わった（2026-10-12の見直し）。
    return rec if isinstance(rec, dict) else None


def _early_reason(now, scheduled):
    """立会前に動いたときの理由。失われた営業日を名指しする。

    以前は「次の営業日の14:10に再試行されます」と書いていたが、
    眠っていて飛ばした日はもう取り戻せない。戻らないことを書く。
    前営業日の記録があれば、失われたとは書かない。記録を見ずに
    「実行されていない」と断定していた（2026-10-05の見直しで判明）。
    """
    missed = []
    d = now.date() - dt.timedelta(days=1)
    for _ in range(7):
        try:
            ok, _why = F.is_business_day(d)
        except Exception:                             # noqa: BLE001
            break
        if not ok:
            d -= dt.timedelta(days=1)
            continue
        missed.append(d)
        break
    lost = ""
    if missed:
        rec = _history_of(missed[0])
        if rec:
            lost = (f"{missed[0]} のぶんは {rec.get('captured_at', '時刻不明')} に"
                    f"実行済みです。")
        else:
            lost = (f"{missed[0]} の{scheduled}ぶんは結果の記録がありません"
                    f"（実行されなかったか、途中で失敗した可能性があります）。"
                    f"さかのぼって出すことはできません。")
    why_no_price = ("この時刻はまだ東証が開いていない（前場は9:00開始）ため、当日の株価が存在せず"
                    if now.time() < dt.time(9, 0) else
                    "寄り付き直後で、当日の株価がまだ配信されていない（配信は15分ほど遅れる）ため")
    return (f"予定の{scheduled}に実行できず、{now.strftime('%H:%M')}に動きました。"
            f"{why_no_price}、判定していません。\n"
            f"{lost}\n"
            + (f"考えられる原因: 中継が予定の時刻に起こせず、予備の定期実行が遅れて届いた。"
               f"本日の{scheduled}には自動で動きます。" if os.environ.get("GITHUB_ACTIONS") == "true" else
               f"考えられる原因: 予定の時刻にMacがスリープしていたか、"
               f"アプリが終了していた。本日の{scheduled}には自動で動きます。"))


def _send_status(asof):
    """過去日の再現は確認用。どの経路でも送信を有効にしない。
    失敗経路だけ send が残っていると、検証のたびに失敗メールが送られる。"""
    return "skip" if asof else "send"


_LOCK_FD = None


def _lock():
    """同じ時刻に2つ走って出力を壊し合わないようにする。
    すでに走っていれば False を返す。

    OSのファイルロック（flock）を使う。以前は「ファイルがあるか見てから
    書く」だったので、同時に呼ぶと両方が鍵を取れた。また中のpidを見ずに
    1時間で古いとみなしたので、生きている実行から鍵を奪えた
    （どちらも2026-10-05の見直しで実測）。flock は取得が一度で済み、
    プロセスが落ちればOSが外すので、古い鍵が残ることもない。
    """
    global _LOCK_FD
    os.makedirs(OUT, exist_ok=True)
    fd = os.open(os.path.join(OUT, "running.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()} {dt.datetime.now(JST).isoformat()}".encode())
    _LOCK_FD = fd
    return True


def _lock_owner():
    """鍵を持っている実行の「pid 開始時刻」。読めなければ空。"""
    try:
        with open(os.path.join(OUT, "running.lock"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _written_by_holder(name, owner):
    """out/name が、鍵を持っている実行が始まった後に書かれたものか。"""
    try:
        start = dt.datetime.fromisoformat(owner.split()[1]).timestamp()
        return os.path.getmtime(os.path.join(OUT, name)) >= start
    except (IndexError, ValueError, OSError):
        return False


def _unlock():
    """鍵ファイルは消さない。消すと、待っている側と新しく来た側が
    別々のファイルを掴んで両方とも鍵を取れてしまう。"""
    global _LOCK_FD
    if _LOCK_FD is not None:
        try:
            fcntl.flock(_LOCK_FD, fcntl.LOCK_UN)
        finally:
            os.close(_LOCK_FD)
            _LOCK_FD = None


def _drop(name):
    """出力を1つ消す。消せなくても止まらない。"""
    try:
        os.remove(os.path.join(OUT, name))
    except OSError:
        pass


def clear_outputs():
    """前回の出力を消す。失敗した日に前回の一覧や画像が残っていると、
    古い結果を今日のものと取り違える（2026-10-04の見直しで判明）。"""
    os.makedirs(OUT, exist_ok=True)
    # status.txt を真っ先に消す。途中で失敗しても「送ってよい」だけが
    # 前回のまま残らないようにするため（定期タスクはこれを見て送る）。
    for name in ("status.txt", "result.json", "attachments.json", "error.txt",
                 "subject.txt", "body.html", "body.txt"):
        fp = os.path.join(OUT, name)
        if os.path.isfile(fp):
            os.remove(fp)
    cdir = os.path.join(OUT, "charts")
    if os.path.isdir(cdir):
        for old in os.listdir(cdir):
            fp = os.path.join(cdir, old)
            if os.path.isfile(fp):
                os.remove(fp)


def _import_failure():
    """部品が読めなかったときの失敗通知。report も使えないことがあるので手で書く。"""
    global OUT
    # 引数は本体と同じ argparse で読む。"--asof" の完全一致だけを見ていたので、
    # "--asof=日付" や "--aso 日付" の確認用の実行が本番の out/ に送信の印付きで
    # 書いていた（2026-10-09の見直しで実測）。
    pa = argparse.ArgumentParser(add_help=False)
    pa.add_argument("--asof")
    try:
        asof = bool(pa.parse_known_args()[0].asof)
    except SystemExit:
        asof = any(x.startswith("--as") for x in sys.argv[1:])
    if asof and not os.environ.get("SCREENER_OUT"):
        OUT = OUT_ASOF
    _drop("status.txt")
    _drop("error.txt")
    now = dt.datetime.now(JST)
    if not asof and F is not None:
        try:
            ok, why = F.is_business_day(now.date())
        except Exception:                             # noqa: BLE001
            ok, why = True, ""
        if not ok:
            # 休場日は送らない決まり。部品が読めない日でも同じ。
            write("subject.txt", f"休場 {now:%Y-%m-%d}（{why}）のため送信なし")
            write("status.txt", "skip")
            print(f"{now:%Y-%m-%d} は休場（{why}）。部品の読み込みにも失敗しています。",
                  file=sys.stderr)
            return 0
    last = (_IMPORT_ERROR or "").strip().splitlines()[-1:] or ["不明"]
    reason = f"プログラムの部品を読み込めませんでした（{last[0]}）。判定していないため、結果は出していません。"
    try:
        write("subject.txt", f"【スクリーニング失敗】{now:%Y-%m-%d %H:%M}時点")
        write("body.txt", f"本日のスクリーニングは完了できませんでした。\n理由: {reason}\n")
        write("body.html", '<!doctype html><html><head><meta charset="utf-8"></head><body>'
              f'<p><b>本日のスクリーニングは完了できませんでした。</b><br>理由: {html.escape(reason)}</p>'
              '</body></html>')
        write("error.txt", _IMPORT_ERROR or "")
        write("status.txt", "skip" if asof else "send")   # 必ず最後
    except Exception as e:                            # noqa: BLE001
        print(f"FAILURE: 失敗通知も書き出せません: {e}", file=sys.stderr)
    print("FAILURE: " + reason, file=sys.stderr)
    return 1


def main():
    """鍵を取れた実行だけが鍵を外す。取れなかった側が外すと、
    走っている方の鍵を消して重なりを許してしまう。"""
    if _IMPORT_ERROR:
        return _import_failure()
    got = [False]
    try:
        return _main(got)
    finally:
        if got[0]:
            _unlock()


def _main(got):
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", help="YYYY-MM-DD。過去の営業日を再現する")
    ap.add_argument("--cutoff", default="14:00")
    ap.add_argument("--volmode", default="avg20",
                    choices=["prevday", "avg20", "sametime"])
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    global OUT
    if a.asof and not os.environ.get("SCREENER_OUT"):
        # 過去日の再現は本番の out/ に一切書かない（鍵も含めて）。
        OUT = OUT_ASOF

    # 前回の出力の消去と引数の解釈をここで失敗すると、out/ に前回のメールが
    # そのまま残り、翌日それが送られる。必ず失敗通知を出せる形にしておく。
    now = dt.datetime.now(JST)
    try:
        # 引数の解釈を先に済ませる。あとにすると、解釈に失敗したときに
        # 前回の出力を消したまま終わり、何も送られない。
        asof = dt.date.fromisoformat(a.asof) if a.asof else None
        hh, mm = (int(x) for x in a.cutoff.split(":"))
        cutoff = dt.time(hh, mm)
        if not _lock():
            # 鍵を持っている実行は、始めに前回の出力を消している。
            # ここでは status.txt を消し、本当の理由を error.txt に残す。
            # 定期タスクは status.txt が無ければ error.txt を失敗理由として送る。
            # 以前は error.txt を残したままにしていたので、過去の別の日の
            # 失敗理由が今日の理由として送られた（2026-10-05の見直しで実測）。
            owner = _lock_owner()
            msg = (f"前の実行がまだ終わっていないため、今回は判定していません"
                   f"（実行中: {owner or '不明'}）。")
            # 休場日なら何も書き換えない。鍵の持ち主が skip を書く。ここで
            # status.txt を消すと、休場日に失敗メールが送られていた
            # （2026-10-11の見直しで判明）。
            try:
                hol = (asof is None) and not F.is_business_day(now.date())[0]
            except Exception:                         # noqa: BLE001
                hol = False
            if hol:
                # 持ち主も同じ内容（休場の skip）を書く。書いておかないと、持ち主が
                # 書き終える前に定期タスクが status.txt の無い状態を見て、休場日に
                # 失敗メールを送る（2026-10-11の見直し）。
                why_hol = F.is_business_day(now.date())[1]
                write("subject.txt", f"休場 {now:%Y-%m-%d}（{why_hol}）のため送信なし")
                write("status.txt", "skip")
                print(msg + "（休場日なので休場の印だけ書きます）", file=sys.stderr)
                return 0
            # 持ち主が書き終えた status.txt は消さない。持ち主は status.txt を
            # 書いてから鍵を外すまでに記録を書くので、その間に来ると、完了した
            # 当日の結果を消して失敗メールにしていた（2026-10-09の見直しで実測）。
            if not _written_by_holder("status.txt", owner):
                _drop("status.txt")
                write("error.txt", msg + "\n")
            print(msg, file=sys.stderr)
            return 1
        got[0] = True
        clear_outputs()
    except Exception as e:                            # noqa: BLE001
        # 前回の status.txt=send が残ったままだと、前日のメールが今日のものとして
        # 送られる。何より先に消す（2026-10-05の見直しで実測）。
        # 消すことすらできない（out/ に書けない）ときは防げない。
        # error.txt も消す。残すと定期タスクが前の失敗日の理由を今日の理由として
        # 送る（2026-10-09の見直しで判明）。
        _drop("status.txt")
        _drop("error.txt")
        try:
            os.makedirs(OUT, exist_ok=True)
            label = now.strftime("%Y-%m-%d")
            s_, h_, t_ = report.build([], [], label, now.strftime("%H:%M"),
                                      failure=f"実行の準備に失敗しました: {e}")
            write("subject.txt", s_), write("body.html", h_), write("body.txt", t_)
            write("status.txt", _send_status(a.asof))      # 必ず最後
        except Exception as e2:                       # noqa: BLE001
            print(f"FAILURE: 準備 {e} / 失敗通知も書き出せません: {e2}",
                  file=sys.stderr)
            return 1
        print(f"FAILURE: 準備 {e}", file=sys.stderr)
        return 1
    target = asof or now.date()
    date_label = target.strftime("%Y-%m-%d")
    # 本番は「いま実際に取ったデータ」なので、名目の時刻ではなく実行時刻を出す。
    # 過去日の再現では、使っているのはその日の終値と1日分の出来高であって
    # 指定時刻の断面ではない。時刻を書くと「その時刻の数字」と誤解されるので、
    # 終値ベースだと分かる書き方にする（2026-10-04の見直しで判明）。
    # sametime は5分足で cutoff 時点の値段と出来高を出すモードなので、
    # 「終値」と書くと中身と食い違う（2026-10-04の見直しで判明）。
    uses_close = a.volmode != "sametime"
    cutoff_label = (("終値" if uses_close else a.cutoff) if asof
                    else (now.strftime("%H:%M") if uses_close else a.cutoff))

    # --- 休場日なら何もしない
    try:
        ok, why = F.is_business_day(target)
    except Exception as e:                            # noqa: BLE001
        s, h, t = report.build([], [], date_label, cutoff_label,
                               failure=f"営業日の判定に失敗しました: {e}")
        write("subject.txt", s), write("body.html", h), write("body.txt", t)
        write("status.txt", _send_status(a.asof))      # 必ず最後
        print("FAILURE: 営業日判定", file=sys.stderr)
        return 1

    if not ok and asof is None:
        # 件名に日付を入れる。定期タスクは skip でも日付を確かめる。前の休場日の
        # skip が残ったまま今日の実行が動けないと、何も送られず「休場」と
        # 報告されていた（2026-10-09の見直しで判明）。
        _drop("status.txt")
        write("subject.txt", f"休場 {date_label}（{why}）のため送信なし")
        write("status.txt", "skip")
        print(f"{date_label} は休場（{why}）。メールは送りません。")
        return 0

    # --- 予定時刻に動けなかった場合の扱い
    # 定期実行はMacが起きていてアプリが開いている時間しか動かない。
    # スリープしていると次に起動したときに遅れて走る（2026-09-29 04:40 に実際に発生）。
    # 立会前に走っても当日の日足は存在しないので、スキャンする前に理由を書いて返す。
    # GitHub 版（2026-10-16 から本番）は中継が 14:05 に起こす。Mac のスリープは原因にならない
    ON_GITHUB = os.environ.get("GITHUB_ACTIONS") == "true"
    SCHEDULED = "14:05" if ON_GITHUB else "14:10"
    late_cause = ("中継が予定の時刻に起こせず、予備の定期実行で動いた可能性" if ON_GITHUB
                  else f"{SCHEDULED}時点でMacがスリープしていた可能性")
    late_note = ""
    early_note = ""
    if asof is None:
        t = now.time()
        # 当日の足は寄り付き（9:00）から15分ほど遅れて届く。09:05で区切っていたので、
        # 09:05〜09:20に起きた回は「日足がまだ配信されていません」の失敗メールになり、
        # 休場日の枠の取り戻しでも送っていた（2026-10-12の見直し）。
        # _expected_data_time と同じく 09:20 まで待つ。
        if t < dt.time(9, 20):
            # 立会前の実行は、前の平日の14:10の枠の取り戻し。その平日が休場日なら
            # 取りこぼしは無いので送らない。休場日の分まで失敗メールにしていた
            # （2026-10-12の見直しで判明）。
            slot = now.date() - dt.timedelta(days=1)
            while slot.weekday() >= 5:
                slot -= dt.timedelta(days=1)
            try:
                slot_open = F.is_business_day(slot)[0]
            except Exception:                         # noqa: BLE001
                slot_open = True
            if not slot_open:
                write("subject.txt", f"{date_label} {now.strftime('%H:%M')} 立会前の実行"
                                     f"（取り戻す枠の {slot} は休場のため送信なし）")
                write("status.txt", "skip")
                print(f"SKIP-EARLY: {slot} は休場の枠", file=sys.stderr)
                return 0
            s, h, tx = report.build(
                [], [], date_label, cutoff_label,
                failure=_early_reason(now, SCHEDULED))
            write("subject.txt", s), write("body.html", h), write("body.txt", tx)
            write("status.txt", _send_status(a.asof))  # 必ず最後
            print(f"SKIP-EARLY: {now.strftime('%H:%M')} は立会前", file=sys.stderr)
            return 1
        if t > dt.time(15, 30):
            # 「確定値」と言えるかは数字の時刻を見てから決める（本体の後で足す）。
            late_note = (f"※ 予定の{SCHEDULED}に実行できず、{now.strftime('%H:%M')}に動きました"
                         f"（{late_cause}）。")
        elif t > dt.time(14, 40):
            late_note = (f"※ 予定の{SCHEDULED}より遅れて{now.strftime('%H:%M')}に実行しました。")
        elif t < dt.time(13, 55):
            early_note = (f"予定の{SCHEDULED}より早い{now.strftime('%H:%M')}に実行したため、"
                          f"当日の出来高がまだ積み上がっていません。"
                          f"{BASE_LABELS.get(a.volmode, '基準')}と比べる出来高倍率は"
                          f"実際より小さく出ています")

    # --- 本体
    try:
        # 株価の取得先に届くかを先に1回だけ確かめる。届かないまま始めると、
        # 1,556銘柄それぞれが「25秒待って4回やり直す」ので何十分も終わらず、
        # 定期タスクの時間切れで止められて理由が残らない。
        # 2026-10-02 と 10-06 の14:10の定期実行がこの形だった（進捗が1行も
        # 出ないまま止まった）。定期タスクの中では Yahoo への接続が
        # 「Connection timed out」になっており、対話中の実行では0.2秒で
        # 返っていたので、定期タスク側のネットワークの許可が原因と見ている（推定）。
        # 2回・1.2秒あけるだけだと、一瞬の不調でも1日まるごと失敗にしていた
        # （2026-10-09の見直し）。3回・5秒と10秒では、すぐ失敗する場合
        # （Mac の起動直後で名前が引けない等）は15秒であきらめていた
        # （2026-10-12の見直し。2026-09-29 04:40 に起動直後の失敗の実例がある）。
        # 4回・10秒、20秒、30秒あける（すぐ失敗なら約1分、毎回待たされると約2分）。
        try:
            F.get_with_retry("https://query1.finance.yahoo.com/v8/finance/chart/"
                             "7203.T?range=5d&interval=1d", tries=4, timeout=15,
                             pause=10.0)
        except Exception as e:                        # noqa: BLE001
            msg = str(e)
            if msg.startswith("curl exit 22"):
                why = "Yahoo Finance がエラーを返しました（つながってはいます）"
            elif msg.startswith(("curl exit 6:", "curl exit 7:", "curl exit 28:",
                                 "curl exit 35:", "curl exit 56:")):
                why = ("株価の取得先（Yahoo Finance）に接続できません。定期タスクから"
                       "動かしたときだけ起きる場合は、その環境で "
                       "query1.finance.yahoo.com への接続が許可されていない可能性があります")
            else:
                why = "株価の取得先（Yahoo Finance）から取得できません"
            raise RuntimeError(f"{why}（{msg.strip()}）。判定していないため、結果は出していません。")

        rows, notes, errors = S.run(asof, cutoff, None, a.workers, a.volmode)

        # --- 取得がほぼ全滅したときに「該当なし」で送らない
        # 該当0件と、データが取れていない0件は意味が正反対なので必ず区別する。
        universe = errors.get("universe") or 0
        no_bar = errors.get("no_bar", 0)
        failed = errors.get("daily", 0)
        skipped = errors.get("not_attempted", 0)
        bad_price = errors.get("implausible", 0)
        intraday = errors.get("intraday", 0)
        stage_b = errors.get("stage_b", 0)
        # 判定に使えなかった銘柄。値段が現実的でないものも数える。数えないと、
        # 壊れた値段が大量に来た日が平常の「該当なし」になる（2026-10-09の見直しで実測）。
        lost = no_bar + failed + skipped + bad_price
        lost_detail = (f"当日の足なし・履歴不足 {no_bar}件 / 取得失敗 {failed}件"
                       + (f" / {'時間切れで' if errors.get('stage_a_timeout') else ''}"
                          f"取りに行っていない {skipped}件" if skipped else "")
                       + (f" / 値が現実的でない {bad_price}件" if bad_price else ""))

        # 銘柄一覧そのものが壊れていると、0銘柄を調べて「該当なし」になる。
        # 以前は universe を 1 に読み替えていたので割合の検査も素通りしていた。
        if universe < 1300:
            raise RuntimeError(
                f"銘柄一覧が{universe}件しか取れていません"
                f"（東証プライムの内国株式は通常1,500件以上）。"
                f"判定していないため、結果は出していません。")
        if no_bar >= universe * 0.5:
            raise RuntimeError(
                f"{date_label} の日足がまだ配信されていません"
                f"（{universe}銘柄中 {no_bar}銘柄で当日の足を取得できず）。"
                f"判定していないため、結果は出していません。")
        if failed + skipped >= universe * 0.3:
            # 取りに行かなかった銘柄を「失敗」に数えて全銘柄失敗と書き、最初の
            # 失敗の理由も落としていた（2026-10-09の見直しで判明）。
            first = errors.get("first_error")
            why_skip = ("時間内に取り終わらなかったため" if errors.get("stage_a_timeout")
                        else "続けて失敗したため")
            raise RuntimeError(
                f"株価データを取得できませんでした（{universe}銘柄中、取得に失敗 {failed}銘柄"
                + (f"・{why_skip}取りに行っていない {skipped}銘柄" if skipped else "")
                + "）。" + (f"最初の失敗: {first}。" if first else "")
                + "結果が信用できないため、一覧は出していません。")

        # 上の2つの線に届かない欠損は、これまで「処理の内訳」の奥に書くだけだった。
        # 2026-10-04 に実際に試したところ、全体の49%が欠けていても件名は
        # 「該当なし」で、見た目は平常どおりだった。欠けた日を平常の日と
        # 区別できないのは、この仕組みでいちばん避けたいことなので、
        # 5%を超えたら本文の先頭と件名に出す。
        # 出来高段階でデータが無くて判定できなかったぶんも守る。
        # 5分足が全滅した日が「該当なし」で送られていた。
        if stage_b and intraday >= stage_b * 0.5:
            raise RuntimeError(
                f"出来高の判定ができませんでした"
                f"（下降トレンドで残った{stage_b}銘柄のうち{intraday}銘柄で"
                f"比較に使うデータが無い）。"
                f"判定していないため、結果は出していません。")

        if lost >= universe * 0.5:
            raise RuntimeError(
                f"株価データの欠損が多すぎます"
                f"（{universe}銘柄中 {lost}銘柄。{lost_detail}）。"
                f"判定していないため、結果は出していません。")

        # 段階Aを1銘柄も通らない日は、条件に合わなかったのではなく
        # データが取れていない可能性が高い。
        if stage_b == 0 and lost < universe * 0.05:
            raise RuntimeError(
                f"下降トレンドの条件を調べられた銘柄が0件でした"
                f"（{universe}銘柄を読み込み、判定に使えなかったのは{lost}銘柄）。"
                f"データがそろっていない可能性が高いため、結果は出していません。")

        # --- 配信の具合（2026-10-05の見直しで、出来高が全体に過少な日が
        # 平常の「該当なし」として送られることが分かった）
        # 欠けていると分かっているもの（parts）、目安から疑っているもの
        # （suspect）、今日のデータとは別のお知らせ（notices）を分ける。
        # 目安だけで「欠けています」と断定していた（2026-10-09の見直しで判明）。
        feed_note = ""
        vol_med = errors.get("vol_median")
        mt = errors.get("market_time")
        data_time = None
        parts, suspect, notices = [], [], []
        if asof is None:
            if mt:
                mtd = dt.datetime.fromtimestamp(mt, JST)
                if mtd.date() == now.date():
                    data_time = mtd
                    if uses_close:
                        # 「実行時刻」ではなく、実際に数字が取れている時刻を出す。
                        cutoff_label = mtd.strftime("%H:%M")
                    # 原因（配信の遅れ・昼休み・引け後）は書かない。時刻だけを書く。
                    # 引け後にも「配信の遅れのため」と書いていた（2026-10-09の見直し）。
                    if errors.get("market_time_from_probe"):
                        why_probe = ("下降トレンドで残った銘柄が無かったため"
                                     if errors.get("market_time_probe_why") == "no_survivors"
                                     else "残った銘柄の時刻が取れなかったため")
                        how = (f"売買の多い銘柄のうち時刻が取れた"
                               f"{errors.get('market_time_probe_n', 0)}銘柄の最終約定時刻。"
                               f"{why_probe}")
                    else:
                        how = "銘柄ごとの最終約定時刻の中央値"
                    if uses_close:
                        feed_note = (f"実行時刻は{now.strftime('%H:%M')}。株価と出来高は"
                                     f"{mtd.strftime('%H:%M')}ごろまでのものです（{how}）。")
                    else:
                        # 同時刻比較（sametime）は cutoff までの5分足の断面を使う。
                        # 最終約定時刻で言い換えていた（2026-10-12の見直し）。
                        feed_note = (f"実行時刻は{now.strftime('%H:%M')}。株価と出来高は"
                                     f"{a.cutoff}までの5分足から出しています（Yahooの"
                                     f"最終約定時刻は{mtd.strftime('%H:%M')}ごろ。{how}）。")
                else:
                    feed_note = (f"Yahooの最終約定時刻（{mtd.strftime('%m-%d %H:%M')}）が当日の"
                                 f"ものではないため、数字がいつの時点のものかは確かめられていません"
                                 + ("（件名の時刻は実行時刻です）。" if uses_close else "。"))
                stale = _stale_feed(now, mtd)
                if stale:
                    suspect.append(stale)
                # 中央値だけだと、一部の銘柄だけ止まった配信を見逃す（2026-10-09の
                # 見直しで、45%の銘柄を11:30で止めても警告が出ないと実測）。
                # 下位2割の銘柄の時刻も見る。
                p20 = errors.get("market_time_p20")
                if p20 and not stale:
                    stale20 = _stale_feed(now, dt.datetime.fromtimestamp(p20, JST))
                    if stale20:
                        # 試し取得（最大5銘柄）の時刻なら、全体の2割とは言えない
                        # （2026-10-12の見直し）。
                        suspect.append(
                            (f"時刻が取れた{errors.get('market_time_probe_n', 0)}銘柄の一部で、"
                             if errors.get("market_time_from_probe") else "2割以上の銘柄で、")
                            + stale20)
            else:
                feed_note = ("Yahooから株価の時刻が取れなかったため、数字がいつの時点のものかは"
                             "確かめられていません"
                             + ("（件名の時刻は実行時刻です）。" if uses_close else "。"))
                suspect.append("Yahooから株価の時刻が取れず、配信が止まっていないか"
                               "確かめられません")
            if late_note and not uses_close:
                late_note += f"数字は{a.cutoff}までの5分足から出しています。"
            elif late_note:
                p20 = errors.get("market_time_p20")
                p20_close = bool(p20) and (
                    dt.datetime.fromtimestamp(p20, JST).time() >= dt.time(15, 30))
                if data_time is None:
                    late_note += "数字がいつの時点のものかは確かめられていません。"
                elif data_time.time() >= dt.time(15, 30) and p20_close:
                    late_note += "数字は大引け（15:30）の値です。"
                elif data_time.time() >= dt.time(15, 30):
                    # 中央値だけで「大引けの値です」と書いていた（2026-10-11の見直し）
                    late_note += ("多くの銘柄の数字は大引け（15:30）の値ですが、"
                                  "大引けまで来ていない銘柄もあります。")
                else:
                    late_note += (f"数字は大引けの値ではありません"
                                  f"（{data_time.strftime('%H:%M')}ごろまで）。")
            # 前営業日の14:10の結果が無ければ、本文の先頭に書く。土日・祝日や
            # 立会前に遅れて動いた回は何も送らないので、ここで書かないと失われた
            # 日に気づけない。処理の内訳の奥に書いていたが目立たなかった
            # （どちらも2026-10-09の見直しで判明）。
            missing, more = _missing_history_days(now.date())
            if missing:
                # 続けて失われた日もすべて書く。前営業日だけを見ていたので、2日続けて
                # 失われると古い方に気づけなかった（2026-10-12の見直し）。
                notices.append(f"{'・'.join(str(d) for d in missing)}"
                               + (f"（ほかにそれより前の{more}日）" if more else "")
                               + " の結果の記録がありません。"
                               f"その日の定期実行が動かなかったか、途中で失敗した可能性が"
                               f"あります（さかのぼって出すことはできません）")
            # 一覧や祝日を取り直せずに古いもので続けたとき（2026-10-09の見直し）
            age = F.STALE.get("prime_list")
            if age is not None and age > 45:
                suspect.append(f"銘柄一覧（JPX）を取り直せず、{age:.0f}日前に取った一覧を"
                               f"使っています。その後に上場した銘柄は調べていません")
            age = F.STALE.get("holidays")
            if age is not None and age > 60:
                suspect.append(f"祝日の一覧（内閣府）を取り直せず、{age:.0f}日前に取った"
                               f"ものを使っています")

        # 引け後の確定した数字なら、1日分の目安（5年分の大引け後で下位1%が0.56倍、
        # 普段の日の最低でも0.40倍ほど）で見る。途中の時刻の目安0.2のままだと、
        # 途中までしか来ていない出来高を見逃した（2026-10-11の見直し）。
        floor = (VOL_MEDIAN_FLOOR_CLOSE
                 if (asof is not None or (data_time and data_time.time() >= dt.time(15, 30)))
                 else VOL_MEDIAN_FLOOR)
        if (vol_med is not None and vol_med < floor
                and (asof is not None or now.time() >= dt.time(13, 55))):
            suspect.append(f"当日の出来高が全体に少なすぎます（下降トレンドで残った"
                           f"{errors.get('vol_median_n')}銘柄で、当日÷過去20日平均の"
                           f"中央値が{vol_med:.2f}倍。2026-10-06は14:10ごろの時点で"
                           f"0.45倍、昼休みの時点でも0.31倍あったので、配信が"
                           f"途中までしか来ていない可能性があります）")
        if errors.get("fund_stale"):
            # 業績は絞り込みに使っていないので見落としは起きない。お知らせにする
            # （【データ要確認】と「見落とし」を付けていた。2026-10-11の見直し）。
            notices.append(f"株探から業績を取得できず、{errors['fund_stale']}銘柄は前に取った数字"
                           f"（最も古いもので{errors.get('fund_stale_max_days', 0):.0f}日前）で"
                           f"参考条件を判定しています")
        # 下降トレンドの条件を満たす銘柄が極端に少ない日は、配信の異常を疑う。
        # 0件のときしか止めていなかったので、出来高の単位が変わる（1/100）などで
        # 294件が21件に減っても、平常の「該当なし」で送っていた。5年の普段の日は
        # 最少でも45件（2026-10-12の見直しで実測）。
        if stage_b < STAGE_B_FLOOR and not (stage_b == 0 and lost < universe * 0.05):
            suspect.append(f"下降トレンドの条件を満たした銘柄が{stage_b}件しかありません"
                           f"（過去5年の普段の日は少なくとも45件）。配信されたデータに"
                           f"問題がある可能性があります")
        flagged = [r for r in rows if r.get("suspect_split")]
        flagged_items = []
        if flagged:
            flagged_items.append(1)
            suspect.append("分割が調整されていない疑いのある銘柄があります（" +
                           "、".join(f"{r['code']} {r['name']}" for r in flagged[:5]) +
                           ("ほか" if len(flagged) > 5 else "") +
                           "）。その銘柄の乖離と順位は信用できません")
        unchecked = failed + skipped + bad_price
        if unchecked and lost < universe * 0.05:
            # 5%未満の取りこぼしは件名に【データ欠損あり】を付けない。ただし、調べられ
            # なかった銘柄（取得失敗・取りに行っていない・値が現実的でない）は先頭の
            # お知らせに書き、0件の日は件名にも書く。時間切れだけ【データ欠損あり】にし、
            # 取得失敗は処理の内訳の奥にしか出ていなかった（2026-10-12の見直し）。
            # 当日の足なし・履歴不足（新規上場・売買停止など）は正当な理由があるので数えない。
            det = []
            if failed:
                det.append(f"取得失敗 {failed}件")
            if skipped:
                det.append(f"{'時間切れで' if errors.get('stage_a_timeout') else ''}"
                           f"取りに行っていない {skipped}件（銘柄コードの大きい方から）")
            if bad_price:
                det.append(f"値が現実的でない {bad_price}件（分割の調整漏れの疑い）")
            notices.append(f"調べられなかった銘柄が{unchecked}件あります（{' / '.join(det)}）。"
                           f"その分の見落としがあります")
        if universe < 1500:
            parts.append(f"銘柄一覧が{universe}件しかありません"
                         f"（東証プライムの内国株式は通常1,500件以上）")
        if early_note:
            parts.append(early_note)
        if lost >= universe * 0.05:
            parts.append(f"判定に使えなかった銘柄が{lost}件"
                         f"（全{universe}銘柄の{100 * lost / universe:.0f}%）。"
                         f"内訳は {lost_detail}")
        if stage_b and intraday >= stage_b * 0.2:
            parts.append(f"出来高を判定できなかった銘柄が{intraday}件"
                         f"（下降トレンドで残った{stage_b}銘柄の"
                         f"{100 * intraday / stage_b:.0f}%）")
        warning = ""
        if parts:
            warning = ("本日は次のデータが欠けています。" + "／".join(parts) +
                       "。その分の見落としがあります。")
        if suspect:
            # 「見落とし」は、見落としにつながる項目があるときだけ書く。分割の目印は
            # 一覧に載っている銘柄の順位の話で、見落としではない（2026-10-11の見直し）。
            omission = len(suspect) > len(flagged_items)
            warning += (("　" if warning else "") +
                        "次の点から、データに問題がある疑いがあります。" +
                        "／".join(suspect) +
                        ("。見落としがあるかもしれません。" if omission else "。"))
        if notices:
            warning += ("　" if warning else "") + "／".join(notices) + "。"
        suspect_only = bool(suspect) and not parts
        notice_only = bool(notices) and not parts and not suspect

        label = f"{date_label} {cutoff_label}"
        for r in rows:
            r["asof_label"] = label
            # 銘柄ごとの最終約定時刻が全体の時刻と30分以上違えば、その銘柄の
            # 時刻を出す（売買停止など。2026-10-09の見直しで判明）。
            own = r.get("market_time")
            if asof is None and data_time and own and uses_close:
                ot = dt.datetime.fromtimestamp(own, JST)
                if abs((ot - data_time).total_seconds()) > 1800:
                    r["asof_label"] = (f"{date_label} {ot.strftime('%H:%M')}"
                                       if ot.date() == now.date()
                                       else ot.strftime("%Y-%m-%d %H:%M"))
                    r["own_time"] = r["asof_label"]

        # --- 日足チャートを1枚にまとめる
        # メールの添付は本文中の好きな位置に置けない（必ず末尾に出る）ことを
        # 2026-09-21 の実機テストで確認済み。よって銘柄ごとに貼るのはやめ、
        # 1枚にまとめて銘柄名を焼き込む。
        cdir = os.path.join(OUT, "charts")
        if os.path.isdir(cdir):
            for old in os.listdir(cdir):
                fp = os.path.join(cdir, old)
                if os.path.isfile(fp):
                    os.remove(fp)
        attachments = []
        if rows:
            sheet = os.path.join(cdir, "charts.png")
            try:
                # matplotlib はここでだけ読む。読めなくても判定とメールは出す
                # （画像はメールに使っていない）。
                import chart as C
                # 全件だと時間がかかりすぎる。メールに出るのも上位なので
                # 同じ数にそろえる。
                made = C.make_sheet(rows[:12], sheet)
                # 全部失敗すると None が返る。存在しない画像を添付すると
                # 送信そのものが弾かれる。
                if made and os.path.isfile(sheet):
                    attachments.append({"filename": "charts.png", "path": sheet})
            except Exception as e:                    # noqa: BLE001
                print(f"チャート作成失敗: {e}", file=sys.stderr)
        write("attachments.json",
              json.dumps(attachments, ensure_ascii=False, indent=1))
        notes.insert(0, f"実行時刻: {now.strftime('%Y-%m-%d %H:%M')} (JST)")
        if feed_note:
            notes.insert(1, feed_note)
        if late_note:
            notes.insert(1, late_note)
        if asof is not None:
            if uses_close:
                notes.append("※ 過去日の再現です。値段はその日の終値、出来高はその日1日分を"
                             "使っています（本番は実行した時点までの途中経過を使います）。")
            else:
                notes.append(f"※ 過去日の再現です。値段と出来高は{a.cutoff}時点の"
                             f"5分足から出しています。")

        base_for_footer = BASE_LABELS.get(a.volmode)
        if a.volmode == "sametime" and any(
                r.get("base_label") != BASE_LABELS["sametime"] for r in rows):
            base_for_footer = "前回の立会日の同時刻まで（5分足が欠けた銘柄はそれより前の日）"
        if a.volmode == "prevday" and any(r.get("prev_gap") for r in rows):
            # 足が欠けた銘柄はカードに「N営業日前の1日」と出る。末尾だけ
            # 「前日1日」と書くと食い違う（2026-10-09の見直し）。
            base_for_footer = "直前の足の1日（足が欠けた銘柄では、前営業日より前の日）"
        # 一覧に出た銘柄のその後（2026-10-12 追加）。本番だけ。失敗しても
        # メールは止めない（末尾の1段落が出ないだけ）。最長1分で打ち切る。
        track_note = ""
        if asof is None:
            try:
                import track as TR
                track_note = TR.note(TR.update(OUT, now.date()))
            except Exception as e:                    # noqa: BLE001
                print(f"その後の成績の更新に失敗: {e}", file=sys.stderr)
        subject, html_doc, text_doc = report.build(
            rows, notes, date_label, cutoff_label, warning=warning,
            base_label=base_for_footer, suspect=suspect_only, notice=notice_only,
            unchecked=unchecked if lost < universe * 0.05 else 0, track_note=track_note)

        write("subject.txt", subject)
        write("body.html", html_doc)
        write("body.txt", text_doc)
        # 生データの記録。書けなくても、出来上がったメールを失敗メールに
        # 置き換えない（status の後で書いていて、書けないと失敗メールに
        # なっていた。2026-10-09の見直しで判明）。
        try:
            write("result.json", json.dumps(
                {"date": date_label, "cutoff": cutoff_label, "volmode": a.volmode,
                 "notes": notes, "errors": {k: v for k, v in errors.items() if k != "high52"},
                 # 52週新高値の一覧（note の記事に使う。2026-10-27）
                 "high52": errors.get("high52"),
                 "rows": [{k: (str(v) if isinstance(v, dt.date) else v)
                           for k, v in h.items()
                           if k not in ("dates", "closes", "vols", "ohlc")}
                          for h in rows]},
                ensure_ascii=False, indent=1, default=str))
        except Exception as e:                        # noqa: BLE001
            print(f"result.json の書き出しに失敗（結果は出します）: {e}", file=sys.stderr)
        # 過去日の再現は確認用なので、送信は有効にしない。
        # 有効にすると、検証のたびにその日のメールが送られ、しかも
        # 本番の結果を上書きしてしまう（2026-10-05の見直しで判明）。
        write("status.txt", _send_status(asof))   # 必ず最後
        # その日の判定に使った出来高を記録に残す。
        # 本番は「実行した時点までに積み上がった当日の出来高」で判定している。
        # Yahooの分足は1日の出来高の40〜71%しか拾えず（2026-10-04 実測）、
        # 後から「何時に何割たまっていたか」を再現できない。ここで残しておけば、
        # 確定した日足と突き合わせて実測できる。
        #
        # 注意: 値は「14:10時点」ではなく「実行した時点」。遅れて動いた日は
        # ほぼ1日分になる。あとで選り分けられるよう captured_at と
        # within_window（予定の時間帯に動いたか）を一緒に残す。
        # 記録は補助的なもの。ここで失敗しても、その日の判定結果まで
        # 失ってはいけない（大きな try の中にあると失敗メールになってしまう）。
        try:
            if asof is None:
                hist = os.path.join(OUT, "history")
                os.makedirs(hist, exist_ok=True)
                within = dt.time(13, 55) <= now.time() <= dt.time(14, 40)
                hp = os.path.join(hist, f"{date_label}.json")
                with open(f"{hp}.{os.getpid()}.new", "w",
                          encoding="utf-8") as f:
                    json.dump({"date": date_label,
                               "captured_at": now.strftime("%Y-%m-%d %H:%M"),
                               # 数字が実際に取れていた時刻（Yahooの配信は遅れる）
                               "data_time": (data_time.strftime("%Y-%m-%d %H:%M")
                                             if data_time else None),
                               "vol_median": vol_med,
                               "scheduled": SCHEDULED,
                               "within_window": within,
                               "volmode": a.volmode,
                               "rows": [{"code": r["code"], "name": r["name"],
                                         "vol_at_run": r["vol_today"],
                                         "vol_base": r["vol_base"],
                                         "base_label": r["base_label"],
                                         "ratio_at_run": r["ratio"],
                                         "price_at_run": r["price"],
                                         "dev25_pct": r["dev25_pct"],
                                         # ランク（◎）。実際の成績をランクごとに数えるため（2026-10-22）
                                         "mark": r.get("mark", "")}
                                        for r in rows]},
                              f, ensure_ascii=False, indent=1)
                os.replace(f"{hp}.{os.getpid()}.new", hp)
        except Exception as e:                        # noqa: BLE001
            print(f"記録の書き出しに失敗（結果は出します）: {e}", file=sys.stderr)

        print(subject)
        print("\n".join("# " + n for n in notes))
        return 0

    except Exception as e:                            # noqa: BLE001
        detail = f"{type(e).__name__}: {e}"
        # 失敗メールの時刻は実行時刻にそろえる。データの時刻に置き換えた後で
        # 失敗すると、失敗メールだけデータの時刻になっていた（2026-10-09の見直し）。
        fail_label = now.strftime("%H:%M") if asof is None else cutoff_label
        subject, html_doc, text_doc = report.build(
            [], [], date_label, fail_label, failure=detail)
        write("subject.txt", subject)
        write("body.html", html_doc)
        write("body.txt", text_doc)
        write("error.txt", traceback.format_exc())
        write("status.txt", _send_status(a.asof))      # 必ず最後
        print("FAILURE:", detail, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
