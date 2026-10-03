from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Rule:
    family: str
    decision_scope: str
    rank_scope: str
    score_min: float
    volume_min: float
    dist52_max: float

    @property
    def rule_id(self) -> str:
        return (
            f"{self.family}__{self.decision_scope}__{self.rank_scope}"
            f"__s{self.score_min:g}__v{self.volume_min:g}__d{self.dist52_max:g}"
        )


FAMILY_GRIDS = {
    "primary_25ma": ([70, 75, 80, 85], [0.7, 0.9, 1.1], [7, 10, 15]),
    "tag_25ma": ([70, 75, 80, 85], [0.7, 0.9, 1.1], [7, 10, 15]),
    "primary_momentum": ([80, 85, 90], [0.9, 1.1, 1.3], [3, 5, 7]),
    "tag_momentum": ([80, 85, 90], [0.9, 1.1, 1.3], [3, 5, 7]),
    "primary_breakout": ([80, 85, 90], [0.9, 1.1, 1.3], [3, 5, 7]),
    "tag_breakout": ([80, 85, 90], [0.9, 1.1, 1.3], [3, 5, 7]),
    "primary_52w_pullback": ([75, 85, 95], [0.7, 1.0, 1.2], [5, 7, 10]),
    "tag_52w_pullback": ([75, 85, 95], [0.7, 1.0, 1.2], [5, 7, 10]),
    "tag_25ma_momentum": ([75, 80, 85], [0.7, 0.9, 1.1], [5, 7, 10]),
    "tag_25ma_52w_pullback": ([70, 75, 80], [0.7, 0.9, 1.1], [7, 10, 15]),
    "tag_momentum_52w_pullback": ([80, 85, 90], [0.9, 1.1, 1.3], [3, 5, 7]),
    "primary_multi": ([85, 95, 105], [1.3, 1.5, 2.0], [3, 5, 7]),
}


def build_rules() -> list[Rule]:
    rules: list[Rule] = []
    for family, (scores, volumes, distances) in FAMILY_GRIDS.items():
        for decision_scope in ("watch", "watch_buy"):
            for rank_scope in ("A", "AS"):
                for score in scores:
                    for volume in volumes:
                        for distance in distances:
                            rules.append(Rule(family, decision_scope, rank_scope, score, volume, distance))
    return rules


def _tags(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.upper().str.replace("、", ",", regex=False)


def _has_tag(tags: pd.Series, tag: str) -> pd.Series:
    return tags.str.split(",").apply(lambda values: tag in {value.strip() for value in values})


def filter_rule(detail: pd.DataFrame, rule: Rule) -> pd.DataFrame:
    decisions = {"WATCH"} if rule.decision_scope == "watch" else {"WATCH", "BUY"}
    ranks = {"A"} if rule.rank_scope == "A" else {"A", "S"}
    mask = detail["decision"].astype(str).str.upper().isin(decisions)
    mask &= detail["rank"].astype(str).str.upper().isin(ranks)
    mask &= pd.to_numeric(detail["score"], errors="coerce").ge(rule.score_min)
    mask &= pd.to_numeric(detail["volume_ratio_5d_20d"], errors="coerce").ge(rule.volume_min)
    mask &= pd.to_numeric(detail["dist_52w_high_pct"], errors="coerce").le(rule.dist52_max)

    primary = detail["screen_type"].fillna("").astype(str).str.upper()
    tags = _tags(detail["screen_tags"])
    family_masks = {
        "primary_25ma": primary.eq("25MA_PULLBACK"),
        "tag_25ma": _has_tag(tags, "25MA_PULLBACK"),
        "primary_momentum": primary.eq("52W_MOMENTUM"),
        "tag_momentum": _has_tag(tags, "52W_MOMENTUM"),
        "primary_breakout": primary.eq("52W_BREAKOUT"),
        "tag_breakout": _has_tag(tags, "52W_BREAKOUT"),
        "primary_52w_pullback": primary.eq("52W_PULLBACK"),
        "tag_52w_pullback": _has_tag(tags, "52W_PULLBACK"),
        "tag_25ma_momentum": _has_tag(tags, "25MA_PULLBACK") & _has_tag(tags, "52W_MOMENTUM"),
        "tag_25ma_52w_pullback": _has_tag(tags, "25MA_PULLBACK") & _has_tag(tags, "52W_PULLBACK"),
        "tag_momentum_52w_pullback": _has_tag(tags, "52W_MOMENTUM") & _has_tag(tags, "52W_PULLBACK"),
        "primary_multi": primary.eq("MULTI"),
    }
    return detail[mask & family_masks[rule.family]].copy()


def select_signals(candidates: pd.DataFrame, all_dates: list[str], cooldown_days: int, daily_limit: int) -> pd.DataFrame:
    if candidates.empty:
        return candidates.copy()
    date_position = {date: position for position, date in enumerate(all_dates)}
    last_selected: dict[str, int] = {}
    rows: list[pd.Series] = []
    for asof_date, day in candidates.groupby("asof_date", sort=True):
        day = day.sort_values(
            ["score", "confidence", "dist_52w_high_pct"],
            ascending=[False, False, True],
        )
        selected_today = 0
        current_position = date_position[str(asof_date)]
        for _, row in day.iterrows():
            code = str(row["code"])
            previous = last_selected.get(code)
            if previous is not None and current_position - previous <= cooldown_days:
                continue
            rows.append(row)
            last_selected[code] = current_position
            selected_today += 1
            if selected_today >= daily_limit:
                break
    return pd.DataFrame(rows).reset_index(drop=True) if rows else candidates.iloc[0:0].copy()


def _period_metrics(signals: pd.DataFrame, prefix: str) -> dict[str, object]:
    result: dict[str, object] = {f"{prefix}_count": int(len(signals))}
    for horizon in (5, 10):
        returns = pd.to_numeric(signals.get(f"return_{horizon}d_net_pct"), errors="coerce").dropna()
        gains = returns[returns > 0].sum()
        losses = returns[returns < 0].sum()
        result[f"{prefix}_win_{horizon}d_pct"] = round(float((returns > 0).mean() * 100), 2) if len(returns) else np.nan
        result[f"{prefix}_avg_{horizon}d_net_pct"] = round(float(returns.mean()), 4) if len(returns) else np.nan
        result[f"{prefix}_median_{horizon}d_net_pct"] = round(float(returns.median()), 4) if len(returns) else np.nan
        result[f"{prefix}_pf_{horizon}d"] = round(float(gains / abs(losses)), 4) if losses < 0 else np.nan
    return result


def summarize_rule(rule: Rule, signals: pd.DataFrame, split_date: str) -> dict[str, object]:
    early = signals[signals["asof_date"].astype(str) <= split_date]
    late = signals[signals["asof_date"].astype(str) > split_date]
    row: dict[str, object] = {"rule_id": rule.rule_id, **asdict(rule)}
    row["unique_symbols"] = int(signals["code"].astype(str).nunique()) if not signals.empty else 0
    row.update(_period_metrics(signals, "all"))
    row.update(_period_metrics(early, "early"))
    row.update(_period_metrics(late, "late"))
    row["avg_max_down_10d_pct"] = round(float(pd.to_numeric(signals.get("max_down_10d_pct"), errors="coerce").mean()), 4) if not signals.empty else np.nan
    row["worst_max_down_10d_pct"] = round(float(pd.to_numeric(signals.get("max_down_10d_pct"), errors="coerce").min()), 4) if not signals.empty else np.nan
    row["pass_5d"] = bool(
        row["all_count"] >= 20
        and row["unique_symbols"] >= 10
        and row["early_count"] >= 8
        and row["late_count"] >= 8
        and row["all_win_5d_pct"] >= 55
        and row["all_avg_5d_net_pct"] > 0
        and row["early_avg_5d_net_pct"] > 0
        and row["late_avg_5d_net_pct"] > 0
        and row["all_pf_5d"] > 1.1
    )
    row["pass_10d"] = bool(
        row["all_count"] >= 20
        and row["unique_symbols"] >= 10
        and row["early_count"] >= 8
        and row["late_count"] >= 8
        and row["all_win_10d_pct"] >= 55
        and row["all_avg_10d_net_pct"] > 0
        and row["early_avg_10d_net_pct"] > 0
        and row["late_avg_10d_net_pct"] > 0
        and row["all_pf_10d"] > 1.1
    )
    return row


def _markdown_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "合格ルールなし。"
    display = frame.fillna("").astype(str)
    headers = list(display.columns)
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for row in display.itertuples(index=False, name=None):
        values = [str(value).replace("|", "\\|").replace("\n", " ") for value in row]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def write_report(results: pd.DataFrame, output: Path, detail_path: Path, split_date: str) -> None:
    passing = results[results["pass_5d"] | results["pass_10d"]]
    top = passing.sort_values(
        ["pass_10d", "all_avg_10d_net_pct", "all_count"], ascending=[False, False, False]
    ).head(20)
    columns = [
        "rule_id", "all_count", "unique_symbols", "all_win_5d_pct", "all_avg_5d_net_pct",
        "all_win_10d_pct", "all_avg_10d_net_pct", "early_avg_10d_net_pct",
        "late_avg_10d_net_pct", "all_pf_10d", "worst_max_down_10d_pct", "pass_5d", "pass_10d",
    ]
    lines = [
        "# BUY3 組み合わせ研究",
        "",
        f"- 入力: {detail_path}",
        f"- 時系列分割日: {split_date}",
        "- 約定: 翌営業日寄り、往復スリッページ控除後",
        "- 同一銘柄5営業日クールダウン、1日最大3銘柄",
        "- 合格条件: 20件以上・10銘柄以上・前後半各8件以上・勝率55%以上・平均損益が前後半ともプラス・PF>1.1",
        f"- 検証ルール数: {len(results)} / 発見用合格: {len(passing)}",
        "",
        "## 上位合格ルール",
        "",
        _markdown_table(top[columns]),
        "",
        "注意: このレポートの合格は発見用標本だけの結果です。別銘柄標本で固定条件を再検証するまで本番採用しません。",
        "",
    ]
    output.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="BUY3詳細結果から事前定義した組み合わせを研究")
    parser.add_argument("--detail", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cooldown-days", type=int, default=5)
    parser.add_argument("--daily-limit", type=int, default=3)
    args = parser.parse_args()

    detail = pd.read_csv(args.detail, dtype={"code": str, "ticker": str})
    detail["asof_date"] = detail["asof_date"].astype(str)
    all_dates = sorted(detail["asof_date"].unique())
    if len(all_dates) < 2:
        raise SystemExit("評価日が不足しています")
    split_date = all_dates[(len(all_dates) - 1) // 2]
    rows: list[dict[str, object]] = []
    for rule in build_rules():
        candidates = filter_rule(detail, rule)
        signals = select_signals(candidates, all_dates, args.cooldown_days, args.daily_limit)
        rows.append(summarize_rule(rule, signals, split_date))
    results = pd.DataFrame(rows).sort_values(["pass_10d", "all_avg_10d_net_pct"], ascending=[False, False])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    results.to_csv(args.output, index=False, encoding="utf-8-sig")
    write_report(results, args.output, args.detail, split_date)
    passing = int((results["pass_5d"] | results["pass_10d"]).sum())
    print(f"buy3_combo_research rules={len(results)} passing={passing} split={split_date} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
