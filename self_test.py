from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from gmail_notify import DISCLAIMER, build_candidate_body, build_subject
from market_regime import Regime, fetch_regime
from paper_portfolio_discipline import build_discipline_portfolio
from pattern_learn import build_pattern_summary
from daily_note_mail import build_mail_body
from note_autosave import extract_body_fragment, is_saved_draft_url
from scanner.indicators import calculate_indicators
from scanner.scoring import meets_s_technical_gate, meets_strict_s_gate, score_stock
from trade_journal import load_journal, log_entry, log_exit


def main() -> None:
    _test_indicators_and_scoring()
    _test_discipline_normal_and_stop()
    _test_market_regime_local_fallback()
    _test_gmail_body()
    _test_note_autosave_and_mail_body()
    _test_journal_and_pattern_learning()
    print("self-test: OK")


def _test_indicators_and_scoring() -> None:
    dates = pd.bdate_range("2025-01-01", periods=260)
    close = pd.Series(range(1000, 1260), index=dates, dtype=float)
    close.iloc[-3] = close.iloc[-1]
    history = pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": 1_000_000,
        }
    )
    indicators = calculate_indicators(history)
    assert indicators is not None
    assert "ma75_gap_pct" in indicators
    assert indicators["days_since_52w_high"] == 0
    assert indicators["ma25_rising"] is True
    assert indicators["ma75_rising"] is True
    scored = score_stock(indicators, None, {"earnings_status": "確認済"}, name="東京エレクトロン", sector="電気機器")
    assert scored["score"] > 0
    assert "MA25上向き" in scored["reason"]
    assert "MA75上向き" in scored["reason"]
    assert "テーマ加点:半導体" in scored["reason"]

    weak_indicators = dict(indicators)
    weak_indicators["ma25_rising"] = False
    weak_indicators["ma75_rising"] = False
    gate_ok, gate_fail = meets_s_technical_gate(weak_indicators)
    assert gate_ok is False
    assert "25日線が上向きでない" in gate_fail
    assert "75日線が上向きでない" in gate_fail

    strict_ok, strict_fail = meets_strict_s_gate(indicators)
    assert strict_ok is False
    assert "出来高倍率1.5倍未満" in strict_fail


def _test_discipline_normal_and_stop() -> None:
    screening = pd.DataFrame(
        [
            {"code": "1111", "ticker": "1111.T", "name": "A", "rank": "S", "score": 100, "current_price": 1000, "dist_52w_high_pct": 1},
            {"code": "2222", "ticker": "2222.T", "name": "B", "rank": "A", "score": 80, "current_price": 1000, "dist_52w_high_pct": 2},
        ]
    )
    normal = build_discipline_portfolio(screening, Regime("NORMAL", "test"))
    assert len(normal) == 3
    assert normal.iloc[0]["action"] == "BUY"
    assert (normal["action"] == "CASH").sum() == 2
    stopped = build_discipline_portfolio(screening, Regime("STOP", "test"))
    assert len(stopped) == 3
    assert stopped["action"].eq("CASH").all()


def _test_market_regime_local_fallback() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "regime.txt"
        path.write_text("# comment\nNORMAL\n", encoding="utf-8")
        regime = fetch_regime(url="", fallback_path=path)
        assert regime.value == "NORMAL"
        assert regime.source == str(path)


def _test_gmail_body() -> None:
    screening = pd.DataFrame(
        [
            {
                "code": "7735",
                "name": "ＳＣＲＥＥＮホールディングス",
                "rank": "S",
                "score": 95,
                "current_price": 10000,
                "lot_value_100": 1_000_000,
                "dist_52w_high_pct": 1.2,
                "volume_ratio_5d_20d": 1.8,
                "reason": "テスト",
            }
        ]
    )
    assert build_subject(pd.Timestamp("2026-06-22").date()) == "【DUKEシステム】本日のS/A/B候補 2026-06-22"
    body = build_candidate_body(screening, "NORMAL")
    assert "■ Sランク" in body
    assert "7735" in body
    assert DISCLAIMER in body

    no_s_body = build_candidate_body(screening.assign(rank="A"), "NORMAL")
    assert "本日はSランクなし" in no_s_body

    many = pd.DataFrame(
        [
            {
                "code": f"{7000 + idx}",
                "name": f"候補{idx}",
                "rank": rank,
                "score": 100 - idx,
                "current_price": 1000,
                "lot_value_100": 100000,
                "dist_52w_high_pct": 1,
                "volume_ratio_5d_20d": 2,
                "reason": "テスト",
            }
            for idx, rank in enumerate(["S"] * 6 + ["A"] * 12 + ["B"] * 12)
        ]
    )
    limited = build_candidate_body(many, "NORMAL", max_rows=25)
    assert limited.count("  理由:") == 25
    assert "■ Sランク（6件中 最大5件表示）" in limited
    assert "■ Aランク（12件中 最大10件表示）" in limited
    assert "■ Bランク（12件中 最大10件表示）" in limited


def _test_journal_and_pattern_learning() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "journal.csv"
        trade_id = log_entry(
            {
                "code": "1111",
                "ticker": "1111.T",
                "name": "A",
                "current_price": 1000,
                "rank": "S",
                "score": 100,
                "volume_ratio_5d_20d": 2.0,
                "dist_52w_high_pct": 1.0,
                "days_since_52w_high": 2,
                "ma25_gap_pct": 3.0,
                "ma75_gap_pct": 5.0,
                "ma200_gap_pct": 8.0,
            },
            "NORMAL",
            100,
            path,
        )
        log_exit(trade_id, 1150, "take_profit", path=path)
        journal = load_journal(path)
        assert journal.iloc[0]["result"] == "WIN"
        summary = build_pattern_summary(journal)
        assert summary.iloc[0]["metric"] == "データ蓄積中"


def _test_note_autosave_and_mail_body() -> None:
    html = "<html><head><title>x</title></head><body><h1>タイトル</h1><p>本文</p></body></html>"
    assert extract_body_fragment(html) == "<h1>タイトル</h1><p>本文</p>"
    assert is_saved_draft_url("https://note.com/notes/abc123")
    assert not is_saved_draft_url("https://note.com/notes/new")

    screening = pd.DataFrame(
        [
            {
                "code": "1111",
                "name": "A",
                "rank": "S",
                "score": 100,
                "current_price": 1000,
                "dist_52w_high_pct": 1,
                "volume_ratio_5d_20d": 2,
                "reason": "テスト",
            }
        ]
    )
    discipline = pd.DataFrame(
        [
            {"slot": 1, "action": "BUY", "code": "1111", "name": "A", "rank": "S", "score": 100, "cash_reason": ""},
            {"slot": 2, "action": "CASH", "code": "", "name": "", "rank": "", "score": "", "cash_reason": "不足"},
        ]
    )
    body = build_mail_body(screening, discipline, "NORMAL", "https://note.com/notes/abc")
    assert "Note下書きURL: https://note.com/notes/abc" in body
    assert "note_daily.md" in body


if __name__ == "__main__":
    main()
