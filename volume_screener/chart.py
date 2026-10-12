"""銘柄ごとの日足チャートをPNGで描く（メールに貼り込む用）。

上段: ローソク足（日足）＋ 25日線・75日線
下段: 出来高の棒グラフ ＋ 20日平均出来高の水平線

色の決め方
  ローソク足と出来高の棒は「上げ/下げ」という向きの色なので、
  メール本文と同じ 上げ=緑 / 下げ=赤 にそろえている。
  移動平均線は別物（どの線かを見分けるための色）なので、
  ローソク足とぶつからない橙と紫を使う。この2色は
  dataviz の validate_palette.js で色覚の見分けやすさを確認済み
  （最悪ペアで ΔE 27.4／通常視 30.1、いずれも合格）。
"""

import datetime as dt
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt          # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

# 日本語が出せるフォント（macOS 標準）
for _f in ("Hiragino Sans", "Hiragino Maru Gothic Pro", "Arial Unicode MS"):
    try:
        matplotlib.font_manager.findfont(_f, fallback_to_default=False)
        plt.rcParams["font.family"] = _f
        break
    except Exception:                      # noqa: BLE001, PERF203
        continue

UP = "#1e7a45"        # 上げ（メール本文の前日比プラスと同じ緑）
DOWN = "#c0392b"      # 下げ（メール本文の前日比マイナスと同じ赤）
MA25 = "#E8833A"
MA75 = "#7B61C9"
INK = "#333333"
MUTED = "#888888"
GRID = "#ebebeb"
SURFACE = "#ffffff"

WINDOW = 60           # 表示する営業日数（約3か月）
DPI = 100             # メールに載せるので、画質より容量を優先する
MAX_COLORS = 24       # PNGを減色して軽くする（線画なので見た目は変わらない）


def _ma(xs, n, i):
    if i + 1 < n:
        return None
    return sum(xs[i + 1 - n:i + 1]) / n


def _yen(v, _pos=None):
    if v >= 10000:
        return f"{v:,.0f}"
    return f"{v:,.0f}"


def _vol(v, _pos=None):
    if v >= 1e8:
        return f"{v / 1e8:.0f}億"
    if v >= 1e4:
        return f"{v / 1e4:.0f}万"
    return f"{v:,.0f}"


def make_chart(row, path, window=WINDOW, title=None):
    """row は screener の hit 1件。PNG を path に書いて path を返す。

    title を渡すと図の上に1行の見出しを描く（1枚にまとめるとき用）。
    """
    dates = row["dates"]
    closes = row["closes"]
    vols = row["vols"]
    o = row["ohlc"]["open"]
    h = row["ohlc"]["high"]
    lo = row["ohlc"]["low"]

    n = len(closes)
    ma25 = [_ma(closes, 25, i) for i in range(n)]
    ma75 = [_ma(closes, 75, i) for i in range(n)]

    s = max(0, n - window)
    idx = list(range(n - s))
    d = dates[s:]
    o, h, lo, c = o[s:], h[s:], lo[s:], closes[s:]
    v = vols[s:]
    m25, m75 = ma25[s:], ma75[s:]

    fig, (ax, axv) = plt.subplots(
        2, 1, figsize=(4.4, 2.5), dpi=DPI, sharex=True,
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.08})
    fig.patch.set_facecolor(SURFACE)

    # ---- ローソク足
    for i in range(len(idx)):
        up = c[i] >= o[i]
        col = UP if up else DOWN
        ax.vlines(i, lo[i], h[i], color=col, linewidth=0.8, zorder=2)
        body_lo, body_hi = min(o[i], c[i]), max(o[i], c[i])
        height = max(body_hi - body_lo, (max(h) - min(lo)) * 0.0015)
        ax.add_patch(plt.Rectangle(
            (i - 0.3, body_lo), 0.6, height,
            facecolor=col, edgecolor=col, linewidth=0.5, zorder=3))

    ax.plot(idx, m25, color=MA25, linewidth=1.6, zorder=4, label="25日線")
    ax.plot(idx, m75, color=MA75, linewidth=1.6, zorder=4, label="75日線")

    leg = ax.legend(loc="upper right", frameon=False, fontsize=7,
                    handlelength=1.6, borderpad=0.1, labelspacing=0.3,
                    ncol=2, columnspacing=1.2)
    for t in leg.get_texts():
        t.set_color(INK)

    ax.set_facecolor(SURFACE)
    ax.yaxis.tick_right()
    ax.grid(axis="y", color=GRID, linewidth=0.7, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.spines["right"].set_color(GRID)
    ax.tick_params(axis="y", labelsize=7, colors=MUTED, length=0, pad=2)
    ax.tick_params(axis="x", length=0)      # 上段に横軸の目盛りを出さない
    ax.yaxis.set_major_formatter(FuncFormatter(_yen))

    # ---- 出来高
    axv.bar(idx, v, width=0.6, zorder=2,
            color=[UP if c[i] >= o[i] else DOWN for i in range(len(idx))])
    if row.get("vol_avg20"):
        axv.axhline(row["vol_avg20"], color=MUTED, linewidth=0.9,
                    linestyle=(0, (4, 3)), zorder=3)

    axv.set_facecolor(SURFACE)
    axv.yaxis.tick_right()
    axv.grid(axis="y", color=GRID, linewidth=0.7, zorder=0)
    axv.set_axisbelow(True)
    for side in ("top", "left", "bottom"):
        axv.spines[side].set_visible(False)
    axv.spines["right"].set_color(GRID)
    axv.tick_params(axis="y", labelsize=6.5, colors=MUTED, length=0, pad=2)
    axv.yaxis.set_major_formatter(FuncFormatter(_vol))
    # 昔の1本だけ突出していると最近の棒が潰れて見えなくなるので、
    # 上限を97パーセンタイル付近で切る。ただし当日の棒は必ず収まるようにする。
    sv = sorted(v)
    p97 = sv[min(len(sv) - 1, int(len(sv) * 0.97))]
    axv.set_ylim(0, max(p97, v[-1], row.get("vol_avg20") or 0) * 1.18)

    # ---- 横軸は月が変わるところだけ
    ticks, labels = [], []
    for i in range(1, len(d)):
        if d[i].month != d[i - 1].month:
            ticks.append(i)
            labels.append(f"{d[i].month}月")
    axv.set_xticks(ticks)
    axv.set_xticklabels(labels, fontsize=7, color=MUTED)
    axv.tick_params(axis="x", length=0, pad=3)
    axv.set_xlim(-1, len(idx))

    # y軸ラベルを右に出しているので、左の余白を詰めて描画領域を広く取る
    fig.subplots_adjust(left=0.015, right=0.885, bottom=0.10,
                        top=0.90 if title else 0.985)
    if title:
        fig.suptitle(title, fontsize=9.5, color=INK, x=0.015, y=0.995,
                     ha="left", va="top", fontweight="bold")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path, facecolor=SURFACE, bbox_inches="tight", pad_inches=0.06)
    plt.close(fig)

    # メールに添付するので、減色して容量を落とす
    from PIL import Image
    im = Image.open(path).convert("RGB")
    im = im.quantize(colors=MAX_COLORS, method=Image.MEDIANCUT)
    im.save(path, optimize=True)
    return path


def make_sheet(hits, path, tmpdir=None):
    """全銘柄のチャートを縦に1枚へまとめる。

    メールの添付は本文中の好きな位置に置けず、必ず末尾に出る。
    そこで1枚にまとめ、各チャートに「順位・コード・銘柄名・出来高倍率」を
    焼き込んでおく。こうすれば本文の数字と突き合わせられる。
    """
    from PIL import Image

    tmpdir = tmpdir or os.path.join(os.path.dirname(path), "_parts")
    os.makedirs(tmpdir, exist_ok=True)
    parts = []
    for i, r in enumerate(hits, 1):
        # 1銘柄で失敗しても残りのチャートを失わない。以前は1件の例外で
        # 全部が作られず、添付が丸ごと消えていた（2026-10-05の見直し）。
        try:
            t = f"{i}. {r['code']} {r['name']}   出来高 {r['ratio']:.2f}倍"
            p = os.path.join(tmpdir, f"part_{i}.png")
            make_chart(r, p, title=t)
            parts.append(p)
        except Exception as e:                        # noqa: BLE001
            print(f"チャート作成に失敗 {r.get('code')}: {e}", file=sys.stderr)
    if not parts:
        return None

    ims = [Image.open(p).convert("RGB") for p in parts]
    w = max(im.width for im in ims)
    gap = 10
    h = sum(im.height for im in ims) + gap * (len(ims) - 1)
    sheet = Image.new("RGB", (w, h), (255, 255, 255))
    y = 0
    for im in ims:
        sheet.paste(im, ((w - im.width) // 2, y))
        y += im.height + gap
    sheet = sheet.quantize(colors=MAX_COLORS, method=Image.MEDIANCUT)
    sheet.save(path, optimize=True)
    for p in parts:
        os.remove(p)
    return path
