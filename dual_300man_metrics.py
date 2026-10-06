"""第2期300万円運用のMFE/MAE・約定差・決済後推移を更新して報告する。"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from dual_300man_config import CONFIG, JOURNAL_COLUMNS, ORDER_COLUMNS, ROOT
from dual_300man_fill import account_paths, cash_balance, read_csv, write_csv, write_ledger
from scanner.prices import normalize_price_history


JST = ZoneInfo("Asia/Tokyo")
REPORT_PATH = ROOT / "docs" / "dual_300man_metrics.md"
PriceFetcher = Callable[[str], pd.DataFrame]


def fetch_metrics_history(ticker: str) -> pd.DataFrame:
    """少数の保有銘柄だけを短期間取得する（全銘柄スキャン用取得器とは分離）。"""
    import yfinance as yf

    data = yf.download(
        ticker,
        period="6mo",
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=False,
        timeout=20,
    )
    return normalize_price_history(data)


def _num(value: object) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) else None


def _history_frame(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    out = frame.copy()
    out.index = pd.to_datetime(out.index)
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_localize(None)
    needed = ["Open", "High", "Low", "Close"]
    if any(column not in out.columns for column in needed):
        return pd.DataFrame()
    for column in needed:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    return out.dropna(subset=needed).sort_index()


def _infer_exit_type(reason: object) -> str:
    text = str(reason or "")
    if "損切" in text:
        return "STOP_LOSS"
    if "利確" in text:
        return "TAKE_PROFIT"
    if "トレーリング" in text:
        return "TRAILING_STOP"
    if "タイムアウト" in text:
        return "TIMEOUT"
    return ""


def _hydrate_metadata(row: pd.Series, orders: pd.DataFrame) -> dict[str, str]:
    updates: dict[str, str] = {}
    buys = orders[
        orders["side"].str.upper().eq("BUY")
        & orders["code"].eq(str(row.get("code", "")))
        & orders["decision_date"].eq(str(row.get("source_order_date", "")))
    ]
    if not buys.empty:
        order = buys.iloc[-1]
        for key in ("phase_id", "strategy_version", "rule_hash"):
            if not str(row.get(key, "")).strip():
                updates[key] = str(order.get(key, ""))
        if not str(row.get("entry_decision_price", "")).strip():
            updates["entry_decision_price"] = str(order.get("decision_price", ""))
        entry = _num(row.get("entry_price"))
        decision = _num(updates.get("entry_decision_price") or row.get("entry_decision_price"))
        if entry and decision and not str(row.get("entry_gap_pct", "")).strip():
            updates["entry_gap_pct"] = f"{(entry / decision - 1) * 100:.2f}"
    if str(row.get("status", "")).upper() == "CLOSED":
        sells = orders[
            orders["side"].str.upper().eq("SELL")
            & orders["code"].eq(str(row.get("code", "")))
            & orders["decision_date"].eq(str(row.get("exit_order_date", "")))
        ]
        if not sells.empty:
            order = sells.iloc[-1]
            for target, source in (
                ("exit_type", "exit_type"), ("exit_reason", "reason"),
                ("exit_decision_price", "decision_price"),
                ("exit_decision_return_pct", "decision_return_pct"),
                ("holding_business_days", "holding_business_days"),
            ):
                if not str(row.get(target, "")).strip():
                    updates[target] = str(order.get(source, ""))
        if not (updates.get("exit_type") or str(row.get("exit_type", "")).strip()):
            updates["exit_type"] = _infer_exit_type(updates.get("exit_reason") or row.get("exit_reason"))
    return updates


def update_account(
    account: str,
    fetcher: PriceFetcher = fetch_metrics_history,
    as_of: date | None = None,
) -> tuple[pd.DataFrame, dict[str, int]]:
    as_of = as_of or datetime.now(JST).date()
    orders_path, journal_path, _ = account_paths(account)
    orders = read_csv(orders_path, ORDER_COLUMNS)
    journal = read_csv(journal_path, JOURNAL_COLUMNS)
    stats = {
        "rows": len(journal),
        "updated": 0,
        "price_unavailable": 0,
        "stale_ignored": 0,
    }
    cache: dict[str, pd.DataFrame] = {}
    for idx, row in journal.iterrows():
        for key, value in _hydrate_metadata(row, orders).items():
            journal.at[idx, key] = value
        entry_price = _num(row.get("entry_price"))
        shares = _num(row.get("shares"))
        if not entry_price or not shares:
            continue
        value = entry_price * shares
        journal.at[idx, "position_pct_initial"] = f"{value / CONFIG.initial_cash * 100:.2f}"
        stop_risk = value * abs(CONFIG.stop_loss_pct) / 100
        journal.at[idx, "planned_stop_risk_jpy"] = f"{stop_risk:.0f}"
        journal.at[idx, "planned_stop_risk_pct_initial"] = f"{stop_risk / CONFIG.initial_cash * 100:.2f}"
        ticker = str(row.get("ticker") or f"{row.get('code')}.T")
        if ticker not in cache:
            try:
                cache[ticker] = _history_frame(fetcher(ticker))
            except Exception as error:  # 外部価格障害で他銘柄まで止めない
                print(f"dual_300man_metrics=price_error account={account} ticker={ticker} err={error}")
                cache[ticker] = pd.DataFrame()
        history = cache[ticker]
        if history.empty:
            stats["price_unavailable"] += 1
            continue
        try:
            entry_date = date.fromisoformat(str(row.get("entry_date", "")))
        except ValueError:
            continue
        status = str(row.get("status", "")).upper()
        exit_date = None
        if status == "CLOSED":
            try:
                exit_date = date.fromisoformat(str(row.get("exit_date", "")))
            except ValueError:
                exit_date = None
        end_date = min(exit_date or as_of, as_of)
        observed = history[
            (history.index.date >= entry_date) & (history.index.date <= end_date)
        ]
        if observed.empty:
            stats["price_unavailable"] += 1
            continue
        # 遅延workflowや価格配信の一時欠損で、すでに記録済みの新しい価格日を
        # 古い日足へ戻してはならない。価格日が後退する場合は、MFE/MAE・時価・
        # 含み損益をまとめて現状維持する。
        candidate_mark_day = observed.index[-1].date()
        try:
            existing_mark_day = date.fromisoformat(str(row.get("last_mark_date", "")))
        except ValueError:
            existing_mark_day = None
        if existing_mark_day is not None and candidate_mark_day < existing_mark_day:
            stats["stale_ignored"] += 1
            print(
                f"dual_300man_metrics=stale_ignored account={account} "
                f"code={row.get('code', '')} existing={existing_mark_day} "
                f"candidate={candidate_mark_day}"
            )
            continue
        peak_stamp = observed["High"].idxmax()
        trough_stamp = observed["Low"].idxmin()
        peak = float(observed.loc[peak_stamp, "High"])
        trough = float(observed.loc[trough_stamp, "Low"])
        peak_day = peak_stamp.date()
        journal.at[idx, "peak_price"] = f"{peak:.2f}"
        journal.at[idx, "peak_date"] = peak_day.isoformat()
        journal.at[idx, "mfe_pct"] = f"{(peak / entry_price - 1) * 100:.2f}"
        journal.at[idx, "mfe_peak_business_day"] = str(sum(
            1 for stamp in observed.index if entry_date < stamp.date() <= peak_day
        ))
        journal.at[idx, "trough_price"] = f"{trough:.2f}"
        journal.at[idx, "trough_date"] = trough_stamp.date().isoformat()
        journal.at[idx, "mae_pct"] = f"{(trough / entry_price - 1) * 100:.2f}"
        last_stamp = observed.index[-1]
        mark = float(observed.iloc[-1]["Close"])
        journal.at[idx, "last_mark_date"] = last_stamp.date().isoformat()
        journal.at[idx, "mark_price"] = f"{mark:.2f}"
        if status == "OPEN":
            journal.at[idx, "unrealized_pnl"] = f"{(mark - entry_price) * shares:.0f}"
            journal.at[idx, "unrealized_return_pct"] = f"{(mark / entry_price - 1) * 100:.2f}"
        else:
            exit_price = _num(row.get("exit_price"))
            if exit_price:
                exit_return = (exit_price / entry_price - 1) * 100
                journal.at[idx, "exit_return_pct"] = f"{exit_return:.2f}"
                mfe = (peak / entry_price - 1) * 100
                if exit_return > 0 and mfe > 0:
                    journal.at[idx, "profit_capture_pct"] = f"{exit_return / mfe * 100:.1f}"
                decision_price = _num(journal.at[idx, "exit_decision_price"])
                if decision_price:
                    journal.at[idx, "exit_decision_return_pct"] = f"{(decision_price / entry_price - 1) * 100:.2f}"
                    journal.at[idx, "exit_gap_pct"] = f"{(exit_price / decision_price - 1) * 100:.2f}"
                if exit_date:
                    after = history[history.index.date > exit_date].iloc[:10]
                    if len(after) >= 5:
                        journal.at[idx, "post_exit_return_5_pct"] = f"{(float(after.iloc[4]['Close']) / exit_price - 1) * 100:.2f}"
                    if len(after) >= 10:
                        journal.at[idx, "post_exit_return_10_pct"] = f"{(float(after.iloc[9]['Close']) / exit_price - 1) * 100:.2f}"
                    if not after.empty:
                        journal.at[idx, "post_exit_max_10_pct"] = f"{(float(after['High'].max()) / exit_price - 1) * 100:.2f}"
        journal.at[idx, "metrics_updated_at"] = datetime.now(JST).isoformat(timespec="seconds")
        stats["updated"] += 1
    write_csv(journal, journal_path, JOURNAL_COLUMNS)
    write_ledger(account, orders, journal)
    return journal, stats


def _mean(values: pd.Series) -> float | None:
    nums = pd.to_numeric(values, errors="coerce").dropna()
    return float(nums.mean()) if not nums.empty else None


def _fmt(value: float | None, digits: int = 2, suffix: str = "%") -> str:
    return "データ待ち" if value is None else f"{value:+.{digits}f}{suffix}"


def _account_report(account: str, journal: pd.DataFrame) -> list[str]:
    label = "Claude" if account == "claude" else "Codex"
    status = journal["status"].str.upper() if not journal.empty else pd.Series(dtype=str)
    open_rows = journal[status.eq("OPEN")].copy() if not journal.empty else journal
    closed = journal[status.eq("CLOSED")].copy() if not journal.empty else journal
    returns = pd.to_numeric(closed.get("exit_return_pct"), errors="coerce").dropna()
    wins = returns[returns > 0]
    losses = returns[returns <= 0]
    win_rate = len(wins) / len(returns) * 100 if len(returns) else None
    avg_win = float(wins.mean()) if len(wins) else None
    avg_loss = float(losses.mean()) if len(losses) else None
    payoff = avg_win / abs(avg_loss) if avg_win is not None and avg_loss not in (None, 0) else None
    breakeven = abs(avg_loss) / (avg_win + abs(avg_loss)) * 100 if avg_win is not None and avg_loss not in (None, 0) else None
    expectancy = (avg_win * len(wins) + avg_loss * len(losses)) / len(returns) if len(returns) and avg_win is not None and avg_loss is not None else None
    exits = closed.get("exit_type", pd.Series(dtype=str)).astype(str)
    stop_rows = closed[exits.eq("STOP_LOSS")]
    stop_returns = pd.to_numeric(stop_rows.get("exit_return_pct"), errors="coerce").dropna()
    stop_gap = _mean(stop_rows.get("exit_gap_pct", pd.Series(dtype=str)))
    timeout_rows = closed[exits.eq("TIMEOUT")]
    profitable = closed[pd.to_numeric(closed.get("exit_return_pct"), errors="coerce") > 0]
    marked_values = (
        pd.to_numeric(open_rows.get("mark_price"), errors="coerce").fillna(pd.to_numeric(open_rows.get("entry_price"), errors="coerce"))
        * pd.to_numeric(open_rows.get("shares"), errors="coerce").fillna(0)
    ) if not open_rows.empty else pd.Series(dtype=float)
    assets = cash_balance(journal) + float(marked_values.sum())
    max_position = _mean(open_rows.get("position_pct_initial", pd.Series(dtype=str)))
    if not open_rows.empty:
        pos = pd.to_numeric(open_rows.get("position_pct_initial"), errors="coerce").dropna()
        max_position = float(pos.max()) if not pos.empty else None
    sample_note = "⚠️ 20決済未満のため、勝率・損益比は暫定値です。" if len(closed) < 20 else "20決済以上。引き続き期間別の安定性を確認します。"
    lines = [f"## {label}", "", f"- {sample_note}",
        f"- 総資産（最終取得終値ベース）: {assets:,.0f}円 / 現金 {cash_balance(journal):,.0f}円 / 保有 {len(open_rows)}銘柄",
        f"- 投入比率: 合計 {marked_values.sum() / CONFIG.initial_cash * 100:.1f}% / 最大1銘柄 {_fmt(max_position)}",
        f"- 決済: {len(closed)}回（勝ち{len(wins)} / 負け{len(losses)}） / 勝率 {_fmt(win_rate, 1)}",
        f"- 平均勝ち {_fmt(avg_win)} / 平均負け {_fmt(avg_loss)} / 損益比 {('データ待ち' if payoff is None else f'{payoff:.2f}')}",
        f"- 損益分岐勝率 {('データ待ち' if breakeven is None else f'{breakeven:.1f}%')} / 1取引期待値 {_fmt(expectancy)}",
        "", "### A) 損切りの実効性", "",
        f"- 損切り決済: {len(stop_rows)}回 / 実約定が−5〜−7%: {int(stop_returns.between(-7, -5).sum())}回 / −10%超: {int((stop_returns < -10).sum())}回",
        f"- 判定翌朝の平均ギャップ: {_fmt(stop_gap)}（マイナスほど損失拡大）",
        "", "### B) 利益の伸ばし方", "",
        f"- 固定利確: {int(exits.eq('TAKE_PROFIT').sum())}回 / トレーリング: {int(exits.eq('TRAILING_STOP').sum())}回 / タイムアウト: {len(timeout_rows)}回",
        f"- 勝ちトレードの平均MFE: {_fmt(_mean(profitable.get('mfe_pct', pd.Series(dtype=str))))}",
        f"- 勝ちトレードの平均利益捕捉率: {_fmt(_mean(profitable.get('profit_capture_pct', pd.Series(dtype=str))), 1)}",
        f"- タイムアウト後5日: {_fmt(_mean(timeout_rows.get('post_exit_return_5_pct', pd.Series(dtype=str))))} / 10日: {_fmt(_mean(timeout_rows.get('post_exit_return_10_pct', pd.Series(dtype=str))))}",
        f"- タイムアウトまでの平均MFE: {_fmt(_mean(timeout_rows.get('mfe_pct', pd.Series(dtype=str))))} / 平均MAE: {_fmt(_mean(timeout_rows.get('mae_pct', pd.Series(dtype=str))))}",
        "",
    ]
    return lines


def build_report(accounts: dict[str, pd.DataFrame], stats: dict[str, dict[str, int]]) -> str:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    lines = [
        "# Codex vs Claude 300万円運用 — 第2期検証レポート", "",
        f"- 更新: {now}", f"- 期: `{CONFIG.phase_id}`",
        f"- ルール版: `{CONFIG.strategy_version}` / 設定ハッシュ `{CONFIG.rule_hash}`",
        f"- 正本ルール: 1枠{CONFIG.slot_yen:,}円、損切り{CONFIG.stop_loss_pct:.0f}%、利確+{CONFIG.take_profit_pct:.0f}%、{CONFIG.timeout_days}営業日",
        "- 売買コスト・税金: ペーパー運用のため0円（実運用との差として明示）",
        "- トレーリング: 数値ルール未定義のため現在は無効。決済種別とMFEは記録済み。", "",
    ]
    for account in ("codex", "claude"):
        lines.extend(_account_report(account, accounts[account]))
    lines.extend(["## データ品質", ""])
    for account in ("codex", "claude"):
        item = stats[account]
        lines.append(
            f"- {account}: {item['updated']}/{item['rows']}行更新、"
            f"価格未取得 {item['price_unavailable']}行、古い価格への後退抑止 {item.get('stale_ignored', 0)}行"
        )
    lines.extend(["", "判定時損益率と翌朝実約定率を分離しているため、閾値・翌朝ギャップ・価格取得障害を切り分けられます。", ""])
    return "\n".join(lines)


def run(as_of: date | None = None, fetcher: PriceFetcher = fetch_metrics_history) -> dict[str, dict[str, int]]:
    accounts: dict[str, pd.DataFrame] = {}
    stats: dict[str, dict[str, int]] = {}
    for account in ("codex", "claude"):
        accounts[account], stats[account] = update_account(account, fetcher=fetcher, as_of=as_of)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(build_report(accounts, stats), encoding="utf-8")
    print(f"dual_300man_metrics={REPORT_PATH} stats={stats}")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="")
    args = parser.parse_args()
    run(date.fromisoformat(args.date) if args.date else None)


if __name__ == "__main__":
    main()
