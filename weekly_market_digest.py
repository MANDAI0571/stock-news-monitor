from __future__ import annotations

import argparse
import csv
import html
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

from gmail_notify import DISCLAIMER, load_gmail_config, send_gmail
from jptime import jst_today


SECTOR_ETFS = {
    "食品": "1617.T", "エネルギー資源": "1618.T", "建設・資材": "1619.T",
    "素材・化学": "1620.T", "医薬品": "1621.T", "自動車・輸送機": "1622.T",
    "鉄鋼・非鉄": "1623.T", "機械": "1624.T", "電機・精密": "1625.T",
    "情報通信・サービスその他": "1626.T", "電力・ガス": "1627.T", "運輸・物流": "1628.T",
    "商社・卸売": "1629.T", "小売": "1630.T", "銀行": "1631.T",
    "金融（除く銀行）": "1632.T", "不動産": "1633.T",
}


@dataclass(frozen=True)
class NewsItem:
    title: str
    link: str
    source: str
    published: str


def fetch_sector_returns() -> pd.DataFrame:
    data = yf.download(list(SECTOR_ETFS.values()), period="1mo", auto_adjust=True, progress=False)
    close = data["Close"] if isinstance(data.columns, pd.MultiIndex) else data
    rows = []
    for sector, ticker in SECTOR_ETFS.items():
        series = pd.to_numeric(close[ticker], errors="coerce").dropna()
        if len(series) < 6:
            continue
        rows.append({"sector": sector, "ticker": ticker, "weekly_return_pct": (series.iloc[-1] / series.iloc[-6] - 1) * 100})
    return pd.DataFrame(rows).sort_values("weekly_return_pct", ascending=False).reset_index(drop=True)


def fetch_news(max_items: int = 5) -> list[NewsItem]:
    query = urllib.parse.quote("日本株 OR 東証 OR 日経平均 when:7d")
    url = f"https://news.google.com/rss/search?q={query}&hl=ja&gl=JP&ceid=JP:ja"
    request = urllib.request.Request(url, headers={"User-Agent": "stock-news-monitor/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        root = ET.fromstring(response.read())
    items = []
    for item in root.findall("./channel/item")[:max_items]:
        source = item.find("source")
        raw_date = item.findtext("pubDate", "")
        try:
            published = parsedate_to_datetime(raw_date).astimezone().strftime("%m/%d %H:%M")
        except (TypeError, ValueError):
            published = raw_date
        items.append(NewsItem(html.unescape(item.findtext("title", "")).strip(), item.findtext("link", "").strip(), (source.text or "").strip() if source is not None else "", published))
    return items


def build_digest(sectors: pd.DataFrame, news: list[NewsItem]) -> tuple[str, str]:
    today = jst_today()
    if sectors.empty:
        raise RuntimeError("セクター騰落率を取得できませんでした")
    top = sectors.head(3)
    bottom = sectors.tail(3).sort_values("weekly_return_pct")
    positive = int((sectors["weekly_return_pct"] > 0).sum())
    subject = f"【週刊日本株】強い{top.iloc[0]['sector']}、弱い{bottom.iloc[0]['sector']}｜{today:%m/%d}"
    lines = [
        "3分で分かる 今週の日本株", "",
        f"17セクター中、上昇は{positive}、下落は{len(sectors) - positive}。今週の主役と逆風を数字で確認します。", "",
        "■ 好調セクター TOP3",
    ]
    lines += [f"{i}. {row.sector}: {row.weekly_return_pct:+.2f}%" for i, row in enumerate(top.itertuples(), 1)]
    lines += ["", "■ 不調セクター BOTTOM3"]
    lines += [f"{i}. {row.sector}: {row.weekly_return_pct:+.2f}%" for i, row in enumerate(bottom.itertuples(), 1)]
    lines += ["", "■ 来週のチェックポイント", "・上位セクターの強さが続くか", "・下位セクターに反発や資金回帰が出るか", "・指数上昇が一部銘柄だけに偏っていないか"]
    if news:
        lines += ["", "■ 今週話題になった記事"]
        for item in news:
            attribution = " / ".join(part for part in (item.source, item.published) if part)
            lines += [f"・{item.title}", f"  {attribution}", f"  {item.link}"]
    lines += ["", "※騰落率はTOPIX-17業種別ETFの直近5営業日終値から算出。記事は過去7日の見出しとリンクです。", DISCLAIMER]
    return subject, "\n".join(lines)


def write_outputs(output_dir: Path, subject: str, body: str, sectors: pd.DataFrame) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "weekly_market_subject.txt").write_text(subject + "\n", encoding="utf-8")
    (output_dir / "weekly_market_digest.md").write_text(body + "\n", encoding="utf-8")
    sectors.to_csv(output_dir / "weekly_sector_returns.csv", index=False, quoting=csv.QUOTE_MINIMAL)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="outputs")
    parser.add_argument("--send-mail", action="store_true")
    args = parser.parse_args()
    sectors = fetch_sector_returns()
    try:
        news = fetch_news()
    except Exception as exc:
        print(f"weekly_news=omitted reason={type(exc).__name__}")
        news = []
    subject, body = build_digest(sectors, news)
    output_dir = Path(args.output_dir)
    write_outputs(output_dir, subject, body, sectors)
    if not args.send_mail:
        print("weekly_market_mail=skipped reason=dry_run")
        return
    config = load_gmail_config()
    if config is None:
        raise RuntimeError("Gmail secrets are missing")
    if send_gmail(subject, body, config, attachments=[output_dir / "weekly_sector_returns.csv"], allow_non_business_day=True):
        print(f"weekly_market_mail=sent to={config.mail_to}")


if __name__ == "__main__":
    main()
