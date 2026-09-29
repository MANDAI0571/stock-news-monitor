"""Codex/Claude 300万円ペーパー運用の比較表を正本CSVから作る。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from dual_300man_config import CONFIG


ROOT = Path(__file__).resolve().parent
INITIAL_CASH = CONFIG.initial_cash
JST = ZoneInfo("Asia/Tokyo")
OUTPUT = ROOT / "docs" / "dual_300man_comparison.md"


def _read(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str).fillna("")


def _sum(frame: pd.DataFrame, column: str) -> float:
    if frame.empty or column not in frame:
        return 0.0
    return float(pd.to_numeric(frame[column], errors="coerce").fillna(0).sum())


def _summary(account: str) -> dict[str, object]:
    journal = _read(ROOT / "data" / f"{account}_300man_journal.csv")
    orders = _read(ROOT / "data" / f"{account}_300man_orders.csv")
    if journal.empty:
        open_rows = closed_rows = journal
    else:
        status = journal["status"].astype(str).str.upper()
        open_rows = journal[status.eq("OPEN")]
        closed_rows = journal[status.eq("CLOSED")]
    bought = _sum(journal, "position_value")
    sold = _sum(journal, "exit_value")
    pending = 0 if orders.empty else int(orders["status"].astype(str).str.upper().eq("DECLARED").sum())
    return {
        "open": len(open_rows),
        "closed": len(closed_rows),
        "cash": INITIAL_CASH - bought + sold,
        "realized": _sum(closed_rows, "realized_pnl"),
        "pending": pending,
    }


def build() -> str:
    codex = _summary("codex")
    claude = _summary("claude")
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    return "\n".join([
        "# Codex vs Claude 300万円ペーパー運用",
        "",
        f"- 期: `{CONFIG.phase_id}`",
        f"- ルール版: `{CONFIG.strategy_version}` / 設定 `{CONFIG.rule_hash}`",
        f"- 再スタート日: {CONFIG.restart_date.isoformat()}",
        f"- 集計時刻: {now}",
        f"- 各口座の初期資金: {CONFIG.initial_cash:,}円",
        "",
        "| 口座 | 戦略 | 現金残 | 保有 | 実現損益 | 約定待ち |",
        "|---|---|---:|---:|---:|---:|",
        f"| Codex | 増収増益の押し目 | {codex['cash']:,.0f}円 | {codex['open']}銘柄 | {codex['realized']:+,.0f}円 | {codex['pending']}件 |",
        f"| Claude | Sランク上昇トレンド | {claude['cash']:,.0f}円 | {claude['open']}銘柄 | {claude['realized']:+,.0f}円 | {claude['pending']}件 |",
        "",
        "含み損益は終値更新用データが揃った日だけ別途評価し、取得原価を時価として扱いません。",
        "",
    ])


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(build(), encoding="utf-8")
    print(OUTPUT)


if __name__ == "__main__":
    main()
