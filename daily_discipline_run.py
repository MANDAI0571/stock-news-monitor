from __future__ import annotations

import argparse
from pathlib import Path

from market_regime import fetch_regime
from paper_portfolio_discipline import build_discipline_portfolio
from run_screening import run_screening
from scanner.prices import timestamped_csv_path


PROJECT_ROOT = Path(__file__).resolve().parent
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="平日引け後のスクリーニング+規律版判定")
    parser.add_argument(
        "--markets",
        nargs="+",
        choices=["prime", "standard", "growth"],
        default=["prime", "standard", "growth"],
    )
    parser.add_argument("--limit", type=int, default=None, help="動作確認用。launchdでは指定しない")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "outputs"))
    parser.add_argument("--include-rejected", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    regime = fetch_regime()
    screening = run_screening(
        markets=tuple(args.markets),
        limit=args.limit,
        output_dir=args.output_dir,
        include_rejected=args.include_rejected,
    )
    screening_path = timestamped_csv_path(args.output_dir, prefix="screening_result")
    screening.to_csv(screening_path, index=False, encoding="utf-8-sig")

    discipline = build_discipline_portfolio(screening, regime)
    discipline_path = timestamped_csv_path(args.output_dir, prefix="discipline_portfolio")
    discipline.to_csv(discipline_path, index=False, encoding="utf-8-sig")

    print(f"regime={regime.value} source={regime.source}")
    if regime.note:
        print(regime.note)
    print(f"screening_csv={screening_path}")
    print(f"discipline_csv={discipline_path}")


if __name__ == "__main__":
    main()
