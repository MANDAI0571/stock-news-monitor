"""OpenWork画面を人が確認するための日別キューを作成・集計する。

外部サイトへのアクセスやスクレイピングは行わない。確認済みの画面または
スクリーンショットから転記したCSVだけを扱う。
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_TARGETS = PROJECT_ROOT / "outputs" / "openwork_collection_20260730" / "openwork_all_targets.csv"
DEFAULT_WORK_DIR = PROJECT_ROOT / "outputs" / "openwork_manual_daily"
SCORES_PATH = PROJECT_ROOT / "data" / "openwork_shared" / "openwork_scores.csv"

RATING_COLUMNS = [
    "overall",
    "treatment",
    "morale",
    "openness",
    "growth_20s",
    "longterm",
    "compliance",
    "evaluation",
]
ENTRY_COLUMNS = [
    "scheduled_date",
    "code",
    "name",
    *RATING_COLUMNS,
    "respondents",
    "fetched_at",
    "source_url",
    "status",
    "notes",
]


def _normalise_code(series: pd.Series) -> pd.Series:
    return (
        series.astype(str)
        .str.strip()
        .str.upper()
        .str.removesuffix(".T")
        .str.removesuffix(".0")
    )


def _is_blank(value: object) -> bool:
    return pd.isna(value) or str(value).strip().lower() in {"", "nan", "none", "null"}


def load_targets(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"対象一覧がありません: {path}")
    frame = pd.read_csv(path, dtype={"code": str})
    if not {"code", "name"}.issubset(frame.columns):
        raise ValueError("対象一覧には code と name が必要です")
    frame["code"] = _normalise_code(frame["code"])
    return frame.drop_duplicates("code", keep="last").reset_index(drop=True)


def load_completed_codes(path: Path = SCORES_PATH) -> set[str]:
    if not path.exists():
        return set()
    frame = pd.read_csv(path, dtype={"code": str})
    if "code" not in frame.columns:
        return set()
    score_col = "overall" if "overall" in frame.columns else "openwork_score"
    if score_col not in frame.columns:
        return set()
    valid = pd.to_numeric(frame[score_col], errors="coerce").between(1.0, 5.0)
    return set(_normalise_code(frame.loc[valid, "code"]))


def create_daily_queues(
    targets_path: Path,
    work_dir: Path,
    start: date,
    per_day: int,
) -> dict[str, int]:
    if per_day < 1:
        raise ValueError("--per-day は1以上にしてください")
    targets = load_targets(targets_path)
    completed = load_completed_codes()
    pending = targets[~targets["code"].isin(completed)].copy()
    work_dir.mkdir(parents=True, exist_ok=True)

    # 再生成前に入力済み内容を退避し、日付や件数を変えても転記値を失わない。
    saved_entries: dict[str, dict] = {}
    for old in work_dir.glob("queue_*.csv"):
        try:
            old_frame = pd.read_csv(old, dtype={"code": str})
            if "code" in old_frame.columns:
                old_frame["code"] = _normalise_code(old_frame["code"])
                for _, row in old_frame.iterrows():
                    saved_entries[str(row["code"])] = row.to_dict()
        except Exception:
            pass
        old.unlink()

    for offset, begin in enumerate(range(0, len(pending), per_day)):
        scheduled = start + timedelta(days=offset)
        batch = pending.iloc[begin : begin + per_day].copy()
        rows: list[dict] = []
        for _, target in batch.iterrows():
            code = str(target["code"])
            row = {column: "" for column in ENTRY_COLUMNS}
            row.update(saved_entries.get(code, {}))
            row["code"] = code
            row["name"] = target["name"]
            row["scheduled_date"] = scheduled.isoformat()
            if not str(row.get("status", "")).strip():
                row["status"] = "未確認"
            rows.append(row)
        out = pd.DataFrame(rows, columns=ENTRY_COLUMNS)
        out["scheduled_date"] = scheduled.isoformat()
        out.to_csv(
            work_dir / f"queue_{scheduled.isoformat()}.csv",
            index=False,
            encoding="utf-8-sig",
        )

    manifest = targets[["code", "name"]].copy()
    manifest["status"] = manifest["code"].map(
        lambda code: "確認済み" if code in completed else "未確認"
    )
    manifest.to_csv(work_dir / "progress.csv", index=False, encoding="utf-8-sig")
    return {
        "total": len(targets),
        "completed": len(completed & set(targets["code"])),
        "pending": len(pending),
        "days": (len(pending) + per_day - 1) // per_day,
    }


def validate_entry(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype={"code": str})
    missing = [column for column in ENTRY_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"必要な列がありません: {', '.join(missing)}")
    frame["code"] = _normalise_code(frame["code"])

    accepted: list[pd.Series] = []
    errors: list[str] = []
    for index, row in frame.iterrows():
        status = "" if _is_blank(row.get("status")) else str(row.get("status")).strip().lower()
        has_input = any(not _is_blank(row.get(column)) for column in RATING_COLUMNS)
        if status not in {"ok", "確認済み"} and not has_input:
            continue
        valid_ratings = 0
        for column in RATING_COLUMNS:
            value = row.get(column)
            if _is_blank(value):
                continue
            number = pd.to_numeric(value, errors="coerce")
            if pd.isna(number) or not 1.0 <= float(number) <= 5.0:
                errors.append(f"{index + 2}行目 {column}: 1.0〜5.0ではありません")
            else:
                valid_ratings += 1
        if valid_ratings == 0:
            errors.append(f"{index + 2}行目: 評価値がありません")
        respondents = row.get("respondents")
        if not _is_blank(respondents):
            number = pd.to_numeric(respondents, errors="coerce")
            if pd.isna(number) or int(number) != float(number) or int(number) < 1:
                errors.append(f"{index + 2}行目 respondents: 1以上の整数ではありません")
        fetched_at = "" if _is_blank(row.get("fetched_at")) else str(row.get("fetched_at")).strip()
        try:
            date.fromisoformat(fetched_at)
        except ValueError:
            errors.append(f"{index + 2}行目 fetched_at: YYYY-MM-DDではありません")
        if _is_blank(row.get("source_url")):
            errors.append(f"{index + 2}行目 source_url: 空欄です")
        accepted.append(row)
    if errors:
        raise ValueError("\n".join(errors))
    return pd.DataFrame(accepted, columns=ENTRY_COLUMNS)


def import_entries(queue_path: Path, scores_path: Path = SCORES_PATH) -> int:
    accepted = validate_entry(queue_path)
    if accepted.empty:
        return 0
    incoming = accepted[["code", "name", *RATING_COLUMNS, "respondents", "fetched_at", "source_url"]].copy()
    incoming["openwork_score"] = incoming["overall"]
    incoming["status"] = "ok"

    if scores_path.exists():
        current = pd.read_csv(scores_path, dtype={"code": str})
        current["code"] = _normalise_code(current["code"])
        merged = pd.concat([current, incoming], ignore_index=True)
    else:
        merged = incoming
    merged = merged.drop_duplicates("code", keep="last").sort_values("code")
    scores_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(scores_path, index=False, encoding="utf-8-sig")
    return len(incoming)


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenWork手動確認の日別キュー管理")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="未確認企業を日別CSVへ分割")
    create.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    create.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    create.add_argument("--start", type=date.fromisoformat, default=date.today())
    create.add_argument("--per-day", type=int, default=10)

    ingest = sub.add_parser("import", help="確認済みキューを検査してscoresへ反映")
    ingest.add_argument("queue", type=Path)
    ingest.add_argument("--scores", type=Path, default=SCORES_PATH)

    args = parser.parse_args()
    if args.command == "create":
        stats = create_daily_queues(args.targets, args.work_dir, args.start, args.per_day)
        print(f"openwork_manual_queue: {stats} -> {args.work_dir}")
    else:
        count = import_entries(args.queue, args.scores)
        print(f"openwork_manual_queue: imported={count} -> {args.scores}")


if __name__ == "__main__":
    main()
