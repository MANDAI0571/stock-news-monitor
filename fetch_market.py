from __future__ import annotations

import argparse
import contextlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Any

from market_regime import fetch_regime


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = PROJECT_ROOT / "outputs" / "market_snapshot.json"
DEFAULT_TIMEOUT = 12

MARKET_INDICATORS: list[dict[str, Any]] = [
    {"key": "nikkei", "label": "日経平均", "short_label": "NIKKEI", "symbol": "^N225"},
    {"key": "topix", "label": "TOPIX", "short_label": "TOPIX", "symbol": "^TOPX", "fallback_symbols": ["1348.T", "1306.T", "1475.T"]},
    {"key": "vix", "label": "VIX", "short_label": "VIX", "symbol": "^VIX"},
    {"key": "sox", "label": "SOX", "short_label": "SOX", "symbol": "^SOX"},
    {"key": "usdjpy", "label": "ドル円", "short_label": "USDJPY", "symbol": "JPY=X"},
]


def _display_number(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "未取得"
    return f"{value:,.{digits}f}"


def _display_change(value: float | None) -> str:
    if value is None:
        return "未取得"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def _unavailable_indicator(meta: dict[str, str], error: str) -> dict[str, Any]:
    return {
        "key": meta["key"],
        "label": meta["label"],
        "short_label": meta["short_label"],
        "symbol": meta["symbol"],
        "source_symbol": meta["symbol"],
        "source_note": "",
        "status": "unavailable",
        "value": None,
        "change": None,
        "change_pct": None,
        "as_of": None,
        "display_value": "未取得",
        "display_change_pct": "未取得",
        "error": str(error),
    }


def _series_from_download(frame: Any, field: str) -> Any:
    columns = getattr(frame, "columns", [])
    if field in columns:
        return frame[field]
    if getattr(columns, "nlevels", 1) > 1:
        for column in columns:
            if field in column:
                return frame[column]
    raise KeyError(field)


def _non_empty_close_series(raw: Any) -> Any:
    if hasattr(raw, "columns"):
        for column in raw.columns:
            series = raw[column].dropna()
            if len(series) > 0:
                return series
        raise ValueError("close dataframe is empty")
    return raw.dropna()


def _fetch_indicator(meta: dict[str, Any], timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    last_error = ""
    for symbol in [meta["symbol"], *meta.get("fallback_symbols", [])]:
        try:
            return _fetch_indicator_symbol(meta, str(symbol), timeout)
        except Exception as exc:
            last_error = str(exc)
            continue
    return _unavailable_indicator(meta, last_error or "no symbols tried")


def _fetch_indicator_symbol(meta: dict[str, Any], symbol: str, timeout: int) -> dict[str, Any]:
    try:
        import yfinance as yf

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            frame = yf.download(
                symbol,
                period="7d",
                interval="1d",
                auto_adjust=False,
                progress=False,
                threads=False,
                timeout=timeout,
            )
        if frame is None or frame.empty:
            detail = " ".join(buffer.getvalue().split())
            raise ValueError(f"empty price frame {detail}".strip())
        close = _non_empty_close_series(_series_from_download(frame, "Close"))
        if len(close) < 1:
            raise ValueError("close series is empty")
        latest = float(close.iloc[-1])
        previous = float(close.iloc[-2]) if len(close) >= 2 else latest
        change = latest - previous
        change_pct = (change / previous * 100.0) if previous else None
        as_of = close.index[-1].date().isoformat() if hasattr(close.index[-1], "date") else str(close.index[-1])
        return {
            "key": meta["key"],
            "label": meta["label"],
            "short_label": meta["short_label"],
            "symbol": meta["symbol"],
            "source_symbol": symbol,
            "source_note": "" if symbol == meta["symbol"] else f"{symbol}代替",
            "status": "ok",
            "value": latest,
            "change": change,
            "change_pct": change_pct,
            "as_of": as_of,
            "display_value": _display_number(latest, 2),
            "display_change_pct": _display_change(change_pct),
            "error": "",
        }
    except Exception as exc:
        raise RuntimeError(f"{symbol}: {exc}") from exc


def _classify_indicator_regime(indicators: dict[str, dict[str, Any]]) -> dict[str, str]:
    available = [item for item in indicators.values() if item.get("status") == "ok"]
    if not available:
        return {"value": "CAUTION", "note": "市場指標が未取得のため、補助判定はCAUTION扱い"}

    warnings: list[str] = []
    risks: list[str] = []
    stops: list[str] = []

    vix = indicators.get("vix", {})
    if vix.get("value") is not None:
        vix_value = float(vix["value"])
        if vix_value >= 35:
            stops.append("VIX 35以上")
        elif vix_value >= 28:
            risks.append("VIX 28以上")
        elif vix_value >= 22:
            warnings.append("VIX 22以上")

    for key in ("nikkei", "topix", "sox"):
        item = indicators.get(key, {})
        label = str(item.get("label", key))
        change_pct = item.get("change_pct")
        if change_pct is None:
            continue
        pct = float(change_pct)
        if pct <= -3.0:
            stops.append(f"{label} -3%以上")
        elif pct <= -2.0:
            risks.append(f"{label} -2%以上")
        elif pct <= -1.0:
            warnings.append(f"{label} -1%以上")

    if stops or len(risks) >= 2:
        return {"value": "STOP", "note": " / ".join(stops + risks)}
    if risks:
        return {"value": "RISK", "note": " / ".join(risks)}
    if warnings:
        return {"value": "CAUTION", "note": " / ".join(warnings)}
    return {"value": "NORMAL", "note": "主要指標に強い悪化シグナルなし"}


def build_market_snapshot(
    output: str | Path = DEFAULT_OUTPUT,
    *,
    fetcher: Callable[[dict[str, Any], int], dict[str, Any]] | None = None,
    regime_fetcher: Callable[[], Any] = fetch_regime,
    timeout: int = DEFAULT_TIMEOUT,
) -> Path:
    """地合い判定をNote下書きArtifact用に保存する。

    市場指標が取得できない場合も止めず、Artifact側には「未取得」として残す。
    """
    regime = regime_fetcher()
    fetch_one = fetcher or _fetch_indicator
    indicators = {meta["key"]: fetch_one(meta, timeout) for meta in MARKET_INDICATORS}
    indicator_regime = _classify_indicator_regime(indicators)
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "regime": regime.value,
        "source": regime.source,
        "note": regime.note,
        "regime_source": regime.source,
        "regime_note": regime.note,
        "indicator_regime": indicator_regime["value"],
        "indicator_regime_note": indicator_regime["note"],
        "indicators": indicators,
    }
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"market_snapshot={path}")
    print(f"regime={regime.value} source={regime.source}")
    print(f"indicator_regime={indicator_regime['value']} note={indicator_regime['note']}")
    for item in indicators.values():
        print(
            f"market_indicator {item['short_label']}={item['display_value']} "
            f"change={item['display_change_pct']} status={item['status']} "
            f"source_symbol={item.get('source_symbol') or item.get('symbol')}"
        )
    if regime.note:
        print(regime.note)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build market snapshot JSON for note draft artifacts.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    build_market_snapshot(args.output, timeout=args.timeout)


if __name__ == "__main__":
    main()
