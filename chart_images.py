"""4本のnote記事用チャート画像を作る（PNG）。

設計方針:
- fetch_history() は yfinance 通信＝Mac専用（クラウドは通信不可）。
- render_chart()/compute_series() は純関数＝通信不要でクラウドでも描画テスト可（--self-test）。
- データが取れなければ画像を作らない（捏造しない）。証拠ベース。
- 公開・送信は一切しない。PNGを outputs/charts_YYYYMMDD/ に置くだけ。

代表銘柄（高重さん指定・2026-06-28）:
  ① note_chatgpt  7173.T 東京きらぼしフィナンシャルグループ（ChatGPT版300万 第1候補）
  ② note_claude   8524.T 北洋銀行（Claude版 上位・銀行テーマ）
  ③ note_pullback 7011.T 三菱重工業（52週新高値後リテスト候補）
  ④ note_highs    6951.T 日本電子（52週新高値更新）
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # GUI不要・サーバ/CIでも描画できる
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "outputs"

# 表示する直近営業日数（要件: 過去260営業日）
DISPLAY_DAYS = 260
# MA200算出のために多めに取得（260表示＋200の助走＋余裕）
FETCH_PERIOD = "600d"


@dataclass(frozen=True)
class ChartSpec:
    key: str          # note種別（chatgpt/claude/pullback/highs）
    ticker: str       # yfinanceティッカー（7173.T 等）
    code: str         # 表示用コード（7173）
    name: str         # 銘柄名
    kind: str         # trade / pullback / highs
    filename: str     # 出力PNG名


SPECS: list[ChartSpec] = [
    ChartSpec("chatgpt", "7173.T", "7173", "東京きらぼしフィナンシャルグループ", "trade", "chart_chatgpt_7173.png"),
    ChartSpec("claude", "8524.T", "8524", "北洋銀行", "trade", "chart_claude_8524.png"),
    ChartSpec("pullback", "7011.T", "7011", "三菱重工業", "pullback", "chart_pullback_7011.png"),
    ChartSpec("highs", "6951.T", "6951", "日本電子", "highs", "chart_highs_6951.png"),
]

# 取引チャートの値幅ルール（高重さん指定）
STOP_LOSS_PCT = -0.07   # 損切り -7%
TAKE_PROFIT_PCT = 0.15  # 利確 +15%


# ---------------------------------------------------------------------------
# 日本語フォント（無ければ豆腐を避けて英語表記にフォールバック）
# ---------------------------------------------------------------------------
_JP_CANDIDATES = [
    "Hiragino Sans", "Hiragino Maru Gothic Pro", "Hiragino Kaku Gothic Pro",
    "YuGothic", "Yu Gothic", "Noto Sans CJK JP", "Noto Sans JP",
    "IPAexGothic", "IPAGothic", "TakaoGothic", "VL Gothic", "MS Gothic", "Meiryo",
]


def setup_jp_font() -> str | None:
    """利用可能な日本語フォントを matplotlib に設定する。見つかれば名前、無ければ None。"""
    available = {f.name for f in font_manager.fontManager.ttflist}
    for cand in _JP_CANDIDATES:
        if cand in available:
            matplotlib.rcParams["font.family"] = cand
            matplotlib.rcParams["axes.unicode_minus"] = False
            return cand
    # 部分一致でも探す（"Hiragino Sans W3" のような派生名対策）
    for cand in _JP_CANDIDATES:
        for name in available:
            if cand.lower() in name.lower():
                matplotlib.rcParams["font.family"] = name
                matplotlib.rcParams["axes.unicode_minus"] = False
                return name
    matplotlib.rcParams["axes.unicode_minus"] = False
    return None


_JP_OK = False  # main()/self-testで設定する


def t(jp: str, en: str) -> str:
    """日本語フォントがあれば日本語、無ければ英語（豆腐回避）。"""
    return jp if _JP_OK else en


# ---------------------------------------------------------------------------
# 純関数: 終値系列から指標を計算（通信不要＝クラウドでもテスト可）
# ---------------------------------------------------------------------------
def compute_series(dates: list, closes: list[float], highs: list[float]) -> dict:
    """終値・高値の配列から表示用の系列・水準を計算する。

    返り値: dates / close / ma25 / ma75 / ma200（Noneパディング済み）/
            current（最終終値）/ high_52w（直近252本の高値最大）。
    """
    n = len(closes)
    if n == 0:
        raise ValueError("価格データが空です")

    def sma(values: list[float], window: int) -> list[float | None]:
        out: list[float | None] = []
        for i in range(len(values)):
            if i + 1 < window:
                out.append(None)
            else:
                seg = values[i + 1 - window:i + 1]
                out.append(sum(seg) / window)
        return out

    ma25 = sma(closes, 25)
    ma75 = sma(closes, 75)
    ma200 = sma(closes, 200)
    window_52w = min(252, n)
    high_52w = max(highs[-window_52w:]) if highs else max(closes[-window_52w:])
    current = closes[-1]
    return {
        "dates": dates,
        "close": closes,
        "ma25": ma25,
        "ma75": ma75,
        "ma200": ma200,
        "current": current,
        "high_52w": high_52w,
    }


def _levels_for(spec: ChartSpec, series: dict, csv_extra: dict | None) -> list[tuple[float, str, str]]:
    """種別ごとの水平線 (価格, ラベル, 色)。csv_extra があれば実データ優先。"""
    current = series["current"]
    high_52w = series["high_52w"]
    levels: list[tuple[float, str, str]] = []
    # 52週高値ラインは全チャート共通
    levels.append((high_52w, t(f"52週高値 {high_52w:,.0f}", f"52w high {high_52w:,.0f}"), "#c0392b"))

    if spec.kind == "trade":
        buy = current
        stop = current * (1 + STOP_LOSS_PCT)
        take = current * (1 + TAKE_PROFIT_PCT)
        levels.append((buy, t(f"想定買値 {buy:,.0f}", f"entry {buy:,.0f}"), "#2c3e50"))
        levels.append((stop, t(f"損切り -7% {stop:,.0f}", f"stop -7% {stop:,.0f}"), "#7f8c8d"))
        levels.append((take, t(f"利確 +15% {take:,.0f}", f"target +15% {take:,.0f}"), "#16a085"))
    elif spec.kind == "pullback":
        # 新高値ブレイクライン: CSVの retest_line_price があれば実値、無ければ52週高値で代用
        line = None
        if csv_extra and csv_extra.get("retest_line_price") is not None:
            line = csv_extra["retest_line_price"]
        if line is None:
            line = high_52w
        levels.append((line, t(f"新高値ブレイクライン {line:,.0f}", f"breakout line {line:,.0f}"), "#8e44ad"))
    elif spec.kind == "highs":
        levels.append((high_52w, t(f"52週新高値更新ライン {high_52w:,.0f}", f"new 52w high {high_52w:,.0f}"), "#8e44ad"))
    return levels


def _caption_for(spec: ChartSpec, series: dict, csv_extra: dict | None) -> str:
    current = series["current"]
    high_52w = series["high_52w"]
    if spec.kind == "trade":
        return t("300万円運用候補（想定買値・損切り-7%・利確+15%）",
                 "300man candidate (entry / stop -7% / target +15%)")
    if spec.kind == "pullback":
        line = (csv_extra or {}).get("retest_line_price") or high_52w
        diff_pct = (current - line) / line * 100 if line else 0.0
        return t(f"52週新高値後リテスト候補（現在値はライン比 {diff_pct:+.1f}%）",
                 f"post-52w-high retest (price vs line {diff_pct:+.1f}%)")
    if spec.kind == "highs":
        return t("52週新高値更新", "52-week new high")
    return ""


# ---------------------------------------------------------------------------
# 純関数: 描画（通信不要）
# ---------------------------------------------------------------------------
def render_chart(spec: ChartSpec, series: dict, out_path: Path, csv_extra: dict | None = None) -> Path:
    dates = series["dates"]
    close = series["close"]

    # 表示は直近 DISPLAY_DAYS 本だけ（MAは全期間で計算済み）
    show = min(DISPLAY_DAYS, len(close))
    d = dates[-show:]
    c = close[-show:]
    ma25 = series["ma25"][-show:]
    ma75 = series["ma75"][-show:]
    ma200 = series["ma200"][-show:]

    fig, ax = plt.subplots(figsize=(11, 6))
    ax.plot(d, c, color="#1f2d3d", linewidth=1.6, label=t("終値", "Close"))
    ax.plot(d, ma25, color="#e67e22", linewidth=1.1, label="MA25")
    ax.plot(d, ma75, color="#2980b9", linewidth=1.1, label="MA75")
    ax.plot(d, ma200, color="#27ae60", linewidth=1.1, label="MA200")

    for price, label, color in _levels_for(spec, series, csv_extra):
        ax.axhline(price, color=color, linewidth=1.0, linestyle="--", alpha=0.85)
        ax.text(d[0], price, " " + label, color=color, fontsize=8,
                va="bottom", ha="left")

    # 現在値ラベル（最終終値）
    current = series["current"]
    ax.scatter([d[-1]], [current], color="#1f2d3d", zorder=5, s=22)
    ax.annotate(t(f"現在値 {current:,.0f}", f"now {current:,.0f}"),
                xy=(d[-1], current), xytext=(6, 0), textcoords="offset points",
                fontsize=9, fontweight="bold", color="#1f2d3d", va="center")

    today = datetime.now().strftime("%Y-%m-%d")
    if _JP_OK:
        title = f"{spec.name}（{spec.code}）  日足  作成日 {today}"
    else:
        title = f"{spec.code}.T  daily  {today}"
    ax.set_title(title, fontsize=13, fontweight="bold", loc="left")

    caption = _caption_for(spec, series, csv_extra)
    if caption:
        ax.text(0.0, -0.12, caption, transform=ax.transAxes, fontsize=10,
                color="#444", va="top", ha="left")

    ax.grid(True, color="#e6e6e6", linewidth=0.7)
    ax.legend(loc="upper left", fontsize=8, ncols=4, framealpha=0.9)
    try:
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%y/%m"))
    except Exception:
        pass
    fig.autofmt_xdate()
    fig.tight_layout()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# 通信あり: yfinanceで取得（Mac専用）
# ---------------------------------------------------------------------------
def fetch_history(ticker: str):
    """yfinanceで日足を取得。MultiIndex/単一どちらの形でも (dates, closes, highs) を返す。
    失敗・空なら None。"""
    try:
        import yfinance as yf
    except Exception as exc:  # noqa: BLE001
        print(f"chart_fetch_error[{ticker}]=yfinance import失敗: {exc}")
        return None
    try:
        df = yf.download(ticker, period=FETCH_PERIOD, interval="1d",
                         auto_adjust=False, progress=False, threads=False)
    except Exception as exc:  # noqa: BLE001
        print(f"chart_fetch_error[{ticker}]={exc}")
        return None
    if df is None or len(df) == 0:
        print(f"chart_fetch_error[{ticker}]=データ空（取得失敗）")
        return None

    def col(name: str):
        if isinstance(df.columns, type(df.columns)) and hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            try:
                return df[name].iloc[:, 0]
            except Exception:
                return df[name]
        return df[name]

    try:
        closes = [float(x) for x in col("Close").tolist()]
        highs = [float(x) for x in col("High").tolist()]
        dates = list(df.index.to_pydatetime())
    except Exception as exc:  # noqa: BLE001
        print(f"chart_fetch_error[{ticker}]=列の取り出しに失敗: {exc}")
        return None
    # NaN除去（休場日など）
    clean = [(dt, cl, hi) for dt, cl, hi in zip(dates, closes, highs)
             if cl == cl and hi == hi]  # NaN!=NaN
    if not clean:
        print(f"chart_fetch_error[{ticker}]=有効データなし")
        return None
    dts = [x[0] for x in clean]
    cls = [x[1] for x in clean]
    his = [x[2] for x in clean]
    return dts, cls, his


# ---------------------------------------------------------------------------
# CSV補助（実データのラインを使うため・通信不要）
# ---------------------------------------------------------------------------
def _latest_csv(prefix: str) -> Path | None:
    paths = list(OUTPUT_DIR.glob(f"{prefix}_*.csv"))
    if not paths:
        return None
    return max(paths, key=lambda p: p.stat().st_mtime)


def lookup_csv_extra(spec: ChartSpec) -> dict | None:
    """note本文と一致させるため、押し目/高値CSVから該当コードの実値を拾う。
    無ければ None（チャートは価格履歴から自己完結で描く）。"""
    try:
        import pandas as pd
    except Exception:
        return None
    prefix = {"pullback": "screening_pullback", "highs": "screening_highs"}.get(spec.kind)
    if not prefix:
        return None
    path = _latest_csv(prefix)
    if path is None:
        return None
    try:
        df = pd.read_csv(path)
        df["code"] = df["code"].astype(str)
        row = df[df["code"] == spec.code]
        if row.empty:
            return None
        r = row.iloc[0]
        extra: dict = {}
        for key in ("retest_line_price", "retest_breakout_date", "high_52w", "high_date"):
            if key in df.columns:
                val = r.get(key)
                try:
                    extra[key] = float(val) if key.endswith("price") or key == "high_52w" else val
                except (TypeError, ValueError):
                    extra[key] = val
        return extra or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 1枚作る（取得＋描画）。Mac専用（通信あり）。
# ---------------------------------------------------------------------------
def make_chart(spec: ChartSpec, charts_dir: Path) -> Path | None:
    fetched = fetch_history(spec.ticker)
    if fetched is None:
        return None  # データ無し＝画像を作らない（捏造しない）
    dates, closes, highs = fetched
    if len(closes) < 25:
        print(f"chart_skip[{spec.key}]=本数不足({len(closes)})でMA算出不可")
        return None
    series = compute_series(dates, closes, highs)
    csv_extra = lookup_csv_extra(spec)
    out_path = charts_dir / spec.filename
    return render_chart(spec, series, out_path, csv_extra)


def build_all() -> list[tuple[str, Path | None]]:
    charts_dir = OUTPUT_DIR / f"charts_{datetime.now().strftime('%Y%m%d')}"
    results: list[tuple[str, Path | None]] = []
    for spec in SPECS:
        try:
            path = make_chart(spec, charts_dir)
        except Exception as exc:  # noqa: BLE001 - 1枚失敗で他を止めない
            print(f"chart_error[{spec.key}]={exc}")
            path = None
        results.append((spec.key, path))
        if path is not None:
            print(f"chart_saved[{spec.key}]={path}")
        else:
            print(f"chart_missing[{spec.key}]=画像なし（データ取得不可・Mac実行が必要）")
    return results


# ---------------------------------------------------------------------------
# self-test（通信不要）: 合成データで compute+render を回し、PNGが出来るか確認
# ---------------------------------------------------------------------------
def _self_test() -> int:
    import math
    import tempfile

    global _JP_OK
    _JP_OK = setup_jp_font() is not None

    n = 300
    base = 1000.0
    closes = [base + 200 * math.sin(i / 18.0) + i * 0.6 for i in range(n)]
    highs = [c * 1.01 for c in closes]
    dates = [datetime(2025, 1, 1) + __import__("datetime").timedelta(days=i) for i in range(n)]

    series = compute_series(dates, closes, highs)
    assert series["ma25"][-1] is not None, "MA25が計算されていない"
    assert series["ma200"][-1] is not None, "MA200が計算されていない"
    assert series["current"] == closes[-1], "current不一致"
    assert series["high_52w"] == max(highs[-252:]), "52週高値不一致"

    passed = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for spec in SPECS:
            extra = {"retest_line_price": series["high_52w"] * 0.98} if spec.kind == "pullback" else None
            out = render_chart(spec, series, tmpdir / spec.filename, extra)
            assert out.exists(), f"{spec.filename} が作られていない"
            assert out.stat().st_size > 1000, f"{spec.filename} が小さすぎる（描画失敗の疑い）"
            passed += 1
            print(f"self_test_render_ok[{spec.key}]={out.name} size={out.stat().st_size}")

    print(f"jp_font={'あり:' + (setup_jp_font() or '') if _JP_OK else 'なし(英語表記にフォールバック)'}")
    print(f"SELF_TEST_PASS render={passed}/{len(SPECS)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="note用チャート画像(PNG)を作成する")
    parser.add_argument("--self-test", action="store_true", help="通信せず描画だけ検証")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    global _JP_OK
    _JP_OK = setup_jp_font() is not None
    results = build_all()
    made = [p for _, p in results if p is not None]
    print(f"charts_built={len(made)}/{len(SPECS)}")
    print(f"charts_dir={OUTPUT_DIR / ('charts_' + datetime.now().strftime('%Y%m%d'))}")
    return 0 if made else 1


if __name__ == "__main__":
    sys.exit(main())
