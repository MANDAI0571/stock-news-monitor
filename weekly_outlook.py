from __future__ import annotations

import concurrent.futures as cf
import math
import time
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd
import requests

from scanner.universe import UniverseConfig, load_jpx_listed


PERIOD = "5y"
MIN_TURNOVER_YEN = 1e8
LIST_TURNOVER_OKU = 20
WEEKLY_CLIP = 0.4
# 東証の値幅制限では1日で6割は動かない。超えていたら分割の未調整なので順位から外す。
BROKEN_DAILY_MOVE = 0.6
TOP_N_STOCKS = 20
FETCH_WORKERS = 6
FETCH_TRIES = 3
FETCH_BUDGET_SECONDS = 600
MIN_COVERAGE = 0.8
EDGE_SECTORS = 5
PRIOR_WEIGHT = 5
OIL_MOVE_PCT = 5.0
OIL_ADJUST_PT = 3
PROBABILITY_RANGE = (35, 65)
SQ_START = "1989-06-12"
GAUGES = (
    ("ブレント原油", "BZ=F", "pct"),
    ("WTI原油", "CL=F", "pct"),
    ("ドル円", "USDJPY=X", "pct"),
    ("米10年金利", "^TNX", "diff"),
    ("VIX", "^VIX", "diff"),
)
QUADRANTS = {"主導": "強く、さらに強まる", "失速": "強いが、弱まる", "回復": "弱いが、強まる", "出遅れ": "弱く、さらに弱まる"}
CYCLE_NAMES = {0: "米大統領選の年", 1: "米大統領選の翌年", 2: "米中間選挙の年", 3: "米大統領選の前年"}
# JPL の近似軌道要素（1800〜2050年）。a, e, I, L, 近日点, 昇交点と、それぞれの世紀あたりの変化。
ORBITS = {
    "mercury": (0.38709927, 0.20563593, 7.00497902, 252.25032350, 77.45779628, 48.33076593,
                0.00000037, 0.00001906, -0.00594749, 149472.67411175, 0.16047689, -0.12534081),
    "earth": (1.00000261, 0.01671123, -0.00001531, 100.46457166, 102.93768193, 0.0,
              0.00000562, -0.00004392, -0.01294668, 35999.37244981, 0.32327364, 0.0),
}


@dataclass(frozen=True)
class Panel:
    close: pd.DataFrame
    high: pd.DataFrame
    volume: pd.DataFrame
    names: pd.Series
    sectors: pd.Series
    topix: pd.Series


@dataclass(frozen=True)
class Outlook:
    mail_text: str
    public_text: str
    stocks: pd.DataFrame
    sectors: pd.DataFrame


def _orbit_xy(body: str, julian_day: float) -> tuple[float, float]:
    t = (julian_day - 2451545.0) / 36525
    a0, e0, i0, l0, w0, o0, da, de, di, dl, dw, do = ORBITS[body]
    a, e, incl = a0 + da * t, e0 + de * t, math.radians(i0 + di * t)
    long_mean, peri, node = l0 + dl * t, w0 + dw * t, o0 + do * t
    mean = math.radians((long_mean - peri) % 360)
    ecc = mean
    for _ in range(12):
        ecc -= (ecc - e * math.sin(ecc) - mean) / (1 - e * math.cos(ecc))
    x, y = a * (math.cos(ecc) - e), a * math.sqrt(1 - e * e) * math.sin(ecc)
    arg, omega = math.radians(peri - node), math.radians(node)
    ca, sa, co, so, ci = math.cos(arg), math.sin(arg), math.cos(omega), math.sin(omega), math.cos(incl)
    return (ca * co - sa * so * ci) * x + (-sa * co - ca * so * ci) * y, (ca * so + sa * co * ci) * x + (-sa * so + ca * co * ci) * y


def _mercury_longitude(day: date) -> float:
    julian_day = day.toordinal() + 1721424.5
    mx, my = _orbit_xy("mercury", julian_day)
    ex, ey = _orbit_xy("earth", julian_day)
    return math.degrees(math.atan2(my - ey, mx - ex)) % 360


def mercury_retrograde_days(start: date, end: date) -> set[date]:
    days: set[date] = set()
    day = start
    while day <= end:
        if ((_mercury_longitude(day + timedelta(days=1)) - _mercury_longitude(day) + 180) % 360) - 180 < 0:
            days.add(day)
        day += timedelta(days=1)
    return days


def retrograde_periods(days: set[date]) -> list[tuple[date, date]]:
    periods: list[tuple[date, date]] = []
    for day in sorted(days):
        if periods and day == periods[-1][1] + timedelta(days=1):
            periods[-1] = (periods[-1][0], day)
        else:
            periods.append((day, day))
    return periods


def regime(weekly_return: float) -> str:
    if weekly_return <= -0.03:
        return "下落"
    return "上昇" if weekly_return >= 0.03 else "横ばい"


def regime_label(name: str) -> str:
    return {"下落": "市場が-3%以下の週", "上昇": "市場が+3%以上の週", "横ばい": "市場が±3%以内の週"}[name]


def quadrant(strength: float, change: float) -> str:
    if strength >= 0:
        return "主導" if change >= 0 else "失速"
    return "回復" if change >= 0 else "出遅れ"


def week_base(index: pd.DatetimeIndex) -> tuple[pd.Timestamp, pd.Timestamp]:
    last = index[-1]
    monday = last.normalize() - pd.Timedelta(days=last.weekday())
    return index[index < monday][-1], last


def _yahoo_chart(symbol: str, params: dict[str, object], tz: str) -> pd.DataFrame:
    response = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(symbol)}",
        params=params,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()["chart"]["result"][0]
    stamps = pd.to_datetime(result["timestamp"], unit="s", utc=True).tz_convert(tz).tz_localize(None).normalize()
    quote = result["indicators"]["quote"][0]
    frame = pd.DataFrame({"Close": quote["close"], "High": quote["high"], "Volume": quote["volume"]}, index=stamps, dtype="float64").dropna(subset=["Close"])
    return frame[~frame.index.duplicated(keep="last")].sort_index()


def _yahoo_close(symbol: str, params: dict[str, object], tz: str) -> pd.Series:
    return _yahoo_chart(symbol, params, tz)["Close"]


def _fetch_daily(ticker: str, deadline: float) -> pd.DataFrame | None:
    for attempt in range(FETCH_TRIES):
        if time.monotonic() > deadline:
            return None
        try:
            return _yahoo_chart(ticker, {"range": PERIOD, "interval": "1d"}, "Asia/Tokyo")
        except Exception:  # noqa: BLE001
            time.sleep(1.2 * (attempt + 1))
    return None


def fetch_nikkei_history() -> pd.Series:
    period2 = int(pd.Timestamp.now(tz="UTC").timestamp())
    return _yahoo_close("^N225", {"period1": -157766400, "period2": period2, "interval": "1d"}, "Asia/Tokyo")


def fetch_gauges() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for label, symbol, kind in GAUGES:
        series = _yahoo_close(symbol, {"range": "1mo", "interval": "1d"}, "UTC")
        base, last = week_base(pd.DatetimeIndex(series.index))
        change = series[last] - series[base] if kind == "diff" else (series[last] / series[base] - 1) * 100
        rows.append({"label": label, "last": float(series[last]), "change": float(change), "kind": kind})
    return rows


def load_panel() -> Panel:
    universe = load_jpx_listed(UniverseConfig(markets=("prime",)))
    universe = universe[~universe["sector"].astype(str).str.strip().isin(["", "-", "nan"])]
    codes = universe.set_index("code")
    deadline = time.monotonic() + FETCH_BUDGET_SECONDS
    with cf.ThreadPoolExecutor(FETCH_WORKERS) as pool:
        histories = dict(zip(codes.index, pool.map(lambda ticker: _fetch_daily(ticker, deadline), codes["ticker"])))
    topix = _fetch_daily("1306.T", deadline)
    fetched = {code: frame for code, frame in histories.items() if frame is not None and not frame.empty}
    if topix is None or len(fetched) < len(codes) * MIN_COVERAGE:
        raise RuntimeError("株価を十分に取得できませんでした")
    return Panel(
        close=pd.DataFrame({code: frame["Close"] for code, frame in fetched.items()}).sort_index(),
        high=pd.DataFrame({code: frame["High"] for code, frame in fetched.items()}).sort_index(),
        volume=pd.DataFrame({code: frame["Volume"] for code, frame in fetched.items()}).sort_index(),
        names=codes["name"],
        sectors=codes["sector"].reindex(list(fetched)),
        topix=topix["Close"],
    )


def weekly_frames(panel: Panel) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    closes = panel.close.resample("W-FRI").last()
    turnover = (panel.close * panel.volume).resample("W-FRI").mean()
    returns = closes.pct_change(fill_method=None).clip(-WEEKLY_CLIP, WEEKLY_CLIP)
    liquid = returns.where(turnover >= MIN_TURNOVER_YEN)
    market = liquid.mean(axis=1).dropna()
    sector_mean = liquid.T.groupby(panel.sectors).mean().T
    return liquid, market, sector_mean.sub(market, axis=0).dropna(how="all")


def build_stock_table(panel: Panel) -> pd.DataFrame:
    base, last = week_base(pd.DatetimeIndex(panel.close.index))
    close = panel.close
    sessions = close.index[(close.index > base) & (close.index <= last)]
    daily = close.loc[close.index >= base].pct_change(fill_method=None).iloc[1:]
    topix_return = panel.topix[last] / panel.topix[base] - 1
    weekly = close.loc[last] / close.loc[base] - 1
    position = close.index.get_loc(last)
    prior_high = panel.high.loc[panel.high.index <= base].iloc[-245:].max()
    table = pd.DataFrame({"銘柄名": panel.names.reindex(close.columns), "業種": panel.sectors})
    table["週間騰落率%"] = weekly * 100
    table["終値"] = close.loc[last]
    table["TOPIX比_pt"] = (weekly - topix_return) * 100
    table["週平均売買代金_億円"] = (close * panel.volume).loc[sessions].mean() / 1e8
    table["1か月%"] = (close.loc[last] / close.iloc[position - 21] - 1) * 100
    table["3か月%"] = (close.loc[last] / close.iloc[position - 63] - 1) * 100
    table["52週高値からの距離%"] = (close.loc[last] / panel.high.iloc[-245:].max() - 1) * 100
    table["今週52週高値更新"] = panel.high.loc[sessions].max() >= prior_high
    table["25日線乖離%"] = (close.loc[last] / close.iloc[-25:].mean() - 1) * 100
    table["出来高倍率(前20日比)"] = panel.volume.loc[sessions].mean() / panel.volume.loc[panel.volume.index <= base].iloc[-20:].mean()
    table["上昇日数"] = (daily > 0).sum()
    table["1日最大上昇%"] = daily.max() * 100
    table["その日"] = daily.idxmax().dt.strftime("%m/%d")
    table["売買代金1億円以上"] = table["週平均売買代金_億円"] >= 1
    table = table[daily.abs().max() < BROKEN_DAILY_MOVE].dropna(subset=["週間騰落率%"])
    table.index.name = "コード"
    return table.sort_values("週間騰落率%", ascending=False)


def _hit_rate(values: list[bool] | np.ndarray) -> float:
    return float(np.mean(values) * 100) if len(values) else 50.0


def _sample_weeks(relative: pd.DataFrame, market: pd.Series, name: str) -> list[int]:
    return [i for i in range(len(relative) - 1) if regime(float(market[relative.index[i]])) == name]


def build_sector_forecast(relative: pd.DataFrame, market: pd.Series, brent_change: float | None) -> tuple[pd.DataFrame, dict[str, object]]:
    current = regime(float(market[relative.index[-1]]))
    weeks = _sample_weeks(relative, market, current)
    strength, change = relative.rolling(12).sum(), relative.rolling(12).sum().diff(4)
    quadrant_wins: dict[str, list[bool]] = {name: [] for name in QUADRANTS}
    rank_wins: dict[str, list[bool]] = {"上位": [], "中位": [], "下位": []}
    for i in weeks:
        order = relative.iloc[i].sort_values(ascending=False).index
        following = relative.iloc[i + 1]
        for j, sector in enumerate(order):
            group = "上位" if j < EDGE_SECTORS else "下位" if j >= len(order) - EDGE_SECTORS else "中位"
            rank_wins[group].append(bool(following[sector] > 0))
            if not (np.isnan(strength.iloc[i][sector]) or np.isnan(change.iloc[i][sector])):
                quadrant_wins[quadrant(strength.iloc[i][sector], change.iloc[i][sector])].append(bool(following[sector] > 0))
    latest = relative.iloc[-1].sort_values(ascending=False)
    rows = []
    for j, sector in enumerate(latest.index):
        wins = sum(bool(relative.iloc[i + 1][sector] > 0) for i in weeks)
        shrunk = (wins + PRIOR_WEIGHT) / (len(weeks) + 2 * PRIOR_WEIGHT) * 100
        place = quadrant(strength.iloc[-1][sector], change.iloc[-1][sector])
        group = "上位" if j < EDGE_SECTORS else "下位" if j >= len(latest) - EDGE_SECTORS else "中位"
        data_probability = float(np.mean([shrunk, _hit_rate(quadrant_wins[place]), _hit_rate(rank_wins[group])]))
        rows.append({
            "業種": sector, "週間順位": j + 1, "市場平均比_pt": latest[sector] * 100,
            "12週累積の市場比_pt": strength.iloc[-1][sector] * 100, "12週相対の4週変化_pt": change.iloc[-1][sector] * 100,
            "循環の位置": place, f"{current}週の翌週の勝ち": f"{wins}/{len(weeks)}", "データの確率": data_probability,
        })
    table = pd.DataFrame(rows).set_index("業種")
    adjust = pd.Series(0, index=table.index)
    reason = pd.Series("", index=table.index, dtype="object")
    if brent_change is not None and abs(brent_change) >= OIL_MOVE_PCT:
        sign = 1 if brent_change < 0 else -1
        for sector, direction, text in (("空運業", 1, "燃料費"), ("石油・石炭製品", -1, "原油価格"), ("鉱業", -1, "原油価格")):
            if sector in adjust.index:
                adjust[sector] = sign * direction * OIL_ADJUST_PT
                reason[sector] = f"ブレント週間{brent_change:+.1f}%（{text}）"
    table["原油の補正"] = adjust
    table["補正の理由"] = reason
    table["最終確率"] = (table["データの確率"] + adjust).clip(*PROBABILITY_RANGE).round(0).astype(int)
    table = table.sort_values(["最終確率", "データの確率"], ascending=False)
    top_wins, bottom_wins = [], []
    for held in weeks:
        others = [i for i in weeks if i != held]
        score = pd.Series({s: (sum(bool(relative.iloc[i + 1][s] > 0) for i in others) + PRIOR_WEIGHT) / (len(others) + 2 * PRIOR_WEIGHT) for s in relative.columns}).sort_values(ascending=False)
        following = relative.iloc[held + 1]
        top_wins += list(following[score.index[:EDGE_SECTORS]] > 0)
        bottom_wins += list(following[score.index[-EDGE_SECTORS:]] > 0)
    stats = {
        "regime": current, "weeks": len(weeks), "quadrants": {k: (_hit_rate(v), len(v)) for k, v in quadrant_wins.items()},
        "holdout_top": _hit_rate(top_wins), "holdout_bottom": _hit_rate(bottom_wins),
        "average": _hit_rate([x for v in rank_wins.values() for x in v]),
    }
    return table, stats


def strong_stock_history(liquid: pd.DataFrame, market: pd.Series) -> dict[str, float]:
    current = regime(float(market.iloc[-1]))
    excess: list[float] = []
    for i in range(1, len(liquid.index) - 1):
        week, following = liquid.index[i], liquid.index[i + 1]
        if week not in market.index or following not in market.index or regime(float(market[week])) != current:
            continue
        ranked = liquid.loc[week].dropna().sort_values(ascending=False)
        if len(ranked) >= 200:
            excess.append(float(liquid.loc[following].reindex(ranked.index[:TOP_N_STOCKS]).mean() - market[following]))
    return {"weeks": len(excess), "excess": float(np.mean(excess) * 100) if excess else 0.0, "win": _hit_rate(np.array(excess) > 0)}


def rotation_lines(relative: pd.DataFrame, stats: dict[str, object], table: pd.DataFrame) -> list[str]:
    lines = ["■ 循環物色（どの業種に資金が回ったか）"]
    for week in relative.index[-5:]:
        leaders = relative.loc[week].sort_values(ascending=False).head(3)
        lines.append(f"・{week:%m/%d}週: " + "、".join(f"{k}{v * 100:+.1f}" for k, v in leaders.items()))
    correlations = {}
    for lag in (1, 2, 3, 4, 8, 12):
        values = [relative.iloc[i].rank().corr(relative.iloc[i + lag].rank()) for i in range(len(relative) - lag)]
        correlations[lag] = (float(np.nanmean(values)), float(np.nanstd(values) / math.sqrt(len(values))))
    found = [f"{lag}週" for lag, (mean, error) in correlations.items() if abs(mean) >= 2 * error]
    worst = max(abs(mean) for mean, _ in correlations.values())
    lines.append(
        f"・周期の検証: 1〜12週のどの間隔でも業種の順位相関は最大{worst:.3f}。" + (f"{'、'.join(found)}に統計的な差があります。" if found else "決まった周期は確認できません。")
    )
    counts = table["循環の位置"].value_counts()
    lines.append("・今の位置: " + "、".join(f"{name}（{QUADRANTS[name]}）{int(counts.get(name, 0))}" for name in QUADRANTS))
    label = regime_label(str(stats["regime"]))
    lines.append(f"・{label}の翌週の勝率: " + "、".join(f"{name}{rate:.0f}%" for name, (rate, _) in stats["quadrants"].items()))
    return lines


def _welch_t(a: pd.Series, b: pd.Series) -> float:
    return float((a.mean() - b.mean()) / math.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b)))


def _verdict(t: float) -> str:
    return "統計的な差あり" if abs(t) >= 2 else "偶然の範囲"


def anomaly_lines(nikkei: pd.Series, next_monday: date) -> tuple[list[str], dict[str, float]]:
    weekly = nikkei.resample("W-FRI").last().pct_change(fill_method=None).dropna()
    following = weekly.shift(-1)
    last_return = float(weekly.iloc[-1])
    edges = [-np.inf, -0.07, -0.03, 0.03, 0.07, np.inf]
    labels = ["-7%以下", "-7〜-3%", "±3%以内", "+3〜+7%", "+7%以上"]
    buckets = pd.cut(weekly, edges, labels=labels)
    sample = following[buckets == buckets.iloc[-1]].dropna()
    up = int((sample > 0).sum())
    probability = (up + PRIOR_WEIGHT) / (len(sample) + 2 * PRIOR_WEIGHT) * 100
    lines = [
        "■ アノマリーの検証（日経平均・1965年〜）",
        f"・今週{last_return * 100:+.1f}%と同じ区分（{buckets.iloc[-1]}）の翌週: 上昇{up / len(sample) * 100:.0f}%、平均{sample.mean() * 100:+.2f}%、中央値{sample.median() * 100:+.2f}%、最悪{sample.min() * 100:+.2f}%（{len(sample)}回）",
        f"  全部の週は上昇{(weekly > 0).mean() * 100:.0f}%、平均{weekly.mean() * 100:+.2f}%",
    ]
    monthly = nikkei.resample("ME").last().pct_change(fill_method=None).dropna()
    complete = monthly[monthly.index.to_period("M") < pd.Timestamp(nikkei.index[-1]).to_period("M")]
    same = complete[complete.index.month == next_monday.month]
    lines.append(f"・{next_monday.month}月: 上昇{(same > 0).mean() * 100:.0f}%、平均{same.mean() * 100:+.2f}%（{len(same)}回）")
    cycle = next_monday.year % 4
    cycle_months = same[[year % 4 == cycle for year in same.index.year]]
    lines.append(f"・{CYCLE_NAMES[cycle]}の{next_monday.month}月: 上昇{(cycle_months > 0).mean() * 100:.0f}%、平均{cycle_months.mean() * 100:+.2f}%（{len(cycle_months)}回）")
    since = weekly[SQ_START:]
    is_sq = pd.Series([8 <= day.day <= 14 for day in since.index], index=since.index)
    t_value = _welch_t(since[is_sq], since[~is_sq])
    friday = next_monday + timedelta(days=4)
    tag = "来週はSQ週" if 8 <= friday.day <= 14 else "来週はSQ週ではありません"
    lines.append(f"・SQ週（{tag}）: 平均{since[is_sq].mean() * 100:+.2f}%、他の週{since[~is_sq].mean() * 100:+.2f}%（{_verdict(t_value)}、t={t_value:.2f}）")
    start = nikkei.index[0].date() - timedelta(days=30)
    days = mercury_retrograde_days(start, next_monday + timedelta(days=60))
    daily = nikkei.pct_change(fill_method=None).dropna()
    in_retrograde = pd.Series([stamp.date() in days for stamp in daily.index], index=daily.index)
    t_mercury = _welch_t(daily[in_retrograde], daily[~in_retrograde])
    lines.append(f"・水星逆行: 逆行中の1日平均{daily[in_retrograde].mean() * 100:+.3f}%、順行中{daily[~in_retrograde].mean() * 100:+.3f}%（{_verdict(t_mercury)}、t={t_mercury:.2f}）")
    current = [(a, b) for a, b in retrograde_periods(days) if a <= next_monday + timedelta(days=4) and b >= next_monday]
    if current:
        lines.append(f"  来週は逆行の期間に当たります（{current[0][0]:%m/%d}〜{current[0][1]:%m/%d}ごろ）。")
    else:
        lines.append("  来週は逆行の期間ではありません。")
    lines.append("  SQ週と水星逆行は、来週の確率には入れていません。")
    return lines, {"probability": probability, "up": up / len(sample) * 100, "count": float(len(sample))}


def gauge_lines(gauges: list[dict[str, object]]) -> list[str]:
    lines = ["■ 地政学リスクの計器（週間の変化）"]
    for row in gauges:
        unit = "%" if row["kind"] == "pct" else "pt"
        lines.append(f"・{row['label']}: {row['last']:,.2f}（{row['change']:+.2f}{unit}）")
    return lines


def _by_cap(frame: pd.DataFrame) -> pd.DataFrame:
    """時価総額の大きい順（2026-11-13 持ち主「スクリーニングした銘柄はすべて時価総額順に並べる」）。
    選ぶのは今までどおり週間の上げ幅の上位で、並びだけを変える。時価総額が出ない銘柄は最後。"""
    import market_cap

    if frame.empty or "終値" not in frame.columns:
        return frame
    out = market_cap.sort_frame_by_market_cap(frame.rename_axis("コード").reset_index(), "コード", "終値")
    return out.set_index("コード")


def _stock_line(rank: int, code: str, row: pd.Series) -> str:
    import market_cap

    high = "・52週高値更新" if row["今週52週高値更新"] else ""
    cap = market_cap.cap_text(row.get("market_cap_yen"))
    return (f"{rank}. {row['銘柄名']}（{code}・{row['業種']}{'・' + cap if cap else ''}）{row['週間騰落率%']:+.1f}%"
            f"（1日最大{row['1日最大上昇%']:+.1f}%{high}）")


def stock_lines(stocks: pd.DataFrame, history: dict[str, float], regime_name: str) -> list[str]:
    liquid = stocks[stocks["売買代金1億円以上"]]
    up, down = int((liquid["週間騰落率%"] > 0).sum()), int((liquid["週間騰落率%"] < 0).sum())
    sessions = int(stocks["上昇日数"].max())
    lines = [
        f"■ 今週強かった銘柄（プライム・売買代金{LIST_TURNOVER_OKU}億円以上。上げ幅の上位10銘柄を時価総額の大きい順に）",
        f"売買代金1億円以上の{len(liquid)}銘柄のうち上昇{up}・下落{down}、中央値{liquid['週間騰落率%'].median():+.2f}%、52週高値更新{int(liquid['今週52週高値更新'].sum())}銘柄。",
    ]
    big = _by_cap(stocks[stocks["週平均売買代金_億円"] >= LIST_TURNOVER_OKU].head(10))
    lines += [_stock_line(i, code, row) for i, (code, row) in enumerate(big.iterrows(), 1)]
    steady = _by_cap(stocks[(stocks["週平均売買代金_億円"] >= LIST_TURNOVER_OKU) & (stocks["1日最大上昇%"] < 8) & (stocks["上昇日数"] >= sessions - 1)].head(6))
    if not steady.empty:
        lines += ["", "単発の急騰なしに上げ続けた銘柄: " + "、".join(f"{row['銘柄名']}{row['週間騰落率%']:+.1f}%" for _, row in steady.iterrows())]
    # 平均がマイナスのときに「-0.88pt上回り」と書かない（2026-11-14 の初めての本番メールで見つけた）
    side = "上回り" if history["excess"] >= 0 else "下回り"
    lines += ["", f"過去5年の{regime_label(regime_name)}（{history['weeks']:.0f}週）では、上位{TOP_N_STOCKS}銘柄は翌週に市場を平均{abs(history['excess']):.2f}pt{side}、勝った週は{history['win']:.0f}%でした。"]
    return lines


def forecast_lines(table: pd.DataFrame, stats: dict[str, object], market_call: dict[str, float], next_monday: date) -> list[str]:
    win_column = f"{stats['regime']}週の翌週の勝ち"
    def line(sector: str, row: pd.Series) -> str:
        reason = f"・{row['補正の理由']}" if row["補正の理由"] else ""
        return f"・{sector} {row['最終確率']}%（今週の市場比{row['市場平均比_pt']:+.1f}pt・過去の勝ち{row[win_column]}{reason}）"
    lines = [
        f"■ 来週（{next_monday:%m/%d}〜）の予想",
        f"日経平均が週間でプラスになる確率: {market_call['probability']:.0f}%（同じ区分の週の翌週 {market_call['count']:.0f}回を、回数が少ない分だけ50%に寄せた値）",
        "",
        f"業種が市場平均に勝つ確率（{regime_label(str(stats['regime']))}の翌週、平均的な業種は{stats['average']:.0f}%）",
        "上位:",
    ]
    lines += [line(sector, row) for sector, row in table.head(6).iterrows()]
    lines.append("下位:")
    lines += [line(sector, row) for sector, row in table.tail(6).iterrows()]
    lines += [
        "",
        f"※確率は、過去の同じ局面での業種の勝率・循環の位置・今週の順位の平均に、原油が週{OIL_MOVE_PCT:.0f}%以上動いたときの補正（±{OIL_ADJUST_PT}pt）を足した機械的な目安です。選挙など一度きりの材料は入っていません。",
        f"※同じ作り方の検証（1週ずつ抜いて判定、{stats['weeks']}週）: 上位{EDGE_SECTORS}業種の勝率{stats['holdout_top']:.0f}%、下位{EDGE_SECTORS}業種{stats['holdout_bottom']:.0f}%。回数が少なく、当たりを保証するものではありません。",
        "※現在上場している銘柄だけの、配当を含まない値段での計算です（数字は良い方に寄ります）。",
    ]
    return lines


def build_outlook() -> Outlook:
    panel = load_panel()
    stocks = build_stock_table(panel)
    liquid, market, relative = weekly_frames(panel)
    try:
        gauges = fetch_gauges()
    except Exception as exc:  # noqa: BLE001
        print(f"weekly_gauges=omitted reason={type(exc).__name__}")
        gauges = []
    brent = next((float(row["change"]) for row in gauges if row["label"] == "ブレント原油"), None)
    sectors, stats = build_sector_forecast(relative, market, brent)
    last = pd.Timestamp(panel.close.index[-1]).date()
    next_monday = last + timedelta(days=7 - last.weekday())
    anomalies, market_call = anomaly_lines(fetch_nikkei_history(), next_monday)
    history = strong_stock_history(liquid, market)
    public = [*forecast_lines(sectors, stats, market_call, next_monday), "", *rotation_lines(relative, stats, sectors), "", *anomalies]
    mail = [*stock_lines(stocks, history, str(stats["regime"])), "", *public]
    if gauges:
        mail += ["", *gauge_lines(gauges)]
    return Outlook("\n".join(mail), "\n".join(public), stocks.round(2), sectors.round(1))
