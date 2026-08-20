"""Claude 300万円ペーパー運用：規律ルールどおりに翌営業日の注文を宣告する。

高重さんの指示(2026-08-18)「規律ルールで自動発注」。
これまでは買い候補が毎日出ていても注文台帳に入る仕組みが無く、
2026-07-21の1件（良品計画）以外は1件も約定していなかった。

ルール（note記事に書いてあるものと同じ）:
  - 新規はS→A→Bの上位から。地合いNORMAL=最大3銘柄 / CAUTION=1銘柄 / RISK・STOP=新規なし
  - 1枠およそ100万円・100株単位・現金の範囲内
  - 保有は最大3銘柄。すでに持っている銘柄は買い増ししない
  - 手仕舞い: 損切 -7% / 利確 +15% / 10営業日タイムアウト

実データ（outputs/screening_result*.csv・regime.txt・台帳CSV）だけで判断する。
値段が取れないものは見送る。推測では埋めない。
これは架空資金の記録であり、投資助言ではない。
"""

from __future__ import annotations

import argparse
import os
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from jptime import is_jpx_business_day
from market_regime import fetch_regime

PROJECT_ROOT = Path(__file__).resolve().parent
ORDERS_PATH = PROJECT_ROOT / "data" / "claude_300man_orders.csv"
JOURNAL_PATH = PROJECT_ROOT / "data" / "claude_300man_journal.csv"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs"
REGIME_PATH = PROJECT_ROOT / "regime.txt"
JST = ZoneInfo("Asia/Tokyo")

INITIAL_CASH = 3_000_000
SLOT_YEN = 1_000_000
MAX_POSITIONS = 3
STOP_LOSS_PCT = -7.0
TAKE_PROFIT_PCT = 15.0
TIMEOUT_DAYS = 10

ORDER_COLUMNS = [
    "decision_date", "execution_date", "side", "code", "ticker",
    "name", "shares", "reason", "status",
]
JOURNAL_COLUMNS = [
    "entry_date", "fill_time_jst", "status", "code", "ticker",
    "name", "entry_price", "shares", "position_value", "source_order_date",
    "exit_date", "exit_price", "exit_value", "realized_pnl", "exit_order_date",
]

# 地合いごとの新規建て上限
REGIME_SLOTS = {"NORMAL": 3, "CAUTION": 1, "RISK": 0, "STOP": 0}


def _read(path: Path, columns: list[str]) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=columns)
    return pd.read_csv(path, dtype=str).reindex(columns=columns).fillna("")


def _write(df: pd.DataFrame, path: Path, columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.reindex(columns=columns).to_csv(path, index=False, encoding="utf-8-sig")


def _numbers(series: pd.Series | None) -> pd.Series:
    if series is None:
        return pd.Series(dtype=float)
    return pd.to_numeric(series, errors="coerce").fillna(0)


def next_business_day(day: date) -> date:
    nxt = day + timedelta(days=1)
    for _ in range(30):
        if is_jpx_business_day(nxt):
            return nxt
        nxt += timedelta(days=1)
    return nxt


def business_days_between(start: date, end: date) -> int:
    """start(約定日)からend(当日)までの営業日数。start当日は0。"""
    if end <= start:
        return 0
    count = 0
    cursor = start + timedelta(days=1)
    while cursor <= end:
        if is_jpx_business_day(cursor):
            count += 1
        cursor += timedelta(days=1)
    return count


def load_regime() -> str:
    """地合いを読む。

    note記事と同じ market_regime.fetch_regime() を使う。
    こうしておけば「記事に書いてある地合い」と「実際に発注した地合い」が
    必ず一致する。取得できないときは fetch_regime が安全側のSTOPを返す。
    """
    try:
        return fetch_regime().value
    except Exception as error:  # noqa: BLE001
        print(f"claude_300man_declare=regime_unreadable err={error}")
        return "STOP"


def _file_day(path: Path) -> date | None:
    """screening_result_YYYYMMDD_HHMMSS.csv から日付を取る。取れなければ更新日時。"""
    stem = path.stem
    if stem.startswith("screening_result_"):
        token = stem[len("screening_result_"):].split("_", 1)[0]
        if len(token) == 8 and token.isdigit():
            try:
                return date(int(token[:4]), int(token[4:6]), int(token[6:8]))
            except ValueError:
                pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, JST).date()
    except OSError:
        return None


def load_screening(output_dir: Path, today: date | None = None) -> pd.DataFrame:
    """当日のスクリーニング結果だけを使う。

    古いCSVがリポジトリに残っているので、日付を確認せずに拾うと
    2か月前の株価で発注してしまう。当日のものが無ければ空を返す
    （見送り。推測では埋めない）。
    """
    today = today or datetime.now(JST).date()
    fixed = output_dir / "screening_result.csv"
    if fixed.exists() and fixed.stat().st_size > 0:
        path = fixed
    else:
        found = sorted(output_dir.glob("screening_result_*.csv"))
        if not found:
            print("claude_300man_declare=no_screening_file")
            return pd.DataFrame()
        path = found[-1]
    day = _file_day(path)
    if day != today:
        print(f"claude_300man_declare=screening_stale file={path.name} day={day} today={today}")
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype=str).fillna("")
    except Exception as error:  # noqa: BLE001
        print(f"claude_300man_declare=screening_unreadable err={error}")
        return pd.DataFrame()


def _price_map(screening: pd.DataFrame) -> dict[str, float]:
    out: dict[str, float] = {}
    if screening.empty:
        return out
    for _, row in screening.iterrows():
        code = str(row.get("code", "")).strip()
        try:
            price = float(str(row.get("current_price", "")).strip())
        except (TypeError, ValueError):
            continue
        if code and price > 0:
            out[code] = price
    return out


def _cash_balance(journal: pd.DataFrame) -> float:
    if journal.empty:
        return float(INITIAL_CASH)
    bought = _numbers(journal.get("position_value")).sum()
    sold = _numbers(journal.get("exit_value")).sum()
    return float(INITIAL_CASH - bought + sold)


def _open_positions(journal: pd.DataFrame) -> pd.DataFrame:
    if journal.empty:
        return journal
    return journal[journal["status"].astype(str).str.upper().eq("OPEN")]


def _declared(orders: pd.DataFrame) -> pd.DataFrame:
    if orders.empty:
        return orders
    return orders[orders["status"].astype(str).str.upper().eq("DECLARED")]


def _exit_reason(entry_price: float, price: float | None, held_days: int) -> str | None:
    if price is not None and entry_price > 0:
        change = (price - entry_price) / entry_price * 100
        if change <= STOP_LOSS_PCT:
            return f"損切ルール {change:+.1f}%（-7%以下）"
        if change >= TAKE_PROFIT_PCT:
            return f"利確ルール {change:+.1f}%（+15%以上）"
    if held_days >= TIMEOUT_DAYS:
        return f"タイムアウト {held_days}営業日（10営業日）"
    return None


def declare(output_dir: Path, today: date | None = None) -> int:
    """翌営業日ぶんの注文を宣告する。宣告した件数を返す。"""
    today = today or datetime.now(JST).date()
    execution_date = next_business_day(today)
    orders = _read(ORDERS_PATH, ORDER_COLUMNS)
    journal = _read(JOURNAL_PATH, JOURNAL_COLUMNS)
    screening = load_screening(output_dir, today)
    prices = _price_map(screening)
    regime = load_regime()
    declared_now: list[dict[str, str]] = []

    pending = _declared(orders)
    pending_codes = set(pending["code"].astype(str)) if not pending.empty else set()
    open_rows = _open_positions(journal)
    held_codes = set(open_rows["code"].astype(str)) if not open_rows.empty else set()

    # --- 手仕舞い（損切 / 利確 / タイムアウト） -------------------------------
    selling: set[str] = set()
    for _, row in open_rows.iterrows():
        code = str(row.get("code", "")).strip()
        if not code or code in pending_codes:
            continue
        try:
            entry_price = float(row.get("entry_price") or 0)
            shares = int(float(row.get("shares") or 0))
            entry_date = date.fromisoformat(str(row.get("entry_date")).strip())
        except (TypeError, ValueError):
            print(f"claude_300man_declare=skip_exit code={code} reason=broken_row")
            continue
        if shares <= 0:
            continue
        reason = _exit_reason(entry_price, prices.get(code), business_days_between(entry_date, today))
        if reason is None:
            continue
        declared_now.append({
            "decision_date": today.isoformat(),
            "execution_date": execution_date.isoformat(),
            "side": "SELL",
            "code": code,
            "ticker": str(row.get("ticker") or f"{code}.T"),
            "name": str(row.get("name") or ""),
            "shares": str(shares),
            "reason": reason,
            "status": "DECLARED",
        })
        selling.add(code)
        print(f"claude_300man_declare=sell code={code} reason={reason}")

    # --- 未約定のBUYぶんを先に取り置く ---------------------------------------
    # 前日の宣告が約定しなかった（始値が上振れた等）まま残っていると、
    # 同じ現金を二重に使ってしまう。枠と現金の両方から先に引いておく。
    pending_buy = pending[pending["side"].astype(str).str.upper().eq("BUY")] if not pending.empty else pending
    reserved = 0.0
    unpriced_pending = False
    for _, row in pending_buy.iterrows():
        code = str(row.get("code", "")).strip()
        price = prices.get(code)
        try:
            shares = int(float(row.get("shares") or 0))
        except (TypeError, ValueError):
            shares = 0
        if price and shares > 0:
            reserved += price * shares
        else:
            unpriced_pending = True

    # --- 新規建て -----------------------------------------------------------
    slots_by_regime = REGIME_SLOTS.get(regime, 1)
    used_slots = len(held_codes) - len(selling) + len(pending_buy)
    free_slots = min(slots_by_regime, MAX_POSITIONS - used_slots)
    cash = _cash_balance(journal) - reserved
    bought = 0
    if unpriced_pending:
        print("claude_300man_declare=no_new reason=pending_order_price_unknown")
    elif free_slots <= 0:
        print(
            f"claude_300man_declare=no_new_slot regime={regime} held={len(held_codes)} "
            f"selling={len(selling)} pending_buy={len(pending_buy)}"
        )
    elif screening.empty:
        print("claude_300man_declare=no_screening")
    else:
        ranked = screening.copy()
        ranked["_rank"] = ranked.get("rank", "").astype(str).str.upper()
        ranked = ranked[ranked["_rank"].isin(["S", "A", "B"])]
        ranked["_order"] = ranked["_rank"].map({"S": 0, "A": 1, "B": 2}).fillna(9)
        ranked["_score"] = pd.to_numeric(ranked.get("score"), errors="coerce").fillna(0)
        ranked = ranked.sort_values(["_order", "_score"], ascending=[True, False])
        for _, row in ranked.iterrows():
            if bought >= free_slots:
                break
            code = str(row.get("code", "")).strip()
            if not code or code in held_codes or code in pending_codes:
                continue
            price = prices.get(code)
            if not price or price <= 0:
                continue
            shares = int(SLOT_YEN // price // 100) * 100
            if shares <= 0:
                continue
            # 約定は翌営業日の始値。上に飛んでも足りるよう5%の余裕を見る。
            cost = price * shares
            if cost * 1.05 > cash:
                continue
            declared_now.append({
                "decision_date": today.isoformat(),
                "execution_date": execution_date.isoformat(),
                "side": "BUY",
                "code": code,
                "ticker": str(row.get("ticker") or f"{code}.T"),
                "name": str(row.get("name") or ""),
                "shares": str(shares),
                "reason": f"自動発注（S→A→B順） {row.get('rank')}ランク・スコア{row.get('score')}",
                "status": "DECLARED",
            })
            pending_codes.add(code)
            cash -= cost
            bought += 1
            print(f"claude_300man_declare=buy code={code} shares={shares} price={price}")

    if not declared_now:
        print(
            f"claude_300man_declare=none regime={regime} held={len(held_codes)} "
            f"pending={len(pending_codes)} execution_date={execution_date.isoformat()}"
        )
        return 0

    orders = pd.concat([orders, pd.DataFrame(declared_now)], ignore_index=True).fillna("")
    _write(orders, ORDERS_PATH, ORDER_COLUMNS)
    print(
        f"claude_300man_declare=written count={len(declared_now)} regime={regime} "
        f"execution_date={execution_date.isoformat()}"
    )
    return len(declared_now)


def push_orders() -> bool:
    """注文台帳を main に push する。

    これをしないと、翌朝9:40の約定ワークフローが別のチェックアウトで動くので
    宣告が消えてしまう。押せなくてもメール送信は止めない。
    """

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            args, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120
        )

    try:
        if run("git", "rev-parse", "--is-inside-work-tree").returncode != 0:
            print("claude_300man_declare=push_skipped reason=not_a_git_repo")
            return False
        run("git", "config", "user.name", "github-actions[bot]")
        run("git", "config", "user.email", "github-actions[bot]@users.noreply.github.com")
        if run("git", "add", str(ORDERS_PATH.relative_to(PROJECT_ROOT))).returncode != 0:
            print("claude_300man_declare=push_failed step=add")
            return False
        if run("git", "diff", "--cached", "--quiet").returncode == 0:
            print("claude_300man_declare=push_skipped reason=no_change")
            return True
        today = datetime.now(JST).date().isoformat()
        commit = run("git", "commit", "-m", f"chore: declare 300man orders {today} [skip ci]")
        if commit.returncode != 0:
            print(f"claude_300man_declare=push_failed step=commit err={commit.stderr.strip()[:200]}")
            return False
        branch = os.environ.get("GITHUB_REF_NAME") or "main"
        run("git", "pull", "--rebase", "origin", branch)
        push = run("git", "push", "origin", f"HEAD:{branch}")
        if push.returncode != 0:
            print(f"claude_300man_declare=push_failed step=push err={push.stderr.strip()[:200]}")
            return False
    except Exception as error:  # noqa: BLE001
        print(f"claude_300man_declare=push_failed reason={error}")
        return False
    print("claude_300man_declare=pushed")
    return True


def declare_production(output_dir: Path, today: date | None = None) -> int:
    """本番の outputs/ のときだけ発注する（セルフテストの一時ディレクトリでは動かない）。"""
    try:
        if output_dir.resolve() != DEFAULT_OUTPUT_DIR.resolve():
            print(f"claude_300man_declare=skipped reason=not_production_output_dir dir={output_dir}")
            return 0
        count = declare(output_dir, today)
        if count:
            push_orders()
        return count
    except Exception as error:  # noqa: BLE001 - 発注に失敗してもメールは送る
        print(f"claude_300man_declare=failed reason={error}")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--date", default=None)
    args = parser.parse_args()
    today = date.fromisoformat(args.date) if args.date else None
    declare(Path(args.output_dir), today)


if __name__ == "__main__":
    main()
