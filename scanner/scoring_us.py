"""米株のスコアリング。日本株の scanner/scoring.py に対応するもの。

日本株の score_stock は円建ての売買代金と「100株単位」が前提なので、そのままでは使えない。
判定の骨組み（52週高値との距離・移動平均・高値更新の鮮度・出来高）は同じにして、
市場の違いで変わるところだけを差し替えた。日本株側のコードには一切触っていない。

米株で変えたところと、その理由
  1. 売買代金の段階をドル建てにした。
     日本株の10億円/3億円/1億円をそのままドルに換算すると米国大型株には低すぎる
     （S&P500の20日平均売買代金は数千万〜数十億ドル）。
     そこで 10億ドル / 3億ドル / 1億ドル にした。これは私が決めた値で、
     データから導いたものではない。合わないと感じたら US_TURNOVER_TIERS を変える。
  2. 「100株購入額が資金の何%以内」の加点を外した。
     米株は1株から買えるので、この条件はほぼ全銘柄で成立し、意味を持たない。
     そのぶん満点が10点下がるので、ランクのしきい値も10点下げてある。
  3. テーマ加点を外した。日本語の社名・業種名で判定する作りなので米株では効かない。
     米株向けのテーマ判定を作るなら、それは別途データを見てから決める。

Sランクの必須条件（上昇トレンドのテクニカル）は日本株と同じものを使う。
scanner.scoring.meets_s_technical_gate は市場に依らない純関数なので、そのまま呼ぶ。
"""

from __future__ import annotations

from scanner.scoring import meets_s_technical_gate

# 20日平均売買代金（ドル）の段階と加点。上から順に判定する。
US_TURNOVER_TIERS: tuple[tuple[float, int, str], ...] = (
    (1_000_000_000, 15, "売買代金10億ドル以上"),
    (300_000_000, 10, "売買代金3億ドル以上"),
    (100_000_000, 5, "売買代金1億ドル以上"),
)

# ランクのしきい値。日本株(85/70/55/40)から、外した加点10点ぶんだけ下げてある。
US_RANK_THRESHOLDS: tuple[tuple[int, str], ...] = (
    (75, "S"),
    (60, "A"),
    (45, "B"),
    (30, "C"),
)

# 流動性の下限。これを下回る銘柄はそもそも候補にしない。
US_MIN_TURNOVER = 20_000_000


def us_rank(score: int) -> str:
    for threshold, rank in US_RANK_THRESHOLDS:
        if score >= threshold:
            return rank
    return "SKIP"


def score_us_stock(
    indicators: dict[str, float],
    cwh: dict[str, float] | None = None,
    earnings: dict[str, object] | None = None,
    name: str = "",
    sector: str = "",
    duke_support: dict[str, object] | None = None,
) -> dict[str, object]:
    """米株1銘柄を採点する。純関数（通信なし）なので自己テストできる。"""
    score = 0
    reasons: list[str] = []

    dist_high = float(indicators.get("dist_52w_high_pct", 999.0))
    if dist_high <= 3:
        score += 25
        reasons.append("52週高値3%以内")
    elif dist_high <= 7:
        score += 20
        reasons.append("52週高値7%以内")
    elif dist_high <= 15:
        score += 12
        reasons.append("52週高値15%以内")

    current = float(indicators.get("current_price", 0.0))
    for key, label, points in (
        ("ma25", "MA25上", 10),
        ("ma75", "MA75上", 10),
        ("ma200", "MA200上", 10),
    ):
        if current > float(indicators.get(key, float("inf"))):
            score += points
            reasons.append(label)
    if indicators.get("ma25_rising"):
        score += 8
        reasons.append("MA25上向き")
    if indicators.get("ma75_rising"):
        score += 8
        reasons.append("MA75上向き")
    if float(indicators.get("ma200_touch_pct", 999.0)) <= 3:
        score += 8
        reasons.append("MA200タッチ±3%")

    freshness = float(indicators.get("days_since_52w_high", 999))
    if freshness <= 3:
        score += 12
        reasons.append("52週高値更新3日以内")
    elif freshness <= 7:
        score += 8
        reasons.append("52週高値更新7日以内")
    elif freshness <= 14:
        score += 5
        reasons.append("52週高値更新14日以内")

    turnover = float(indicators.get("turnover_20d", 0.0))
    for threshold, points, label in US_TURNOVER_TIERS:
        if turnover >= threshold:
            score += points
            reasons.append(label)
            break

    volume_ratio = float(indicators.get("volume_ratio_5d_20d", 0.0))
    if volume_ratio >= 2:
        score += 15
        reasons.append("出来高比2倍以上")
    elif volume_ratio >= 1.5:
        score += 10
        reasons.append("出来高比1.5倍以上")
    elif volume_ratio >= 1.1:
        score += 5
        reasons.append("出来高増加")

    if cwh:
        score += 10
        reasons.append("CWH候補")

    if duke_support and bool(duke_support.get("duke_support_signal")):
        duke_score = int(duke_support.get("duke_support_score", 0) or 0)
        if duke_score > 0:
            score += duke_score
            reasons.append(f"DUKE旧52週高値サポート+{duke_score}")

    rank = us_rank(score)
    gate_ok, gate_fail = meets_s_technical_gate(indicators)
    if rank == "S" and not gate_ok:
        rank = "A"
        reasons.append("Sゲート未達(" + "・".join(gate_fail) + ")で最大A")
    if earnings and str(earnings.get("earnings_status", "")) == "未確認" and rank == "S":
        rank = "A"
        reasons.append("決算未確認で最大A")

    return {
        "score": score,
        "rank": rank,
        "turnover_20d": turnover,
        "reason": " / ".join(reasons),
    }
