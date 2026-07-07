from __future__ import annotations

import os
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from gmail_notify import DISCLAIMER, build_candidate_body, build_subject
from fetch_market import build_market_snapshot
from market_regime import Regime, fetch_regime
from paper_portfolio_discipline import build_discipline_portfolio
from pattern_learn import build_pattern_summary
from daily_note_mail import build_mail_body
from note_autosave import extract_body_fragment, is_saved_draft_url, load_storage_state
from scanner.highs import build_high_sections_markdown, classify_high_profile, detect_duke_old_high_support, detect_previous_52w_high_line_retest, detect_swing_high_break
from scanner.indicators import calculate_indicators
from scanner.openwork import add_openwork_scores, format_openwork_score
from scanner.scoring import meets_s_technical_gate, meets_strict_s_gate, score_stock
from scanner.universe import JPX_CACHE_PATH, UniverseConfig, load_jpx_listed, normalize_jpx_listed
from trade_journal import load_journal, log_entry, log_exit
from validate_note_artifact import validate_artifact
import trade_verification as tv


def main() -> None:
    _test_indicators_and_scoring()
    _test_discipline_normal_and_stop()
    _test_market_regime_local_fallback()
    _test_market_snapshot_artifact_schema()
    _test_note_artifact_validator_contract()
    _test_jpx_universe_cache()
    _test_gmail_body()
    _test_openwork_display_only()
    _test_note_autosave_and_mail_body()
    _test_production_paths_do_not_use_limit()
    _test_jpx_float_codes_normalized()
    _test_price_cache_and_prefetch()
    _test_high_classification()
    _test_previous_52w_high_line_retest()
    _test_duke_old_high_support()
    _test_9256_limit50_excluded_but_full_universe_included()
    _test_swing_high_break_9256_style()
    _test_journal_and_pattern_learning()
    _test_intraday_watchlist()
    _test_learning_log()
    _test_csv_schema_contract()
    _test_decision_engine()
    _test_trade_verification()
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


def _test_market_snapshot_artifact_schema() -> None:
    import json
    from build_note_assets import _build_note_body

    def fake_fetch(meta, timeout):
        if meta["key"] == "sox":
            return {
                "key": meta["key"],
                "label": meta["label"],
                "short_label": meta["short_label"],
                "symbol": meta["symbol"],
                "status": "unavailable",
                "value": None,
                "change": None,
                "change_pct": None,
                "as_of": None,
                "display_value": "未取得",
                "display_change_pct": "未取得",
                "error": "test missing",
            }
        return {
            "key": meta["key"],
            "label": meta["label"],
            "short_label": meta["short_label"],
            "symbol": meta["symbol"],
            "status": "ok",
            "value": 100.0,
            "change": 1.0,
            "change_pct": 1.0,
            "as_of": "2026-07-06",
            "display_value": "100.00",
            "display_change_pct": "+1.00%",
            "error": "",
        }

    with TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        path = build_market_snapshot(
            out_dir / "market_snapshot.json",
            fetcher=fake_fetch,
            regime_fetcher=lambda: Regime("NORMAL", "test"),
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["regime"] == "NORMAL"
        assert data["indicator_regime"] in {"NORMAL", "CAUTION", "RISK", "STOP"}
        assert data["indicators"]["nikkei"]["display_value"] == "100.00"
        assert data["indicators"]["sox"]["display_value"] == "未取得"

        (out_dir / "screening_result.csv").write_text("code,name,rank,score\n", encoding="utf-8")
        (out_dir / "decision_result.csv").write_text("code,name,decision,rank,confidence,skip_reason\n", encoding="utf-8")
        body = _build_note_body(out_dir)
        assert "フクロウ補助判定" in body
        assert "日経平均" in body
        assert "SOX: **未取得**" in body


def _write_fake_png(path: Path) -> None:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 1400)


def _test_note_artifact_validator_contract() -> None:
    market = {
        "regime": "NORMAL",
        "indicator_regime": "CAUTION",
        "indicators": {
            "nikkei": {"label": "日経平均", "display_value": "40,000.00", "display_change_pct": "+0.10%", "status": "ok"},
            "topix": {"label": "TOPIX", "display_value": "2,900.00", "display_change_pct": "+0.20%", "status": "ok"},
            "vix": {"label": "VIX", "display_value": "15.00", "display_change_pct": "-1.00%", "status": "ok"},
            "sox": {"label": "SOX", "display_value": "5,000.00", "display_change_pct": "+1.00%", "status": "ok"},
            "usdjpy": {"label": "ドル円", "display_value": "160.00", "display_change_pct": "+0.30%", "status": "ok"},
        },
    }
    note_body = """# 本日の日本株短期売買メモ 2026-07-07

## 市場状況

![市場状況](market_status.png)

- 地合い: **NORMAL**
- 日経平均: **40,000.00**
- TOPIX: **2,900.00**
- VIX: **15.00**
- SOX: **5,000.00**
- ドル円: **160.00**
- 判定: BUY 0件 / WATCH 1件 / SKIP 29件

## 本日の300万円運用判断

- 資金: **3,000,000円**
- ウォーレン判断: **CASH**
- 地合い: **NORMAL**
- BUY件数: 0件
- WATCH件数: 1件
- SKIP件数: 29件
- CASH枠: 3枠
- 地合いによる制御理由: 条件がそろうまで現金待機します。
- CASH理由: 条件が同時にそろう銘柄がありませんでした。

### 今日は無理に買わない

BUY条件を満たす銘柄がないため、現金を守ります。

## BUYカード または CASHカード

![CASHカード](buy_cash.png)

本日はBUY候補を出さず、現金待機とします。

### なぜBUY0件なのか

- 条件が同時にそろう銘柄がありませんでした。

## WATCHカード

![WATCHカード](watch.png)

## 免責文

この下書きは投資助言ではありません。
"""
    preview_html = """<!doctype html>
<html lang="ja"><body>
<h1>本日の日本株短期売買メモ</h1>
<h2>本日の300万円運用判断</h2>
<p>ウォーレン判断: CASH / BUY件数: 0件 / WATCH件数: 1件</p>
<img src="eyecatch.png">
<img src="market_status.png">
<img src="funnel.png">
<img src="buy_cash.png">
<img src="watch.png">
</body></html>
"""
    with TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        warren = {
            "date": "2026-07-07",
            "capital": 3_000_000,
            "regime": "NORMAL",
            "buy_count": 0,
            "watch_count": 1,
            "skip_count": 29,
            "cash_count": 3,
            "selected_symbols": [],
            "cash_reason": "条件が同時にそろう銘柄がありませんでした。",
            "risk_control_reason": "条件がそろうまで現金待機します。",
            "source_files": {
                "decision_result": "outputs/decision_result.csv",
                "discipline_result": "outputs/discipline_result.csv",
            },
            "generated_at": "2026-07-07T00:00:00+00:00",
        }
        (out_dir / "note_body.md").write_text(note_body, encoding="utf-8")
        (out_dir / "note_preview.html").write_text(preview_html, encoding="utf-8")
        (out_dir / "market_snapshot.json").write_text(json.dumps(market, ensure_ascii=False), encoding="utf-8")
        (out_dir / "warren_summary.json").write_text(json.dumps(warren, ensure_ascii=False), encoding="utf-8")
        (out_dir / "decision_result.csv").write_text("code,name,decision\n1111,A,SKIP\n", encoding="utf-8")
        (out_dir / "discipline_result.csv").write_text("slot,action,cash_reason\n1,CASH,条件不足\n", encoding="utf-8")
        (out_dir / "paper_portfolio_decision.csv").write_text("slot,action,cash_reason\n1,CASH,条件不足\n", encoding="utf-8")
        (out_dir / "note_cloud_artifact_manifest.json").write_text("{}", encoding="utf-8")
        for name in ["eyecatch.png", "market_status.png", "funnel.png", "buy_cash.png", "watch.png"]:
            _write_fake_png(out_dir / name)

        valid = validate_artifact(out_dir)
        assert valid.valid is True
        assert valid.warren_valid is True
        assert valid.buy_cash_judgement == "CASH"
        assert valid.watch_count == 1

        (out_dir / "watch.png").unlink()
        invalid = validate_artifact(out_dir)
        assert invalid.valid is False
        assert "watch.png" in invalid.missing_files


def _test_jpx_universe_cache() -> None:
    from tempfile import TemporaryDirectory
    from scanner import universe as universe_module

    source = pd.DataFrame(
        [
            {"コード": "1111", "銘柄名": "A", "市場・商品区分": "プライム（内国株式）", "33業種区分": "電気機器"},
        ]
    )
    normalized = normalize_jpx_listed(source, ("prime",))
    assert len(normalized) == 1

    with TemporaryDirectory() as tmp:
        old_cache_dir = universe_module.CACHE_DIR
        old_cache_path = universe_module.JPX_CACHE_PATH
        old_meta_path = universe_module.JPX_CACHE_META_PATH
        old_urls = universe_module.JPX_LISTED_URLS
        old_get = universe_module.requests.get
        try:
            universe_module.CACHE_DIR = Path(tmp)
            universe_module.JPX_CACHE_PATH = Path(tmp) / "jpx_listed.csv"
            universe_module.JPX_CACHE_META_PATH = Path(tmp) / "jpx_listed.meta.json"
            universe_module._save_jpx_cache(normalized, "https://example.com/jpx.xls")
            cached = universe_module._load_jpx_cache()
            assert cached is not None and len(cached) == 1

            def _fail_get(*args, **kwargs):
                raise RuntimeError("network unavailable")

            universe_module.requests.get = _fail_get
            loaded = universe_module.load_jpx_listed(universe_module.UniverseConfig(markets=("prime",)))
            assert len(loaded) == 1
            assert loaded.iloc[0]["ticker"] == "1111.T"
        finally:
            universe_module.CACHE_DIR = old_cache_dir
            universe_module.JPX_CACHE_PATH = old_cache_path
            universe_module.JPX_CACHE_META_PATH = old_meta_path
            universe_module.JPX_LISTED_URLS = old_urls
            universe_module.requests.get = old_get


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


def _test_swing_high_break_9256_style() -> None:
    dates = pd.bdate_range(end="2026-06-23", periods=260)
    close = pd.Series(range(3000, 3260), index=dates, dtype=float)
    high = close + 20
    low = close - 20
    volume = pd.Series(100_000, index=dates, dtype=float)

    high.loc["2026-06-10"] = 3520
    close.loc["2026-06-10"] = 3340
    high.loc["2026-06-11":"2026-06-19"] = [3435, 3435, 3365, 2714, 1778, 2040, 2540]
    close.loc["2026-06-11":"2026-06-19"] = [3340, 3340, 2640, 2140, 1640, 2040, 2540]
    high.iloc[-1] = 3560
    close.iloc[-1] = 3540
    volume.iloc[-1] = 300_000

    history = pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close, "Volume": volume})
    swing = detect_swing_high_break(history)
    assert swing["swing_high_price"] == 3520
    assert swing["swing_high_date"] == "2026-06-10"
    assert swing["swing_high_break"] is True

    profile = classify_high_profile(history)
    assert profile["high_type"] == "SWING_HIGH_BREAK"
    assert profile["swing_high_label"] == "直近スイング高値ブレイク"

    mail_df = pd.DataFrame(
        [
            {
                "code": "9256",
                "name": "サクシード",
                "sector": "サービス業",
                "rank": "S",
                "score": 90,
                "current_price": 3540,
                "ma25": 3000,
                "turnover_20d": 200_000_000,
                "volume_ratio_5d_20d": 1.8,
                "dist_52w_high_pct": 0,
                "lot_value_100": 354000,
                "reason": "テスト",
                **profile,
            }
        ]
    )
    body = build_candidate_body(mail_df, "NORMAL")
    assert "## 【直近高値ブレイク】" in body
    assert "9256" in body
    assert "3520" in body


def _test_openwork_display_only() -> None:
    with TemporaryDirectory() as tmp:
        score_path = Path(tmp) / "openwork_scores.csv"
        score_path.write_text("code,name,openwork_score\n1111,A,3.78\n", encoding="utf-8")
        base = pd.DataFrame(
            [
                {"code": "1111", "name": "A", "rank": "A", "score": 80, "current_price": 1000, "dist_52w_high_pct": 1, "volume_ratio_5d_20d": 2, "reason": "テスト", "lot_value_100": 100000},
                {"code": "2222", "name": "B", "rank": "A", "score": 90, "current_price": 1000, "dist_52w_high_pct": 1, "volume_ratio_5d_20d": 2, "reason": "テスト", "lot_value_100": 100000},
            ]
        )
        merged = add_openwork_scores(base, score_path)
        assert list(merged["score"]) == [80, 90]
        assert format_openwork_score(merged.loc[0, "openwork_score"]) == "3.78"
        assert format_openwork_score(merged.loc[1, "openwork_score"]) == "未取得"
        body = build_candidate_body(merged, "NORMAL")
        assert "OpenWork: 3.78" in body
        assert "OpenWork: 未取得" in body


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
    import base64
    import json
    import os

    html = "<html><head><title>x</title></head><body><h1>タイトル</h1><p>本文</p></body></html>"
    assert extract_body_fragment(html) == "<h1>タイトル</h1><p>本文</p>"
    assert is_saved_draft_url("https://note.com/notes/abc123")
    assert not is_saved_draft_url("https://note.com/notes/new")

    encoded = base64.b64encode(json.dumps({"cookies": [], "origins": []}).encode("utf-8")).decode("ascii")
    old = os.environ.get("NOTE_STORAGE_STATE")
    try:
        os.environ["NOTE_STORAGE_STATE"] = encoded
        state = load_storage_state()
        assert state is not None and state["cookies"] == [] and state["origins"] == []
    finally:
        if old is None:
            os.environ.pop("NOTE_STORAGE_STATE", None)
        else:
            os.environ["NOTE_STORAGE_STATE"] = old

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
    body = build_mail_body(screening, discipline, "NORMAL", "https://note.com/notes/abc", "Note下書きURL")
    assert "Note下書きURL: https://note.com/notes/abc" in body
    assert "note_daily.md" in body


def _test_production_paths_do_not_use_limit() -> None:
    root = Path(__file__).resolve().parent
    daily = (root / "daily_discipline_run.py").read_text(encoding="utf-8")
    assert "--limit" not in daily
    assert "limit=None" in daily
    app = (root / "app.py").read_text(encoding="utf-8")
    assert 'number_input("動作確認用の上限銘柄数' not in app
    assert "limit=None" in app
    for workflow in [root / ".github/workflows/daily-discipline.yml", root / ".github/workflows/note_autosave.yml"]:
        text = workflow.read_text(encoding="utf-8")
        assert "--limit" not in text
        # 本番ワークフローに QUICK_MODE / MAX_SYMBOLS が固定されていないこと（全銘柄で実行される）。
        assert "QUICK_MODE" not in text, f"{workflow.name} に QUICK_MODE が残っています"
        assert "MAX_SYMBOLS" not in text, f"{workflow.name} に MAX_SYMBOLS が残っています"
        assert "actions/checkout@v7" in text
        assert "actions/setup-python@v6" in text
        assert "actions/upload-artifact@v7" in text
    run_screening_text = (root / "run_screening.py").read_text(encoding="utf-8")
    assert 'parser.add_argument("--limit"' in run_screening_text
    assert "WARNING: run_screening limit=" in run_screening_text
    # pandas 3系は文字列列への数値代入がTypeErrorになるため、2系固定を必須にする。
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    assert "pandas>=2.2,<3.0" in requirements, "requirements.txt で pandas を 2 系に固定してください"


def _test_jpx_float_codes_normalized() -> None:
    """Excelがコード列を数値(1000.0形式)で返しても銘柄が弾かれないこと。"""
    source = pd.DataFrame(
        {
            "コード": [1000.0, 1301.0, "7203", 1305.0],
            "銘柄名": ["テスト製造", "極洋", "トヨタ自動車", "テストETF上場投信"],
            "市場・商品区分": [
                "プライム（内国株式）",
                "プライム（内国株式）",
                "プライム（内国株式）",
                "ETF・ETN",
            ],
            "33業種区分": ["機械", "水産・農林業", "輸送用機器", "-"],
        }
    )
    out = normalize_jpx_listed(source, ("prime", "standard", "growth"))
    assert set(out["code"]) == {"1000", "1301", "7203"}, out["code"].tolist()
    assert set(out["ticker"]) == {"1000.T", "1301.T", "7203.T"}


def _test_price_cache_and_prefetch() -> None:
    """価格キャッシュ: バッチプリフェッチ→キャッシュヒットで再ダウンロードしないこと（オフライン）。"""
    import numpy as np

    from scanner import prices

    def _fake_history(days: int = 30) -> pd.DataFrame:
        idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=days)
        close = pd.Series(np.linspace(100, 110, days), index=idx)
        return pd.DataFrame(
            {
                "Open": close,
                "High": close * 1.01,
                "Low": close * 0.99,
                "Close": close,
                "Volume": 100000,
            },
            index=idx,
        )

    calls = {"batch": 0, "single": 0}

    def fake_download(tickers, *args, **kwargs):
        if isinstance(tickers, (list, tuple)):
            calls["batch"] += 1
            frames = {}
            for t in tickers:
                if t == "9998.T":
                    continue  # データ無し銘柄（空マーカーが保存されるはず）
                frames[t] = _fake_history()
            if not frames:
                return pd.DataFrame()
            return pd.concat(frames, axis=1)
        calls["single"] += 1
        return _fake_history()

    original_root = prices.PRICE_CACHE_ROOT
    original_download = prices.yf.download
    original_wall_timeout = prices.YFINANCE_WALL_TIMEOUT
    try:
        with TemporaryDirectory() as tmp:
            prices.PRICE_CACHE_ROOT = Path(tmp) / "prices"
            prices.yf.download = fake_download
            prices.YFINANCE_WALL_TIMEOUT = 0

            tickers = ["1001.T", "1002.T", "9998.T"]
            stats = prefetch_stats = prices.prefetch_price_histories(tickers, batch_size=2)
            assert prefetch_stats["fetched"] == 2, stats
            assert prefetch_stats["empty"] == 1, stats
            assert calls["batch"] == 2  # batch_size=2 で3銘柄→2チャンク

            # キャッシュヒット: 単発ダウンロードが発生しないこと
            hist = prices.fetch_price_history("1001.T")
            assert not hist.empty
            assert list(hist.columns) == ["Open", "High", "Low", "Close", "Volume"]
            assert calls["single"] == 0

            # プリフェッチの空結果は一時的な通信失敗かもしれないため、空キャッシュ固定しない。
            empty_hist = prices.fetch_price_history("9998.T")
            assert not empty_hist.empty
            assert calls["single"] == 1

            # 2回目のプリフェッチは単発取得済みも含めて全てキャッシュ扱い
            stats2 = prices.prefetch_price_histories(tickers, batch_size=2)
            assert stats2["cached"] == 3, stats2
            assert calls["batch"] == 2

            # キャッシュ未登録銘柄は従来通り単発取得され、以後はキャッシュされる
            hist_new = prices.fetch_price_history("2002.T")
            assert not hist_new.empty
            assert calls["single"] == 2
            prices.fetch_price_history("2002.T")
            assert calls["single"] == 2

            # 古い日付ディレクトリの掃除
            old_dir = prices.PRICE_CACHE_ROOT / "2000-01-01__18mo"
            old_dir.mkdir(parents=True, exist_ok=True)
            prices.cleanup_old_price_cache()
            assert not old_dir.exists()
    finally:
        prices.PRICE_CACHE_ROOT = original_root
        prices.yf.download = original_download
        prices.YFINANCE_WALL_TIMEOUT = original_wall_timeout


def _test_9256_limit50_excluded_but_full_universe_included() -> None:
    from scanner.universe import UniverseConfig, load_jpx_listed
    universe = load_jpx_listed(UniverseConfig(markets=("prime", "standard", "growth")))
    codes = universe["code"].astype(str)
    assert codes.eq("9256").any()
    assert not codes.head(50).eq("9256").any()


def _test_high_classification() -> None:
    dates = pd.bdate_range("2025-01-01", periods=260)
    up = pd.Series(range(1000, 1260), index=dates, dtype=float)
    history_new = pd.DataFrame({"Open": up, "High": up * 1.01, "Low": up * 0.99, "Close": up, "Volume": 1_000_000})
    profile_new = classify_high_profile(history_new)
    assert profile_new["high_type"] == "52W_NEW_HIGH"

    recent = up.copy()
    recent.iloc[:-60] = 1200
    recent.iloc[-60:] = list(range(1000, 1060))
    history_recent = pd.DataFrame({"Open": recent, "High": recent * 1.01, "Low": recent * 0.99, "Close": recent, "Volume": 1_000_000})
    profile_recent = classify_high_profile(history_recent)
    assert profile_recent["high_type"] in {"RECENT_NEW_HIGH", "RECENT_NEAR_HIGH"}

    screening = pd.DataFrame(
        [
            {"code": "1", "name": "A", "rank": "S", "score": 1, "high_type": "52W_NEW_HIGH", "high_label": "52週新高値", "high_date": "2026-01-01", "dist_to_high_pct": 0, "reason": "a"},
            {"code": "2", "name": "B", "rank": "A", "score": 2, "high_type": "RECENT_NEAR_HIGH", "high_label": "直近高値接近", "high_date": "2026-01-02", "dist_to_high_pct": 2, "reason": "b"},
        ]
    )
    lines = build_high_sections_markdown(screening, max_rows=5)
    assert any("【52週高値更新】" in line for line in lines)
    assert any("【その他】" in line for line in lines)
    assert any("直近高値接近" in line for line in lines)


def _test_previous_52w_high_line_retest() -> None:
    dates = pd.bdate_range("2025-01-01", periods=280)
    close = pd.Series(900.0, index=dates)
    close.iloc[:180] = pd.Series(range(800, 980), index=dates[:180], dtype=float)
    close.iloc[180:252] = 950
    close.iloc[252] = 1005
    close.iloc[253:270] = pd.Series(range(1020, 1190, 10), index=dates[253:270], dtype=float)
    close.iloc[270:] = [1160, 1130, 1100, 1070, 1040, 1010, 995, 1005, 1010, 1008]
    open_ = close * 0.995
    high = close * 1.01
    low = close * 0.985
    volume = pd.Series(100_000, index=dates, dtype=float)
    volume.iloc[-3:] = [120_000, 180_000, 160_000]
    history = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume})
    indicators = calculate_indicators(history)
    assert indicators is not None
    retest = detect_previous_52w_high_line_retest(history, indicators, min_volume_20d=10_000)
    assert retest["previous_52w_high_line"] == 979.0
    assert retest["line_deviation_pct"] == 2.96
    assert retest["drawdown_from_recent_high_pct"] < -8
    assert retest["prev_52w_retest_rank"] in {"S", "A", "B"}
    assert retest["candidate_action"] == ("BUY" if retest["prev_52w_retest_rank"] == "S" else "CASH")


def _test_duke_old_high_support() -> None:
    dates = pd.bdate_range("2025-01-01", periods=280)
    close = pd.Series(950.0, index=dates, dtype=float)
    high = pd.Series(950.0, index=dates, dtype=float)
    low = close - 15
    open_ = close - 5
    high.iloc[:180] = pd.Series(range(800, 980), index=dates[:180], dtype=float)
    close.iloc[:180] = high.iloc[:180] - 2
    high.iloc[180:252] = 950
    close.iloc[180:252] = 945
    high.iloc[252] = 1005
    close.iloc[252] = 1000
    high.iloc[253] = 1180
    close.iloc[253] = 1160
    pullback = pd.Series([980 + i * (35 / 25) for i in range(26)], index=dates[254:280], dtype=float)
    close.iloc[254:280] = pullback
    high.iloc[254:280] = pullback + 8
    open_.iloc[254:280] = pullback - 4
    low.iloc[254:280] = pullback - 18
    volume = pd.Series(100_000, index=dates, dtype=float)
    volume.iloc[-5:] = 180_000
    history = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume})
    indicators = calculate_indicators(history)
    assert indicators is not None
    duke = detect_duke_old_high_support(history, indicators, min_turnover_20d=1)
    assert duke["duke_old_high_support"] is True, duke
    assert duke["old_52w_high"] == 979.0, duke
    assert duke["duke_support_score"] >= 80, duke
    assert duke["duke_support_rank"] == "S", duke
    scored = score_stock(indicators, None, {"earnings_status": "確認済"}, duke_support=duke)
    assert scored["score"] >= duke["duke_support_score"], scored
    assert "DUKE旧52週高値サポート" in scored["reason"], scored


def _test_intraday_watchlist() -> None:
    """日中監視ウォッチリスト: 選定・優先順・上限クリップ・英数字コード・全銘柄フォールバック。"""
    from build_intraday_watchlist import select_watchlist, build, WATCHLIST_NAME
    from intraday_high_alert import build_alert, build_body, build_subject, intraday_mail_enabled, load_watchlist_codes

    df = pd.DataFrame([
        {"code": "7203", "name": "トヨタ", "market": "東証プライム", "rank": "S", "score": 90,
         "dist_52w_high_pct": 0.5, "turnover_20d": 5e10, "high_type": "52W_NEW_HIGH", "volume_ratio_5d_20d": 2.0},
        {"code": "285A", "name": "キオクシア", "market": "東証プライム", "rank": "見送り", "score": 40,
         "dist_52w_high_pct": 2.0, "turnover_20d": 1e10, "high_type": "52W_NEAR_HIGH", "volume_ratio_5d_20d": 1.1},
        {"code": "0000", "name": "除外", "market": "東証スタンダード", "rank": "見送り", "score": 5,
         "dist_52w_high_pct": 50.0, "turnover_20d": 1e7, "high_type": "OTHER", "volume_ratio_5d_20d": 0.8},
    ])
    wl = select_watchlist(df, max_symbols=300, near_pct=5.0, turnover_top=2, vol_mult=1.5)
    codes = list(wl["code"])
    assert codes[0] == "7203", codes            # S候補が最優先
    assert "285A" in codes                        # 英数字コードが52週接近で残る
    assert "0000" not in codes                    # 非該当は除外

    # 上限クリップ（200〜500）
    big = pd.DataFrame([
        {"code": f"{1000 + i}", "name": f"n{i}", "market": "東証プライム", "rank": "S", "score": 100 - i,
         "dist_52w_high_pct": 0.1, "turnover_20d": 1e9, "high_type": "52W_NEW_HIGH", "volume_ratio_5d_20d": 1.6}
        for i in range(600)
    ])
    assert len(select_watchlist(big, max_symbols=1000)) == 500
    assert len(select_watchlist(big, max_symbols=50)) == 200

    # turnover_20d 列が無くても落ちない
    assert "7203" in list(select_watchlist(df.drop(columns=["turnover_20d", "volume_ratio_5d_20d"]))["code"])
    # 空入力 → 空
    assert select_watchlist(pd.DataFrame()).empty

    with TemporaryDirectory() as tmp:
        out = Path(tmp)
        # screening_result.csv から build → intraday が読める
        df.to_csv(out / "screening_result.csv", index=False, encoding="utf-8-sig")
        path = build(out)
        assert path is not None and path.name == WATCHLIST_NAME
        loaded = load_watchlist_codes(path)
        assert loaded is not None and "7203" in loaded and "285A" in loaded, loaded
        # ファイルが無ければ None（＝全銘柄フォールバック）
        assert load_watchlist_codes(out / "does_not_exist.csv") is None
        # screening が無ければ build は None（フォールバック）
        assert build(Path(tmp) / "empty_sub") is None

    indicators = {
        "current_price": 1000.0,
        "high_52w": 1000.0,
        "dist_52w_high_pct": 0.0,
        "turnover_20d": 300_000_000.0,
        "volume_ratio_5d_20d": 1.3,
    }
    alert = build_alert("7011", "三菱重工", indicators, {
        "high_type": "SWING_HIGH_BREAK",
        "high_price": 990.0,
        "dist_to_high_pct": 0.0,
    })
    assert alert is not None
    subject = build_subject([alert])
    assert subject.startswith("[GitHub][Intraday][v2026-07-06]"), subject

    old_env = {key: os.environ.get(key) for key in ("GITHUB_SHA", "GITHUB_RUN_ID", "ENABLE_INTRADAY_MAIL")}
    try:
        os.environ["GITHUB_SHA"] = "abc123"
        os.environ["GITHUB_RUN_ID"] = "98765"
        body = build_body([alert])
        assert body.splitlines()[:5] == [
            "workflow: Intraday High Alert",
            "source: GitHub Actions",
            "commit: abc123",
            "run_id: 98765",
            "version: 2026-07-06",
        ], body
        os.environ["ENABLE_INTRADAY_MAIL"] = "false"
        assert intraday_mail_enabled() is False
        os.environ["ENABLE_INTRADAY_MAIL"] = "true"
        assert intraday_mail_enabled() is True
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _test_learning_log() -> None:
    from learning_log import append_learning_candidates, build_learning_rows, infer_strategy

    sample = pd.DataFrame([
        {
            "code": "1111",
            "ticker": "1111.T",
            "name": "A",
            "market": "東証プライム",
            "sector": "情報・通信業",
            "score": 90,
            "rank": "S",
            "current_price": 1000,
            "dist_52w_high_pct": 1.2,
            "pullback_from_recent_high_pct": -12.3,
            "volume_ratio_5d_20d": 1.8,
            "turnover_20d": 200_000_000,
            "high_type": "SWING_HIGH_BREAK",
            "reason": "テスト",
        },
        {
            "code": "2222",
            "name": "B",
            "score": 70,
            "rank": "A",
            "current_price": 800,
            "duke_old_high_support": True,
            "duke_support_score": 85,
            "duke_support_rank": "S",
            "duke_support_signal": True,
            "reason": "DUKE",
        },
    ])
    rows = build_learning_rows(sample, run_date="2026-07-03")
    assert list(rows["code"]) == ["1111", "2222"]
    assert rows.loc[0, "strategy"] == "swing_high_break"
    assert rows.loc[1, "strategy"] == "duke_old_high_support"
    assert infer_strategy(sample.iloc[1]) == "duke_old_high_support"

    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        inp = tmp_path / "screening_result.csv"
        out = tmp_path / "learning_candidates.csv"
        sample.to_csv(inp, index=False, encoding="utf-8-sig")
        first = append_learning_candidates(inp, out, run_date="2026-07-03")
        assert first.input_rows == 2 and first.appended_rows == 2 and first.total_rows == 2
        second = append_learning_candidates(inp, out, run_date="2026-07-03")
        assert second.input_rows == 2 and second.appended_rows == 0 and second.total_rows == 2
        third = append_learning_candidates(inp, out, run_date="2026-07-04")
        assert third.appended_rows == 2 and third.total_rows == 4
        saved = pd.read_csv(out, dtype={"code": str})
        assert {"date", "code", "strategy", "current", "volume_ratio"}.issubset(saved.columns)


def _test_decision_engine() -> None:
    import decision_engine as de

    # A=BUY（Sランク・資金内・高値近い・出来高増・MA上・地合いNORMAL）
    a = {
        "code": "6920", "name": "テストA", "rank": "S", "score": 90,
        "current_price": 3000, "dist_to_high_pct": 1.0, "volume_ratio_5d_20d": 1.5,
        "ma25": 2500, "ma75": 2400, "ma200": 2000,
    }
    # B=WATCH（Sだが100株=120万で資金20%(60万)超＝高すぎ）
    b = {
        "code": "6857", "name": "テストB", "rank": "S", "score": 85,
        "current_price": 12000, "dist_to_high_pct": 1.0, "volume_ratio_5d_20d": 1.5,
        "ma25": 10000, "ma75": 9000, "ma200": 8000,
    }
    # C=SKIP（ランクB・高値から遠い・出来高細り・MA割れ）
    c = {
        "code": "1301", "name": "テストC", "rank": "B", "score": 40,
        "current_price": 2000, "dist_to_high_pct": 18.0, "volume_ratio_5d_20d": 0.8,
        "ma25": 2100, "ma75": 2200, "ma200": 2300,
    }

    df = pd.DataFrame([a, b, c])
    dec = de.build_decisions(df, learning=None, regime="NORMAL")
    by = {r["code"]: r for _, r in dec.iterrows()}

    assert by["6920"]["decision"] == "BUY"
    assert by["6920"]["position_size"] == 100
    assert "Sランク" in by["6920"]["entry_reason"]
    assert float(by["6920"]["stop_loss_price"]) == 2790.0
    assert float(by["6920"]["take_profit_price"]) == 3450.0

    assert by["6857"]["decision"] == "WATCH"
    assert by["6857"]["position_size"] == 0
    assert "高すぎ" in by["6857"]["skip_reason"] or "資金20%" in by["6857"]["skip_reason"]

    assert by["1301"]["decision"] == "SKIP"
    assert by["1301"]["entry_reason"] == ""
    assert by["1301"]["skip_reason"] != ""

    # 地合いSTOPなら BUY はゼロ（新規停止）
    dec_stop = de.build_decisions(df, learning=None, regime="STOP")
    assert int((dec_stop["decision"] == "BUY").sum()) == 0
    dec_caution = de.build_decisions(pd.concat([df.iloc[[0]], df.iloc[[0]].assign(code="6921", name="テストA2")], ignore_index=True), learning=None, regime="CAUTION")
    assert int((dec_caution["decision"] == "BUY").sum()) == 1
    assert int((dec_caution["decision"] == "WATCH").sum()) == 1

    # 最大3銘柄の枠上限（BUY適格4件→BUY3＋WATCH1に降格）
    many = pd.DataFrame([
        {"code": f"A{i}", "name": f"m{i}", "rank": "S", "score": s,
         "current_price": 3000, "dist_to_high_pct": 1.0, "volume_ratio_5d_20d": 1.5,
         "ma25": 2500, "ma75": 2400, "ma200": 2000}
        for i, s in enumerate([90, 85, 80, 75], start=1)
    ])
    dec_many = de.build_decisions(many, learning=None, regime="NORMAL")
    assert int((dec_many["decision"] == "BUY").sum()) == de.MAX_POSITIONS
    assert int((dec_many["decision"] == "WATCH").sum()) == 1
    assert {"screen_type", "screen_tags", "strategy", "high_type", "lot_value_100", "dist_25ma_pct", "dist_200ma_pct", "volume_ratio_5d_20d", "buy_reason"}.issubset(dec_many.columns)

    # run(): CSV + MD 出力、集計、入力欠損の安全動作
    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        inp = tmp_path / "screening_result.csv"
        df.to_csv(inp, index=False, encoding="utf-8-sig")
        res = de.run(screening_path=inp, learning_path=tmp_path / "none.csv",
                     out_dir=tmp_path, regime="NORMAL")
        assert res["input_exists"] is True
        assert res["buy"] == 1 and res["watch"] == 1 and res["skip"] == 1
        assert (tmp_path / "decision_result.csv").exists()
        assert (tmp_path / "decision_report.md").exists()

        missing = de.run(screening_path=tmp_path / "no_such.csv",
                         learning_path=tmp_path / "none.csv", out_dir=tmp_path)
        assert missing["input_exists"] is False and missing["rows"] == 0


def _test_csv_schema_contract() -> None:
    import run_screening as rs
    import decision_engine as de

    raw = pd.DataFrame([
        {
            "code": "1111", "name": "A", "rank": "S", "score": 90,
            "current_price": 1000, "high_type": "52W_NEW_HIGH",
            "dist_52w_high_pct": 0.0, "ma25_gap_pct": 1.2, "ma200_gap_pct": 8.0,
            "volume_ratio_5d_20d": 1.5, "turnover_20d": 200_000_000,
            "reason": "52週高値更新",
        },
        {
            "code": "2222", "name": "B", "rank": "見送り", "score": 20,
            "current_price": 900, "high_type": "OTHER",
            "dist_52w_high_pct": 9.0, "ma25_gap_pct": 2.5, "ma200_gap_pct": 2.0,
            "volume_ratio_5d_20d": 0.7, "turnover_20d": 80_000_000,
            "reason": "流動性不足",
        },
        {
            "code": "3333", "name": "C", "rank": "見送り", "score": 0,
            "current_price": 900, "high_type": "52W_NEW_HIGH",
            "dist_52w_high_pct": 0.4, "ma25_gap_pct": 8.0, "ma200_gap_pct": 12.0,
            "volume_ratio_5d_20d": 0.7, "turnover_20d": 80_000_000,
            "reason": "流動性不足",
        },
        {
            "code": "4444", "name": "D", "rank": "C", "score": 45,
            "current_price": 900, "high_type": "OTHER",
            "dist_52w_high_pct": 18.0, "ma25_gap_pct": 8.0, "ma200_gap_pct": 12.0,
            "volume_ratio_5d_20d": 1.2, "turnover_20d": 200_000_000,
            "reason": "様子見",
        },
    ])
    normalized = rs._normalize_screening_schema(raw)
    assert set(normalized["rank"]) == {"S", "SKIP", "C"}
    assert set(normalized["screen_type"]).issubset(set(rs.SCREEN_TYPE_VALUES))
    assert "OTHER" not in set(normalized["screen_type"])
    assert normalized.loc[0, "screen_type"] == "MULTI"
    assert normalized.loc[0, "screen_tags"] == "52W_BREAKOUT,25MA_PULLBACK"
    assert normalized.loc[1, "screen_type"] == "SKIP"
    assert normalized.loc[1, "screen_tags"] == "25MA_PULLBACK,200MA_TOUCH"
    assert normalized.loc[2, "screen_type"] == "SKIP"
    assert normalized.loc[2, "screen_tags"] == "52W_BREAKOUT"
    assert normalized.loc[3, "screen_type"] == "WATCH"
    assert normalized.loc[3, "screen_tags"] == "WATCH"
    assert normalized.loc[0, "dist_25ma_pct"] == 1.2
    assert normalized.loc[1, "dist_200ma_pct"] == 2.0
    assert normalized.loc[0, "buy_reason"] == "52週高値更新"
    assert normalized.loc[1, "buy_reason"] == ""
    assert normalized.loc[3, "buy_reason"] == "様子見"

    decisions = de.build_decisions(normalized, regime="NORMAL")
    de.validate_decision_consistency(normalized, decisions)
    shared = {"screen_type", "screen_tags", "dist_25ma_pct", "dist_200ma_pct", "buy_reason"}
    assert shared.issubset(decisions.columns)
    by_code = {r["code"]: r for _, r in decisions.iterrows()}
    assert by_code["1111"]["screen_type"] == "MULTI"
    assert by_code["1111"]["screen_tags"] == "52W_BREAKOUT,25MA_PULLBACK"
    assert by_code["1111"]["buy_reason"] == "52週高値更新"
    assert by_code["2222"]["rank"] == "SKIP"
    assert by_code["3333"]["screen_type"] == "SKIP"
    assert by_code["3333"]["screen_tags"] == "52W_BREAKOUT"
    assert by_code["4444"]["screen_type"] == "WATCH"


def _test_trade_verification() -> None:
    """BUY3銘柄の自動検証（record→update→report）をオフラインで検証する。"""

    def _prices(opens_hlc: list[tuple[float, float, float, float]],
                start: str = "2026-06-01") -> pd.DataFrame:
        idx = pd.bdate_range(start, periods=len(opens_hlc))
        return pd.DataFrame(
            [{"Open": o, "High": h, "Low": lo, "Close": c} for o, h, lo, c in opens_hlc],
            index=idx,
        )

    # ── evaluate_signal 単体 ─────────────────────
    from datetime import date as _date
    signal = _date(2026, 5, 31)  # 6/1(月)が翌営業日

    # 利確: 3日目に高値が +15% を超える
    tp_days = [(1000, 1010, 990, 1005), (1010, 1100, 1000, 1090), (1090, 1160, 1080, 1150)]
    r = tv.evaluate_signal(signal, _prices(tp_days))
    assert r["status"] == "CLOSED" and r["first_hit"] == "TP"
    assert r["entry_open"] == 1000.0 and r["exit_return_pct"] == 15.0
    assert r["tp_hit"] is True and r["stop_hit"] is False

    # 損切り: 2日目に安値が -7% を割る
    stop_days = [(1000, 1010, 980, 990), (985, 990, 920, 930), (930, 940, 900, 910)]
    r = tv.evaluate_signal(signal, _prices(stop_days))
    assert r["status"] == "CLOSED" and r["first_hit"] == "STOP"
    assert r["exit_return_pct"] == -7.0

    # 同日に両方到達 → 保守的に損切り扱い
    both_days = [(1000, 1200, 900, 1000)]
    r = tv.evaluate_signal(signal, _prices(both_days))
    assert r["first_hit"] == "STOP" and r["exit_return_pct"] == -7.0

    # 時間切れ: 10営業日 ±7%以内 → 10日目の引けで決済
    flat_days = [(1000 + i, 1000 + i + 20, 1000 + i - 20, 1000 + i + 10) for i in range(12)]
    r = tv.evaluate_signal(signal, _prices(flat_days))
    assert r["status"] == "CLOSED" and "時間切れ" in r["exit_reason"]
    assert r["close_d2"] == 1011.0 and r["close_d3"] == 1012.0
    assert r["close_d5"] == 1014.0 and r["close_d10"] == 1019.0
    assert r["exit_price"] == 1019.0
    assert r["max_gain_pct"] == round((1029 / 1000 - 1) * 100, 2)
    assert r["max_drop_pct"] == round((980 / 1000 - 1) * 100, 2)

    # 途中経過: 3営業日分しか無い → OPEN（捏造しない）
    r = tv.evaluate_signal(signal, _prices(flat_days[:3]))
    assert r["status"] == "OPEN" and "close_d10" not in r
    assert r["close_d3"] == 1012.0

    # 翌営業日がまだ来ていない → PENDING
    r = tv.evaluate_signal(_date(2026, 6, 30), _prices(flat_days))
    assert r["status"] == "PENDING"

    # ── record → update → report の一連 ─────────────
    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dec_path = tmp_path / "decision_result.csv"
        hist_path = tmp_path / "trade_history.csv"
        rep_path = tmp_path / "performance_report.md"

        pd.DataFrame([
            {"code": "6920", "name": "レーザーテック", "decision": "BUY", "rank": "S",
             "score": 90, "confidence": 100,
             "entry_reason": "Sランク / 52週高値まで1%（近い） / 出来高1.5倍（増加）",
             "skip_reason": ""},
            {"code": "7203", "name": "トヨタ自動車", "decision": "WATCH", "rank": "S",
             "score": 80, "confidence": 88, "entry_reason": "Sランク",
             "skip_reason": "出来高0.9倍（細り）"},
            {"code": "1301", "name": "極洋", "decision": "SKIP", "rank": "B",
             "score": 40, "confidence": 10, "entry_reason": "", "skip_reason": "ランク不足"},
        ]).to_csv(dec_path, index=False, encoding="utf-8-sig")

        rec = tv.record_signals(dec_path, hist_path, run_date="2026-05-31")
        assert rec["appended"] == 2 and rec["total"] == 2  # SKIP は記録しない
        rec2 = tv.record_signals(dec_path, hist_path, run_date="2026-05-31")
        assert rec2["appended"] == 0 and rec2["total"] == 2  # 同日重複なし

        hist = tv.load_history(hist_path)
        assert set(hist["decision"]) == {"BUY", "WATCH"}
        assert {"screen_tags", "strategy", "high_type", "lot_value_100", "volume_ratio_5d_20d", "turnover_20d"}.issubset(hist.columns)
        assert (hist["status"] == "PENDING").all()
        buy_row = hist[hist["code"] == "6920"].iloc[0]
        assert str(buy_row["near_high"]).lower() == "true"
        assert str(buy_row["vol_up"]).lower() == "true"

        fake = {"6920": _prices(tp_days), "7203": _prices(stop_days)}
        upd = tv.update_history(hist_path, fetcher=lambda c: fake.get(c),
                                today=_date(2026, 6, 20))
        assert upd["updated"] == 2 and upd["closed"] == 2

        hist = tv.load_history(hist_path)
        assert tv._num(hist[hist["code"] == "6920"].iloc[0]["exit_return_pct"]) == 15.0
        assert tv._num(hist[hist["code"] == "7203"].iloc[0]["exit_return_pct"]) == -7.0

        stats = tv.aggregate(hist)
        buy_stats = stats[stats["decision"] == "BUY"].iloc[0]
        assert buy_stats["trades"] == 1 and buy_stats["win_rate_pct"] == 100.0
        watch_stats = stats[stats["decision"] == "WATCH"].iloc[0]
        assert watch_stats["win_rate_pct"] == 0.0

        out = tv.write_report(hist_path, rep_path, today=_date(2026, 6, 20))
        text = out.read_text(encoding="utf-8")
        assert "BUY銘柄 検証レポート" in text
        assert "| BUY | 1 | 100.0% |" in text
        assert "利確(+15%)" in text and "損切り(-7%)" in text

        # 履歴なしでも空レポートを安全に出す
        empty_rep = tv.write_report(tmp_path / "no_hist.csv", tmp_path / "empty.md",
                                    today=_date(2026, 6, 20))
        assert "確定トレードがまだありません" in empty_rep.read_text(encoding="utf-8")


if __name__ == "__main__":
    main()
