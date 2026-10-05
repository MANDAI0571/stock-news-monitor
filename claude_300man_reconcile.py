"""Claude 300万円ペーパー口座の注文台帳と保有台帳を完全照合する。

異常を見つけても過去記録を自動修正しない。宣告workflowはこの結果を使い、
既存ポジションの出口宣告を続けたまま、新規BUYだけを安全側に停止する。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from dual_300man_config import (
    CONFIG,
    JOURNAL_COLUMNS,
    ORDER_COLUMNS,
    ROOT,
    order_compatibility,
)


JST = ZoneInfo("Asia/Tokyo")
ORDERS_PATH = ROOT / "data" / "claude_300man_orders.csv"
JOURNAL_PATH = ROOT / "data" / "claude_300man_journal.csv"
RESULT_PATH = ROOT / "data" / "claude_300man_reconciliation.csv"
REPORT_PATH = ROOT / "docs" / "claude_300man_reconciliation.md"
RESULT_COLUMNS = ["as_of", "severity", "check", "status", "subject", "details"]


@dataclass(frozen=True)
class Finding:
    severity: str
    check: str
    status: str
    subject: str
    details: str


def _read_raw(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str).fillna("")


def _number(value: object) -> float | None:
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return result if pd.notna(result) else None


def _shares(frame: pd.DataFrame, keys: list[str]) -> dict[tuple[str, ...], int]:
    if frame.empty:
        return {}
    work = frame.copy()
    work["_shares"] = pd.to_numeric(work.get("shares"), errors="coerce").fillna(0).astype(int)
    grouped = work.groupby(keys, dropna=False)["_shares"].sum()
    return {tuple(str(part) for part in key): int(value) for key, value in grouped.items()}


def _comparison_findings(
    *,
    check: str,
    left: dict[tuple[str, ...], int],
    right: dict[tuple[str, ...], int],
    left_label: str,
    right_label: str,
) -> list[Finding]:
    findings: list[Finding] = []
    mismatches = 0
    for key in sorted(set(left) | set(right)):
        left_value = left.get(key, 0)
        right_value = right.get(key, 0)
        if left_value == right_value:
            continue
        mismatches += 1
        findings.append(
            Finding(
                "CRITICAL",
                check,
                "FAIL",
                " / ".join(key),
                f"{left_label}={left_value} {right_label}={right_value}",
            )
        )
    if not mismatches:
        findings.append(Finding("INFO", check, "PASS", "all", f"{len(set(left) | set(right))} keys matched"))
    return findings


def reconcile(orders: pd.DataFrame, journal: pd.DataFrame, *, as_of: date) -> pd.DataFrame:
    findings: list[Finding] = []

    missing_orders = [column for column in ORDER_COLUMNS if column not in orders.columns]
    missing_journal = [column for column in JOURNAL_COLUMNS if column not in journal.columns]
    if missing_orders:
        findings.append(Finding("CRITICAL", "schema_orders", "FAIL", "orders", ",".join(missing_orders)))
    else:
        findings.append(Finding("INFO", "schema_orders", "PASS", "orders", f"{len(orders)} rows"))
    if missing_journal:
        findings.append(Finding("CRITICAL", "schema_journal", "FAIL", "journal", ",".join(missing_journal)))
    else:
        findings.append(Finding("INFO", "schema_journal", "PASS", "journal", f"{len(journal)} rows"))
    if missing_orders or missing_journal:
        return pd.DataFrame(
            [{"as_of": as_of.isoformat(), **finding.__dict__} for finding in findings]
        ).reindex(columns=RESULT_COLUMNS)

    orders = orders.reindex(columns=ORDER_COLUMNS).fillna("")
    journal = journal.reindex(columns=JOURNAL_COLUMNS).fillna("")
    phase_orders = orders[orders["phase_id"].eq(CONFIG.phase_id)].copy()
    phase_journal = journal[journal["phase_id"].eq(CONFIG.phase_id)].copy()

    order_key = ["phase_id", "decision_date", "execution_date", "side", "code"]
    duplicated_orders = phase_orders[phase_orders.duplicated(order_key, keep=False)]
    if duplicated_orders.empty:
        findings.append(Finding("INFO", "duplicate_orders", "PASS", "orders", "none"))
    else:
        for key, part in duplicated_orders.groupby(order_key, dropna=False):
            findings.append(Finding("CRITICAL", "duplicate_orders", "FAIL", " / ".join(key), f"rows={len(part)}"))

    journal_key = ["phase_id", "source_order_date", "entry_date", "code"]
    duplicated_journal = phase_journal[phase_journal.duplicated(journal_key, keep=False)]
    if duplicated_journal.empty:
        findings.append(Finding("INFO", "duplicate_journal_entries", "PASS", "journal", "none"))
    else:
        for key, part in duplicated_journal.groupby(journal_key, dropna=False):
            findings.append(Finding("CRITICAL", "duplicate_journal_entries", "FAIL", " / ".join(key), f"rows={len(part)}"))

    filled = phase_orders[phase_orders["status"].str.upper().eq("FILLED")]
    buy_orders = filled[filled["side"].str.upper().eq("BUY")]
    sell_orders = filled[filled["side"].str.upper().eq("SELL")]
    buy_map = _shares(buy_orders, ["code", "decision_date", "fill_date"])
    journal_buy_map = _shares(phase_journal, ["code", "source_order_date", "entry_date"])
    findings.extend(
        _comparison_findings(
            check="filled_buys_to_journal",
            left=buy_map,
            right=journal_buy_map,
            left_label="filled_buy_shares",
            right_label="journal_shares",
        )
    )

    closed = phase_journal[phase_journal["status"].str.upper().eq("CLOSED")]
    sell_map = _shares(sell_orders, ["code", "decision_date", "fill_date"])
    journal_sell_map = _shares(closed, ["code", "exit_order_date", "exit_date"])
    findings.extend(
        _comparison_findings(
            check="filled_sells_to_journal",
            left=sell_map,
            right=journal_sell_map,
            left_label="filled_sell_shares",
            right_label="closed_journal_shares",
        )
    )

    bought_by_code = _shares(buy_orders, ["code"])
    sold_by_code = _shares(sell_orders, ["code"])
    expected_open = {
        key: bought_by_code.get(key, 0) - sold_by_code.get(key, 0)
        for key in set(bought_by_code) | set(sold_by_code)
        if bought_by_code.get(key, 0) - sold_by_code.get(key, 0) != 0
    }
    open_rows = phase_journal[phase_journal["status"].str.upper().eq("OPEN")]
    actual_open = _shares(open_rows, ["code"])
    findings.extend(
        _comparison_findings(
            check="open_share_balance",
            left=expected_open,
            right=actual_open,
            left_label="buy_minus_sell",
            right_label="open_journal_shares",
        )
    )

    arithmetic_errors = 0
    tolerance = 1.0
    for index, row in phase_journal.iterrows():
        entry_price = _number(row.get("entry_price"))
        shares = _number(row.get("shares"))
        position_value = _number(row.get("position_value"))
        subject = f"row={index} code={row.get('code', '')} entry={row.get('entry_date', '')}"
        if entry_price is None or shares is None or position_value is None or shares <= 0:
            arithmetic_errors += 1
            findings.append(Finding("CRITICAL", "journal_arithmetic", "FAIL", subject, "invalid entry numeric field"))
            continue
        expected_position = entry_price * shares
        if abs(position_value - expected_position) > tolerance:
            arithmetic_errors += 1
            findings.append(Finding("CRITICAL", "journal_arithmetic", "FAIL", subject, f"position_value={position_value:.2f} expected={expected_position:.2f}"))
        if str(row.get("status", "")).upper() == "CLOSED":
            exit_price = _number(row.get("exit_price"))
            exit_value = _number(row.get("exit_value"))
            realized = _number(row.get("realized_pnl"))
            if exit_price is None or exit_value is None or realized is None:
                arithmetic_errors += 1
                findings.append(Finding("CRITICAL", "journal_arithmetic", "FAIL", subject, "closed row missing exit numeric field"))
                continue
            expected_exit = exit_price * shares
            expected_realized = expected_exit - position_value
            if abs(exit_value - expected_exit) > tolerance or abs(realized - expected_realized) > tolerance:
                arithmetic_errors += 1
                findings.append(
                    Finding(
                        "CRITICAL",
                        "journal_arithmetic",
                        "FAIL",
                        subject,
                        f"exit_value={exit_value:.2f}/{expected_exit:.2f} realized={realized:.2f}/{expected_realized:.2f}",
                    )
                )
    if not arithmetic_errors:
        findings.append(Finding("INFO", "journal_arithmetic", "PASS", "journal", f"{len(phase_journal)} rows matched"))

    bought_value = pd.to_numeric(phase_journal.get("position_value"), errors="coerce").fillna(0).sum()
    sold_value = pd.to_numeric(phase_journal.get("exit_value"), errors="coerce").fillna(0).sum()
    cash = float(CONFIG.initial_cash - bought_value + sold_value)
    findings.append(
        Finding(
            "INFO" if cash >= 0 else "CRITICAL",
            "cash_balance",
            "PASS" if cash >= 0 else "FAIL",
            "paper_cash",
            f"cash_jpy={cash:.0f}",
        )
    )

    open_codes = open_rows["code"].astype(str)
    if len(open_rows) <= CONFIG.max_positions and not open_codes.duplicated().any():
        findings.append(Finding("INFO", "open_position_limits", "PASS", "portfolio", f"open={len(open_rows)} max={CONFIG.max_positions}"))
    else:
        findings.append(
            Finding(
                "CRITICAL",
                "open_position_limits",
                "FAIL",
                "portfolio",
                f"open={len(open_rows)} max={CONFIG.max_positions} duplicate_codes={sorted(open_codes[open_codes.duplicated()].unique())}",
            )
        )

    pending = phase_orders[phase_orders["status"].str.upper().eq("DECLARED")]
    incompatible = 0
    for index, row in pending.iterrows():
        compatible, reason = order_compatibility(row)
        if compatible:
            continue
        incompatible += 1
        findings.append(Finding("CRITICAL", "pending_rule_compatibility", "FAIL", f"row={index} code={row.get('code', '')}", reason))
    if not incompatible:
        findings.append(Finding("INFO", "pending_rule_compatibility", "PASS", "pending_orders", f"count={len(pending)}"))

    return pd.DataFrame(
        [{"as_of": as_of.isoformat(), **finding.__dict__} for finding in findings]
    ).reindex(columns=RESULT_COLUMNS)


def write_report(result: pd.DataFrame, *, as_of: date) -> None:
    critical = result[(result["severity"] == "CRITICAL") & (result["status"] == "FAIL")]
    status = "CRITICAL" if len(critical) else "HEALTHY"
    lines = [
        "# Claude 300万円 台帳完全照合", "",
        "注文台帳・約定・保有台帳・現金を突合する安全ゲートです。記録は自動修正しません。", "",
        f"- 基準日: {as_of.isoformat()}",
        f"- 判定: **{status}**",
        f"- CRITICAL: {len(critical)}",
        f"- 新規買い: {'停止' if len(critical) else '許可'}",
        "- CRITICALでも既存ポジションの損切り・利確・タイムアウト宣告は継続", "",
        "| 重要度 | 検査 | 判定 | 対象 | 詳細 |",
        "|---|---|---|---|---|",
    ]
    for _, row in result.iterrows():
        details = str(row["details"]).replace("|", "/")
        subject = str(row["subject"]).replace("|", "/")
        lines.append(f"| {row['severity']} | {row['check']} | {row['status']} | {subject} | {details} |")
    lines.append("")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def _write_github_output(path: Path, result: pd.DataFrame) -> None:
    critical_count = int(((result["severity"] == "CRITICAL") & (result["status"] == "FAIL")).sum())
    with path.open("a", encoding="utf-8") as output:
        output.write(f"status={'critical' if critical_count else 'healthy'}\n")
        output.write(f"critical_count={critical_count}\n")
        output.write(f"allow_new_buys={'false' if critical_count else 'true'}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now(JST).date().isoformat())
    parser.add_argument("--github-output", default="")
    parser.add_argument("--fail-on-critical", action="store_true")
    args = parser.parse_args()
    as_of = date.fromisoformat(args.date)
    result = reconcile(_read_raw(ORDERS_PATH), _read_raw(JOURNAL_PATH), as_of=as_of)
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(RESULT_PATH, index=False, encoding="utf-8-sig")
    write_report(result, as_of=as_of)
    critical_count = int(((result["severity"] == "CRITICAL") & (result["status"] == "FAIL")).sum())
    if args.github_output:
        _write_github_output(Path(args.github_output), result)
    print(f"claude_300man_reconcile status={'critical' if critical_count else 'healthy'} critical={critical_count}")
    if args.fail_on_critical and critical_count:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
