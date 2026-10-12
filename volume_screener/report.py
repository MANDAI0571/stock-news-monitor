"""スクリーニング結果をメール本文（スマホ向け）に組み立てる。

メールアプリは CSS の対応がまちまちなので、flex や grid は使わず、
幅100%の2列テーブル（左=項目名／右=数字）とインラインスタイルだけで組む。
横に長い表は作らず、銘柄ごとにカードを縦に積む。
"""

import html as H

SOURCES = (
    "株価・出来高: Yahoo Finance（日足）／"
    "業績・ROE・自己資本比率: 株探 kabutan.jp／"
    "銘柄一覧: JPX 上場銘柄一覧／"
    "休場日: 内閣府 国民の祝日CSV"
)

def criteria_text(base_label="過去20日平均"):
    """メール末尾の「判定条件」。出来高の基準は実際に使ったものを書く。

    数字は2026-10-05の再測定による。判定できなかった銘柄日を「満たさない」に
    混ぜると結論が逆に見えるので、必ず3つに分けて書く。
    """
    return (
        f"出来高倍率 2.0倍以上（当日 ÷ {base_label}）／"
        "25日線が5本前の足の時点より低い／現在値 &lt; 25日線 &lt; 75日線／"
        "20日平均売買代金 1億円以上。業績は絞り込みにも並べ替えにも使っていません。<br>"
        "<b>業績の「参考条件」について</b><br>"
        "売上高の前年同期比プラスとROE 8%以上を参考条件として表示していますが、"
        "<b>満たす方が良いという意味ではありません。測った範囲ではほとんど差が"
        "ありませんでした。</b><br>"
        "この一覧に出る条件（下降トレンド×出来高2倍）で2026-02-06〜2026-09-30に"
        "決着した売買を、その日に公表済みだった決算だけで分けると<br>"
        "・参考条件を満たす: 753件、損益比1.07（売買コスト0.2%を引くと0.99）<br>"
        "・満たさない: 778件、損益比1.03（同0.96）<br>"
        "・業績が判定できなかったもの: 65件、損益比0.86（同0.81）<br>"
        "条件ごとに見ても、売上高プラスは1.10対1.12、ROE 8%以上は1.06対1.15で、"
        "満たす側が良いとは言えませんでした"
        "（売買代金1億円以上の全銘柄日で集計）。<br>"
        "そのため業績での仕分けをやめ、数字だけを出しています。<br>"
        "<b>この数字の限界</b>：期間が約8か月と短いこと（株探に残る決算が"
        "8四半期ぶんしかなく、それ以前は条件を判定できません）、"
        "母集団が今日のプライム一覧なので途中で上場廃止や市場変更になった銘柄が"
        "入っていないこと（下げた銘柄を選ぶ条件なので、数字は良い方に偏ります）。<br>"
        "損益比は「終値で決めて翌営業日の寄付で売買し、損切-5%・利確+12%・"
        "15営業日で手仕舞い」とした場合の、決着した売買の集計です。"
        "宣告から約定まで1日あくので、実際の損切りは平均で-5%より深くなります。<br>"
        "過去の集計であって、今後を保証するものではありません。"
        + ("" if base_label == "過去20日平均" else
           f"<br><b>なお上の損益比は「当日 ÷ 過去20日平均が2倍以上」で測った"
           f"数字です。今回の一覧は「当日 ÷ {base_label}」で選んでいるので、"
           f"同じ集団ではありません。</b>")
    )



FONT = "-apple-system,BlinkMacSystemFont,'Hiragino Sans','Noto Sans JP',sans-serif"

_S = {
    "card": ("background:#ffffff;border:1px solid #e3e5e8;border-radius:10px;"
             "padding:14px 16px;margin:0 0 12px 0;"),
    "rank": ("display:inline-block;width:22px;height:22px;line-height:22px;"
             "text-align:center;background:#1a1a1a;color:#ffffff;"
             "border-radius:11px;font-size:12px;font-weight:bold;"),
    "name": "font-size:17px;font-weight:bold;line-height:1.4;",
    "big": "font-size:21px;font-weight:bold;padding:10px 0 2px 0;",
    "sub": "font-size:12px;color:#888888;line-height:1.6;padding:4px 0 0 0;",
    "k": ("font-size:14px;color:#555555;line-height:1.8;"
          "padding:1px 0;text-align:left;"),
    "v": ("font-size:14px;color:#1a1a1a;line-height:1.8;"
          "padding:1px 0;text-align:right;white-space:nowrap;"),
    "sec": "border-top:1px solid #eeeeee;padding:9px 0 0 0;margin:10px 0 0 0;",
}


def _chart_url(code):
    """株探のチャートURL。4桁でない銘柄コード（優先株など7件）は
    株探に個別ページが無く404になるので、Yahooに回す。"""
    c = str(code)
    if len(c) == 4:
        return f"https://kabutan.jp/stock/chart?code={c}"
    return f"https://finance.yahoo.co.jp/quote/{c}.T/chart"


def _plain(html_text):
    """HTML用の文字列をプレーンテキストに直す。<br>がそのまま本文に出ていた。"""
    return (html_text.replace("<br>", "\n").replace("<b>", "").replace("</b>", "")
            .replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))


def _chg_label(r):
    """前日比の呼び方。直前の足が前営業日でない日は「前日比」と言わない。"""
    return "前回の足から" if r.get("prev_gap") else "前日比"


def _split_note(r):
    """分割が調整されていない疑いの説明。無ければ空。"""
    x = r.get("suspect_split")
    if not x:
        return ""
    d, ratio = x[0], x[1]
    return (f"分割が調整されていない疑い（{d} に前の足から {(ratio - 1) * 100:+.0f}%。"
            f"値幅制限を超える段差で、比率が分割によくある値に近い）。"
            f"この銘柄の移動平均・乖離・出来高倍率は信用できません")


def _prev_vol_label(r):
    """直前の足の出来高の呼び方。「前日比の出来高」と出ていた
    （2026-10-08 のメールで確認）。比ではなく前日1日の出来高そのもの。"""
    return "前回の足の出来高" if r.get("prev_gap") else "前日の出来高"


def _pct(v):
    return "—" if v is None else f"{v:.1f}%"


def _fund_note(f):
    if not f or not f.get("period"):
        return "業績データを取得できませんでした"
    kind = f.get("period_kind") or ""
    k = f"（{kind}）" if kind else ""

    base = f"決算 {f.get('period')}{k}（{f.get('announced')} 発表）"
    # ROEの決算期が取れないことがある（実データで1件確認）。
    # 取れていないのに「ROEはNone期の実績」と書かない。
    if f.get("roe") is not None and f.get("roe_period"):
        base += f"／ROEは{f.get('roe_period')}期の実績"
    if f.get("cache_age_days") is not None:
        # 株探に届かず、前に取った数字を使ったとき（2026-10-09の見直し）
        base += f"／株探から取得できなかったため、{f['cache_age_days']:.0f}日前に取った数字です"
    return H.escape(base)


def _fund_state(r):
    """参考条件の判定。取れていない数字を「満たさない」と書かないための3状態。

    以前は fund_ok がどちらの欄に入れるかを決めるだけだったので、取得できな
    かった銘柄が②に落ちても誤解は生じなかった。1つの一覧にして銘柄ごとに
    ○×を出すようになったので、取れていないものは「不明」と書く。
    """
    f = r.get("fund") or {}
    rev_known = (f.get("rev_yoy") is not None
                 or f.get("rev_yoy_raw", "") in LOSS_WORDS)
    roe_known = f.get("roe") is not None
    if not (rev_known and roe_known):
        return "不明"
    return "満たす" if r.get("fund_ok") else "満たさない"


def _reason_html(r):
    why = r.get("fund_reason")
    if not why or _fund_state(r) == "不明":
        return ""
    return ('<div style="margin:8px 0 0 0;padding:7px 10px;background:#fff6ef;'
            'border:1px solid #f0d8c4;border-radius:6px;font-size:12px;'
            'line-height:1.6;color:#8a4b1e;">業績の参考条件に未達: ' + H.escape(why) + '</div>')


# 株探は赤字がらみの前年同期比を言葉で書く。数値にならないので言葉のまま出す。
LOSS_WORDS = {"黒転": "黒字転換", "赤転": "赤字転落",
              "赤縮": "赤字縮小", "赤拡": "赤字拡大"}


def _yoy(value, raw):
    """前年同期比の表示。数値にならない「黒転」「赤拡」等は言葉で出す。"""
    if raw in LOSS_WORDS:
        return LOSS_WORDS[raw]
    if raw in ("－", "-", "―"):
        return "開示なし"
    return "—" if value is None else f"{value:+.1f}%"


def _oku(x):
    return f"{x / 1e8:,.1f}億円"


def _rows(pairs):
    """[(項目名, 数字, 太字か)] を幅100%の2列テーブルにする。"""
    trs = "".join(
        f'<tr><td style="{_S["k"]}">{k}</td>'
        f'<td style="{_S["v"]}">{"<b>" + v + "</b>" if b else v}</td></tr>'
        for k, v, b in pairs)
    return (f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" border="0" style="width:100%;border-collapse:collapse;">'
            f'{trs}</table>')


CHART_NOTE = (
    "銘柄は、カード（「日足チャートを開く」ボタン付き）、1行ずつの短い形（チャートへの"
    "リンク付き）、名前と数字だけの行（リンクなし）のどれかで、上位から順に出します。"
    "件数が多い日ほどカードやリンクの付く銘柄は減り、非常に多い日はカードが0件に"
    "なります（Gmailは長すぎるメールを途中で切るため）。チャートは、4桁の銘柄コードは"
    "株探、優先株など4桁でないものはYahooファイナンスです。"
    "銘柄コードで株探やYahooファイナンスを引けば同じチャートが見られます。"
)


def _chart_html(r):
    """カード内に日足チャートを開くリンクを置く。

    メールの添付画像は本文中の好きな位置に置けず、必ず末尾に出ることを
    2026-09-21 の実機テストで確認した。そこでカード内はリンクにして、
    タップすれば拡大も期間変更もできる本物のチャートを開けるようにする。
    """
    s = _S
    code = H.escape(str(r["code"]))
    url = H.escape(_chart_url(r["code"]))
    return (f'<div style="{s["sec"]}">'
            f'<a href="{url}" '
            f'style="display:block;text-align:center;text-decoration:none;'
            f'background:#f2f4f7;border:1px solid #dfe3e8;border-radius:8px;'
            f'padding:11px 8px;font-size:14px;font-weight:bold;color:#1a4f8a;">'
            f'日足チャートを開く &rsaquo;</a></div>')


def _split_html(r):
    note = _split_note(r)
    if not note:
        return ""
    return ('<div style="margin:8px 0 0 0;padding:8px 10px;background:#fff4f2;'
            'border:1px solid #f0c8bf;border-radius:6px;font-size:13px;'
            'line-height:1.6;color:#7a2d1c;"><b>要注意:</b> ' + H.escape(note) + '</div>')


def card(i, r):
    f = r["fund"]
    chg = r["chg_pct"]
    color = "#c0392b" if chg < 0 else "#1e7a45"
    s = _S
    return f"""
<div style="{s['card']}">
  <div><span style="{s['rank']}">{i}</span>&nbsp;<span style="{s['name']}">{H.escape(str(r['code']))} {H.escape(r['name'])}</span></div>
  <div style="{s['big']}">{r['price']:,.1f}円 <span style="color:{color};">({_chg_label(r)} {chg:+.2f}%)</span></div>
  <div style="{s['sub']}">{r['asof_label']}時点</div>
{_split_html(r)}{_chart_html(r)}
  <div style="{s['sec']}">{_rows([
      ("出来高倍率", f"{r['ratio']:.2f}倍", True),
      ("当日の出来高", f"{r['vol_today']:,.0f}株", False),
      (r['base_label'], f"{r['vol_base']:,.0f}株", False),
      (_prev_vol_label(r), f"{r['vol_prev_daily']:,.0f}株", False),
  ])}</div>

  <div style="{s['sec']}">{_rows([
      ("現在値と25日線", f"{r['dev25_pct']:+.1f}%", True),
      ("25日線", f"{r['ma25']:,.1f}円", False),
      ("75日線", f"{r['ma75']:,.1f}円", False),
  ])}
    <div style="{s['sub']}">現在値 &lt; 25日線 &lt; 75日線。25日線は5本前の足の時点（{r['ma25_prev']:,.1f}円）より低い。</div>
  </div>

  <div style="{s['sec']}">{_rows([
      ("業績の参考条件", _fund_state(r), True),
      ("売上高 前年同期比", _yoy(f.get('rev_yoy'), f.get('rev_yoy_raw', '')), False),
      ("営業益 前年同期比", _yoy(f.get('op_yoy'), f.get('op_yoy_raw', '')), False),
      ("ROE", _pct(f.get('roe')), False),
      ("自己資本比率", _pct(f.get('equity_ratio')), False),
  ])}
    <div style="{s['sub']}">{_fund_note(f)}</div>
    {_reason_html(r)}
  </div>

  <div style="{s['sec']}">{_rows([
      ("20日平均売買代金", _oku(r['turnover20']), False),
  ])}</div>
</div>"""


def _tiny(i, r):
    """最小限の1行。全銘柄を必ずどこかに出すための最後の段。

    1件ずつdivで包むと100バイトを超える。多い日は300件を超えるので、
    <br>でつないで1件55バイトほどに抑える。
    """
    flag = "【分割未調整の疑い】" if r.get("suspect_split") else ""
    return (f'{i}. {H.escape(str(r["code"]))} {H.escape(r["name"])}{flag}'
            f' ／乖離 {r["dev25_pct"]:+.1f}% ／出来高 {r["ratio"]:.2f}倍{_own_time(r)}<br>')


def _own_time(r):
    """全体と時刻がずれている銘柄だけ、その銘柄の時刻を添える（2026-10-11の見直し）。"""
    return f"［{r['own_time']}時点］" if r.get("own_time") else ""


def _fund_age(r):
    """業績を前に取った数字で判定したとき、その古さを添える（2026-10-11の見直し）。"""
    age = (r.get("fund") or {}).get("cache_age_days")
    return f"（業績は{age:.0f}日前の数字）" if age is not None else ""


def _brief(i, r):
    """カードに入りきらないぶんを1行で出す。"""
    s = _S
    code = H.escape(str(r["code"]))
    flag = ('<span style="color:#c0392b;"> 【分割未調整の疑い】</span>'
            if r.get("suspect_split") else "")
    return (f'<div style="border-top:1px solid #eceef0;padding:9px 2px;'
            f'font-size:13px;line-height:1.6;color:#333333;">'
            f'<b>{i}. {code} {H.escape(r["name"])}</b>{flag}<br>'
            f'乖離 {r["dev25_pct"]:+.1f}% ／ 出来高 {r["ratio"]:.2f}倍 ／ '
            f'{r["price"]:,.1f}円（{_chg_label(r)} {r["chg_pct"]:+.2f}%）'
            f'{_own_time(r)}／ '
            f'業績の参考条件 {_fund_state(r)}{_fund_age(r)}<br>'
            f'<a href="{H.escape(_chart_url(r["code"]))}" '
            f'style="color:#1a4f8a;">日足チャートを開く &rsaquo;</a></div>')


def _section(title, lead, rows, start_no, plan):
    head = (f'<div style="font-size:16px;font-weight:bold;color:#1a1a1a;'
            f'margin:22px 0 4px 0;">{title}</div>'
            f'<div style="font-size:13px;color:#666666;line-height:1.7;'
            f'margin:0 0 12px 0;">{lead}</div>')
    if not rows:
        return head + ('<div style="background:#ffffff;border:1px solid #e3e5e8;'
                       'border-radius:10px;padding:18px 16px;text-align:center;'
                       'font-size:14px;color:#777777;">該当なし</div>')
    total = len(rows)
    n_card, n_brief, n_list = plan
    listed = rows[:n_list]
    dropped = total - len(listed)
    cards = [card(start_no + i, r) for i, r in enumerate(listed[:n_card])]
    briefs = [_brief(start_no + n_card + i, r)
              for i, r in enumerate(listed[n_card:n_card + n_brief])]
    tinies = [_tiny(start_no + n_card + n_brief + i, r)
              for i, r in enumerate(listed[n_card + n_brief:])]

    body = "".join(cards)
    if briefs:
        body += ('<div style="background:#ffffff;border:1px solid #e3e5e8;'
                 'border-radius:10px;padding:4px 14px 10px 14px;margin:0 0 12px 0;">'
                 f'<div style="font-size:13px;color:#666666;line-height:1.7;'
                 f'padding:10px 0 2px 0;">次の{len(briefs)}件'
                 f'（メールが長くなりすぎないよう短く出します）</div>'
                 + "".join(briefs) + '</div>')
    if tinies:
        body += ('<div style="background:#ffffff;border:1px solid #e3e5e8;'
                 'border-radius:10px;padding:10px 14px;margin:0 0 12px 0;">'
                 f'<div style="font-size:13px;color:#666666;line-height:1.7;'
                 f'padding:0 0 6px 0;">残りの{len(tinies)}件'
                 f'（全{total}件とも同じ条件を満たしています）</div>'
                 f'<div style="font-size:12px;line-height:1.8;color:#444444;">'
                 + "".join(tinies) + '</div></div>')
    if dropped:
        body += ('<div style="background:#fff4f2;border:1px solid #f0c8bf;'
                 'border-radius:10px;padding:12px 14px;font-size:13px;'
                 'line-height:1.7;color:#7a2d1c;">'
                 f'さらに{dropped}件が条件を満たしましたが、メールに入りきらない'
                 f'ため載せていません（乖離の浅い方から順に省きました。全{total}件）。'
                 '</div>')
    return head + body


def _text_block(title, rows, start_no, plan):
    """テキスト版。HTML版と同じ plan で出すので、載る銘柄は必ず一致する。"""
    if not rows:
        return f"── {title} ──\n該当なし\n"
    total = len(rows)
    n_card, n_brief, n_list = plan
    listed = rows[:n_list]
    dropped = total - len(listed)
    out = [f"── {title} ──"]
    for i, r in enumerate(listed):
        f = r.get("fund") or {}
        mark = {"満たす": "参考条件を満たす", "満たさない": "参考条件は満たさない",
                "不明": "業績の数字が取れず判定できず"}[_fund_state(r)]
        n = start_no + i
        if i < n_card:
            line = (f"{n}. {r['code']} {r['name']}  [{mark}]\n"
                    f"  {r['price']:,.1f}円 ({_chg_label(r)} {r['chg_pct']:+.2f}%)"
                    f" {r['asof_label']}時点\n"
                    f"  出来高倍率 {r['ratio']:.2f}倍"
                    f"（当日 {r['vol_today']:,.0f}株 ÷ {r['base_label']} {r['vol_base']:,.0f}株）\n"
                    f"  {_prev_vol_label(r)} {r['vol_prev_daily']:,.0f}株\n"
                    f"  25日線 {r['ma25']:,.1f} / 75日線 {r['ma75']:,.1f}"
                    f" 現在値は25日線から {r['dev25_pct']:+.1f}%"
                    f"（25日線は5本前の足の時点 {r['ma25_prev']:,.1f}円 より低い）\n"
                    f"  売上高 {_yoy(f.get('rev_yoy'), f.get('rev_yoy_raw', ''))}"
                    f" / 営業益 {_yoy(f.get('op_yoy'), f.get('op_yoy_raw', ''))}"
                    f" ROE {_pct(f.get('roe'))} 自己資本比率 {_pct(f.get('equity_ratio'))}\n"
                    f"  {_plain(_fund_note(f))}\n"
                    f"  20日平均売買代金 {_oku(r['turnover20'])}\n"
                    f"  日足チャート {_chart_url(r['code'])}")
            if r.get("fund_reason") and _fund_state(r) != "不明":
                line += f"\n  ※業績の参考条件に未達: {r['fund_reason']}"
            if r.get("suspect_split"):
                line += f"\n  ※要注意: {_split_note(r)}"
        elif i < n_card + n_brief:
            line = (f"{n}. {r['code']} {r['name']}"
                    f"{'【分割未調整の疑い】' if r.get('suspect_split') else ''}  "
                    f"乖離 {r['dev25_pct']:+.1f}% / 出来高 {r['ratio']:.2f}倍 / "
                    f"{r['price']:,.1f}円({_chg_label(r)} {r['chg_pct']:+.2f}%){_own_time(r)}"
                    f" [{mark}{_fund_age(r)}]\n"
                    f"  {_chart_url(r['code'])}")
        else:
            line = (f"{n}. {r['code']} {r['name']}"
                    f"{'【分割未調整の疑い】' if r.get('suspect_split') else ''}"
                    f" ／乖離 {r['dev25_pct']:+.1f}% ／出来高 {r['ratio']:.2f}倍{_own_time(r)}")
        out.append(line)
    if dropped:
        out.append(f"（さらに{dropped}件が条件を満たしましたが、メールに"
                   f"入りきらないため載せていません。乖離の浅い方から順に"
                   f"省きました。全{total}件）")
    return "\n\n".join(out) + "\n"


SEC_TITLE = "出来高が急に増えた下降トレンドの銘柄"
SEC_LEAD = ("25日線からの乖離が深い順に並べています。"
            "業績は一覧を分けるのに使っていません。"
            "過去検証で、業績の条件を満たす側と満たさない側に"
            "ほとんど差が出なかったためです（下の「判定条件」に数字があります）。"
            "業績の数字は銘柄ごとにそのまま出しています。")

# Gmailは約102KBを超えると途中で切る。切られると末尾の「売買をすすめる
# ものではありません」まで消える。_fit が測るのは銘柄一覧の部分だけで、
# この外側に冒頭の説明・欠損の警告・末尾の判定条件や出所が付く（約19KB）。
MIME_LIMIT = 78000


def _mime_size(html, text):
    import base64
    return len(base64.b64encode(html.encode())) + len(base64.b64encode(text.encode()))


def _fit(rows):
    """収まる組み合わせを探す。

    まずカードと短縮行を減らし、それでも収まらなければ掲載件数を
    二分探索で詰める。以前は(320,240,160,80,40)と飛び飛びに落として
    いたため、収まるのに30件以上を落としていた（2026-10-05の実測）。
    """
    n = len(rows)

    def made(plan):
        body = _section(SEC_TITLE, SEC_LEAD, rows, 1, plan)
        text = _text_block(SEC_TITLE, rows, 1, plan)
        return body, text, _mime_size(body, text)

    for n_card in (12, 8, 5, 3, 2, 1, 0):
        for n_brief in (60, 40, 20, 10, 0):
            body, text, size = made((n_card, n_brief, n))
            if size <= MIME_LIMIT:
                return body, text

    # 全件は入らない。カード0・短縮行0で、収まる最大の件数を探す。
    lo, hi, best = 1, n, None
    while lo <= hi:
        mid = (lo + hi) // 2
        body, text, size = made((0, 0, mid))
        if size <= MIME_LIMIT:
            best = (body, text); lo = mid + 1
        else:
            hi = mid - 1
    if best:
        return best
    return made((0, 0, 1))[:2]


def build(rows, notes, date_label, cutoff_label, failure=None, warning="",
          base_label=None, suspect=False, notice=False, unchecked=0, track_note=""):
    """(subject, html, text) を返す。

    rows     条件に当てはまった銘柄の一覧（25日線からの乖離が深い順）
    failure  渡すと失敗通知メールになる。
    warning  データの欠損など、一覧は出すが但し書きが要るときの文言。
             件名と本文の先頭の両方に出す。奥に書くと気づけないため。
    notice   warning が「お知らせ」だけ（前営業日の結果が無い等）のとき True。
             件名に印は付けず、本文の先頭に出す。
    suspect  warning が目安から推した「疑い」だけのとき True。欠損と断定しない
             （件名は【データ要確認】）。2026-10-09の見直しで、目安だけで
             「欠けています」と断定していたことが分かった。
    unchecked 調べられなかった銘柄の数（全体の5%未満のとき）。0件の日は件名に
             「該当なし（ただしN銘柄は調べられず）」と書く。取れなかった銘柄がある
             日を、ただの「該当なし」にしない（2026-10-12の見直し）。
    """
    rows = rows or []
    if failure:
        subject = f"【スクリーニング失敗】{date_label} {cutoff_label}時点"
        body = (f'<div style="background:#fff4f2;border:1px solid #f0c8bf;'
                f'border-radius:10px;padding:16px;font-size:14px;line-height:1.8;'
                f'color:#7a2d1c;"><b>本日のスクリーニングは完了できませんでした。</b>'
                f'<br>理由: {H.escape(str(failure)).replace(chr(10), "<br>")}</div>')
        text = f"本日のスクリーニングは完了できませんでした。\n理由: {failure}\n"
        lead = ""
    else:
        n = len(rows)
        n_ok = sum(1 for r in rows if r.get("fund_ok"))
        word = "データ要確認" if suspect else "データ欠損あり"
        mark = f"【{word}】" if warning and not notice else ""
        if n == 0:
            label = (f"該当なし（ただし{word}）" if mark
                     else f"該当なし（ただし{unchecked}銘柄は調べられず）" if unchecked
                     else "該当なし")
            subject = (f"【出来高急増×下降】{mark}{date_label} {cutoff_label}時点 "
                       f"{label}")
        else:
            n_unknown = sum(1 for r in rows if _fund_state(r) == "不明")
            n_ng = n - n_ok - n_unknown
            inner = f"満たす {n_ok} / 満たさない {n_ng}"
            if n_unknown:
                inner += f" / 判定できず {n_unknown}"
            subject = (f"【出来高急増×下降】{mark}{date_label} {cutoff_label}時点 "
                       f"{n}件（業績の参考条件 {inner}）")
        body, text = _fit(rows)
        warn_html = ""
        if warning:
            warn_html = ('<div style="background:#fff4f2;border:1px solid #f0c8bf;'
                         'border-radius:10px;padding:13px 15px;margin:0 0 12px 0;'
                         'font-size:14px;line-height:1.75;color:#7a2d1c;">' +
                         ('<b>お知らせ</b><br>' if notice
                          else '<b>データに問題がある疑いがあります</b><br>' if suspect
                          else '<b>データの欠損があります</b><br>') + H.escape(warning) +
                         '</div>')
        lead = (warn_html +
                f'<div style="font-size:14px;line-height:1.75;color:#444444;'
                f'padding:0 0 4px 0;">東証プライム全銘柄から、'
                f'<b>下降トレンドのまま出来高が急に増えた銘柄</b>を探した結果です。'
                f'25日線からの乖離が深い順に並べています。</div>')
        if warning:
            text = (("※ お知らせ: " if notice
                     else "※ データに問題がある疑いがあります: " if suspect
                     else "※ データの欠損があります: ") + warning + "\n\n" + text)

    # 0件の日は rows から取れないので、呼び出し側（実際に使ったモード）から
    # 受け取る。以前は常に「過去20日平均」と書いていた。
    base_label = (base_label
                  or (rows[0].get("base_label") if rows else None)
                  or "過去20日平均")
    criteria = criteria_text(base_label)

    if failure:
        # 失敗通知に「銘柄の一覧です」と書くと中身と食い違う。出すのは理由と出所だけ。
        foot = ('<div style="font-size:12px;color:#777777;line-height:1.8;'
                'margin:18px 0 0 0;padding:14px 0 0 0;'
                'border-top:1px solid #dcdee1;">'
                '<b>数字の出所</b><br>' + SOURCES +
                '<br><br>判定していないため、銘柄の一覧は出していません。</div>')
        foot_text = ("\n── 数字の出所 ──\n" + SOURCES +
                     "\n\n判定していないため、銘柄の一覧は出していません。\n")
    else:
        foot = ('<div style="font-size:12px;color:#777777;line-height:1.8;'
                'margin:18px 0 0 0;padding:14px 0 0 0;'
                'border-top:1px solid #dcdee1;">'
                '<b>チャートについて</b><br>' + CHART_NOTE +
                '<br><br><b>判定条件</b><br>' + criteria +
                (('<br><br><b>この一覧に出た銘柄のその後</b><br>' + H.escape(track_note))
                 if track_note else '') +
                '<br><br><b>数字の出所</b><br>' + SOURCES +
                '<br><br><b>処理の内訳</b><br>' +
                '<br>'.join(H.escape(n) for n in notes) +
                '<br><br>これは条件に当てはまった銘柄の一覧です。'
                '売買をすすめるものではありません。</div>')
        foot_text = ("\n── チャート ──\n" + CHART_NOTE +
                     "\n\n── 判定条件 ──\n" + _plain(criteria) +
                     ("\n\n── この一覧に出た銘柄のその後 ──\n" + track_note
                      if track_note else "") +
                     "\n\n── 数字の出所 ──\n" + SOURCES +
                     "\n\n── 処理の内訳 ──\n" + "\n".join(notes) +
                     "\n\nこれは条件に当てはまった銘柄の一覧です。"
                     "売買をすすめるものではありません。\n")

    html_doc = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '</head>'
        f'<body style="margin:0;padding:0;background:#f4f5f7;font-family:{FONT};'
        'color:#1a1a1a;-webkit-text-size-adjust:100%;">'
        f'<div style="max-width:560px;margin:0 auto;padding:16px;'
        f'background:#f4f5f7;font-family:{FONT};color:#1a1a1a;">'
        f'{lead}{body}{foot}</div></body></html>')

    text_doc = text + foot_text
    return subject, html_doc, text_doc