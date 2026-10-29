"""Claude 300万円運用の正本設定とCSVスキーマ（2026-10-16 に Codex 側を削除。名前の dual は当時のまま）。

ルール値を複数のスクリプトへ直書きすると、ローカルとGitHub Actionsで
別のルールが動いても気づけない。data/dual_300man_start.json を唯一の正本にし、
宣告ごとに期・ルール版・設定ハッシュ・Git commitを残す。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "data" / "dual_300man_start.json"

ORDER_COLUMNS = [
    "phase_id", "strategy_version", "rule_hash", "declaration_commit",
    "decision_date", "execution_date", "side", "code", "ticker",
    "name", "sector", "shares", "decision_price", "strategy", "exit_type",
    "decision_return_pct", "holding_business_days", "reason", "status",
    "fill_date", "fill_price", "fill_gap_pct", "status_note",
]

JOURNAL_COLUMNS = [
    "phase_id", "strategy_version", "rule_hash",
    "entry_date", "fill_time_jst", "status", "code", "ticker",
    "name", "sector", "entry_price", "shares", "position_value", "position_pct_initial",
    "planned_stop_risk_jpy", "planned_stop_risk_pct_initial",
    "strategy", "source_order_date", "entry_decision_price", "entry_gap_pct",
    "last_mark_date", "mark_price", "unrealized_pnl", "unrealized_return_pct",
    "peak_price", "peak_date", "mfe_pct", "mfe_peak_business_day",
    "trough_price", "trough_date", "mae_pct",
    "exit_date", "exit_price", "exit_value", "realized_pnl", "exit_return_pct",
    "exit_order_date", "exit_type", "exit_reason", "exit_decision_price",
    "exit_decision_return_pct", "exit_gap_pct", "holding_business_days",
    "profit_capture_pct", "post_exit_return_5_pct", "post_exit_return_10_pct",
    "post_exit_max_10_pct", "metrics_updated_at",
]


@dataclass(frozen=True)
class StrategyConfig:
    phase_id: str
    strategy_version: str
    restart_date: date
    initial_cash: int
    max_positions: int
    slot_yen: int
    stop_loss_pct: float
    take_profit_pct: float
    timeout_days: int
    max_entry_gap_pct: float
    circuit_reduce_pct: float | None
    circuit_stop_pct: float | None
    pending_expiry_business_days: int
    strategy_rules: Mapping[str, object]
    rule_hash: str
    # 3.0.0（2026-10-30）から。旧ルールの設定ファイルでは既定値（終値判定・翌寄付）のまま
    exit_execution: str = "next_open"
    regime_slots: Mapping[str, int] | None = None
    peak_drawdown_reduce_pct: float | None = None
    previous_rules_accepted: tuple[tuple[str, str], ...] = ()

    @property
    def standing_exit_orders(self) -> bool:
        """売りを逆指値・指値（日中）と期限の大引けで行うか。False なら終値で判定して翌寄付で売る（2.x）。"""
        return self.exit_execution == "standing_orders"


def _canonical_payload(raw: dict) -> bytes:
    tracked = {
        "schema_version": raw.get("schema_version"),
        "phase_id": raw.get("phase_id"),
        "strategy_version": raw.get("strategy_version"),
        "restart_date": raw.get("restart_date"),
        "mode": raw.get("mode"),
        "accounts": raw.get("accounts"),
        "execution": raw.get("execution"),
        "shared_risk_rules": raw.get("shared_risk_rules"),
        "strategy_rules": raw.get("strategy_rules"),
    }
    return json.dumps(tracked, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _optional_float(value: object) -> float | None:
    return None if value is None or str(value).strip() == "" else float(value)


def load_strategy_config(path: Path = CONFIG_PATH) -> StrategyConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    rules = raw["shared_risk_rules"]
    required_text = ("phase_id", "strategy_version", "restart_date")
    missing = [key for key in required_text if not str(raw.get(key, "")).strip()]
    if missing:
        raise ValueError(f"300man config missing: {', '.join(missing)}")
    if int(raw.get("schema_version", 0)) < 2:
        raise ValueError("300man config schema_version must be >= 2")
    config = StrategyConfig(
        phase_id=str(raw["phase_id"]),
        strategy_version=str(raw["strategy_version"]),
        restart_date=date.fromisoformat(str(raw["restart_date"])),
        initial_cash=int(next(iter(raw["accounts"].values()))["starting_capital_jpy"]),
        max_positions=int(rules["max_positions"]),
        slot_yen=int(rules["slot_capital_jpy"]),
        stop_loss_pct=float(rules["stop_loss_pct"]),
        take_profit_pct=float(rules["take_profit_pct"]),
        timeout_days=int(rules["timeout_business_days"]),
        max_entry_gap_pct=float(rules["max_entry_gap_pct"]),
        circuit_reduce_pct=_optional_float(rules.get("drawdown_reduce_pct")),
        circuit_stop_pct=_optional_float(rules.get("drawdown_stop_pct")),
        pending_expiry_business_days=int(rules.get("pending_expiry_business_days", 3)),
        strategy_rules=raw.get("strategy_rules", {}),
        rule_hash=hashlib.sha256(_canonical_payload(raw)).hexdigest()[:12],
        exit_execution=str(rules.get("exit_execution", "next_open")),
        regime_slots={str(k): int(v) for k, v in rules["regime_slots"].items()} if rules.get("regime_slots") else None,
        peak_drawdown_reduce_pct=_optional_float(rules.get("peak_drawdown_reduce_pct")),
        previous_rules_accepted=tuple(
            (str(item["strategy_version"]), str(item["rule_hash"]))
            for item in raw.get("previous_rules_accepted", [])
        ),
    )
    if config.exit_execution not in {"next_open", "standing_orders"}:
        raise ValueError(f"unknown exit_execution: {config.exit_execution}")
    if config.circuit_reduce_pct is None and config.peak_drawdown_reduce_pct is None:
        raise ValueError("300man config needs drawdown_reduce_pct or peak_drawdown_reduce_pct")
    if config.stop_loss_pct >= 0 or config.take_profit_pct <= 0:
        raise ValueError("stop_loss_pct must be negative and take_profit_pct positive")
    if config.slot_yen <= 0 or config.initial_cash <= 0 or config.max_positions <= 0:
        raise ValueError("capital and position settings must be positive")
    return config


CONFIG = load_strategy_config()


def git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def order_metadata() -> dict[str, str]:
    return {
        "phase_id": CONFIG.phase_id,
        "strategy_version": CONFIG.strategy_version,
        "rule_hash": CONFIG.rule_hash,
        "declaration_commit": git_commit(),
    }


def order_compatibility(row: Mapping[str, object]) -> tuple[bool, str]:
    if str(row.get("phase_id", "")).strip() != CONFIG.phase_id:
        return False, "PHASE_MISMATCH"
    version = str(row.get("strategy_version", "")).strip()
    rule_hash = str(row.get("rule_hash", "")).strip()
    # ルールを切り替えた日に宣言済みだった注文は、設定ファイルに書いた旧版だけ通す（2026-10-30 の 2.1.0 → 3.0.0）
    if (version, rule_hash) not in CONFIG.previous_rules_accepted:
        if version != CONFIG.strategy_version:
            return False, "STRATEGY_VERSION_MISMATCH"
        if rule_hash != CONFIG.rule_hash:
            return False, "RULE_HASH_MISMATCH"
    try:
        execution_date = date.fromisoformat(str(row.get("execution_date", "")))
    except ValueError:
        return False, "INVALID_EXECUTION_DATE"
    if execution_date < CONFIG.restart_date:
        return False, "BEFORE_RESTART"
    return True, ""


CRITICAL_RULE_PATHS = (
    "data/dual_300man_start.json",
    "dual_300man_config.py",
    "claude_300man_declare.py",
    "dual_300man_fill.py",
    "claude_300man_fill.py",
    "claude_300man_exits.py",
)


def production_rules_are_clean() -> tuple[bool, str]:
    """未pushのルールで注文だけを作る事故をローカル実行時に止める。"""
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        return True, ""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", *CRITICAL_RULE_PATHS],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as error:
        return False, f"git_status_failed:{error}"
    if result.returncode != 0:
        return False, "git_status_failed"
    dirty = result.stdout.strip()
    return (not dirty, dirty.replace("\n", "; "))
