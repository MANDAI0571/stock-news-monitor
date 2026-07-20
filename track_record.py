"""バックテスト博士: 掲載銘柄の実績トラッキング。

52週高値スクリーニング（screening_highs）でnoteに掲載した銘柄を
data/highs_track_record.csv に日次で蓄積し、「掲載後どうなったか」
（+1/+5/+20営業日の騰落率・勝率）を機械的に集計する。

原則:
- 事実のみ。価格が取得できない銘柄は集計から除外し、件数を明示する（捏造しない）。
- 蓄積データが無い/浅い場合は「データ不足」を明示する。
- 記録ファイルは data/ に置き、ワークフローがコミットして永続化する
  （outputs/ は実行ごとに消えるため）。
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "outputs"
RECORD_PATH = DATA_DIR / "highs_track_record.csv"
SUMMARY_PATH = OUTPUT_DIR / "track_record.json"

RECORD_COLUMNS = [
    "date",
    "code",
    "ticker",
    "name",
    "sector",
    "high_type",
    "first_break_60d",
    "inago_suspect",
    "tob_suspect",
    "entry_close",
]

# 実績を測る保有期間（営業日）
HORIZONS = (1, 5, 20)
# 集計対象は直近この営業日数分の掲載のみ（ファイル肥大と処理時間を抑える）
MAX_RECORD_DAYS = 120


def _latest_highs_csv(output_dir: Path) -> Path | None:
    files = sorted(output_dir.glob("screening_highs_*.csv"))
    return files[-1] if files else None


def load_record(record_path: Path = RECORD_PATH) -> pd.DataFrame:
    if not record_path.exists():
        return pd.DataFrame(columns=RECORD_COLUMNS)
    try:
        df = pd.read_csv(record_path, dtype={"code": str, "ticker": str})
    except Exception:
        return pd.DataFrame(columns=RECORD_COLUMNS)
    for column in RECORD_COLUMNS:
        if column not in df.columns:
            df[column] = ""
    return df[RECORD_COLUMNS]


def append_today_snapshot(
    output_dir: Path = OUTPUT_DIR,
    record_path: Path = RECORD_PATH,
    today: date | None = None,
) -> int:
    """本日の screening_highs を記録ファイルへ追記する。戻り値は追加件数。

    同一 (date, code) は追記しない（再実行しても重複しない）。
    """
    today = today or date.today()
    source = _latest_highs_csv(Path(output_dir))
    if source is None:
        print("track_record: screening_highs が無いため追記なし", flush=True)
        return 0
    try:
        highs = pd.read_csv(source, dtype={"code": str, "ticker": str})
    except Exception as exc:
        print(f"track_record: screening_highs 読込失敗 {exc}", flush=True)
        return 0
    if highs.empty or "code" not in highs.columns:
        print("track_record: screening_highs が空のため追記なし", flush=True)
        return 0

    record = load_record(record_path)
    existing = set(zip(record["date"].astype(str), record["code"].astype(str)))
    today_text = today.isoformat()
    added: list[dict[str, object]] = []
    for _, row in highs.iterrows():
        code = str(row.get("code", "")).strip()
        if not code or (today_text, code) in existing:
            continue
        added.append(
            {
                "date": today_text,
                "code": code,
                "ticker": str(row.get("ticker", f"{code}.T")),
                "name": str(row.get("name", "")),
                "sector": str(row.get("sector", "")),
                "high_type": str(row.get("high_type", "")),
                "first_break_60d": bool(str(row.get("first_break_60d", "")).strip().lower() in ("true", "1")),
                "inago_suspect": bool(str(row.get("inago_suspect", "")).strip().lower() in ("true", "1")),
                "tob_suspect": bool(str(row.get("tob_suspect", "")).strip().lower() in ("true", "1")),
                "entry_close": row.get("current_price", ""),
            }
        )
    if not added:
        print("track_record: 追加0件（既に記録済み）", flush=True)
        return 0

    frames = [df for df in (record, pd.DataFrame(added)) if not df.empty]
    merged = pd.concat(frames, ignore_index=True)
    # 古い記録は落とす（直近 MAX_RECORD_DAYS 日分だけ保持）
    merged["_d"] = pd.to_datetime(merged["date"], errors="coerce")
    cutoff = pd.Timestamp(today) - pd.Timedelta(days=MAX_RECORD_DAYS * 2)
    merged = merged[merged["_d"].isna() | (merged["_d"] >= cutoff)].drop(columns=["_d"])
    record_path.parent.mkdir(parents=True, exist_ok=True)
    merged[RECORD_COLUMNS].to_csv(record_path, index=False, encoding="utf-8-sig")
    print(f"track_record: {len(added)}件追記 -> {record_path}", flush=True)
    return len(added)


def _returns_for_entry(history: pd.DataFrame, entry_date: str) -> dict[int, float]:
    """掲載日の終値を基準に +1/+5/+20営業日の騰落率(%)を返す。データが無い地平は含めない。"""
    out: dict[int, float] = {}
    if history is None or history.empty or "Close" not in history.columns:
        return out
    close = history["Close"].astype(float)
    idx = pd.to_datetime(pd.Series(history.index)).dt.date.astype(str).tolist()
    if entry_date not in idx:
        return out
    pos = idx.index(entry_date)
    entry_close = float(close.iloc[pos])
    if entry_close <= 0:
        return out
    for horizon in HORIZONS:
        target = pos + horizon
        if target < len(close):
            out[horizon] = (float(close.iloc[target]) / entry_close - 1.0) * 100
    return out


def evaluate_track_record(
    record_path: Path = RECORD_PATH,
    summary_path: Path = SUMMARY_PATH,
    price_fetcher=None,
    today: date | None = None,
) -> dict:
    """記録済み銘柄の実績を集計して outputs/track_record.json に保存する。"""
    if price_fetcher is None:
        from scanner.prices import fetch_price_history
        price_fetcher = fetch_price_history

    today = today or date.today()
    record = load_record(record_path)
    summary: dict = {
        "as_of": today.isoformat(),
        "total_records": int(len(record)),
        "evaluated": 0,
        "price_missing": 0,
        "horizons": {},
        "first_break": {},
    }
    if record.empty:
        _write_summary(summary, summary_path)
        return summary

    # 当日掲載分は「その後」が無いので除外
    record = record[record["date"].astype(str) < today.isoformat()]
    history_cache: dict[str, pd.DataFrame] = {}
    rows: list[dict[str, object]] = []
    for _, entry in record.iterrows():
        ticker = str(entry.get("ticker", "")).strip()
        if not ticker:
            continue
        if ticker not in history_cache:
            try:
                history_cache[ticker] = price_fetcher(ticker)
            except Exception:
                history_cache[ticker] = pd.DataFrame()
        returns = _returns_for_entry(history_cache[ticker], str(entry["date"]))
        if not returns:
            summary["price_missing"] += 1
            continue
        rows.append(
            {
                "first_break": bool(entry.get("first_break_60d")) if not isinstance(entry.get("first_break_60d"), str)
                else str(entry.get("first_break_60d")).strip().lower() in ("true", "1"),
                **{f"ret_{h}d": returns.get(h) for h in HORIZONS},
            }
        )

    summary["evaluated"] = len(rows)
    frame = pd.DataFrame(rows)
    for horizon in HORIZONS:
        column = f"ret_{horizon}d"
        if frame.empty or column not in frame.columns:
            continue
        values = pd.to_numeric(frame[column], errors="coerce").dropna()
        if values.empty:
            continue
        summary["horizons"][str(horizon)] = _stats(values)
        if "first_break" in frame.columns:
            fb_values = pd.to_numeric(
                frame.loc[frame["first_break"].astype(bool), column], errors="coerce"
            ).dropna()
            if not fb_values.empty:
                summary["first_break"][str(horizon)] = _stats(fb_values)

    _write_summary(summary, summary_path)
    return summary


def _stats(values: pd.Series) -> dict:
    return {
        "n": int(len(values)),
        "win_rate_pct": round(float((values > 0).mean() * 100), 1),
        "avg_return_pct": round(float(values.mean()), 2),
        "best_pct": round(float(values.max()), 2),
        "worst_pct": round(float(values.min()), 2),
    }


def _write_summary(summary: dict, summary_path: Path) -> None:
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"track_record: summary -> {summary_path}", flush=True)


HORIZON_LABELS = {"1": "翌営業日", "5": "5営業日後", "20": "20営業日後"}


def build_track_record_lines(summary: dict | None) -> list[str]:
    """noteに載せる実績セクションの行を返す。データが無ければ「データ不足」を明記。"""
    lines = ["## 実績（過去に掲載した銘柄のその後）", ""]
    horizons = (summary or {}).get("horizons") or {}
    usable = {k: v for k, v in horizons.items() if int(v.get("n", 0)) > 0}
    if not summary or not usable:
        lines.append(
            "> データ不足：実績データは掲載記録の蓄積開始後、営業日を重ねると自動表示されます。"
            "毎日同じ基準で記録し、良い日も悪い日もそのまま載せます。"
        )
        lines.append("")
        return lines

    lines.append("掲載した銘柄がその後どう動いたかを、毎営業日同じ基準で機械集計しています（勝率＝掲載日終値より上昇した割合）。")
    lines.append("")
    lines.append("| 期間 | 銘柄数 | 勝率 | 平均騰落率 | 最大 | 最小 |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for key in ("1", "5", "20"):
        stats = usable.get(key)
        if not stats:
            continue
        lines.append(
            f"| {HORIZON_LABELS[key]} | {stats['n']} | {stats['win_rate_pct']}% | "
            f"{stats['avg_return_pct']:+}% | {stats['best_pct']:+}% | {stats['worst_pct']:+}% |"
        )
    first_break = (summary or {}).get("first_break") or {}
    fb5 = first_break.get("5")
    if fb5 and int(fb5.get("n", 0)) > 0:
        lines.append("")
        lines.append(
            f"うち**初回ブレイク**銘柄だけに絞ると、5営業日後の勝率は**{fb5['win_rate_pct']}%**"
            f"（{fb5['n']}銘柄・平均{fb5['avg_return_pct']:+}%）でした。"
        )
    missing = int((summary or {}).get("price_missing", 0))
    if missing > 0:
        lines.append("")
        lines.append(f"※ 価格データを取得できなかった{missing}件は集計から除外しています。")
    lines.append("")
    return lines


def load_track_record_summary(summary_path: Path = SUMMARY_PATH) -> dict | None:
    if not summary_path.exists():
        return None
    try:
        return json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="掲載銘柄の実績トラッキング（バックテスト博士）")
    parser.add_argument("--update", action="store_true", help="本日分を追記して実績を集計する")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    args = parser.parse_args()
    if args.update:
        added = append_today_snapshot(output_dir=Path(args.output_dir))
        summary = evaluate_track_record()
        print(
            f"track_record: added={added} total={summary['total_records']} "
            f"evaluated={summary['evaluated']} horizons={list(summary['horizons'].keys())}",
            flush=True,
        )


if __name__ == "__main__":
    main()
