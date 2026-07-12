from __future__ import annotations

from pathlib import Path
import json
from datetime import date, datetime

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OPENWORK_PATH = PROJECT_ROOT / "data" / "openwork_scores.csv"
DEFAULT_OPENWORK_META_PATH = PROJECT_ROOT / "data" / "openwork_scores_meta.json"


def load_openwork_scores(path: Path | str = DEFAULT_OPENWORK_PATH) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=["code", "openwork_score"])
    df = pd.read_csv(path, dtype={"code": str})
    required = {"code", "openwork_score"}
    if not required.issubset(df.columns):
        return pd.DataFrame(columns=["code", "openwork_score"])
    out = df[["code", "openwork_score"]].copy()
    out["code"] = out["code"].astype(str).str.strip().str.removesuffix(".0")
    out["openwork_score"] = pd.to_numeric(out["openwork_score"], errors="coerce")
    return out.drop_duplicates("code", keep="last")


def openwork_cache_status(
    path: Path | str = DEFAULT_OPENWORK_PATH,
    meta_path: Path | str = DEFAULT_OPENWORK_META_PATH,
    today: date | None = None,
) -> dict[str, object]:
    """OpenWorkキャッシュの状態を返す。通信は一切しない。

    月1回更新の運用を前提に、日次Note生成は保存済みCSVだけを読む。
    metaが無い場合もCSVがあれば「保存済みキャッシュあり」として扱う。
    """
    score_path = Path(path)
    meta = Path(meta_path)
    now = today or date.today()
    payload: dict[str, object] = {
        "path": str(score_path),
        "meta_path": str(meta),
        "exists": score_path.exists(),
        "updated_at": "",
        "update_month": "",
        "rows": 0,
        "monthly_cache": True,
        "source": "saved_cache_only",
        "note": "日次Note生成ではOpenWorkへアクセスせず、保存済みキャッシュだけを使用します。",
    }
    if score_path.exists():
        try:
            payload["rows"] = int(len(load_openwork_scores(score_path)))
        except Exception:
            payload["rows"] = 0
    if meta.exists():
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            payload["updated_at"] = str(data.get("updated_at") or "")
            payload["update_month"] = str(data.get("update_month") or "")[:7]
            if data.get("last_error"):
                payload["last_error"] = str(data.get("last_error"))
                payload["note"] = "OpenWork取得失敗時は前回キャッシュを保持しています。"
        except Exception:
            payload["note"] = "OpenWorkメタ情報は読めませんでしたが、日次Note生成はCSVキャッシュだけで続行します。"
    elif score_path.exists():
        try:
            updated = datetime.fromtimestamp(score_path.stat().st_mtime).date()
            payload["updated_at"] = updated.isoformat()
            payload["update_month"] = updated.strftime("%Y-%m")
        except OSError:
            pass
    update_month = str(payload.get("update_month") or "")
    payload["is_current_month"] = bool(update_month and update_month == now.strftime("%Y-%m"))
    return payload


def save_openwork_scores_cache(
    scores: pd.DataFrame | None,
    path: Path | str = DEFAULT_OPENWORK_PATH,
    meta_path: Path | str = DEFAULT_OPENWORK_META_PATH,
    *,
    error: str = "",
    today: date | None = None,
) -> dict[str, object]:
    """月次OpenWork更新用の保存処理。通信は呼び出し側で行う。

    取得失敗時(errorあり)は既存CSVを上書きせず、メタ情報にエラーだけを残す。
    これにより日次Note生成は前回値を保持したまま続行できる。
    """
    score_path = Path(path)
    meta = Path(meta_path)
    now = today or date.today()
    score_path.parent.mkdir(parents=True, exist_ok=True)
    meta.parent.mkdir(parents=True, exist_ok=True)
    if error:
        payload = openwork_cache_status(score_path, meta, today=now)
        payload["last_error"] = error
        payload["updated_at"] = str(payload.get("updated_at") or "")
        payload["update_month"] = str(payload.get("update_month") or "")
        meta.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return payload

    if scores is None:
        raise ValueError("scores is required when error is empty")
    df = scores.copy()
    if "code" not in df.columns or "openwork_score" not in df.columns:
        raise ValueError("scores must include code and openwork_score")
    out = df[["code", "openwork_score"]].copy()
    out["code"] = out["code"].astype(str).str.strip().str.removesuffix(".0")
    out["openwork_score"] = pd.to_numeric(out["openwork_score"], errors="coerce")
    out = out.dropna(subset=["code"]).drop_duplicates("code", keep="last")
    out.to_csv(score_path, index=False, encoding="utf-8")
    payload = {
        "path": str(score_path),
        "meta_path": str(meta),
        "exists": True,
        "updated_at": now.isoformat(),
        "update_month": now.strftime("%Y-%m"),
        "rows": int(len(out)),
        "monthly_cache": True,
        "source": "monthly_update",
        "last_error": "",
        "note": "OpenWork月次キャッシュを更新しました。",
    }
    meta.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def add_openwork_scores(screening: pd.DataFrame, path: Path | str = DEFAULT_OPENWORK_PATH) -> pd.DataFrame:
    out = screening.copy()
    if "code" not in out.columns:
        out["openwork_score"] = pd.NA
        return out
    scores = load_openwork_scores(path)
    if scores.empty:
        out["openwork_score"] = pd.NA
        return out
    out["code"] = out["code"].astype(str).str.strip().str.removesuffix(".0")
    return out.merge(scores, on="code", how="left")


def format_openwork_score(value, missing_label: str = "未取得") -> str:
    if pd.isna(value):
        return missing_label
    try:
        number = float(value)
        if not pd.notna(number):
            return missing_label
        return f"{number:.2f}"
    except Exception:
        return missing_label
