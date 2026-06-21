from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


PROJECT_ROOT = Path(__file__).resolve().parent
REGIME_TXT_URL = os.environ.get(
    "REGIME_TXT_URL",
    "https://raw.githubusercontent.com/MANDAI0571/stock-news-monitor/main/regime.txt",
).strip()
REGIME_RAW_URL = REGIME_TXT_URL
REGIME_TXT_LOCAL = Path(os.environ.get("REGIME_TXT_LOCAL", str(PROJECT_ROOT / "regime.txt")))
VALID_REGIMES = ("NORMAL", "CAUTION", "RISK", "STOP")


@dataclass(frozen=True)
class Regime:
    value: str
    source: str
    note: str = ""

    @property
    def stop_new_buys(self) -> bool:
        return self.value == "STOP"


def fetch_regime(
    url: str = REGIME_TXT_URL,
    timeout: int = 10,
    fallback_path: str | Path | None = REGIME_TXT_LOCAL,
) -> Regime:
    try:
        if not url:
            raise URLError("REGIME_TXT_URL is empty")
        with urlopen(url, timeout=timeout) as response:
            text = response.read().decode("utf-8").strip()
        return Regime(_parse_regime(text), url)
    except (OSError, URLError, TimeoutError, ValueError) as exc:
        if fallback_path:
            path = Path(fallback_path)
            if path.exists():
                try:
                    return Regime(_parse_regime(path.read_text(encoding="utf-8").strip()), str(path), f"raw取得失敗: {exc}")
                except ValueError:
                    pass
        return Regime("STOP", "fallback", f"raw取得失敗のため安全側でSTOP: {exc}")


def _parse_regime(text: str) -> str:
    for line in text.splitlines():
        value = line.split("#", 1)[0].strip().upper()
        if not value:
            continue
        if value not in VALID_REGIMES:
            raise ValueError(f"invalid regime: {value}")
        return value
    raise ValueError("regime is empty")


if __name__ == "__main__":
    print(f"REGIME_TXT_URL={REGIME_TXT_URL}")
    print(f"REGIME_TXT_LOCAL={REGIME_TXT_LOCAL}")
    regime = fetch_regime()
    print(f"regime={regime.value} source={regime.source}")
    if regime.note:
        print(regime.note)
