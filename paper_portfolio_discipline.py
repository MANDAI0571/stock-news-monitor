from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pandas as pd

from market_regime import Regime, fetch_regime
from scanner.prices import timestamped_csv_path


PROJECT_ROOT = Path(__file__).resolve().parent
CAPITAL = 3_000_000
MAX_POSITIONS = 3
SLOT_CAPITAL = 1_000_000
STOP_LOSS_PCT = 0.07
TAKE_PROFIT_PCT = 0.15
TIMEOUT_BUSINESS_DAYS = 10


def build_discipline_portfolio(screening: pd.DataFrame, regime: Regime | str) -> pd.DataFrame:
    regime_value = regime.value if isinstance(regime, Regime) else str(regime).upper()
    max_positions = _max_positions_for_regime(regime_value)
    rows: list[dict[str, object]] = []

    if regime_value == "STOP":
        return _cash_rows(regime_value, "地合いSTOPのため新規買い停止", MAX_POSITIONS)
    if regime_value == "RISK":
        return _cash_rows(regime_value, "地合いRISKのため新規買い停止", MAX_POSITIONS)
    if screening.empty or "rank" not in screening.columns:
        return _cash_rows(regime_value, "Sランク不足のため現金保有", MAX_POSITIONS)

    candidates = screening[screening["rank"].astype(str).str.upper().eq("S")].copy()
    if not candidates.empty:
        candidates = candidates.sort_values(["score", "dist_52w_high_pct"], ascending=[False, True])

    for _, item in candidates.iterrows():
        if len(rows) >= max_positions:
            break
        price = float(item["current_price"])
        shares = _round_lot(SLOT_CAPITAL // price) if price > 0 else 0
        if shares <= 0:
            continue
        position_value = int(shares * price)
        entry_date = date.today()
        timeout_date = pd.bdate_range(start=pd.Timestamp(entry_date), periods=TIMEOUT_BUSINESS_DAYS + 1)[-1].date()
        rows.append(
            {
                "slot": len(rows) + 1,
                "action": "BUY",
                "regime": regime_value,
                "code": item.get("code", ""),
                "ticker": item.get("ticker", ""),
                "name": item.get("name", ""),
                "rank": item.get("rank", ""),
                "score": item.get("score", ""),
                "entry_price": round(price, 1),
                "shares": int(shares),
                "position_value": position_value,
                "stop_loss": round(price * (1 - STOP_LOSS_PCT), 1),
                "take_profit": round(price * (1 + TAKE_PROFIT_PCT), 1),
                "timeout_date": timeout_date.isoformat(),
                "rule": f"Sランクのみ / 地合い{regime_value}は最大{max_positions}銘柄 / 1枠100万円 / 損切7% / 利確15% / 10営業日タイムアウト",
                "cash_reason": "",
            }
        )

    if len(rows) < MAX_POSITIONS:
        reason = "Sランク不足のため現金保有"
        if len(rows) >= max_positions:
            reason = f"地合い{regime_value}のためBUY枠を{max_positions}銘柄に制限"
        rows.extend(_cash_rows(regime_value, reason, MAX_POSITIONS - len(rows), start_slot=len(rows) + 1).to_dict("records"))

    return pd.DataFrame(rows)


def _max_positions_for_regime(regime_value: str) -> int:
    if regime_value in {"STOP", "RISK"}:
        return 0
    if regime_value == "CAUTION":
        return 1
    return MAX_POSITIONS


def _cash_rows(regime: str, reason: str, count: int, start_slot: int = 1) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "slot": start_slot + i,
                "action": "CASH",
                "regime": regime,
                "code": "",
                "ticker": "",
                "name": "現金",
                "rank": "",
                "score": "",
                "entry_price": "",
                "shares": 0,
                "position_value": 0,
                "stop_loss": "",
                "take_profit": "",
                "timeout_date": "",
                "rule": "最大3銘柄 / Sランクのみ",
                "cash_reason": reason,
            }
            for i in range(count)
        ]
    )


def _round_lot(shares: float) -> int:
    return int(shares // 100 * 100)


def latest_screening_csv(output_dir: str | Path = PROJECT_ROOT / "outputs") -> Path:
    paths = sorted(Path(output_dir).glob("screening_result_*.csv"))
    if not paths:
        raise FileNotFoundError(f"screening_result_*.csv not found in {output_dir}")
    return paths[-1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="300万円規律版ペーパーポートフォリオ")
    parser.add_argument("--input", default=None, help="スクリーニングCSV。省略時はoutputs内の最新CSV")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "outputs"), help="CSV保存先")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = Path(args.input) if args.input else latest_screening_csv(args.output_dir)
    screening = pd.read_csv(input_path)
    regime = fetch_regime()
    portfolio = build_discipline_portfolio(screening, regime)
    path = timestamped_csv_path(args.output_dir, prefix="discipline_portfolio")
    portfolio.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"regime={regime.value} source={regime.source}")
    print(portfolio.to_string(index=False))
    print(f"保存しました: {path}")


if __name__ == "__main__":
    main()
