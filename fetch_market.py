from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from market_regime import fetch_regime


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = PROJECT_ROOT / "outputs" / "market_snapshot.json"


def build_market_snapshot(output: str | Path = DEFAULT_OUTPUT) -> Path:
    """地合い判定をNote下書きArtifact用に保存する。

    fetch_regime() は取得失敗時にSTOPへ倒すため、このスクリプトも安全側で完了する。
    """
    regime = fetch_regime()
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "regime": regime.value,
        "source": regime.source,
        "note": regime.note,
    }
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"market_snapshot={path}")
    print(f"regime={regime.value} source={regime.source}")
    if regime.note:
        print(regime.note)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build market snapshot JSON for note draft artifacts.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    build_market_snapshot(args.output)


if __name__ == "__main__":
    main()
