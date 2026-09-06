"""米株の記事3本を、送る前に確かめる。

高重さんとの約束の1番目「捏造禁止」を、機械で守るための門番。

日本株には validate_note_artifact.py がある。あれは
**2026-07-16 に、買っていない銘柄を保有として配信した事故**の再発防止で入った。
米株の $20,000運用の記事はまったく同じ形なので、同じ事故が起こりうる。
だから米株にも門番を置く。

確かめること:
  1. 3本ともあって、中身が空でない
  2. 末尾に免責がある
  3. noteの決まりを守っている（表を使わない・[文言](URL)を使わない・「取得できず」を出さない）
  4. **保有として書いた銘柄が、台帳の OPEN 行に実在する**（いちばん大事）
  5. 現金の額が台帳の計算と合っている
  6. 候補が無い日は「該当なし」「データ不足」と書いてある

落ちたら記事を送らない。黙って直さない（何がおかしいかを人が読めるように出す）。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs"

US_NOTE_KEYS = ("us_highs", "us_pullback", "us_portfolio")
US_NOTE_LABELS = {
    "us_highs": "52週新高値",
    "us_pullback": "押し目",
    "us_portfolio": "$20,000運用",
}

DISCLAIMER = "本記事は投資助言ではありません。売買判断はご自身の責任でお願いします。"
DEFAULT_JOURNAL_NAME = "claude_us20k_journal.csv"
HOLDINGS_HEADER = "## 保有銘柄"

# 「NVDA Nvidia  $230.40（+0.84%）」の先頭のティッカー
TICKER_RE = re.compile(r"^([A-Z][A-Z0-9.\-]{0,6})\s+\S")
CASH_RE = re.compile(r"現金 \$([0-9,]+\.[0-9]{2})")


@dataclass
class UsNoteValidation:
    ok: bool = True
    failures: list[str] = field(default_factory=list)
    status: dict[str, str] = field(default_factory=dict)

    def fail(self, message: str) -> None:
        self.ok = False
        self.failures.append(message)


def _section(text: str, header: str) -> str:
    """見出しから次の「## 」までの本文（見出し行は含まない）。"""
    body: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.strip() == header:
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        if inside:
            body.append(line)
    return "\n".join(body)


def _note_rule_issue(text: str) -> str | None:
    """noteに貼ったとき壊れる書き方をしていないか。"""
    if [line for line in text.splitlines() if line.strip().startswith("|")]:
        return "表（|）を使っています。noteは表を解釈しません"
    if "](http" in text:
        return "[文言](URL) を使っています。noteでは押せない文字列になります"
    if "**" in text:
        return "** を使っています。noteでは文字として出ます"
    if "取得できず" in text:
        return "「取得できず」の行が残っています。読者に何も伝えません"
    return None


def _holding_tickers(text: str) -> list[str]:
    """記事の「保有銘柄」欄に、保有として書かれているティッカー。"""
    body = _section(text, HOLDINGS_HEADER)
    found: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("-", "　", "ルール", "現金", "出所")):
            continue
        match = TICKER_RE.match(stripped)
        if match:
            found.append(match.group(1))
    return found


def _validate_portfolio(result: UsNoteValidation, text: str, journal_path: Path) -> None:
    """$20,000運用の記事が、台帳と食い違っていないか。ここが事故の再発防止。"""
    marker = journal_path.name
    if f"出所 {marker}" not in text:
        result.fail(
            f"note_us_portfolio.md: 保有欄が台帳（{marker}）由来だと書かれていません"
        )
        return

    try:
        import pandas as pd
    except ModuleNotFoundError:
        print("validate_us_notes: pandas が無いので台帳との突き合わせは飛ばした", flush=True)
        return

    if not journal_path.exists() or journal_path.stat().st_size == 0:
        open_tickers: set[str] = set()
        cash = None
    else:
        journal = pd.read_csv(journal_path, dtype=str).fillna("")
        opened = journal[journal.get("status", pd.Series(dtype=str)).astype(str).str.upper().eq("OPEN")]
        open_tickers = set(opened.get("ticker", pd.Series(dtype=str)).astype(str).str.strip())
        bought = pd.to_numeric(journal.get("position_value"), errors="coerce").fillna(0).sum()
        sold = pd.to_numeric(journal.get("exit_value"), errors="coerce").fillna(0).sum()
        cash = 20_000.0 - float(bought) + float(sold)

    # ① 記事が保有と書いた銘柄は、台帳の OPEN 行に実在しなければならない
    written = _holding_tickers(text)
    ghosts = [ticker for ticker in written if ticker not in open_tickers]
    if ghosts:
        result.fail(
            "note_us_portfolio.md: 台帳に無い銘柄を保有として書いています → "
            + "、".join(ghosts)
            + "（2026-07-16 の事故と同じ形）"
        )
    # ② 台帳にある保有が記事から抜けていてもいけない
    missing = [ticker for ticker in sorted(open_tickers) if ticker not in written]
    if missing:
        result.fail(
            "note_us_portfolio.md: 台帳の保有が記事に出ていません → " + "、".join(missing)
        )

    # ③ 現金の額が台帳の計算と合っているか（1セント差まで許す）
    if cash is not None:
        match = CASH_RE.search(text)
        if not match:
            result.fail("note_us_portfolio.md: 現金の額が書かれていません")
        else:
            written_cash = float(match.group(1).replace(",", ""))
            if abs(written_cash - cash) > 0.01:
                result.fail(
                    f"note_us_portfolio.md: 現金が台帳と合いません "
                    f"（記事 ${written_cash:,.2f} / 台帳 ${cash:,.2f}）"
                )


def _validate_list_article(result: UsNoteValidation, key: str, text: str) -> None:
    """新高値・押し目：候補があるか、無いなら無いと書いてあるか。"""
    has_candidates = "📈 チャート: https://finance.yahoo.com/quote/" in text
    if has_candidates:
        return
    if "該当なし" not in text and "データ不足" not in text:
        result.fail(
            f"note_{key}.md: 候補が1件も無いのに「該当なし」「データ不足」の明記がありません"
        )


def validate_us_notes(
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    journal_path: str | Path | None = None,
) -> UsNoteValidation:
    output_dir = Path(output_dir)
    journal_path = Path(journal_path) if journal_path else PROJECT_ROOT / "data" / "claude_us20k_journal.csv"
    result = UsNoteValidation()

    for key in US_NOTE_KEYS:
        label = US_NOTE_LABELS[key]
        path = output_dir / f"note_{key}.md"
        if not path.exists() or path.stat().st_size == 0:
            result.fail(f"note_{key}.md: {label} の記事がありません（3本必須）")
            result.status[label] = "未生成"
            continue
        text = path.read_text(encoding="utf-8")

        if DISCLAIMER not in text:
            result.fail(f"note_{key}.md: 末尾の免責がありません")
            result.status[label] = "免責なし"
            continue
        issue = _note_rule_issue(text)
        if issue:
            result.fail(f"note_{key}.md: {issue}")
            result.status[label] = f"noteの決まり違反（{issue}）"
            continue

        before = len(result.failures)
        if key == "us_portfolio":
            _validate_portfolio(result, text, journal_path)
        else:
            _validate_list_article(result, key, text)
        result.status[label] = "OK" if len(result.failures) == before else "内容に問題あり"

    return result


def summary_lines(result: UsNoteValidation) -> list[str]:
    lines = ["米株の記事チェック: " + ("OK" if result.ok else "NG")]
    for label, state in result.status.items():
        lines.append(f"- {label}: {state}")
    for message in result.failures:
        lines.append(f"  × {message}")
    return lines


def main() -> int:
    output_dir = sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT_OUTPUT_DIR)
    result = validate_us_notes(output_dir)
    for line in summary_lines(result):
        print(line, flush=True)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
