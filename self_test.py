from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from gmail_notify import DISCLAIMER, build_candidate_body, build_subject
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


def main() -> None:
    _test_indicators_and_scoring()
    _test_discipline_normal_and_stop()
    _test_market_regime_local_fallback()
    _test_jpx_universe_cache()
    _test_gmail_body()
    _test_openwork_display_only()
    _test_note_autosave_and_mail_body()
    _test_production_paths_do_not_use_limit()
    _test_high_classification()
    _test_previous_52w_high_line_retest()
    _test_duke_old_high_support()
    _test_9256_limit50_excluded_but_full_universe_included()
    _test_swing_high_break_9256_style()
    _test_journal_and_pattern_learning()
    _test_intraday_watchlist()
    _test_learning_log()
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
    run_screening_text = (root / "run_screening.py").read_text(encoding="utf-8")
    assert 'parser.add_argument("--limit"' in run_screening_text
    assert "WARNING: run_screening limit=" in run_screening_text


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
    from intraday_high_alert import load_watchlist_codes

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


if __name__ == "__main__":
    main()
