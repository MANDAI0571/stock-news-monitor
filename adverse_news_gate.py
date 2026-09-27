"""300万円ペーパー運用の悪材料ゲート。

候補銘柄について、手動確認済みの除外一覧と直近Google News RSSを確認する。
通信・解析に失敗した場合は ``UNKNOWN`` を返し、発注側が安全側で見送れるようにする。
これは銘柄の安全性を保証するものではなく、公開情報の一次スクリーニングである。
"""

from __future__ import annotations

import csv
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_EXCLUSIONS = ROOT / "data" / "adverse_materials_watchlist.csv"

ADVERSE_TERMS = (
    "不正", "粉飾", "改ざん", "認証不正", "行政処分", "業務停止",
    "リコール", "自主回収", "情報漏洩", "サイバー攻撃", "不正アクセス",
    "下方修正", "赤字転落", "債務超過", "訴訟", "逮捕", "書類送検",
    "事故", "火災", "死亡", "食中毒", "品質問題",
)


@dataclass(frozen=True)
class NewsGateResult:
    status: str  # CLEAR / BLOCK / UNKNOWN
    reason: str = ""
    source_url: str = ""


def _code(value: object) -> str:
    text = str(value or "").strip()
    return text.split(".", 1)[0].removesuffix(".0")


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def static_exclusion(
    code: object,
    *,
    today: date | None = None,
    path: Path = DEFAULT_EXCLUSIONS,
) -> NewsGateResult | None:
    if not path.exists():
        return None
    today = today or date.today()
    target = _code(code)
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = csv.DictReader(handle)
            for row in rows:
                if _code(row.get("code")) != target:
                    continue
                review_after = _date(row.get("review_after", ""))
                if review_after is not None and review_after < today:
                    continue
                return NewsGateResult(
                    "BLOCK",
                    str(row.get("reason") or "確認済み悪材料あり").strip(),
                    str(row.get("source_url") or "").strip(),
                )
    except (OSError, csv.Error) as error:
        return NewsGateResult("UNKNOWN", f"除外一覧を確認できない: {type(error).__name__}")
    return None


def _name_key(name: str) -> str:
    cleaned = re.sub(r"[\s　株式会社・（）()ホールディングス]", "", str(name or ""))
    return cleaned[:5]


def live_news_gate(
    code: object,
    name: str,
    *,
    today: date | None = None,
    lookback_days: int = 45,
    timeout: int = 12,
) -> NewsGateResult:
    static = static_exclusion(code, today=today)
    if static is not None:
        return static

    today = today or date.today()
    query = f'"{name}" (不正 OR 事故 OR リコール OR 行政処分 OR 情報漏洩 OR 下方修正 OR 火災 OR 食中毒) when:{lookback_days}d'
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "ja", "gl": "JP", "ceid": "JP:ja"}
    )
    request = urllib.request.Request(url, headers={"User-Agent": "stock-news-monitor/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
        root = ET.fromstring(payload)
    except Exception as error:  # noqa: BLE001 - 失敗時は発注側が安全側で見送る
        return NewsGateResult("UNKNOWN", f"悪材料ニュースを確認できない: {type(error).__name__}", url)

    key = _name_key(name)
    cutoff = datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp() - lookback_days * 86400
    for item in root.findall(".//item"):
        title = str(item.findtext("title") or "").strip()
        link = str(item.findtext("link") or url).strip()
        if key and key not in _name_key(title) and _code(code) not in title:
            continue
        published = str(item.findtext("pubDate") or "").strip()
        if published:
            try:
                if parsedate_to_datetime(published).timestamp() < cutoff:
                    continue
            except (TypeError, ValueError, OverflowError):
                pass
        term = next((word for word in ADVERSE_TERMS if word in title), "")
        if term:
            return NewsGateResult("BLOCK", f"直近ニュースに『{term}』: {title}", link)
    return NewsGateResult("CLEAR", "直近の重大悪材料見出しなし", url)
