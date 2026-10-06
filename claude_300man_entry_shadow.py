"""Claudeの入口候補を無料日足だけで固定比較するシャドー実験。

本番注文、資金、保有上限には一切影響しない。各日の同じスクリーニングから
入口構成だけを変え、翌営業日寄付から現行出口までを前向きに記録する。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from claude_300man_declare import _claude_candidates, next_business_day
from claude_shadow_exits import EXIT_VARIANTS, normalize_history, simulate_exit
from dual_300man_config import CONFIG, ROOT
from scanner.prices import fetch_price_history


JST = ZoneInfo("Asia/Tokyo")
DATA_PATH = ROOT / "data" / "claude_300man_entry_shadow.csv"
REPORT_PATH = ROOT / "docs" / "claude_300man_entry_shadow.md"
CURRENT_EXIT = EXIT_VARIANTS[0]
ROUNDTRIP_COST_PCT = 0.30
ENTRY_COLUMNS = [
    "phase_id", "strategy_version", "rule_hash", "variant", "variant_label",
    "decision_date", "execution_date", "code", "ticker", "name", "sector",
    "candidate_strategy", "score", "screen_tags", "decision_price", "status",
    "entry_date", "entry_price", "entry_gap_pct", "exit_type", "exit_signal_date",
    "exit_date", "exit_price", "gross_return_pct", "net_return_pct",
    "holding_business_days", "mfe_pct", "mae_pct", "last_observed_date",
    "roundtrip_cost_pct", "updated_at_jst",
]


@dataclass(frozen=True)
class EntryVariant:
    key: str
    label: str


ENTRY_VARIANTS = (
    EntryVariant("current_combined_v1", "現行候補順: 押し目優先+順張り補完"),
    EntryVariant("pullback_only_v1", "押し目のみ"),
    EntryVariant("momentum_only_v1", "厳格順張りのみ"),
    EntryVariant("balanced_2p_1m_v1", "押し目2+順張り1"),
)


def _read(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=ENTRY_COLUMNS)
    return pd.read_csv(path, dtype=str).reindex(columns=ENTRY_COLUMNS).fillna("")


def _number(value: object) -> float | None:
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return result if pd.notna(result) else None


def _unique_sector_top(frame: pd.DataFrame, limit: int) -> pd.DataFrame:
    rows: list[pd.Series] = []
    used_codes: set[str] = set()
    used_sectors: set[str] = set()
    for _, row in frame.iterrows():
        code = str(row.get("code", "")).strip()
        sector = str(row.get("sector", "")).strip()
        if not code or code in used_codes or (sector and sector in used_sectors):
            continue
        rows.append(row)
        used_codes.add(code)
        if sector:
            used_sectors.add(sector)
        if len(rows) >= limit:
            break
    return pd.DataFrame(rows, columns=frame.columns)


def select_candidates(screening: pd.DataFrame) -> dict[str, pd.DataFrame]:
    ranked = _claude_candidates(screening)
    if ranked.empty:
        return {variant.key: ranked.copy() for variant in ENTRY_VARIANTS}
    pullback = ranked[ranked["_candidate_strategy"].eq("claude_pullback_combo_v1")]
    momentum = ranked[ranked["_candidate_strategy"].eq("claude_momentum")]
    balanced_source = pd.concat([pullback.head(2), momentum.head(1)], ignore_index=False)
    return {
        "current_combined_v1": _unique_sector_top(ranked, 3),
        "pullback_only_v1": _unique_sector_top(pullback, 3),
        "momentum_only_v1": _unique_sector_top(momentum, 3),
        "balanced_2p_1m_v1": _unique_sector_top(balanced_source, 3),
    }


def add_cohort(existing: pd.DataFrame, screening: pd.DataFrame, *, as_of: date) -> pd.DataFrame:
    if screening.empty:
        return existing
    if "data_date" in screening.columns:
        dates = screening["data_date"].astype(str).str.slice(0, 10)
        screening = screening[dates.eq(as_of.isoformat())].copy()
        if screening.empty:
            print(f"claude_entry_shadow=screening_stale target={as_of}")
            return existing
    selections = select_candidates(screening)
    execution = next_business_day(as_of).isoformat()
    labels = {variant.key: variant.label for variant in ENTRY_VARIANTS}
    prior_keys = {
        (str(row.get("variant", "")), str(row.get("decision_date", "")), str(row.get("code", "")))
        for _, row in existing.iterrows()
    }
    rows: list[dict[str, object]] = []
    for variant, selection in selections.items():
        for _, candidate in selection.iterrows():
            code = str(candidate.get("code", "")).strip()
            key = (variant, as_of.isoformat(), code)
            if not code or key in prior_keys:
                continue
            price = _number(candidate.get("current_price"))
            if not price or price <= 0:
                continue
            rows.append({
                "phase_id": CONFIG.phase_id,
                "strategy_version": CONFIG.strategy_version,
                "rule_hash": CONFIG.rule_hash,
                "variant": variant,
                "variant_label": labels[variant],
                "decision_date": as_of.isoformat(),
                "execution_date": execution,
                "code": code,
                "ticker": str(candidate.get("ticker") or f"{code}.T"),
                "name": candidate.get("name", ""),
                "sector": candidate.get("sector", ""),
                "candidate_strategy": candidate.get("_candidate_strategy", ""),
                "score": candidate.get("score", ""),
                "screen_tags": candidate.get("screen_tags", ""),
                "decision_price": f"{price:.2f}",
                "status": "DECLARED",
                "roundtrip_cost_pct": f"{ROUNDTRIP_COST_PCT:.2f}",
                "updated_at_jst": f"{as_of.isoformat()}T23:59:59+09:00",
            })
            prior_keys.add(key)
    if rows:
        existing = pd.concat([existing, pd.DataFrame(rows)], ignore_index=True).fillna("")
    return existing.reindex(columns=ENTRY_COLUMNS).fillna("")


def update_rows(
    existing: pd.DataFrame,
    *,
    as_of: date,
    fetcher=fetch_price_history,
) -> pd.DataFrame:
    if existing.empty:
        return existing.reindex(columns=ENTRY_COLUMNS)
    output = existing.copy().reindex(columns=ENTRY_COLUMNS).fillna("")
    histories: dict[str, pd.DataFrame] = {}
    terminal = {"CLOSED", "SKIPPED_ENTRY_GAP"}
    for index, row in output.iterrows():
        if str(row.get("status", "")).upper() in terminal:
            continue
        try:
            execution = date.fromisoformat(str(row.get("execution_date", "")))
        except ValueError:
            output.at[index, "status"] = "ENTRY_UNAVAILABLE"
            continue
        output.at[index, "updated_at_jst"] = f"{as_of.isoformat()}T23:59:59+09:00"
        if execution > as_of:
            output.at[index, "status"] = "DECLARED"
            continue
        ticker = str(row.get("ticker", "")).strip()
        if ticker not in histories:
            try:
                histories[ticker] = normalize_history(fetcher(ticker, period="18mo"))
            except TypeError:
                # テスト用・既存fetcherはperiodを受けない場合がある。
                histories[ticker] = normalize_history(fetcher(ticker))
            except Exception as error:  # 1銘柄の価格障害で全実験を止めない
                print(f"claude_entry_shadow=price_error ticker={ticker} err={error}")
                histories[ticker] = pd.DataFrame()
        history = histories[ticker]
        stamp = pd.Timestamp(execution)
        if history.empty or stamp not in history.index:
            output.at[index, "status"] = "ENTRY_PENDING"
            continue
        entry_price = float(history.loc[stamp, "Open"])
        decision_price = _number(row.get("decision_price"))
        if not decision_price or entry_price <= 0:
            output.at[index, "status"] = "ENTRY_UNAVAILABLE"
            continue
        gap = (entry_price / decision_price - 1) * 100
        output.at[index, "entry_date"] = execution.isoformat()
        output.at[index, "entry_price"] = f"{entry_price:.2f}"
        output.at[index, "entry_gap_pct"] = f"{gap:.2f}"
        if abs(gap) > CONFIG.max_entry_gap_pct:
            output.at[index, "status"] = "SKIPPED_ENTRY_GAP"
            continue
        result = simulate_exit(
            history,
            entry_date=execution,
            entry_price=entry_price,
            variant=CURRENT_EXIT,
            stop_loss_pct=CONFIG.stop_loss_pct,
            take_profit_pct=CONFIG.take_profit_pct,
            timeout_days=CONFIG.timeout_days,
            roundtrip_cost_pct=ROUNDTRIP_COST_PCT,
            as_of=as_of,
        )
        for key, value in result.items():
            if key in output.columns:
                output.at[index, key] = value
    return output.reindex(columns=ENTRY_COLUMNS).fillna("")


def _profit_factor(returns: pd.Series) -> str:
    gains = float(returns[returns > 0].sum())
    losses = float(returns[returns < 0].sum())
    return f"{gains / abs(losses):.2f}" if losses < 0 else "待ち"


def write_report(data: pd.DataFrame, *, as_of: date) -> None:
    lines = [
        "# Claude 300万円 無料入口シャドー比較", "",
        "同じ当日スクリーニングから入口構成だけを変える前向きペーパー比較です。正本の注文・資金・株数は変更しません。", "",
        f"- 更新基準日: {as_of.isoformat()}",
        "- 約定: 翌JPX営業日の寄付（本番同様、判定価格から±3%超のギャップは見送り）",
        "- 出口: 現行の終値-5% / +12% / 15営業日",
        f"- コスト: 往復{ROUNDTRIP_COST_PCT:.2f}%控除",
        "- 注意: ニュースゲート・実口座の保有枠は再現しない価格/候補比較",
        "- 採用条件: 40決済以上かつ十分な同日比較が揃ってから人がレビュー。自動採用しない", "",
        "| 入口方式 | 候補 | 約定 | 決済 | 勝率 | 平均(net) | PF | 最悪(net) | -10%超 | 利確 | 時間切れ |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in ENTRY_VARIANTS:
        part = data[data["variant"].eq(variant.key)] if not data.empty else data
        entered = part[part["entry_price"].astype(str).ne("")] if not part.empty else part
        closed = part[part["status"].eq("CLOSED")] if not part.empty else part
        returns = pd.to_numeric(closed.get("net_return_pct"), errors="coerce").dropna()
        exits = closed.get("exit_type", pd.Series(dtype=str)).astype(str)
        win_rate = f"{(returns > 0).mean() * 100:.1f}%" if len(returns) else "待ち"
        average = f"{returns.mean():+.2f}%" if len(returns) else "待ち"
        worst = f"{returns.min():+.2f}%" if len(returns) else "待ち"
        below_ten = int((pd.to_numeric(closed.get("gross_return_pct"), errors="coerce") < -10).sum()) if not closed.empty else 0
        lines.append(
            f"| {variant.label} | {len(part)} | {len(entered)} | {len(closed)} | {win_rate} | {average} | "
            f"{_profit_factor(returns)} | {worst} | {below_ten} | {int(exits.eq('TAKE_PROFIT').sum())} | {int(exits.eq('TIMEOUT').sum())} |"
        )
    lines.extend(["", "## 明細（直近60件）", "", "| 判定日 | 方式 | 銘柄 | 状態 | 入口 | 出口 | 損益(net) |", "|---|---|---|---|---:|---|---:|"])
    for _, row in data.sort_values(["decision_date", "variant", "code"]).tail(60).iterrows():
        lines.append(
            f"| {row['decision_date']} | {row['variant']} | {row['code']} {row['name']} | {row['status']} | "
            f"{row['entry_price'] or '-'}円 | {row['exit_type'] or '-'} {row['exit_date'] or row['exit_signal_date']} | "
            f"{row['net_return_pct'] or '-'}% |"
        )
    if data.empty:
        lines.append("| - | - | 新しい当日スクリーニング待ち | - | - | - | - |")
    lines.append("")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now(JST).date().isoformat())
    parser.add_argument("--screening", default="")
    args = parser.parse_args()
    as_of = date.fromisoformat(args.date)
    data = _read(DATA_PATH)
    if args.screening:
        path = Path(args.screening)
        if path.exists() and path.stat().st_size > 0:
            screening = pd.read_csv(path, dtype=str).fillna("")
            data = add_cohort(data, screening, as_of=as_of)
    data = update_rows(data, as_of=as_of)
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    data.to_csv(DATA_PATH, index=False, encoding="utf-8-sig")
    write_report(data, as_of=as_of)
    closed = int(data["status"].eq("CLOSED").sum()) if not data.empty else 0
    print(f"claude_entry_shadow as_of={as_of} rows={len(data)} closed={closed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
