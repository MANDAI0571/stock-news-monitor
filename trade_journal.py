from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_JOURNAL_PATH = PROJECT_ROOT / "outputs" / "trade_journal.csv"

JOURNAL_COLUMNS = [
    "trade_id",
    "entry_date",
    "exit_date",
    "code",
    "ticker",
    "name",
    "entry_price",
    "exit_price",
    "shares",
    "entry_rank",
    "entry_score",
    "entry_regime",
    "volume_ratio_5d_20d",
    "dist_52w_high_pct",
    "days_since_52w_high",
    "ma25_gap_pct",
    "ma75_gap_pct",
    "ma200_gap_pct",
    "pnl",
    "pnl_pct",
    "result",
    "exit_reason",
    "earnings_failure",
    "regime_failure",
]


def load_journal(path: str | Path = DEFAULT_JOURNAL_PATH) -> pd.DataFrame:
    journal_path = Path(path)
    if not journal_path.exists():
        return pd.DataFrame(columns=JOURNAL_COLUMNS, dtype=object)
    return pd.read_csv(journal_path).astype(object)


def save_journal(journal: pd.DataFrame, path: str | Path = DEFAULT_JOURNAL_PATH) -> Path:
    journal_path = Path(path)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal = journal.reindex(columns=JOURNAL_COLUMNS)
    journal.to_csv(journal_path, index=False, encoding="utf-8-sig")
    return journal_path


def log_entry(
    candidate: dict[str, object] | pd.Series,
    regime: str,
    shares: int,
    path: str | Path = DEFAULT_JOURNAL_PATH,
) -> str:
    item = dict(candidate)
    journal = load_journal(path)
    trade_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid4().hex[:8]
    row = {
        "trade_id": trade_id,
        "entry_date": datetime.now().date().isoformat(),
        "exit_date": "",
        "code": item.get("code", ""),
        "ticker": item.get("ticker", ""),
        "name": item.get("name", ""),
        "entry_price": item.get("entry_price", item.get("current_price", "")),
        "exit_price": "",
        "shares": shares,
        "entry_rank": item.get("rank", ""),
        "entry_score": item.get("score", ""),
        "entry_regime": regime,
        "volume_ratio_5d_20d": item.get("volume_ratio_5d_20d", ""),
        "dist_52w_high_pct": item.get("dist_52w_high_pct", ""),
        "days_since_52w_high": item.get("days_since_52w_high", ""),
        "ma25_gap_pct": item.get("ma25_gap_pct", ""),
        "ma75_gap_pct": item.get("ma75_gap_pct", ""),
        "ma200_gap_pct": item.get("ma200_gap_pct", ""),
        "pnl": "",
        "pnl_pct": "",
        "result": "",
        "exit_reason": "",
        "earnings_failure": False,
        "regime_failure": False,
    }
    new_row = pd.DataFrame([row], columns=JOURNAL_COLUMNS)
    journal = new_row if journal.empty else pd.concat([journal, new_row], ignore_index=True)
    save_journal(journal, path)
    return trade_id


def log_exit(
    trade_id: str,
    exit_price: float,
    exit_reason: str,
    earnings_failure: bool = False,
    regime_failure: bool = False,
    path: str | Path = DEFAULT_JOURNAL_PATH,
) -> None:
    journal = load_journal(path).astype(object)
    matched = journal["trade_id"].astype(str).eq(str(trade_id))
    if not matched.any():
        raise ValueError(f"trade_id not found: {trade_id}")

    idx = journal[matched].index[-1]
    entry_price = float(journal.at[idx, "entry_price"])
    shares = int(journal.at[idx, "shares"])
    pnl = (float(exit_price) - entry_price) * shares
    pnl_pct = (float(exit_price) - entry_price) / entry_price * 100 if entry_price else 0

    journal.loc[idx, "exit_date"] = datetime.now().date().isoformat()
    journal.loc[idx, "exit_price"] = round(float(exit_price), 2)
    journal.loc[idx, "pnl"] = round(pnl, 0)
    journal.loc[idx, "pnl_pct"] = round(pnl_pct, 2)
    journal.loc[idx, "result"] = "WIN" if pnl > 0 else "LOSS"
    journal.loc[idx, "exit_reason"] = exit_reason
    journal.loc[idx, "earnings_failure"] = bool(earnings_failure)
    journal.loc[idx, "regime_failure"] = bool(regime_failure)
    save_journal(journal, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="売買記録")
    sub = parser.add_subparsers(dest="command", required=True)
    entry = sub.add_parser("entry")
    entry.add_argument("--csv", required=True, help="エントリー候補CSV")
    entry.add_argument("--row", type=int, default=0, help="CSV内の行番号")
    entry.add_argument("--regime", required=True)
    entry.add_argument("--shares", type=int, required=True)
    entry.add_argument("--journal", default=str(DEFAULT_JOURNAL_PATH))
    exit_cmd = sub.add_parser("exit")
    exit_cmd.add_argument("--trade-id", required=True)
    exit_cmd.add_argument("--exit-price", type=float, required=True)
    exit_cmd.add_argument("--exit-reason", required=True)
    exit_cmd.add_argument("--earnings-failure", action="store_true")
    exit_cmd.add_argument("--regime-failure", action="store_true")
    exit_cmd.add_argument("--journal", default=str(DEFAULT_JOURNAL_PATH))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "entry":
        source = pd.read_csv(args.csv)
        trade_id = log_entry(source.iloc[args.row], args.regime, args.shares, args.journal)
        print(f"entry logged: {trade_id}")
    else:
        log_exit(args.trade_id, args.exit_price, args.exit_reason, args.earnings_failure, args.regime_failure, args.journal)
        print(f"exit logged: {args.trade_id}")


if __name__ == "__main__":
    main()
