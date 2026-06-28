"""ザラ場中のリアルタイム高値アラート（Gmail通知）。

目的: ザラ場中に「直近高値の接近・ブレイク」と「52週高値の接近・更新」に該当した
個別株だけを Gmail で即通知する。日次note処理（daily_discipline_run.py /
note_draft.py / note_autosave.py）とは完全に独立。投稿・公開は一切しない。

通知する対象（high_type）:
  - 52W_NEW_HIGH   → 「52週高値更新」
  - 52W_NEAR_HIGH  → 「52週高値接近」（52週高値まで3%以内）
  - SWING_HIGH_BREAK / RECENT_NEW_HIGH → 「直近高値ブレイク」
  - RECENT_NEAR_HIGH → 「直近高値接近」（直近スイング高値まで3%以内）

通知しない対象（このスクリプトでは扱わない）:
  - ChatGPT 300万円運用 / Claude 300万円運用
  - 25MAタッチ / 200MAタッチ / 240MAタッチ
  - 新高値後リテスト
  - note下書き保存完了通知

重複通知防止: 同じ銘柄・同じアラート種別は 1日1回だけ。
  履歴は outputs/intraday_alert_state_YYYYMMDD.json に保存。
出力CSV: outputs/intraday_high_alerts_YYYYMMDD_HHMMSS.csv

データ取得は yfinance（ネット必須＝GitHub Actions / Mac でのみ動く）。
ネットが無い環境（クラウド）では --self-test の純粋ロジック検証のみ可能。捏造はしない。
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from scanner.highs import classify_high_profile
from scanner.indicators import calculate_indicators
from scanner.prices import ensure_output_dir, fetch_price_history
from scanner.universe import UniverseConfig, load_jpx_listed


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs"

DISCLAIMER = "※これは投資助言ではなく、スクリーニング通知です。売買判断は自己責任で行ってください。"

# 流動性ゲート（出来高が極端に少ない銘柄を弾く）。20日平均売買代金の下限（円）。
MIN_TURNOVER = float(os.environ.get("IH_MIN_TURNOVER", "100000000"))  # 1億円

# 通知対象の high_type → アラート種別（日本語）。これ以外（MAタッチ/リテスト等）は対象外。
ALERT_TYPE_BY_HIGH_TYPE: dict[str, str] = {
    "52W_NEW_HIGH": "52週高値更新",
    "52W_NEAR_HIGH": "52週高値接近",
    "SWING_HIGH_BREAK": "直近高値ブレイク",
    "RECENT_NEW_HIGH": "直近高値ブレイク",
    "RECENT_NEAR_HIGH": "直近高値接近",
}

# 「更新・ブレイク」系（接近ではなく既に達成）の high_type。
BREAK_HIGH_TYPES = {"52W_NEW_HIGH", "SWING_HIGH_BREAK", "RECENT_NEW_HIGH"}

# 52週系か直近系かでラインの出所を変える。
FIFTYTWO_HIGH_TYPES = {"52W_NEW_HIGH", "52W_NEAR_HIGH"}


@dataclass
class Alert:
    code: str
    name: str
    current_price: float
    alert_type: str          # 52週高値更新 / 52週高値接近 / 直近高値ブレイク / 直近高値接近
    high_type: str           # 元の high_type（監査用）
    line_label: str          # 「52週高値」/「直近高値」
    line_price: float        # 高値ライン
    dist_pct: float          # 高値ラインまでの乖離率（%）。更新時は0.0。
    is_break: bool           # 更新・ブレイクなら True
    volume_ratio: float      # 出来高比（5日平均/20日平均）
    turnover_20d: int        # 20日平均売買代金（円）
    reason: str              # 判定理由

    def dedup_key(self) -> str:
        return f"{self.code}|{self.alert_type}"


# --------------------------------------------------------------------------
# 純粋ロジック（ネット不要・クラウドでも検証可能）
# --------------------------------------------------------------------------
def build_alert(
    code: str,
    name: str,
    indicators: dict[str, float],
    high_info: dict[str, object],
) -> Alert | None:
    """銘柄の指標と高値プロファイルから Alert を組み立てる。対象外/流動性不足は None。"""
    high_type = str(high_info.get("high_type", ""))
    alert_type = ALERT_TYPE_BY_HIGH_TYPE.get(high_type)
    if alert_type is None:
        return None  # MAタッチ・リテスト・分類外などは通知しない

    turnover = float(indicators.get("turnover_20d", 0) or 0)
    if turnover < MIN_TURNOVER:
        return None  # 出来高が極端に少ない銘柄は弾く

    current = float(indicators.get("current_price", 0) or 0)
    is_break = high_type in BREAK_HIGH_TYPES

    if high_type in FIFTYTWO_HIGH_TYPES:
        line_label = "52週高値"
        line_price = round(float(indicators.get("high_52w", 0) or 0), 1)
        dist_pct = max(0.0, round(float(indicators.get("dist_52w_high_pct", 0) or 0), 2))
    else:
        line_label = "直近高値"
        line_price = _to_float(high_info.get("high_price"))
        dist_pct = max(0.0, _to_float(high_info.get("dist_to_high_pct")))

    if is_break:
        dist_pct = 0.0

    volume_ratio = round(float(indicators.get("volume_ratio_5d_20d", 0) or 0), 2)
    reason = _build_reason(alert_type, line_label, dist_pct, is_break, volume_ratio, turnover)

    return Alert(
        code=code,
        name=name,
        current_price=round(current, 1),
        alert_type=alert_type,
        high_type=high_type,
        line_label=line_label,
        line_price=line_price,
        dist_pct=dist_pct,
        is_break=is_break,
        volume_ratio=volume_ratio,
        turnover_20d=int(turnover),
        reason=reason,
    )


def _build_reason(
    alert_type: str,
    line_label: str,
    dist_pct: float,
    is_break: bool,
    volume_ratio: float,
    turnover: float,
) -> str:
    oku = turnover / 100_000_000
    tail = f"出来高比{volume_ratio:.2f}倍・売買代金{oku:.1f}億円"
    if is_break:
        head = f"{line_label}を更新・ブレイク"
    else:
        head = f"{line_label}まで{dist_pct:.1f}%に接近"
    return f"{head}。{tail}。"


def _to_float(value: object) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------
# 件名・本文（純粋）
# --------------------------------------------------------------------------
def build_subject(new_alerts: list[Alert]) -> str:
    if not new_alerts:
        return "【高値アラート】新規なし"
    head = new_alerts[0]
    if head.is_break:
        detail = f"{head.line_label}更新"
    else:
        detail = f"{head.line_label}まで{head.dist_pct:.1f}%"
    base = f"【高値接近アラート】{head.code} {head.name}｜{detail}"
    if len(new_alerts) > 1:
        base += f"｜ほか{len(new_alerts) - 1}件"
    return base


def build_body(new_alerts: list[Alert]) -> str:
    lines: list[str] = [
        "ザラ場リアルタイム高値アラート",
        f"検知時刻: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"新規アラート: {len(new_alerts)}件",
        "",
    ]
    # 種別ごとにまとめる（更新→接近の順）
    order = ["52週高値更新", "52週高値接近", "直近高値ブレイク", "直近高値接近"]
    grouped: dict[str, list[Alert]] = {key: [] for key in order}
    for alert in new_alerts:
        grouped.setdefault(alert.alert_type, []).append(alert)

    for key in order:
        group = grouped.get(key, [])
        if not group:
            continue
        lines.append(f"■ {key}（{len(group)}件）")
        for alert in group:
            lines.extend(_format_alert(alert))
        lines.append("")

    lines.append(DISCLAIMER)
    return "\n".join(lines)


def _format_alert(alert: Alert) -> list[str]:
    if alert.is_break:
        dist_text = "更新済み（乖離0%）"
    else:
        dist_text = f"あと{alert.dist_pct:.1f}%"
    return [
        f"{alert.code} {alert.name}",
        f"  現在値:{alert.current_price:,.1f}円 / 種別:{alert.alert_type}",
        f"  {alert.line_label}ライン:{alert.line_price:,.1f}円 / ラインまで:{dist_text}",
        f"  出来高比:{alert.volume_ratio:.2f}倍 / 売買代金:{alert.turnover_20d / 100_000_000:.1f}億円",
        f"  理由:{alert.reason}",
        "",
    ]


# --------------------------------------------------------------------------
# 重複通知防止（日次state）
# --------------------------------------------------------------------------
class DedupState:
    """同じ銘柄・同じアラート種別を 1日1回だけにする日次state。"""

    def __init__(self, path: Path, day: str):
        self.path = path
        self.day = day
        self.notified: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if str(data.get("date")) == self.day:
            self.notified = set(data.get("notified", []))

    def is_new(self, alert: Alert) -> bool:
        return alert.dedup_key() not in self.notified

    def mark(self, alert: Alert) -> None:
        self.notified.add(alert.dedup_key())

    def save(self) -> None:
        ensure_output_dir(self.path.parent)
        payload = {
            "date": self.day,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "notified": sorted(self.notified),
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def state_path(output_dir: Path, day: str) -> Path:
    return output_dir / f"intraday_alert_state_{day}.json"


# --------------------------------------------------------------------------
# CSV出力
# --------------------------------------------------------------------------
def write_csv(alerts: list[Alert], new_keys: set[str], output_dir: Path) -> Path | None:
    if not alerts:
        return None
    ensure_output_dir(output_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"intraday_high_alerts_{stamp}.csv"
    rows = []
    for alert in alerts:
        row = asdict(alert)
        row["is_new"] = alert.dedup_key() in new_keys
        rows.append(row)
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    return path


# --------------------------------------------------------------------------
# スキャン（ネット必須＝Mac / GitHub Actions のみ）
# --------------------------------------------------------------------------
def scan(
    markets: tuple[str, ...] = ("prime", "standard", "growth"),
    limit: int | None = None,
    period: str = "14mo",
) -> list[Alert]:
    universe = load_jpx_listed(UniverseConfig(markets=markets))
    if limit:
        print(f"WARNING: limit={limit} は動作確認用。本番は全銘柄で実行してください。", flush=True)
        universe = universe.head(limit)

    total = len(universe)
    alerts: list[Alert] = []
    for idx, stock in enumerate(universe.itertuples(index=False), start=1):
        if idx % 200 == 0:
            print(f"[{idx}/{total}] scanning...", flush=True)
        try:
            history = fetch_price_history(stock.ticker, period=period)
            indicators = calculate_indicators(history)
            if indicators is None:
                continue
            high_info = classify_high_profile(history)
            alert = build_alert(stock.code, stock.name, indicators, high_info)
            if alert is not None:
                alerts.append(alert)
        except Exception as exc:  # 1銘柄の失敗で全体を止めない
            print(f"skip {stock.ticker}: {exc}", flush=True)
            continue
    return alerts


# --------------------------------------------------------------------------
# Gmail送信（gmail_notify を再利用）
# --------------------------------------------------------------------------
def send_alert_mail(new_alerts: list[Alert]) -> bool:
    from gmail_notify import load_gmail_config, send_gmail

    config = load_gmail_config()
    if config is None:
        print("intraday_alert_mail=skipped reason=missing_secrets "
              "required=GMAIL_USER,GMAIL_APP_PASSWORD,MAIL_TO")
        return False
    subject = build_subject(new_alerts)
    body = build_body(new_alerts)
    send_gmail(subject, body, config)
    print(f"intraday_alert_mail=sent to={config.mail_to} subject={subject}")
    return True


# --------------------------------------------------------------------------
# メイン
# --------------------------------------------------------------------------
def run(
    output_dir: Path,
    markets: tuple[str, ...],
    limit: int | None,
    dry_run: bool,
) -> int:
    day = date.today().strftime("%Y%m%d")
    try:
        alerts = scan(markets=markets, limit=limit)
    except Exception as exc:
        print(f"intraday_scan_error={exc}")
        print("（ネット未接続のクラウドでは取得できません。GitHub Actions / Mac で実行してください）")
        return 1

    state = DedupState(state_path(output_dir, day), day)
    new_alerts = [a for a in alerts if state.is_new(a)]
    new_keys = {a.dedup_key() for a in new_alerts}

    csv_path = write_csv(alerts, new_keys, output_dir)
    print(f"intraday_alerts_detected={len(alerts)} new={len(new_alerts)}")
    if csv_path:
        print(f"intraday_csv={csv_path}")

    if not new_alerts:
        print("intraday_alert_mail=skipped reason=no_new_alerts")
        return 0

    if dry_run:
        print("--- DRY-RUN（送信せず・state未更新）---")
        print("subject:", build_subject(new_alerts))
        print(build_body(new_alerts))
        return 0

    sent = send_alert_mail(new_alerts)
    if sent:
        for alert in new_alerts:
            state.mark(alert)
        state.save()
        print(f"intraday_state_saved={state.path}")
    return 0


# --------------------------------------------------------------------------
# セルフテスト（純粋ロジック・ネット不要）
# --------------------------------------------------------------------------
def _self_test() -> int:
    print("intraday_high_alert self-test ...")

    def ind(**kw):
        base = {
            "current_price": 1000.0,
            "high_52w": 1000.0,
            "dist_52w_high_pct": 0.0,
            "turnover_20d": 300_000_000.0,
            "volume_ratio_5d_20d": 1.3,
        }
        base.update(kw)
        return base

    # 52週高値更新
    a = build_alert("7173", "東京きらぼし", ind(), {"high_type": "52W_NEW_HIGH"})
    assert a is not None and a.alert_type == "52週高値更新" and a.is_break and a.dist_pct == 0.0, a

    # 52週高値接近
    a = build_alert("8524", "北洋銀行",
                    ind(current_price=992.0, dist_52w_high_pct=0.8),
                    {"high_type": "52W_NEAR_HIGH"})
    assert a is not None and a.alert_type == "52週高値接近" and not a.is_break and a.dist_pct == 0.8, a
    subj = build_subject([a])
    assert "52週高値まで0.8%" in subj, subj

    # 直近高値ブレイク（スイング）
    a = build_alert("7011", "三菱重工",
                    ind(), {"high_type": "SWING_HIGH_BREAK", "high_price": 990.0,
                            "dist_to_high_pct": 0.0})
    assert a is not None and a.alert_type == "直近高値ブレイク" and a.is_break, a

    # 直近高値接近（recent near）
    a = build_alert("6951", "日本電子",
                    ind(), {"high_type": "RECENT_NEAR_HIGH", "high_price": 1010.0,
                            "dist_to_high_pct": 1.0})
    assert a is not None and a.alert_type == "直近高値接近" and a.line_label == "直近高値" and a.dist_pct == 1.0, a

    # 対象外: MAタッチ・分類外は None
    assert build_alert("0000", "x", ind(), {"high_type": "OTHER"}) is None
    assert build_alert("0000", "x", ind(), {"high_type": "RETEST_52W"}) is None

    # 流動性不足は None
    assert build_alert("0000", "x", ind(turnover_20d=10_000_000.0),
                       {"high_type": "52W_NEW_HIGH"}) is None

    # 重複通知防止
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        sp = state_path(Path(td), "20260628")
        st = DedupState(sp, "20260628")
        alert = build_alert("7173", "東京きらぼし", ind(), {"high_type": "52W_NEW_HIGH"})
        assert st.is_new(alert)
        st.mark(alert)
        st.save()
        st2 = DedupState(sp, "20260628")
        assert not st2.is_new(alert), "同日・同種別は再通知しないはず"
        # 別種別なら通知可
        near = build_alert("7173", "東京きらぼし",
                           ind(current_price=992.0, dist_52w_high_pct=0.8),
                           {"high_type": "52W_NEAR_HIGH"})
        assert st2.is_new(near), "別アラート種別は通知できるはず"
        # 翌日は同種別でも再通知
        st3 = DedupState(state_path(Path(td), "20260629"), "20260629")
        assert st3.is_new(alert)

    # CSV出力（新規フラグ付き）
    with tempfile.TemporaryDirectory() as td:
        alerts = [
            build_alert("7173", "A", ind(), {"high_type": "52W_NEW_HIGH"}),
            build_alert("8524", "B", ind(current_price=992.0, dist_52w_high_pct=0.8),
                        {"high_type": "52W_NEAR_HIGH"}),
        ]
        new_keys = {alerts[0].dedup_key()}
        path = write_csv(alerts, new_keys, Path(td))
        assert path is not None and path.exists()
        df = pd.read_csv(path, dtype={"code": str})
        assert len(df) == 2 and df["is_new"].sum() == 1, df

    # 本文・件名（複数）
    multi = [
        build_alert("7173", "東京きらぼし", ind(), {"high_type": "52W_NEW_HIGH"}),
        build_alert("8524", "北洋銀行", ind(current_price=992.0, dist_52w_high_pct=0.8),
                    {"high_type": "52W_NEAR_HIGH"}),
    ]
    body = build_body(multi)
    assert "52週高値更新（1件）" in body and "52週高値接近（1件）" in body, body
    assert DISCLAIMER in body
    assert "ほか1件" in build_subject(multi)

    print("SELF_TEST_PASS")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ザラ場リアルタイム高値アラート（Gmail）")
    parser.add_argument("--markets", nargs="+",
                        choices=["prime", "standard", "growth"],
                        default=["prime", "standard", "growth"], help="対象市場")
    parser.add_argument("--limit", type=int, default=None, help="動作確認用に先頭N銘柄だけ処理")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="出力先")
    parser.add_argument("--dry-run", action="store_true", help="検知のみ。送信せず・state未更新")
    parser.add_argument("--self-test", action="store_true", help="純粋ロジックの自己テスト（ネット不要）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        return _self_test()
    return run(
        output_dir=Path(args.output_dir),
        markets=tuple(args.markets),
        limit=args.limit,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
