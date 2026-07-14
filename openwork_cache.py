"""OpenWork評価の月次キャッシュ（T-K 2026-07-12）。

【この仕組みの正確な位置づけ】
これは「OpenWorkの自動取得」ではない。
**手動で用意したOpenWorkデータ（data/openwork_scores.csv）を、月次でキャッシュ
（data/openwork_cache.csv）へ反映する**仕組みである。外部サイトへは一切アクセスしない。

月次workflow（openwork_monthly.yml、毎月1日 9:00 JST = 0:00 UTC）が行うこと:
1. data/openwork_scores.csv（手動整備）を読み込む
2. 新しい取得日・評価値を data/openwork_cache.csv へ反映する
3. 取得日から30日未満の既存正常値は上書きしない（30日ルール）
4. 空欄・異常値（評価は1.0〜5.0の範囲外など）で既存の正常値を消さない
5. 変更がある場合のみ commit する
6. 外部サイトへ自動アクセスしない（requests/urllib/ブラウザ等を使わない）

運用ルール（高重さん指定）:
- 日次のnote生成では **OpenWorkへ一切アクセスしない**。data/openwork_cache.csv だけを読む。
- 更新対象 = 直近60日で記事候補に出た企業 + 既存キャッシュのうち取得日から30日以上経過分。
- 取得失敗時（scores.csvに無い）は前回の正常値を残す。前回値が無ければ「取得できず」。
- 古いデータを使う場合も取得日を必ず表示する。回答者数が少ない場合は「参考値」。

重要（規約遵守・捏造禁止）:
- OpenWorkの利用規約・robots.txt・ログイン制限に反する自動取得は実装しない。
  CAPTCHA回避・ログイン回避・大量スクレイピングは禁止。
- 自動スクレイパーは意図的に存在しない（既知の制限として明記）。
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
CACHE_PATH = DATA_DIR / "openwork_cache.csv"
MANUAL_SCORES_PATH = DATA_DIR / "openwork_scores.csv"
RECORD_PATH = DATA_DIR / "highs_track_record.csv"

FRESH_DAYS = 30           # 30日未満は再取得しない
CANDIDATE_WINDOW_DAYS = 60  # 直近60日で候補に出た企業を対象母集団にする
FEW_RESPONDENTS = 30      # 回答者数がこれ未満なら「参考値」

CACHE_COLUMNS = [
    "code", "name",
    "overall", "treatment", "morale", "openness", "growth_20s",
    "longterm", "compliance", "evaluation", "respondents",
    "fetched_at", "source_url", "status",
]

# note表示用のラベル（取得できた項目だけ表示する）
ITEM_LABELS = [
    ("overall", "総合評価"),
    ("treatment", "待遇面の満足度"),
    ("morale", "社員の士気"),
    ("openness", "風通しの良さ"),
    ("growth_20s", "20代成長環境"),
    ("longterm", "人材の長期育成"),
    ("compliance", "法令順守意識"),
    ("evaluation", "人事評価の適正感"),
]


def load_cache(path: Path = CACHE_PATH) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=CACHE_COLUMNS)
    try:
        df = pd.read_csv(path, dtype={"code": str})
    except Exception:
        return pd.DataFrame(columns=CACHE_COLUMNS)
    for col in CACHE_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df["code"] = df["code"].astype(str).str.strip().str.upper().str.removesuffix(".T")
    return df[CACHE_COLUMNS].drop_duplicates("code", keep="last").reset_index(drop=True)


def _parse_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def needs_update(row: pd.Series | dict, today: date) -> bool:
    """取得日から30日未満なら再取得しない。取得日不明・30日以上経過は更新対象。"""
    fetched = _parse_date(row.get("fetched_at") if isinstance(row, dict) else row.get("fetched_at"))
    if fetched is None:
        return True
    return (today - fetched).days >= FRESH_DAYS


def _recent_candidate_codes(record_path: Path, today: date) -> list[str]:
    """直近60日で記事候補（highs実績記録）に出た銘柄コード。無ければ空。"""
    if not record_path.exists():
        return []
    try:
        record = pd.read_csv(record_path, dtype={"code": str})
    except Exception:
        return []
    if "code" not in record.columns:
        return []
    if "date" in record.columns:
        cutoff = (today - timedelta(days=CANDIDATE_WINDOW_DAYS)).isoformat()
        record = record[record["date"].astype(str) >= cutoff]
    codes = record["code"].astype(str).str.strip().str.upper().tolist()
    out: list[str] = []
    for code in codes:
        if code and code not in out:
            out.append(code)
    return out


_RATING_KEYS = ("overall", "treatment", "morale", "openness", "growth_20s", "longterm", "compliance", "evaluation")


def _valid_rating(value: object) -> float | None:
    """OpenWork評価値の妥当性検証。1.0〜5.0の数値のみ有効（異常値で既存値を消さない）。"""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or not (1.0 <= number <= 5.0):
        return None
    return number


def _valid_respondents(value: object) -> int | None:
    """回答者数の妥当性検証。1以上の整数のみ有効。"""
    try:
        number = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= 1_000_000 else None


def manual_source_fetcher(codes: list[str], scores_path: Path = MANUAL_SCORES_PATH, today: date | None = None) -> dict[str, dict]:
    """既定の取得元: 手動整備の data/openwork_scores.csv（正規手段・外部通信なし）。

    openwork_scores.csv に code 行があり、かつ評価値が妥当（1.0〜5.0）な場合のみ
    「取得成功」として返す。無い銘柄・空欄だけの行・異常値だけの行は返さない
    （＝取得できず扱い。前回の正常値があれば update_cache 側で保持される）。
    列は code, openwork_score(=overall) を最低限とし、CACHE_COLUMNS と同名列が
    あればそのまま取り込む（respondents, treatment など）。
    """
    today = today or date.today()
    if not scores_path.exists():
        return {}
    try:
        df = pd.read_csv(scores_path, dtype={"code": str})
    except Exception:
        return {}
    if "code" not in df.columns:
        return {}
    df["code"] = df["code"].astype(str).str.strip().str.upper().str.removesuffix(".0")
    wanted = set(codes)
    result: dict[str, dict] = {}
    for _, row in df.iterrows():
        code = str(row["code"])
        if code not in wanted:
            continue
        item: dict[str, object] = {"fetched_at": today.isoformat(), "status": "ok", "source_url": "data/openwork_scores.csv"}
        overall = _valid_rating(row.get("overall", row.get("openwork_score")))
        if overall is not None:
            item["overall"] = overall
        for col in _RATING_KEYS[1:]:
            rating = _valid_rating(row.get(col))
            if rating is not None:
                item[col] = rating
        respondents = _valid_respondents(row.get("respondents"))
        if respondents is not None:
            item["respondents"] = respondents
        name = str(row.get("name") or "").strip()
        if name and name.lower() not in ("nan", "none", "null"):
            item["name"] = name
        # 妥当な評価値が1つも無ければ「取得できず」扱い（既存の正常値を消さない）
        if any(key in item for key in _RATING_KEYS):
            result[code] = item
    return result


def update_cache(
    codes: list[str] | None = None,
    cache_path: Path = CACHE_PATH,
    record_path: Path = RECORD_PATH,
    fetcher=None,
    today: date | None = None,
) -> dict[str, int]:
    """月次更新。30日ルールで対象を絞り、取得失敗は前回値を保持する。

    fetcher(codes) -> {code: {列: 値, ...}} 。既定は manual_source_fetcher。
    戻り値: {"population": 対象母集団, "targets": 更新対象, "updated": 更新できた件数,
             "kept": 失敗して前回値を保持, "missing": 前回値も無く取得できず}
    """
    today = today or date.today()
    cache = load_cache(cache_path)
    if codes is None:
        codes = _recent_candidate_codes(record_path, today)
        # 既にキャッシュにある企業も母集団に含める（30日超なら更新対象）
        codes = list(dict.fromkeys(list(codes) + cache["code"].astype(str).tolist()))
    codes = [str(c).strip().upper().removesuffix(".T") for c in codes if str(c).strip()]

    by_code = {str(r["code"]): dict(r) for _, r in cache.iterrows()}
    targets = [c for c in codes if c not in by_code or needs_update(by_code[c], today)]

    if fetcher is None:
        fetcher = manual_source_fetcher
    try:
        fetched: dict[str, dict] = fetcher(targets) or {}
    except Exception:
        fetched = {}

    updated = kept = missing = 0
    for code in targets:
        item = fetched.get(code)
        if item:
            row = by_code.get(code, {"code": code})
            row.update({k: v for k, v in item.items() if k in CACHE_COLUMNS})
            row.setdefault("fetched_at", today.isoformat())
            row["status"] = "ok"
            by_code[code] = row
            updated += 1
        elif code in by_code and str(by_code[code].get("status", "")) == "ok":
            kept += 1  # 取得失敗 → 前回の正常値を残す（fetched_atも前回のまま＝古さが分かる）
        else:
            by_code[code] = {"code": code, "status": "unavailable", "fetched_at": today.isoformat()}
            missing += 1

    merged = pd.DataFrame(list(by_code.values()))
    for col in CACHE_COLUMNS:
        if col not in merged.columns:
            merged[col] = ""
    merged = merged[CACHE_COLUMNS]
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(cache_path, index=False, encoding="utf-8-sig")
    stats = {"population": len(codes), "targets": len(targets), "updated": updated, "kept": kept, "missing": missing}
    print(f"openwork_cache: {stats} -> {cache_path}")
    return stats


def _num_text(value: object, digits: int = 2) -> str | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return f"{number:.{digits}f}"


def build_openwork_lines(code: str, cache: pd.DataFrame | None, today: date | None = None) -> list[str]:
    """note用のOpenWork表示行。通信しない。取得できた項目だけ・取得日必須・捏造しない。"""
    today = today or date.today()
    if cache is None or cache.empty:
        return ["👔 OpenWork：取得できず"]
    row = cache[cache["code"].astype(str).str.upper() == str(code).upper()]
    if row.empty:
        return ["👔 OpenWork：取得できず"]
    r = row.iloc[0]
    if str(r.get("status", "")) != "ok":
        return ["👔 OpenWork：取得できず"]
    items: list[str] = []
    for key, label in ITEM_LABELS:
        text = _num_text(r.get(key))
        if text is not None:
            items.append(f"・{label}：{text}")
    if not items:
        return ["👔 OpenWork：取得できず"]
    lines = ["👔 OpenWork："] + items
    respondents = None
    try:
        respondents = int(float(r.get("respondents")))
    except (TypeError, ValueError):
        pass
    if respondents is not None:
        note = "（回答者数が少なく参考値）" if respondents < FEW_RESPONDENTS else ""
        lines.append(f"・回答者数：{respondents}人{note}")
    fetched = _parse_date(r.get("fetched_at"))
    if fetched is not None:
        stale = "（更新待ち）" if (today - fetched).days > FRESH_DAYS else ""
        lines.append(f"・取得日：{fetched.isoformat()}{stale}")
    else:
        lines.append("・取得日：不明（参考値）")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenWork月次キャッシュ更新")
    parser.add_argument("--update", action="store_true", help="キャッシュを更新する")
    args = parser.parse_args()
    if args.update:
        update_cache()
    else:
        cache = load_cache()
        print(f"openwork_cache: {len(cache)}件（{CACHE_PATH}）")


if __name__ == "__main__":
    main()
