from __future__ import annotations

import argparse
from pathlib import Path

from gmail_notify import maybe_send_gmail
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
    parser.add_argument("--send-gmail", action="store_true", help="GMAIL_USER/GMAIL_APP_PASSWORD/MAIL_TOでGmail通知を送る")
    parser.add_argument("--mail-max-rows", type=int, default=30, help="メール本文に表示するS/A/B候補の最大件数")
    parser.add_argument("--max-candidates", type=int, default=20, help="毎日の買い候補(S/A/B)の最大件数。0なら制限なし")
    parser.add_argument("--strict", action="store_true", help="Sランクにstrictゲートを適用する")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    regime = fetch_regime()
    screening = run_screening(
        markets=tuple(args.markets),
        limit=args.limit,
        output_dir=args.output_dir,
        include_rejected=args.include_rejected,
        max_candidates=None if args.max_candidates == 0 else args.max_candidates,
        strict=args.strict,
    )
    screening_path = timestamped_csv_path(args.output_dir, prefix="screening_result")
    screening.to_csv(screening_path, index=False, encoding="utf-8-sig")
    s_rank_path = timestamped_csv_path(args.output_dir, prefix="s_rank_candidates")
    write_s_rank_csv(screening, s_rank_path)

    discipline = build_discipline_portfolio(screening, regime)
    discipline_path = timestamped_csv_path(args.output_dir, prefix="discipline_portfolio")
    discipline.to_csv(discipline_path, index=False, encoding="utf-8-sig")

    print(f"regime={regime.value} source={regime.source}")
    if regime.note:
        print(regime.note)
    print(f"strict_mode={args.strict}")
    print_candidate_summary(screening)
    print_s_rank_details(screening)
    print(f"screening_csv={screening_path}")
    print(f"s_rank_csv={s_rank_path}")
    print(f"discipline_csv={discipline_path}")
    maybe_send_gmail(screening, regime.value, enabled=args.send_gmail, max_rows=args.mail_max_rows)


def print_candidate_summary(screening) -> None:
    if screening.empty or "rank" not in screening.columns:
        print("candidate_summary total=0 S=0 A=0 B=0")
        return
    ranks = screening["rank"].astype(str)
    total = int(ranks.isin(["S", "A", "B"]).sum())
    s_count = int(ranks.eq("S").sum())
    a_count = int(ranks.eq("A").sum())
    b_count = int(ranks.eq("B").sum())
    print(f"candidate_summary total={total} S={s_count} A={a_count} B={b_count}")
    if s_count == 0:
        print("s_rank_summary 本日はSランクなし")
    print("s_rank_gate ma25_rising=Falseまたはma75_rising=Falseの銘柄はSランクになりません")


def print_s_rank_details(screening) -> None:
    if screening.empty or "rank" not in screening.columns:
        return
    s_rank = screening[screening["rank"].astype(str).eq("S")]
    if s_rank.empty:
        return
    print("s_rank_details")
    for _, row in s_rank.iterrows():
        print(f"S {row.get('code', '')} {row.get('name', '')} score={row.get('score', '')} reason={row.get('reason', '')}")


def write_s_rank_csv(screening, path: Path) -> None:
    import pandas as pd

    columns = [
        "code",
        "name",
        "current",
        "score",
        "distance_to_52w_high_pct",
        "ma25_rising",
        "ma75_rising",
        "volume_ratio",
        "turnover_20d_avg",
        "reason",
    ]
    if screening.empty or "rank" not in screening.columns:
        pd.DataFrame(columns=columns).to_csv(path, index=False, encoding="utf-8-sig")
        return
    else:
        s_rank = screening[screening["rank"].astype(str).eq("S")]
    rows = []
    for _, row in s_rank.iterrows():
        rows.append(
            {
                "code": row.get("code", ""),
                "name": row.get("name", ""),
                "current": row.get("current_price", ""),
                "score": row.get("score", ""),
                "distance_to_52w_high_pct": row.get("dist_52w_high_pct", ""),
                "ma25_rising": row.get("ma25_rising", ""),
                "ma75_rising": row.get("ma75_rising", ""),
                "volume_ratio": row.get("volume_ratio_5d_20d", ""),
                "turnover_20d_avg": row.get("turnover_20d", ""),
                "reason": row.get("reason", ""),
            }
        )
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
