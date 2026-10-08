"""Claude本番台帳を変えず、無料日足で出口方式を並走比較する。"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from claude_shadow_exits import EXIT_VARIANTS, simulate_exit
from dual_300man_config import CONFIG, JOURNAL_COLUMNS, ROOT
from dual_300man_fill import read_csv
from dual_300man_metrics import fetch_metrics_history


JST = ZoneInfo("Asia/Tokyo")
SHADOW_PATH = ROOT / "data" / "claude_300man_shadow.csv"
REPORT_PATH = ROOT / "docs" / "claude_300man_shadow.md"
EXPERIMENT_START = date(2026, 10, 5)
SHADOW_COLUMNS = [
    "phase_id", "strategy_version", "rule_hash", "variant", "variant_label",
    "source_order_date", "entry_date", "code", "ticker", "name", "sector",
    "entry_price", "shares", "status", "activation_date", "peak_close",
    "peak_close_date", "mfe_pct", "mae_pct", "last_observed_date", "exit_type",
    "exit_signal_date", "exit_date", "exit_price", "gross_return_pct",
    "net_return_pct", "holding_business_days", "roundtrip_cost_pct",
    "updated_at_jst",
]


def _number(value: object) -> float | None:
    try:
        out = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return out if pd.notna(out) else None


def build_shadow(
    journal: pd.DataFrame,
    *,
    as_of: date,
    fetcher=fetch_metrics_history,
    existing: pd.DataFrame | None = None,
) -> pd.DataFrame:
    entry_days = pd.to_datetime(journal["entry_date"], errors="coerce")
    sources = journal[
        journal["phase_id"].eq(CONFIG.phase_id)
        & entry_days.ge(pd.Timestamp(EXPERIMENT_START))
    ].copy()
    rows: list[dict[str, object]] = []
    histories: dict[str, pd.DataFrame] = {}
    closed_rows: dict[tuple[str, str, str, str, str], dict[str, object]] = {}
    if existing is not None and not existing.empty:
        for _, prior in existing[existing["status"].eq("CLOSED")].iterrows():
            key = (
                str(prior.get("phase_id", "")),
                str(prior.get("source_order_date", "")),
                str(prior.get("entry_date", "")),
                str(prior.get("code", "")),
                str(prior.get("variant", "")),
            )
            closed_rows[key] = prior.reindex(SHADOW_COLUMNS).fillna("").to_dict()
    for _, source in sources.iterrows():
        entry_price = _number(source.get("entry_price"))
        try:
            entry_day = date.fromisoformat(str(source.get("entry_date", "")))
        except ValueError:
            continue
        if not entry_price or entry_price <= 0:
            continue
        ticker = str(source.get("ticker", "")).strip()
        if not ticker:
            continue
        for variant in EXIT_VARIANTS:
            key = (
                str(source.get("phase_id", "")),
                str(source.get("source_order_date", "")),
                entry_day.isoformat(),
                str(source.get("code", "")),
                variant.key,
            )
            if key in closed_rows:
                rows.append(closed_rows[key])
                continue
            if ticker not in histories:
                try:
                    histories[ticker] = fetcher(ticker)
                except Exception as error:  # 価格障害で他銘柄の実験を止めない
                    print(f"claude_shadow=price_error ticker={ticker} err={error}", flush=True)
                    histories[ticker] = pd.DataFrame()
            result = simulate_exit(
                histories[ticker],
                entry_date=entry_day,
                entry_price=entry_price,
                variant=variant,
                stop_loss_pct=CONFIG.stop_loss_pct,
                take_profit_pct=CONFIG.take_profit_pct,
                timeout_days=variant.timeout_business_days or CONFIG.timeout_days,
                roundtrip_cost_pct=0.30,
                as_of=as_of,
            )
            row = {
                "phase_id": source.get("phase_id", ""),
                "strategy_version": source.get("strategy_version", ""),
                "rule_hash": source.get("rule_hash", ""),
                "variant": variant.key,
                "variant_label": variant.label,
                "source_order_date": source.get("source_order_date", ""),
                "entry_date": entry_day.isoformat(),
                "code": source.get("code", ""),
                "ticker": ticker,
                "name": source.get("name", ""),
                "sector": source.get("sector", ""),
                "entry_price": f"{entry_price:.2f}",
                "shares": source.get("shares", ""),
                "roundtrip_cost_pct": "0.30",
                # 同じ価格日で再実行しても不要なcommitを作らないよう決定的にする。
                "updated_at_jst": f"{as_of.isoformat()}T23:59:59+09:00",
                **result,
            }
            rows.append(row)
    return pd.DataFrame(rows).reindex(columns=SHADOW_COLUMNS).fillna("")


def _profit_factor(returns: pd.Series) -> str:
    gains = returns[returns > 0].sum()
    losses = returns[returns < 0].sum()
    return f"{gains / abs(losses):.2f}" if losses < 0 else "データ待ち"


def write_report(shadow: pd.DataFrame, as_of: date) -> None:
    lines = [
        "# Claude 300万円運用 無料シャドー出口比較", "",
        "正本の注文・資金には影響しないペーパー比較です。Yahoo Financeの日足だけを使い、有料データは使いません。", "",
        f"- 更新基準日: {as_of.isoformat()}",
        f"- 対象: 現行の期で{EXPERIMENT_START.isoformat()}以降に実際に約定したClaude買い",
        "- 共通コスト: 往復0.30%を控除",
        "- 昇格条件: 各方式40決済・同一入口40組以上になるまで正本ルールへ反映しない",
        "- 日中損切り: 寄付が-5%以下なら寄付、そうでなければ日中安値が触れた時点で-5%約定と仮定",
        "- 同一日内の高値・安値の順序は推測せず、損切りを先に判定", "",
        "| 方式 | 対象 | 決済 | 勝率 | 平均(net) | PF | 最悪(net) | -10%超 | 利確 | 追随 | 時間切れ |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in EXIT_VARIANTS:
        part = shadow[shadow["variant"].eq(variant.key)] if not shadow.empty else shadow
        closed = part[part["status"].eq("CLOSED")] if not part.empty else part
        returns = pd.to_numeric(closed.get("net_return_pct"), errors="coerce").dropna()
        win_rate = f"{(returns > 0).mean() * 100:.1f}%" if len(returns) else "待ち"
        average = f"{returns.mean():+.2f}%" if len(returns) else "待ち"
        worst = f"{returns.min():+.2f}%" if len(returns) else "待ち"
        below_ten = int((pd.to_numeric(closed.get("gross_return_pct"), errors="coerce") < -10).sum()) if not closed.empty else 0
        exits = closed.get("exit_type", pd.Series(dtype=str)).astype(str)
        lines.append(
            f"| {variant.label} | {len(part)} | {len(closed)} | {win_rate} | {average} | "
            f"{_profit_factor(returns)} | {worst} | {below_ten} | "
            f"{int(exits.eq('TAKE_PROFIT').sum())} | {int(exits.eq('TRAILING_STOP').sum())} | "
            f"{int(exits.eq('TIMEOUT').sum())} |"
        )
    lines.extend(["", "## 明細", "", "| 方式 | 銘柄 | 入口 | 状態 | 出口 | 損益(net) | MFE | MAE |", "|---|---|---:|---|---|---:|---:|---:|"])
    for _, row in shadow.sort_values(["entry_date", "code", "variant"]).iterrows():
        lines.append(
            f"| {row['variant']} | {row['code']} {row['name']} | {row['entry_price']}円 | {row['status']} | "
            f"{row['exit_type'] or '-'} {row['exit_date'] or row['exit_signal_date'] or ''} | "
            f"{row['net_return_pct'] or '-'}% | {row['mfe_pct'] or '-'}% | {row['mae_pct'] or '-'}% |"
        )
    if shadow.empty:
        lines.append("| - | v2.1.0の約定待ち | - | - | - | - | - | - |")
    lines.append("")
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now(JST).date().isoformat())
    args = parser.parse_args()
    as_of = date.fromisoformat(args.date)
    journal = read_csv(ROOT / "data" / "claude_300man_journal.csv", JOURNAL_COLUMNS)
    existing = read_csv(SHADOW_PATH, SHADOW_COLUMNS)
    shadow = build_shadow(journal, as_of=as_of, existing=existing)
    SHADOW_PATH.parent.mkdir(parents=True, exist_ok=True)
    shadow.to_csv(SHADOW_PATH, index=False, encoding="utf-8-sig")
    write_report(shadow, as_of)
    print(
        f"claude_shadow as_of={as_of} source_entries="
        f"{shadow[['source_order_date', 'code']].drop_duplicates().shape[0] if not shadow.empty else 0} "
        f"rows={len(shadow)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
