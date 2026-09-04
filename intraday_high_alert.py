"""ザラ場中のリアルタイム高値アラート（Gmail通知）。

目的: ザラ場中に「52週高値の更新（一時更新含む）」と「直近高値の接近・ブレイク」に該当した
個別株だけを Gmail で即通知する。note下書き通知、300万円運用通知は送らない。
日次note処理（daily_discipline_run.py / note_draft.py / note_autosave.py）とは完全に独立。投稿・公開は一切しない。

通知する対象（high_type）:
  - 52W_NEW_HIGH → 「52週高値更新」（カブタン/みんかぶの「52週来高値更新・一時更新含む」に対応。
    当日ザラ場のHighが過去52週の高値を上抜けた瞬間タッチも detect_intraday_52w_touch で拾う）
  - 52W_NEAR_HIGH → 「52週高値接近」（52週高値まで3%以内）
  - SWING_HIGH_BREAK / RECENT_NEW_HIGH → 「直近高値ブレイク」
  - RECENT_NEAR_HIGH → 「直近高値接近」（直近スイング高値まで3%以内）

メール通知の絞り込み（環境変数 IH_ALERT_SCOPE）:
  - "break"（既定）= すでに高値を抜けた銘柄だけ通知（52週高値更新＋直近高値ブレイク）。
    「接近」系はメールに載せない。全銘柄スキャンだと接近系が大量に出てメールが読めなくなるため。
  - "52w"          = 52週高値更新（一時更新含む）のみ通知。さらに絞りたいとき。
  - "all"          = 従来どおり接近系も含めて全部通知。
  絞り込みは補足情報（決算予定日・OpenWork）の取得より前に行うので、実行時間も短くなる。

通知しない対象（このスクリプトでは扱わない）:
  - Claude 300万円運用
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
from html import escape
import re
from dataclasses import dataclass, asdict
from functools import lru_cache
from urllib.parse import quote
from pathlib import Path

import pandas as pd

from scanner.highs import classify_high_profile
from scanner.indicators import calculate_indicators
from scanner.prices import (
    ensure_output_dir,
    fetch_next_earnings_date,
    fetch_price_history,
    prefetch_price_histories,
)
from scanner.universe import UniverseConfig, load_jpx_listed

# T-K修正(2026-08-04): GitHub Actionsのランナーは UTC で動くため、datetime.now() /
# date.today() をそのまま使うとメール本文の「検知時刻」が9時間ずれて表示されていた
# （例: 15:36 JST に届いたメールの本文が「06:36」）。時刻・日付は必ず jptime 経由で取る。
from jptime import jst_now, jst_today


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs"

# 日中監視の軽量ウォッチリスト（前日EODから build_intraday_watchlist.py が生成）。
# これがあれば全銘柄ではなく200〜500銘柄だけを監視する。無ければ全銘柄にフォールバック。
WATCHLIST_NAME = "intraday_watchlist.csv"

DISCLAIMER = "※これは投資助言ではなく、スクリーニング通知です。売買判断は自己責任で行ってください。"


def _watchlist_enabled() -> bool:
    """INTRADAY_USE_WATCHLIST（既定 1=有効）。0/false でウォッチリストを無視し全銘柄に戻す。"""
    return os.environ.get("INTRADAY_USE_WATCHLIST", "1").strip().lower() not in ("0", "false", "no", "off")


def load_watchlist_codes(path: Path) -> set[str] | None:
    """intraday_watchlist.csv から監視対象コードを読む。無ければ None（＝全銘柄フォールバック）。
    英数字4桁コード(285A等)もそのまま保持する。捏造しない。"""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        df = pd.read_csv(path, dtype={"code": str}, encoding="utf-8-sig")
    except Exception as exc:  # 壊れていても全体は止めない
        print(f"watchlist_read_error={exc} -> 全銘柄にフォールバック", flush=True)
        return None
    if "code" not in df.columns or df.empty:
        return None
    codes: set[str] = set()
    for raw in df["code"].tolist():
        s = str(raw).strip().upper()
        if not s:
            continue
        if re.fullmatch(r"[0-9A-Z]{4}", s):
            codes.add(s)
        else:
            digits = re.sub(r"\D", "", s)
            if digits:
                codes.add(digits.zfill(4))
    return codes or None

# 流動性ゲート（出来高が極端に少ない銘柄を弾く）。20日平均売買代金の下限（円）。
MIN_TURNOVER = float(os.environ.get("IH_MIN_TURNOVER", "100000000"))  # 1億円

# 1通のメールに載せる明細の最大件数。
# 【2026-08-03 追加の理由】その日の最初のスキャンは重複判定の基準(state)が空なので、
# すでに高値を更新済みの銘柄が全部「新規」として1通に載る。実測で347件になり、
# メールとして読めなかった。件数そのものは件名と本文冒頭に必ず残し、
# 本文の明細だけを売買代金の大きい順で上位N件に絞る。
# 全件は intraday_high_alerts_*.csv に残るので情報は失われない。
# 0以下を指定すると無制限（従来どおり全件掲載）。
DEFAULT_MAX_MAIL_ITEMS = 25


def _max_mail_items() -> int:
    raw = os.environ.get("IH_MAX_MAIL_ITEMS", str(DEFAULT_MAX_MAIL_ITEMS)).strip()
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_MAX_MAIL_ITEMS

# 通知対象の high_type → アラート種別（日本語）。
# Gmail通知は「52週高値更新（一時更新含む）・接近」と「直近高値ブレイク・接近」。
# MAタッチ、リテスト、note通知は対象外。
ALERT_TYPE_BY_HIGH_TYPE: dict[str, str] = {
    "52W_NEW_HIGH": "52週高値更新",
    "52W_NEAR_HIGH": "52週高値接近",
    "SWING_HIGH_BREAK": "直近高値ブレイク",
    "RECENT_NEW_HIGH": "直近高値ブレイク",
    "RECENT_NEAR_HIGH": "直近高値接近",
}

# 「更新・ブレイク」系（接近ではなく既に達成）の high_type。
BREAK_HIGH_TYPES = {"52W_NEW_HIGH", "SWING_HIGH_BREAK", "RECENT_NEW_HIGH"}

# 52週系（高値ライン表示に indicators の high_52w / dist_52w_high_pct を使う）。
FIFTYTWO_HIGH_TYPES: set[str] = {"52W_NEW_HIGH", "52W_NEAR_HIGH"}

# メール通知の絞り込み既定値（詳細は _alert_scope / alert_in_scope）。
DEFAULT_ALERT_SCOPE = "break"

# 表記ゆれを吸収する（yml やターミナルでの打ち間違いで黙って全件通知にならないように）。
_ALERT_SCOPE_ALIASES: dict[str, str] = {
    "break": "break",
    "breaks": "break",
    "breakout": "break",
    "new_high": "break",
    "update": "break",
    "52w": "52w",
    "52週": "52w",
    "52week": "52w",
    "52w_new_high": "52w",
    "all": "all",
    "full": "all",
}

ALERT_SCOPE_LABELS: dict[str, str] = {
    "break": "52週高値更新＋直近高値ブレイク（接近は通知しない）",
    "52w": "52週高値更新のみ",
    "all": "全種別（接近も通知）",
}


@dataclass
class Alert:
    code: str
    name: str
    current_price: float
    alert_type: str          # 直近高値ブレイク / 直近高値接近
    high_type: str           # 元の high_type（監査用）
    line_label: str          # 「直近高値」
    line_price: float        # 高値ライン
    dist_pct: float          # 高値ラインまでの乖離率（%）。更新時は0.0。
    is_break: bool           # 更新・ブレイクなら True
    volume_ratio: float      # 出来高比（5日平均/20日平均）
    turnover_20d: int        # 20日平均売買代金（円）
    reason: str              # 判定理由
    earnings_date: str = "未取得"    # 決算予定日（7営業日以内なら警告付き）
    # fix45(2026-09-04): 「なぜ更新済みなのか」を読んだ人が検算できるようにする。
    line_date: str = ""             # 高値ラインがいつの高値か（YYYY-MM-DD）
    today_high: float = 0.0         # 当日ザラ場の高値（0は未取得）
    bar_date: str = ""              # 使った価格データの日付（前日ならデータ遅れ）

    def dedup_key(self) -> str:
        return f"{self.code}|{self.alert_type}"


def _alert_scope() -> str:
    """IH_ALERT_SCOPE を正規化して返す。未知の値は既定にフォールバックし、必ず警告を出す。"""
    raw = os.environ.get("IH_ALERT_SCOPE", DEFAULT_ALERT_SCOPE).strip().lower()
    scope = _ALERT_SCOPE_ALIASES.get(raw)
    if scope is None:
        print(
            f"WARNING: IH_ALERT_SCOPE={raw!r} は未知の値です。"
            f"既定の {DEFAULT_ALERT_SCOPE!r} で実行します"
            f"（指定できる値: break / 52w / all）",
            flush=True,
        )
        return DEFAULT_ALERT_SCOPE
    return scope


def alert_in_scope(alert: Alert, scope: str) -> bool:
    """このアラートをメール通知の対象にするか。純関数（通信なし）。"""
    if scope == "all":
        return True
    if scope == "52w":
        return alert.high_type == "52W_NEW_HIGH"
    # "break": すでに高値を更新・ブレイクしたものだけ（＝接近は除外）
    return alert.is_break


# --------------------------------------------------------------------------
# 補足情報（決算予定日 / OpenWork）
# --------------------------------------------------------------------------
def _code_text(code: object) -> str:
    text = str(code).strip()
    return text[:-2] if text.endswith(".0") else text


def earnings_label(d: object, today: object) -> str:
    """決算予定日の表示文字列を作る。外部通信をしないので自己テストできる。

    調査(2026-08-10): 取得元の Yahoo Finance は、次回の発表予定が未定のとき
    「前回の実績日」を返すことがある（例: 2607 不二製油 = 2026-08-07。当日は 8/10）。
    それをそのまま「決算予定日」として出すと、過ぎた日を予定日だと誤解させる。
    そこで過去日は「次回未定（前回 YYYY-MM-DD）」と書き分ける。捏造はしない。
    """
    if d is None:
        return "未取得"
    text = d.isoformat()
    try:
        t0 = pd.Timestamp(today)
        t1 = pd.Timestamp(d)
        if pd.isna(t0) or pd.isna(t1):
            return text
        if t1 < t0:
            return f"次回未定（前回 {text}）"
        bdays = max(len(pd.bdate_range(t0, t1)) - 1, 0)
        if bdays <= 7:
            return f"{text} ⚠️ 決算接近"
    except Exception:
        pass
    return text


@lru_cache(maxsize=1024)
def _fetch_earnings_label(code: str) -> str:
    """決算予定日を取得。失敗時は未取得。7営業日以内は警告を付ける。"""
    try:
        d = fetch_next_earnings_date(f"{_code_text(code)}.T")
    except Exception:
        d = None
    try:
        today = jst_today()
    except Exception:
        today = None
    return earnings_label(d, today)


OPENWORK_SEARCH_BASE = "https://www.openwork.jp/search.php?src_str="


def openwork_search_url(name: object) -> str:
    """社名からOpenWorkの検索URLを組み立てる。外部通信は一切しない。

    OpenWorkの利用規約(第11条)は、断続的な機械的アクセス・コンテンツの一括ダウンロード、
    本サービスを通じて入手した情報の複製/編集/掲載/転載/公衆送信/配布/提供、および
    商業目的での利用を禁止している。したがって評価値そのものは取得も保存も掲載もせず、
    受け取った人が利用者として自分でOpenWorkを見に行くためのリンクだけを載せる。
    """
    text = str(name or "").strip()
    if not text or text.lower() in ("nan", "none", "null"):
        return ""
    return OPENWORK_SEARCH_BASE + quote(text, safe="")


def _apply_extra_fields(alert: Alert, code: object) -> Alert:
    alert.earnings_date = _fetch_earnings_label(_code_text(code))
    return alert

# --------------------------------------------------------------------------
# 純粋ロジック（ネット不要・クラウドでも検証可能）
# --------------------------------------------------------------------------
def _last_bar_facts(history) -> dict[str, object]:
    """価格データの最後のバーから「当日高値」と「その日付」を取る。

    fix45(2026-09-04): メールが「更新済み」と書く根拠は当日ザラ場の高値なのに、
    本文には現在値しか出ていなかった。読んだ人が検算できるように持ち回る。
    取れないときは空にする（捏造しない）。
    """
    try:
        if history is None or history.empty or "High" not in history.columns:
            return {}
        import pandas as _pd

        return {
            "today_high": round(float(history["High"].astype(float).iloc[-1]), 1),
            "bar_date": _pd.Timestamp(history.index[-1]).date().isoformat(),
        }
    except Exception:
        return {}


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

    # fix45(2026-09-04): ラインがいつの高値か、当日どこまで上げたかを持ち回る。
    line_date = str(high_info.get("high_date") or "").strip()
    today_high = _to_float(high_info.get("today_high"))
    bar_date = str(high_info.get("bar_date") or "").strip()

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
        line_date=line_date,
        today_high=today_high,
        bar_date=bar_date,
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


def detect_intraday_52w_touch(history: pd.DataFrame, window_days: int = 252) -> dict[str, object] | None:
    """当日の高値(High)が「前日までの過去52週の高値」を上抜けたか（＝一時更新も含む52週高値更新）。

    scanner.highs.classify_high_profile は終値(Close)ベースのため、ザラ場中に高値タッチして
    押し戻された銘柄（カブタン/みんかぶの「52週来高値更新・一時更新も含む」）を拾えない。
    ここでは当日バーのHighと、当日を除く直近 window_days 本のHighの最大値を比べる。
    純関数・通信なし。データ不足（上場1年未満など）は None＝判定しない（捏造しない）。"""
    if history is None or history.empty or "High" not in history.columns:
        return None
    if len(history) < window_days + 1:
        return None
    high = history["High"].astype(float)
    prior_max = float(high.iloc[-(window_days + 1):-1].max())
    today_high = float(high.iloc[-1])
    if prior_max <= 0 or today_high <= prior_max:
        return None
    return {
        "high_type": "52W_NEW_HIGH",
        "high_label": "52週新高値（一時更新含む）",
        "high_price": round(prior_max, 1),
        "dist_to_high_pct": 0.0,
    }


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


def select_mail_alerts(
    new_alerts: list[Alert], max_items: int | None = None
) -> tuple[list[Alert], int]:
    """メール本文に載せる分だけを選ぶ（売買代金の大きい順）。

    戻り値は (掲載するアラート, 省略した件数)。
    上限が0以下、または件数が上限以下ならそのまま全件を返す。
    """
    limit = _max_mail_items() if max_items is None else max_items
    if limit <= 0 or len(new_alerts) <= limit:
        return list(new_alerts), 0
    ranked = sorted(new_alerts, key=lambda a: a.turnover_20d, reverse=True)
    return ranked[:limit], len(new_alerts) - limit


def build_body(new_alerts: list[Alert], max_items: int | None = None) -> str:
    shown, omitted = select_mail_alerts(new_alerts, max_items)
    lines: list[str] = [
        "ザラ場リアルタイム高値アラート",
        f"検知時刻: {jst_now().strftime('%Y-%m-%d %H:%M')} JST",
        f"新規アラート: {len(new_alerts)}件",
    ]
    # fix45(2026-09-04): 使った価格データの日付を出す。
    #   ここが前日の日付なら、本当にデータが1日遅れている（判定の前に気づける）。
    _bar_dates = sorted({a.bar_date for a in new_alerts if a.bar_date})
    if _bar_dates:
        _shown = _bar_dates[-1]
        _extra = f"（ほかに{len(_bar_dates) - 1}種類あり）" if len(_bar_dates) > 1 else ""
        lines.append(f"価格データ日: {_shown}{_extra}")
    if omitted:
        lines.append(
            f"※本文には売買代金の大きい順に{len(shown)}件だけ掲載しています"
            f"（残り{omitted}件は省略）。全件は intraday_high_alerts_*.csv にあります。"
        )
    lines.append("")
    # 種別ごとにまとめる（52週更新→直近ブレイク→52週接近→直近接近の順）。
    order = ["52週高値更新", "直近高値ブレイク", "52週高値接近", "直近高値接近"]
    grouped: dict[str, list[Alert]] = {key: [] for key in order}
    for alert in shown:
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


def _line_suffix(alert: Alert) -> str:
    """ラインがいつの高値かを添える。日付が無ければ何も書かない。"""
    return f"（{alert.line_date}の高値）" if alert.line_date else ""


def _price_text(alert: Alert) -> str:
    """現在値と、当日ザラ場の高値。当日高値が取れないときは現在値だけ。"""
    head = f"現在値:{alert.current_price:,.1f}円"
    if alert.today_high > 0:
        head += f" / 当日高値:{alert.today_high:,.1f}円"
    return head


def _format_alert(alert: Alert) -> list[str]:
    if alert.is_break:
        dist_text = "更新済み（乖離0%）"
    else:
        dist_text = f"あと{alert.dist_pct:.1f}%"
    lines = [
        f"{alert.code} {alert.name}",
        f"  {_price_text(alert)} / 種別:{alert.alert_type}",
        f"  {alert.line_label}ライン:{alert.line_price:,.1f}円{_line_suffix(alert)}"
        f" / ラインまで:{dist_text}",
        f"  出来高比:{alert.volume_ratio:.2f}倍 / 売買代金:{alert.turnover_20d / 100_000_000:.1f}億円",
        f"  🗓 決算予定日:{alert.earnings_date}",
    ]
    url = openwork_search_url(alert.name)
    if url:
        lines.append(f"  👥 OpenWork:{url}")
    lines.append(f"  理由:{alert.reason}")
    lines.append("")
    return lines


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
            "updated_at": jst_now().isoformat(timespec="seconds"),
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
    stamp = jst_now().strftime("%Y%m%d_%H%M%S")
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
    watchlist_codes: set[str] | None = None,
    scope: str | None = None,
) -> list[Alert]:
    scope = scope or _alert_scope()
    print(
        f"intraday_alert_scope={scope} ({ALERT_SCOPE_LABELS.get(scope, scope)})",
        flush=True,
    )
    universe = load_jpx_listed(UniverseConfig(markets=markets))
    if watchlist_codes:
        # 日中は前日EODで選んだ200〜500銘柄だけを監視（軽量化）。該当0件なら絞らない（フォールバック）。
        filtered = universe[universe["code"].astype(str).str.strip().str.upper().isin(watchlist_codes)]
        if not filtered.empty:
            print(f"intraday_watchlist_applied={len(filtered)}/{len(universe)}銘柄", flush=True)
            universe = filtered.reset_index(drop=True)
        else:
            print("intraday_watchlist_empty_match -> 全銘柄で実行", flush=True)
    if limit:
        print(f"WARNING: limit={limit} は動作確認用。本番は全銘柄で実行してください。", flush=True)
        universe = universe.head(limit)

    total = len(universe)
    # 全銘柄スキャンでも数分で終わるよう、先にバッチ一括取得してキャッシュを温める。
    # （1銘柄ずつの逐次取得だと3,500銘柄で1時間超かかりザラ場の15分間隔に間に合わない）
    if total > 50:
        prefetch_stats = prefetch_price_histories(
            [str(t) for t in universe["ticker"].tolist()], period=period
        )
        print(f"intraday_prefetch={prefetch_stats}", flush=True)
    alerts: list[Alert] = []
    out_of_scope = 0
    for idx, stock in enumerate(universe.itertuples(index=False), start=1):
        if idx % 200 == 0:
            print(f"[{idx}/{total}] scanning...", flush=True)
        try:
            history = fetch_price_history(stock.ticker, period=period)
            indicators = calculate_indicators(history)
            if indicators is None:
                continue
            high_info = classify_high_profile(history)
            # 終値ベース分類が52週更新を取り逃しても、当日Highの一時タッチがあれば52週更新として扱う。
            touch = detect_intraday_52w_touch(history)
            if touch is not None and str(high_info.get("high_type", "")) != "52W_NEW_HIGH":
                high_info = dict(high_info) | touch
            # fix45(2026-09-04): 最後のバーの高値と日付を添える。
            #   データが前日のままなら bar_date が前日になり、メールで気づける。
            high_info = dict(high_info) | _last_bar_facts(history)
            alert = build_alert(stock.code, stock.name, indicators, high_info)
            if alert is None:
                continue
            # 通知対象外は補足情報（決算予定日・OpenWork）を取りに行かずここで捨てる。
            # 1件ごとに通信が発生するため、除外を先に行うと実行時間も短くなる。
            if not alert_in_scope(alert, scope):
                out_of_scope += 1
                continue
            alerts.append(_apply_extra_fields(alert, stock.code))
        except Exception as exc:  # 1銘柄の失敗で全体を止めない
            print(f"skip {stock.ticker}: {exc}", flush=True)
            continue
    print(
        f"intraday_scope_filter=scope:{scope} kept:{len(alerts)} excluded:{out_of_scope}",
        flush=True,
    )
    return alerts


# --------------------------------------------------------------------------
# Gmail送信（gmail_notify を再利用）
# --------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# fix32(2026-08-28): メールのHTML版。コードは青・銘柄名は黒の太字にする。
# 文面はプレーンテキスト版と同じ順序・同じ中身にする（片方だけ増減させない）。
# Gmailは <style> は読むがアニメーションは読まないので、色と太さだけで組む。
# ---------------------------------------------------------------------------

ALERT_MAIL_CSS = (
    "body{margin:0;padding:16px;background:#ffffff;color:#111111;"
    "font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans','Yu Gothic',sans-serif;}"
    ".hd{background:#1f745f;color:#ffffff;padding:12px 14px;border-radius:10px;}"
    ".hd .t{font-size:18px;font-weight:700;}"
    ".hd .s{font-size:13px;opacity:.9;}"
    ".cut{margin:10px 0 0;font-size:13px;color:#8a949c;}"
    ".grp{margin:18px 0 8px;font-size:15px;font-weight:700;color:#1f745f;"
    "border-bottom:2px solid #d8e3e0;padding-bottom:4px;}"
    ".card{border:1px solid #e3e8ec;border-radius:8px;padding:10px 12px;margin:0 0 10px;}"
    ".ttl{font-size:16px;margin:0 0 4px;}"
    ".cd{color:#1d4ed8;font-weight:700;}"
    ".nm{color:#111111;font-weight:700;}"
    ".row{font-size:13px;color:#333333;line-height:1.7;}"
    ".row a{color:#1d4ed8;}"
    ".dis{margin-top:16px;font-size:12px;color:#6b7280;}"
)


def _alert_html_card(alert: Alert) -> str:
    if alert.is_break:
        dist_text = "更新済み（乖離0%）"
    else:
        dist_text = f"あと{alert.dist_pct:.1f}%"
    rows = [
        f"{escape(_price_text(alert))} / 種別:{escape(alert.alert_type)}",
        f"{escape(alert.line_label)}ライン:{alert.line_price:,.1f}円{escape(_line_suffix(alert))} / ラインまで:{dist_text}",
        f"出来高比:{alert.volume_ratio:.2f}倍 / 売買代金:{alert.turnover_20d / 100_000_000:.1f}億円",
        f"🗓 決算予定日:{escape(alert.earnings_date)}",
    ]
    url = openwork_search_url(alert.name)
    if url:
        rows.append(f'👥 <a href="{escape(url)}">OpenWorkで社員クチコミを見る</a>')
    rows.append(f"理由:{escape(alert.reason)}")
    body = "".join(f'<div class="row">{row}</div>' for row in rows)
    return (
        '<div class="card">'
        f'<div class="ttl"><span class="cd">{escape(alert.code)}</span> '
        f'<span class="nm">{escape(alert.name)}</span></div>'
        f"{body}</div>"
    )


def build_html_body(new_alerts: list[Alert], max_items: int | None = None) -> str:
    """プレーンテキスト版と同じ中身のHTML。銘柄コードを青、銘柄名を黒の太字で出す。"""
    shown, omitted = select_mail_alerts(new_alerts, max_items)
    parts = [
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<style>{ALERT_MAIL_CSS}</style></head><body>",
        '<div class="hd"><div class="s">ザラ場リアルタイム高値アラート</div>'
        f'<div class="t">{escape(jst_now().strftime("%Y-%m-%d %H:%M"))} JST'
        f"／新規アラート {len(new_alerts)}件</div></div>",
    ]
    if omitted:
        parts.append(
            f'<p class="cut">※本文には売買代金の大きい順に{len(shown)}件だけ掲載しています'
            f"（残り{omitted}件は省略）。全件は intraday_high_alerts_*.csv にあります。</p>"
        )

    order = ["52週高値更新", "直近高値ブレイク", "52週高値接近", "直近高値接近"]
    grouped: dict[str, list[Alert]] = {key: [] for key in order}
    for alert in shown:
        grouped.setdefault(alert.alert_type, []).append(alert)
    for key in order:
        group = grouped.get(key, [])
        if not group:
            continue
        parts.append(f'<div class="grp">■ {escape(key)}（{len(group)}件）</div>')
        parts.extend(_alert_html_card(alert) for alert in group)

    parts.append(f'<div class="dis">{escape(DISCLAIMER)}</div>')
    parts.append("</body></html>")
    return "".join(parts)


def send_alert_mail(new_alerts: list[Alert]) -> bool:
    from gmail_notify import load_gmail_config, send_gmail

    config = load_gmail_config()
    if config is None:
        print("intraday_alert_mail=skipped reason=missing_secrets "
              "required=GMAIL_USER,GMAIL_APP_PASSWORD,MAIL_TO")
        return False
    subject = build_subject(new_alerts)
    body = build_body(new_alerts)
    # fix32(2026-08-28): 色付きのHTML版も一緒に送る。
    # ここが落ちてもアラートは届けたいので、失敗したらHTMLなしで送る。
    try:
        html_body = build_html_body(new_alerts)
    except Exception as exc:  # noqa: BLE001
        print(f"intraday_alert_mail_html=failed error={exc!r}", flush=True)
        html_body = None
    # T-K修正(2026-08-02): send_gmail の戻り値を必ず見る。
    # 旧実装は戻り値を捨てて常に True を返していたため、
    # 実際には送れていなくてもログに「送信」と出て、さらに呼び出し側が
    # 重複防止の状態を更新してしまい、同じアラートが二度と送られなかった。
    try:
        delivered = send_gmail(subject, body, config, html_body=html_body)
    except Exception as exc:  # noqa: BLE001 - 送信失敗を握りつぶさず記録する
        print(f"intraday_alert_mail=failed to={config.mail_to} error={exc!r}", flush=True)
        return False
    if not delivered:
        print(f"intraday_alert_mail=not_sent to={config.mail_to} subject={subject}", flush=True)
        return False
    print(f"intraday_alert_mail=sent to={config.mail_to} subject={subject}", flush=True)
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
    day = jst_today().strftime("%Y%m%d")
    watchlist_codes: set[str] | None = None
    if _watchlist_enabled():
        watchlist_codes = load_watchlist_codes(output_dir / WATCHLIST_NAME)
        if watchlist_codes:
            print(f"intraday_watchlist_loaded={len(watchlist_codes)}銘柄 path={output_dir / WATCHLIST_NAME}", flush=True)
        else:
            print("intraday_watchlist_none -> 全銘柄で監視（フォールバック）", flush=True)
    try:
        alerts = scan(markets=markets, limit=limit, watchlist_codes=watchlist_codes)
    except Exception as exc:
        print(f"intraday_scan_error={exc}")
        print("（ネット未接続のクラウドでは取得できません。GitHub Actions / Mac で実行してください）")
        return 1

    state = DedupState(state_path(output_dir, day), day)
    new_alerts = [a for a in alerts if state.is_new(a)]
    new_keys = {a.dedup_key() for a in new_alerts}

    csv_path = write_csv(alerts, new_keys, output_dir)
    # T-K修正(2026-08-03): 日次サマリーが前日のログを巻き込んで同じ数字を出す事故が
    # 起きていた。集計側が当日分だけを数えられるよう、JSTの日付を必ず添える。
    print(
        f"intraday_alerts_detected={len(alerts)} new={len(new_alerts)} "
        f"date={jst_today().isoformat()}"
    )
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

    # 52週高値更新（一時更新含む）は通知対象（カブタン/みんかぶの一覧に対応）。
    a52 = build_alert("7173", "東京きらぼし", ind(), {"high_type": "52W_NEW_HIGH"})
    assert a52 is not None and a52.alert_type == "52週高値更新" and a52.is_break, a52
    assert a52.line_label == "52週高値" and a52.line_price == 1000.0 and a52.dist_pct == 0.0, a52
    a52n = build_alert("8524", "北洋銀行", ind(current_price=992.0, dist_52w_high_pct=0.8), {"high_type": "52W_NEAR_HIGH"})
    assert a52n is not None and a52n.alert_type == "52週高値接近" and not a52n.is_break and a52n.dist_pct == 0.8, a52n

    # 一時更新の検出（当日Highが前日までの52週高値を上抜け→押し戻されてもOK）
    idx = pd.bdate_range(end="2026-07-24", periods=253)
    highs = [1000.0] * 252 + [1010.0]   # 当日だけ一時的に上抜け
    closes = [990.0] * 252 + [995.0]    # 終値は高値未満（＝Closeベース分類では拾えない）
    hist = pd.DataFrame({"High": highs, "Close": closes}, index=idx)
    touch = detect_intraday_52w_touch(hist)
    assert touch is not None and touch["high_type"] == "52W_NEW_HIGH" and touch["high_price"] == 1000.0, touch
    # 上抜けていない日は None
    hist2 = pd.DataFrame({"High": [1000.0] * 253, "Close": closes}, index=idx)
    assert detect_intraday_52w_touch(hist2) is None
    # データ不足（上場1年未満）は None＝捏造しない
    assert detect_intraday_52w_touch(hist.tail(100)) is None

    # 直近高値ブレイク（スイング）
    a = build_alert("7011", "三菱重工", ind(), {
        "high_type": "SWING_HIGH_BREAK",
        "high_price": 990.0,
        "dist_to_high_pct": 0.0,
    })
    assert a is not None and a.alert_type == "直近高値ブレイク" and a.is_break, a
    assert a.line_label == "直近高値", a

    # 直近高値接近（recent near）
    a = build_alert("6951", "日本電子", ind(), {
        "high_type": "RECENT_NEAR_HIGH",
        "high_price": 1010.0,
        "dist_to_high_pct": 1.0,
    })
    assert a is not None and a.alert_type == "直近高値接近" and a.line_label == "直近高値" and a.dist_pct == 1.0, a
    subj = build_subject([a])
    assert "直近高値まで1.0%" in subj, subj

    # 対象外: MAタッチ・分類外は None
    assert build_alert("0000", "x", ind(), {"high_type": "OTHER"}) is None
    assert build_alert("0000", "x", ind(), {"high_type": "RETEST_52W"}) is None

    # 流動性不足は None
    assert build_alert("0000", "x", ind(turnover_20d=10_000_000.0), {
        "high_type": "RECENT_NEAR_HIGH",
        "high_price": 1010.0,
        "dist_to_high_pct": 1.0,
    }) is None

    # 重複通知防止
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        sp = state_path(Path(td), "20260703")
        st = DedupState(sp, "20260703")
        alert = build_alert("7173", "東京きらぼし", ind(), {
            "high_type": "SWING_HIGH_BREAK",
            "high_price": 990.0,
            "dist_to_high_pct": 0.0,
        })
        assert st.is_new(alert)
        st.mark(alert)
        st.save()
        st2 = DedupState(sp, "20260703")
        assert not st2.is_new(alert), "同日・同種別は再通知しないはず"
        # 別種別なら通知可
        near = build_alert("7173", "東京きらぼし", ind(), {
            "high_type": "RECENT_NEAR_HIGH",
            "high_price": 1010.0,
            "dist_to_high_pct": 1.0,
        })
        assert st2.is_new(near), "別アラート種別は通知できるはず"
        # 翌日は同種別でも再通知
        st3 = DedupState(state_path(Path(td), "20260704"), "20260704")
        assert st3.is_new(alert)

    # CSV出力（新規フラグ付き）
    with tempfile.TemporaryDirectory() as td:
        alerts = [
            build_alert("7173", "A", ind(), {"high_type": "SWING_HIGH_BREAK", "high_price": 990.0, "dist_to_high_pct": 0.0}),
            build_alert("8524", "B", ind(), {"high_type": "RECENT_NEAR_HIGH", "high_price": 1010.0, "dist_to_high_pct": 0.8}),
        ]
        new_keys = {alerts[0].dedup_key()}
        path = write_csv(alerts, new_keys, Path(td))
        assert path is not None and path.exists()
        df = pd.read_csv(path, dtype={"code": str})
        assert len(df) == 2 and df["is_new"].sum() == 1, df

    # 本文・件名（複数）
    multi = [
        build_alert("1518", "三井松島HD", ind(), {"high_type": "52W_NEW_HIGH"}),
        build_alert("7173", "東京きらぼし", ind(), {"high_type": "SWING_HIGH_BREAK", "high_price": 990.0, "dist_to_high_pct": 0.0}),
        build_alert("8524", "北洋銀行", ind(), {"high_type": "RECENT_NEAR_HIGH", "high_price": 1010.0, "dist_to_high_pct": 0.8}),
    ]
    body = build_body(multi)
    assert "52週高値更新（1件）" in body, body
    assert "直近高値ブレイク（1件）" in body and "直近高値接近（1件）" in body, body
    # 52週更新が本文の先頭グループに来る
    assert body.index("52週高値更新") < body.index("直近高値ブレイク"), body
    assert "決算予定日" in body, body
    assert "OpenWork評価" not in body, body
    assert "👥 OpenWork:https://www.openwork.jp/search.php?src_str=" in body, body
    assert quote("三井松島HD", safe="") in body, body
    # --- 決算予定日: 過去日を「予定日」と偽らない（2026-08-10 の誤表示の再発防止）---
    import datetime as _dt
    _today = _dt.date(2026, 8, 10)
    assert earnings_label(None, _today) == "未取得"
    assert earnings_label(_dt.date(2026, 8, 7), _today) == "次回未定（前回 2026-08-07）", \
        earnings_label(_dt.date(2026, 8, 7), _today)
    assert earnings_label(_dt.date(2026, 8, 12), _today) == "2026-08-12 ⚠️ 決算接近", \
        earnings_label(_dt.date(2026, 8, 12), _today)
    assert earnings_label(_dt.date(2026, 11, 5), _today) == "2026-11-05", \
        earnings_label(_dt.date(2026, 11, 5), _today)
    print("self-test: 決算予定日は過去日を「次回未定（前回…）」と書き分ける OK")
    assert DISCLAIMER in body
    assert "ほか2件" in build_subject(multi)
    assert "52週高値更新" in build_subject(multi), build_subject(multi)

    # --- 通知の絞り込み（IH_ALERT_SCOPE）---
    a_52w = build_alert("1518", "三井松島HD", ind(), {"high_type": "52W_NEW_HIGH"})
    a_swing = build_alert("7011", "三菱重工", ind(), {
        "high_type": "SWING_HIGH_BREAK", "high_price": 990.0, "dist_to_high_pct": 0.0})
    a_recent_new = build_alert("7173", "東京きらぼし", ind(), {
        "high_type": "RECENT_NEW_HIGH", "high_price": 990.0, "dist_to_high_pct": 0.0})
    a_52w_near = build_alert("8524", "北洋銀行", ind(current_price=992.0, dist_52w_high_pct=0.8),
                             {"high_type": "52W_NEAR_HIGH"})
    a_recent_near = build_alert("6951", "日本電子", ind(), {
        "high_type": "RECENT_NEAR_HIGH", "high_price": 1010.0, "dist_to_high_pct": 1.0})
    sample = [a_52w, a_swing, a_recent_new, a_52w_near, a_recent_near]

    # break（既定）= 更新・ブレイクの3件だけ通す。接近2件は除外。
    kept = [x for x in sample if alert_in_scope(x, "break")]
    assert kept == [a_52w, a_swing, a_recent_new], kept
    # 52w = 52週高値更新の1件だけ。直近高値ブレイクも除外される。
    kept = [x for x in sample if alert_in_scope(x, "52w")]
    assert kept == [a_52w], kept
    # all = 従来どおり全件。
    assert all(alert_in_scope(x, "all") for x in sample)

    # 環境変数の読み取り（表記ゆれ吸収・未知の値は既定にフォールバック）
    saved = os.environ.get("IH_ALERT_SCOPE")
    try:
        os.environ.pop("IH_ALERT_SCOPE", None)
        assert _alert_scope() == DEFAULT_ALERT_SCOPE == "break", _alert_scope()
        for raw, want in (("52w", "52w"), ("ALL", "all"), (" Break ", "break"),
                          ("breakout", "break"), ("yes-please", DEFAULT_ALERT_SCOPE)):
            os.environ["IH_ALERT_SCOPE"] = raw
            assert _alert_scope() == want, (raw, _alert_scope(), want)
    finally:
        if saved is None:
            os.environ.pop("IH_ALERT_SCOPE", None)
        else:
            os.environ["IH_ALERT_SCOPE"] = saved

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
