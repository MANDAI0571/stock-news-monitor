"""Claude 300万円運用の X 投稿用の文を、持ち主宛てのメールで届ける（2026-10-30 追加）。

持ち主「300万運用でXでリアルタイム情報流しますと言ってXのフォローを増やすよう考えて」（2026-10-30）。
X への投稿は持ち主が自分でする（このプログラムは X に何も送らない）。メールには投稿ごとに
「📋 文とチャート画像」（docs/clip.html を開く。文とチャートはリンクの # の後ろで運ぶのでサーバーに残らない）と
「𝕏に投稿（文だけ）」のボタンを付ける。宛先は持ち主（MAIL_TO の1つ目）だけ。

  朝（約定のあと。claude_300man_fill.yml）  : 寄付で約定した買い（逆指値・指値・期限つき）、前の営業日の売り（夜に付けられなかった分）
  夜（注文のあと。claude_300man_declare.yml）: 今日の売り、明日の寄付の買い注文、口座のまとめ

同じ投稿は二度送らない（送った投稿の印をキャッシュに残す。印は銘柄名を含まないハッシュ）。
これは架空資金の記録であり、投資助言ではない。

  python3 claude_300man_x_posts.py --kind evening --date 2026-10-30            （送らずに中身を表示）
  X_POST_SEND=1 python3 claude_300man_x_posts.py --kind evening --date 2026-10-30
"""

from __future__ import annotations

import argparse
import hashlib
import html as H
import json
import os
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd

import dual_300man_fill as F
from claude_300man_exits import deadline, exit_levels
from dual_300man_config import CONFIG, JOURNAL_COLUMNS, ORDER_COLUMNS
from jpx_calendar import is_jpx_business_day

JST = ZoneInfo("Asia/Tokyo")
X_URL = "https://x.com/Breakout_Scan"          # note のプロフィールにつながっている X（note API の socials で確認）
NOTE_URL = "https://note.com/breakout_scan"
STATE_PATH = Path(os.environ.get("X_POST_STATE", "x_posts_state/sent.json"))
TAGS = "#日本株 #AI投資 #Claude"
FOOT = "※架空の300万円の記録です。売買をすすめるものではありません"
# 2.x の売り（終値で決めて翌日の寄付）と 3.0.0 の売り（逆指値・指値・期限の大引け）のどちらにも当てはまる言い方にする
EXIT_LABEL = {"STOP_LOSS": "損切り", "TAKE_PROFIT": "利確", "TIMEOUT": "期限"}
PriceFetcher = Callable[[str], pd.DataFrame]


def _f(value: object) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _md(day: str | date) -> str:
    d = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
    return f"{d.month}/{d.day}"


def previous_business_day(day: date) -> date:
    cursor = day - timedelta(days=1)
    while not is_jpx_business_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def _fit(lines: list[str], optional: list[str], limit: int = 280) -> str:
    """必須の行に、入るだけ任意の行（前から優先）を足す。X の280字（漢字・かなは2）に収める。"""
    from intraday_high_alert import x_length

    keep = list(optional)
    while True:
        text = "\n".join(lines[:-1] + keep + lines[-1:])
        if x_length(text) <= limit or not keep:
            break
        keep.pop()
    head = lines[0]
    while x_length(text) > limit and len(head) > 10:       # 銘柄名がとても長いときは1行目を詰める
        head = head[:-2] + "…"
        text = "\n".join([head] + lines[1:-1] + lines[-1:])
    return text


def order_post(row: pd.Series) -> str:
    price = _f(row.get("decision_price")) or 0.0
    shares = int(_f(row.get("shares")) or 0)
    lines = [f"【Claude×300万円 注文】{_md(row['execution_date'])}の寄付で買います",
             f"{row['code']} {row['name']}　{shares:,}株（{_md(row['decision_date'])}の終値 {price:,.1f}円で約{price * shares / 10000:,.0f}万円）",
             f"買えたら 逆指値{CONFIG.stop_loss_pct:+.0f}%・指値{CONFIG.take_profit_pct:+.0f}%・{CONFIG.timeout_days}営業日で手じまい",
             FOOT]
    return _fit(lines, ["注文は寄付の前にここで出します（後出しなし）", TAGS])


def fill_post(row: pd.Series) -> str:
    entry = _f(row.get("entry_price")) or 0.0
    shares = int(_f(row.get("shares")) or 0)
    stop, take = exit_levels(entry)
    due = deadline(date.fromisoformat(str(row["entry_date"])[:10]))
    lines = [f"【Claude×300万円 約定】{row['code']} {row['name']}",
             f"{_md(row['entry_date'])}の寄付で {entry:,.1f}円×{shares:,}株（約{entry * shares / 10000:,.0f}万円）",
             # 「逆指値ライン X円」は clip.html が画像の黄色い点線の見出しに使う
             f"逆指値ライン {stop:,.1f}円（{CONFIG.stop_loss_pct:+.0f}%）／指値 {take:,.1f}円（{CONFIG.take_profit_pct:+.0f}%）／期限 {_md(due)}",
             FOOT]
    return _fit(lines, ["届いたらその日のうちにここで流します", TAGS])


def sell_post(row: pd.Series) -> str:
    entry = _f(row.get("entry_price")) or 0.0
    exit_price = _f(row.get("exit_price")) or 0.0
    ret = _f(row.get("exit_return_pct"))
    ret = ret if ret is not None else ((exit_price / entry - 1) * 100 if entry else 0.0)
    pnl = _f(row.get("realized_pnl")) or 0.0
    held = int(_f(row.get("holding_business_days")) or 0)
    label = EXIT_LABEL.get(str(row.get("exit_type") or ""), "売り")
    mark = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
    lines = [f"【Claude×300万円 売り】{row['code']} {row['name']}",
             f"{mark} {_md(row['exit_date'])} {label}で売り",
             f"買値 {entry:,.1f}円 → 売値 {exit_price:,.1f}円（{ret:+.1f}%・{pnl:+,.0f}円）保有{held}営業日",
             FOOT]
    return _fit(lines, ["負けも全部出します", TAGS])


def summary_post(day: date, equity: float, cash: float, holdings: int, sells: int, orders: int) -> str:
    start = float(CONFIG.initial_cash)
    lines = [f"【Claude×300万円】{_md(day)}の口座",
             f"評価額 {equity:,.0f}円（スタートの300万円から {(equity / start - 1) * 100:+.2f}%）",
             f"保有{holdings}銘柄・現金{cash / 10000:,.0f}万円／今日の売り{sells}件・次の寄付の買い注文{orders}件",
             FOOT]
    return _fit(lines, ["注文は前の晩、約定は朝、売りはその日の夜にここで流しています",
                        f"毎日の記録は note {NOTE_URL}", TAGS])


def _close(history: pd.DataFrame | None) -> float | None:
    if history is None or getattr(history, "empty", True) or "Close" not in history.columns:
        return None
    s = pd.to_numeric(history["Close"], errors="coerce").dropna()
    return float(s.iloc[-1]) if len(s) else None


def build_posts(kind: str, day: date, orders: pd.DataFrame, journal: pd.DataFrame,
                fetch: PriceFetcher | None = None) -> list[dict]:
    """[{key, kind, text, ticker, line}]。key は二度送らないための印（銘柄名を含まないハッシュにして残す）。"""
    posts: list[dict] = []
    prev = previous_business_day(day)
    closed = journal[journal["status"].astype(str).str.upper().eq("CLOSED")] if not journal.empty else journal
    sell_days = {day.isoformat(), prev.isoformat()}
    sells = closed[closed["exit_date"].astype(str).str[:10].isin(sell_days)] if not closed.empty else closed
    for _, row in sells.iterrows():
        posts.append({"key": f"sell|{row['code']}|{row['entry_date']}|{row['exit_date']}", "kind": "sell",
                      "text": sell_post(row), "ticker": str(row.get("ticker") or f"{row['code']}.T"),
                      "line": _f(row.get("entry_price")) or 0.0})
    if kind == "morning":
        opened = journal[journal["entry_date"].astype(str).str[:10].eq(day.isoformat())] if not journal.empty else journal
        for _, row in opened.iterrows():
            stop, _ = exit_levels(_f(row.get("entry_price")) or 0.0)
            posts.append({"key": f"fill|{row['code']}|{row['entry_date']}", "kind": "fill", "text": fill_post(row),
                          "ticker": str(row.get("ticker") or f"{row['code']}.T"), "line": stop})
        return posts
    side = orders["side"].astype(str).str.upper() if not orders.empty else pd.Series(dtype=str)
    new = orders[side.eq("BUY") & orders["decision_date"].astype(str).str[:10].eq(day.isoformat())
                 & orders["status"].astype(str).str.upper().eq("DECLARED")] if not orders.empty else orders
    for _, row in new.iterrows():
        posts.append({"key": f"order|{row['code']}|{row['decision_date']}", "kind": "order", "text": order_post(row),
                      "ticker": str(row.get("ticker") or f"{row['code']}.T"), "line": 0.0})
    holding = journal[journal["status"].astype(str).str.upper().eq("OPEN")] if not journal.empty else journal
    value = 0.0
    for _, row in holding.iterrows():
        shares = _f(row.get("shares")) or 0.0
        price = None
        if fetch is not None:
            try:
                price = _close(fetch(str(row.get("ticker") or f"{row['code']}.T")))
            except Exception:  # noqa: BLE001 - 値段が取れなければ記録の値を使う
                price = None
        price = price or _f(row.get("mark_price")) or _f(row.get("entry_price")) or 0.0
        value += shares * price
    cash = F.cash_balance(journal)
    n_sells = int(closed["exit_date"].astype(str).str[:10].eq(day.isoformat()).sum()) if not closed.empty else 0
    posts.append({"key": f"summary|{day.isoformat()}", "kind": "summary",
                  "text": summary_post(day, cash + value, cash, len(holding), n_sells, len(new)), "ticker": "", "line": 0.0})
    return posts


def _hash(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def load_state(path: Path = STATE_PATH) -> set[str]:
    try:
        return set(json.loads(path.read_text(encoding="utf-8")).get("sent", []))
    except (OSError, ValueError, AttributeError):
        return set()


def save_state(sent: set[str], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sent": sorted(sent)[-2000:]}, ensure_ascii=False), encoding="utf-8")


def _chart(post: dict, fetch: PriceFetcher | None) -> str:
    if not post.get("ticker") or fetch is None:
        return ""
    try:
        from intraday_high_alert import chart_payload

        return chart_payload(fetch(post["ticker"]), post.get("line") or 0.0)
    except Exception:  # noqa: BLE001 - 画像が作れなくても文は届ける
        return ""


def build_mail(kind: str, day: date, posts: list[dict], fetch: PriceFetcher | None = None) -> tuple[str, str, str]:
    from intraday_high_alert import clip_url

    when = "朝（約定）" if kind == "morning" else "夜（注文と売り）"
    counts = {k: sum(1 for p in posts if p["kind"] == k) for k in ("order", "fill", "sell")}
    parts = [f"{name}{counts[k]}件" for k, name in (("fill", "約定"), ("sell", "売り"), ("order", "注文")) if counts[k]]
    subject = f"【X投稿用】Claude 300万円 {_md(day)} {when}" + (f" {'・'.join(parts)}" if parts else "")
    lead = (f"X（{X_URL}）に貼る文です。上から順に投稿できます。ボタン「📋 文とチャート画像」はコピー用のページを開き、"
            "そこで文のコピーと画像の保存・共有ができます（文とチャートはリンクの # の後ろで運ぶので、どこにも残りません）。"
            "「𝕏に投稿」は文が入った投稿画面を開きます（画像は運べません）。架空資金の記録で、売買をすすめるものではありません。")
    blocks, texts = [], [lead, ""]
    for i, post in enumerate(posts, 1):
        chart = _chart(post, fetch)
        clip = clip_url(post["text"], chart)
        xpost = "https://x.com/intent/post?text=" + quote(post["text"], safe="")
        label = "📋 文とチャート画像" if chart else "📋 文をコピー"
        btn = ("display:inline-block;margin:6px 6px 0 0;padding:9px 12px;border-radius:8px;color:#ffffff;"
               "text-decoration:none;font-weight:bold;font-size:14px;")
        blocks.append(
            f'<div style="border:1px solid #e3e5e8;border-radius:10px;padding:12px 14px;margin:0 0 12px 0;background:#ffffff;">'
            f'<div style="font-size:12px;color:#888888;">{i}/{len(posts)}</div>'
            f'<div style="white-space:pre-wrap;font-size:14px;line-height:1.7;color:#1a1a1a;">{H.escape(post["text"])}</div>'
            f'<a href="{H.escape(clip)}" style="{btn}background:#1f745f;">{label}</a>'
            f'<a href="{H.escape(xpost)}" style="{btn}background:#111111;">𝕏に投稿</a></div>')
        texts += [f"── {i}/{len(posts)} ──", post["text"], ""]
    html_doc = ('<!doctype html><html><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
                '<body style="margin:0;padding:0;background:#f4f5f7;">'
                '<div style="max-width:560px;margin:0 auto;padding:16px;font-family:-apple-system,\'Hiragino Sans\',sans-serif;">'
                f'<div style="font-size:13px;line-height:1.7;color:#555555;margin:0 0 12px 0;">{H.escape(lead)}</div>'
                + "".join(blocks) + "</div></body></html>")
    return subject, "\n".join(texts), html_doc


def run(kind: str, day: date, send: bool, fetch: PriceFetcher | None = None,
        state_path: Path = STATE_PATH) -> int:
    """送った（送るはずだった）投稿の件数を返す。"""
    orders_path, journal_path, _ = F.account_paths("claude")
    orders = F.read_csv(orders_path, ORDER_COLUMNS)
    journal = F.read_csv(journal_path, JOURNAL_COLUMNS)
    if fetch is not None:                    # 同じ銘柄を何度も取りに行かない
        memo: dict[str, pd.DataFrame] = {}
        raw = fetch

        def fetch(ticker: str) -> pd.DataFrame:
            if ticker not in memo:
                memo[ticker] = raw(ticker)
            return memo[ticker]
    sent = load_state(state_path)
    posts = [p for p in build_posts(kind, day, orders, journal, fetch) if _hash(p["key"]) not in sent]
    # まとめだけの日（注文も売りもない夜）も、口座の数字は毎日出す。何も無い朝は送らない
    if not posts:
        print(f"claude_300man_x_posts=skip kind={kind} date={day} reason=nothing_new")
        return 0
    subject, text, html_doc = build_mail(kind, day, posts, fetch)
    if not send:
        print(f"claude_300man_x_posts=dry_run kind={kind} date={day} posts={len(posts)}")
        return len(posts)
    from gmail_notify import GmailConfig, load_gmail_config, send_gmail

    config = load_gmail_config()
    if config is None:
        print("claude_300man_x_posts=failed reason=no_gmail_config")
        return 0
    # 宛先は持ち主（MAIL_TO の1つ目）だけ
    config = GmailConfig(user=config.user, app_password=config.app_password,
                         mail_to=config.mail_to.split(",")[0].strip())
    if not send_gmail(subject, text, config, allow_non_business_day=True, html_body=html_doc):
        print("claude_300man_x_posts=failed reason=send")
        return 0
    save_state(sent | {_hash(p["key"]) for p in posts}, state_path)
    print(f"claude_300man_x_posts=sent kind={kind} date={day} posts={len(posts)}")   # 公開ログなので銘柄は出さない
    return len(posts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude 300万円運用の X 投稿用の文を持ち主にメールで届ける")
    parser.add_argument("--kind", choices=("morning", "evening"), required=True)
    parser.add_argument("--date", default=datetime.now(JST).date().isoformat())
    args = parser.parse_args()
    from dual_300man_metrics import fetch_metrics_history

    run(args.kind, date.fromisoformat(args.date), os.environ.get("X_POST_SEND") == "1", fetch_metrics_history)


if __name__ == "__main__":
    main()
