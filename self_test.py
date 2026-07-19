from __future__ import annotations

import os
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from gmail_notify import DISCLAIMER, _body_to_html, build_candidate_body, build_subject
from fetch_market import build_market_snapshot
from market_regime import Regime, fetch_regime
from paper_portfolio_discipline import build_discipline_portfolio
from pattern_learn import build_pattern_summary
from daily_note_mail import build_mail_body
from note_autosave import extract_body_fragment, is_saved_draft_url, load_cloud_note_payload, load_storage_state
from scanner.highs import analyze_high_freshness, build_high_sections_markdown, classify_high_profile, detect_duke_old_high_support, detect_previous_52w_high_line_retest, detect_quality_flags, detect_swing_high_break
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
    _test_sns_promo()
    _test_production_paths_do_not_use_limit()
    _test_jpx_float_codes_normalized()
    _test_price_cache_and_prefetch()
    _test_high_classification()
    _test_kabutan_high_and_quality_flags()
    _test_track_record()
    _test_kabutan_check()
    _test_note_highs_v2()
    _test_fundamentals_contract()
    _test_openwork_cache_contract()
    _test_jpx_business_day_calendar()
    _test_holiday_target_date_and_skip_guard()
    _test_openwork_manual_reflection_contract()
    _test_previous_52w_high_line_retest()
    _test_duke_old_high_support()
    _test_9256_limit50_excluded_but_full_universe_included()
    _test_swing_high_break_9256_style()
    _test_journal_and_pattern_learning()
    _test_intraday_watchlist()
    _test_intraday_cloud_workflow_contract()
    _test_cloud_digest_mail()
    _test_metron_kpi()
    _test_paper_open_fill()
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
        # 正しい4本（highs/pullback/chatgpt/claude）＋各冒頭の市場ステータス＋最低限の記事内容が新契約
        status_block = "## 市場ステータス\n\n- 本日の地合い: **NORMAL**\n- 判定元: regime.txt\n\n"
        portfolio_block = (
            "## 保有銘柄・CASH判断\n\n- 本日は新規買いなし → CASH判断（現金維持）\n\n"
            "## 売買理由\n\n- CASH: Sランク不足のため現金保有\n\n"
            "## 評価額・現金比率\n\n- 運用資金: 3,000,000円\n- 現金: 3,000,000円（現金比率 100.0%）\n\n"
            "## 損益（未実現損益）\n\n- 未実現損益: 0円（保有なし・現金のみ）\n\n"
            "## 次営業日の方針\n\n- 地合いNORMAL: 規律どおりSランク上位を最大3銘柄で買付。\n"
        )
        note4_manifest = []
        for key in ["highs", "pullback", "chatgpt", "claude"]:
            if key in ("chatgpt", "claude"):
                body = f"# ダミー {key} 2026-07-07\n\n{status_block}{portfolio_block}"
            else:
                body = f"# ダミー {key} 2026-07-07\n\n{status_block}- 該当なし\n"
            (out_dir / f"note_{key}.md").write_text(body, encoding="utf-8")
            note4_manifest.append({"key": key, "title": f"ダミー {key}", "md_file": f"note_{key}.md"})
        (out_dir / "note_drafts_manifest.json").write_text(
            json.dumps(note4_manifest, ensure_ascii=False), encoding="utf-8"
        )
        for name in ["eyecatch.png", "market_status.png", "funnel.png", "buy_cash.png", "watch.png"]:
            _write_fake_png(out_dir / name)

        valid = validate_artifact(out_dir)
        assert valid.valid is True
        assert valid.warren_valid is True
        assert valid.buy_cash_judgement == "CASH"
        assert valid.watch_count == 1
        assert set(valid.note4_status.values()) == {"OK"}

        # 4本のうち1本の市場ステータスが欠けたら失敗扱い
        (out_dir / "note_chatgpt.md").write_text("# ダミー chatgpt 2026-07-07\n\n- 該当なし\n", encoding="utf-8")
        broken = validate_artifact(out_dir)
        assert broken.valid is False
        assert any("note_chatgpt.md" in item for item in broken.missing_items)

        # 中身が薄い300万円運用（市場ステータスのみ・運用セクションなし）は失敗扱い
        (out_dir / "note_chatgpt.md").write_text(
            f"# ダミー chatgpt 2026-07-07\n\n{status_block}- 該当なし\n", encoding="utf-8"
        )
        thin = validate_artifact(out_dir)
        assert thin.valid is False
        assert any("必須セクション欠落" in item for item in thin.missing_items)

        # 運用セクションのうち1つ（損益）が欠けても失敗扱い
        (out_dir / "note_claude.md").write_text(
            f"# ダミー claude 2026-07-07\n\n{status_block}"
            + portfolio_block.replace("## 損益（未実現損益）\n\n- 未実現損益: 0円（保有なし・現金のみ）\n\n", ""),
            encoding="utf-8",
        )
        thin2 = validate_artifact(out_dir)
        assert any("損益" in item for item in thin2.missing_items)

        # highs/pullback: 候補なしかつ「該当なし/データ不足」の明記もなければ失敗扱い
        (out_dir / "note_highs.md").write_text(
            f"# ダミー highs 2026-07-07\n\n{status_block}本日のまとめです。\n", encoding="utf-8"
        )
        thin3 = validate_artifact(out_dir)
        assert any("note_highs.md" in item and "該当なし" in item for item in thin3.missing_items)

        # 全部復元して再びPASSすること
        (out_dir / "note_chatgpt.md").write_text(
            f"# ダミー chatgpt 2026-07-07\n\n{status_block}{portfolio_block}", encoding="utf-8"
        )
        (out_dir / "note_claude.md").write_text(
            f"# ダミー claude 2026-07-07\n\n{status_block}{portfolio_block}", encoding="utf-8"
        )
        (out_dir / "note_highs.md").write_text(
            f"# ダミー highs 2026-07-07\n\n{status_block}- 該当なし\n", encoding="utf-8"
        )
        restored = validate_artifact(out_dir)
        assert restored.valid is True
        assert set(restored.note4_status.values()) == {"OK"}

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
    assert "[7735 ＳＣＲＥＥＮホールディングス](https://finance.yahoo.co.jp/quote/7735.T/chart" in body
    assert "📈 チャート:https://finance.yahoo.co.jp/quote/7735.T/chart" in body
    assert '<a href="https://finance.yahoo.co.jp/quote/7735.T/chart' in _body_to_html(body)
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


def _test_sns_promo() -> None:
    """編集長: X告知文が4本分・URL枠・ハッシュタグ付きで生成されること。"""
    import json as _json

    from sns_promo import build_post, build_sns_posts

    post = build_post("highs", "更新した銘柄は**3銘柄**、迫った銘柄は**5銘柄**でした。", "NORMAL", "2026-07-10")
    assert "新高値3銘柄" in post and "接近5銘柄" in post and "{URL}" in post and "#52週新高値" in post
    with TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        # manifestが無い場合はスキップ（例外を出さない）
        assert build_sns_posts(out_dir) is None
        (out_dir / "note_drafts_manifest.json").write_text("[]", encoding="utf-8")
        for key in ("highs", "pullback", "chatgpt", "claude"):
            (out_dir / f"note_{key}.md").write_text(f"# dummy {key}\n", encoding="utf-8")
        md_path = build_sns_posts(out_dir)
        assert md_path is not None and md_path.exists()
        posts = _json.loads((out_dir / "sns_posts.json").read_text(encoding="utf-8"))
        assert [p["key"] for p in posts] == ["highs", "pullback", "chatgpt", "claude"]
        assert all("{URL}" in p["text"] for p in posts)


def _test_cloud_verify_ok_contract() -> None:
    """保存後確認（verify_ok）の判定契約。実際のnote画面に合わせた条件。"""
    from note_autosave import _cloud_verify_ok

    url = "https://editor.note.com/notes/abc123/edit/?from=notes"
    # 正常系: 今回の実ログと同じ状態（title/画像/不足なし/一覧あり/非公開下書き）→ 成功
    ok_case = {
        "title_found": True,
        "missing_texts": [],
        "image_count": 5,
        "min_image_count": 5,
        "public_state": "draft_unpublished_assumed",
        "draft_list": {"checked": True, "title_found": True},
        "url_pattern_ok": True,
    }
    assert _cloud_verify_ok(ok_case, url) is True
    # URLパターンが不一致でも本文確認がそろっていれば成功
    weird_url_case = dict(ok_case, url_pattern_ok=False)
    assert _cloud_verify_ok(weird_url_case, "https://editor.note.com/notes/abc123/edit/?weird=1") is True
    # 編集画面の再確認がフレーキーに失敗しても、下書き一覧にタイトルがあれば成功
    flaky_case = {
        "title_found": False,
        "missing_texts": ["x"],
        "image_count": 0,
        "min_image_count": 5,
        "error": "timeout",
        "public_state": "draft_unpublished_assumed",
        "draft_list": {"checked": True, "title_found": True},
        "url_pattern_ok": True,
    }
    assert _cloud_verify_ok(flaky_case, url) is True
    # 異常系: タイトルも一覧も確認できない → 失敗
    bad_case = dict(flaky_case, draft_list={"checked": True, "title_found": False})
    assert _cloud_verify_ok(bad_case, url) is False
    # 異常系: draft_urlなし → 失敗
    assert _cloud_verify_ok(ok_case, "") is False
    # 異常系: 公開されてしまっている状態は成功にしない
    published_case = dict(ok_case, public_state="published")
    assert _cloud_verify_ok(published_case, url) is False


def _test_note_autosave_and_mail_body() -> None:
    import base64
    import json
    import os

    html = "<html><head><title>x</title></head><body><h1>タイトル</h1><p>本文</p></body></html>"
    assert extract_body_fragment(html) == "<h1>タイトル</h1><p>本文</p>"
    assert is_saved_draft_url("https://note.com/notes/abc123")
    assert not is_saved_draft_url("https://note.com/notes/new")
    # クエリ・フラグメント付きの下書きURLも保存済みとして認める
    assert is_saved_draft_url("https://editor.note.com/notes/abc123/edit/?from=notes")
    assert is_saved_draft_url("https://note.com/notes/abc123?magazine_key=x#top")
    assert not is_saved_draft_url("https://note.com/notes/new?from=menu")
    _test_cloud_verify_ok_contract()
    with TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        for name in ["eyecatch.png", "market_status.png", "funnel.png", "buy_cash.png", "watch.png"]:
            (out_dir / name).write_bytes(b"png")
        (out_dir / "note_preview.html").write_text("<html><body>preview</body></html>", encoding="utf-8")
        (out_dir / "note_body.md").write_text(
            """# 本日の日本株短期売買メモ 2026-07-07

![アイキャッチ](eyecatch.png)

## 市場状況

![市場状況](market_status.png)

- 判定: BUY 0件 / WATCH 2件 / SKIP 5件

## 本日の300万円運用判断

- ウォーレン判断: **CASH**

### なぜBUY0件なのか

- Sランク不足のため現金保有

## WATCHカード

![WATCHカード](watch.png)

## 免責文

この下書きは投資助言ではありません。
""",
            encoding="utf-8",
        )
        cloud_payload = load_cloud_note_payload(out_dir)
        assert cloud_payload.title == "本日の日本株短期売買メモ 2026-07-07"
        assert len(cloud_payload.image_paths) == 3
        assert "本日の300万円運用判断" in cloud_payload.body_html
        assert "CASH" in cloud_payload.verify_texts

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


def _test_kabutan_high_and_quality_flags() -> None:
    """カブタン準拠の52W高値判定（ザラバ高値ベース）と鮮度・イナゴ・TOBフラグの検証。"""
    dates = pd.bdate_range("2025-01-01", periods=300)

    def _hist(close: pd.Series, high: pd.Series | None = None, low: pd.Series | None = None) -> pd.DataFrame:
        high = close * 1.005 if high is None else high
        low = close * 0.995 if low is None else low
        return pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close, "Volume": 1_000_000})

    # 1) ザラバ高値で更新・終値は前日終値高値未満 → 旧実装は接近扱い、新実装は52W_NEW_HIGH。
    close = pd.Series(range(700, 1000), index=dates, dtype=float)
    high = close + 3.0
    close.iloc[-2], high.iloc[-2] = 1002.0, 1004.0
    close.iloc[-1], high.iloc[-1] = 1000.0, 1010.0  # 当日High 1010 > 前日までの最高High 1004
    low = close - 3.0
    low.iloc[-8] = 500.0  # 直近5-30日に明確なスイング高値を作らない（低値のみ変化）
    profile = classify_high_profile(_hist(close, high, low))
    assert profile["high_type"] == "52W_NEW_HIGH", profile["high_type"]

    # 2) 高値未更新・終値が52週高値から3%以内 → 52W_NEAR_HIGH。
    close2 = pd.Series(range(700, 1000), index=dates, dtype=float)
    high2 = close2 + 2.0
    high2.iloc[-40] = 1030.0  # 40日前に52週高値
    close2.iloc[-1], high2.iloc[-1] = 1005.0, 1007.0  # 乖離 (1030-1005)/1030 ≒ 2.4%
    profile2 = classify_high_profile(_hist(close2, high2))
    assert profile2["high_type"] == "52W_NEAR_HIGH", profile2["high_type"]
    assert float(profile2["dist_to_high_pct"]) <= 3

    # 3) 上場1年未満（120営業日）でも上場来ベースで新高値判定できる。
    short_dates = pd.bdate_range("2026-01-01", periods=120)
    close3 = pd.Series(range(500, 620), index=short_dates, dtype=float)
    profile3 = classify_high_profile(_hist(close3))
    assert profile3["high_type"] in {"52W_NEW_HIGH", "SWING_HIGH_BREAK"}, profile3["high_type"]

    # 4) 鮮度: 毎日更新の右肩上がり → breaks_20d=20・初回ブレイクではない。
    fresh_up = analyze_high_freshness(_hist(pd.Series(range(700, 1000), index=dates, dtype=float)))
    assert fresh_up["is_new_high_today"] is True
    assert int(fresh_up["breaks_20d"]) >= 15
    assert fresh_up["first_break_60d"] is False

    # 5) 鮮度: 長期横ばい→当日だけ更新 → 初回ブレイク。
    flat = pd.Series(900.0, index=dates)
    flat_high = pd.Series(905.0, index=dates)
    flat.iloc[-1], flat_high.iloc[-1] = 930.0, 935.0
    fresh_first = analyze_high_freshness(_hist(flat, flat_high))
    assert fresh_first["is_new_high_today"] is True
    assert fresh_first["first_break_60d"] is True
    assert int(fresh_first["breaks_20d"]) == 1

    # 6) TOB疑い: 直近8日終値が完全固定・値幅ゼロ・52週高値圏。
    tob = pd.Series(1000.0, index=dates)
    tob.iloc[:150] = pd.Series(range(700, 850), index=dates[:150], dtype=float)
    tob.iloc[-8:] = 1200.0
    tob_high = tob.copy()
    tob_low = tob.copy()
    flags_tob = detect_quality_flags(pd.DataFrame({"Open": tob, "High": tob_high, "Low": tob_low, "Close": tob, "Volume": 1_000_000}))
    assert flags_tob["tob_suspect"] is True, flags_tob
    assert "TOB疑い" in str(flags_tob["quality_flags"])

    # 7) イナゴ疑い: 直近5営業日で+40%急騰。
    inago = pd.Series(1000.0, index=dates)
    inago.iloc[-5:] = [1100.0, 1200.0, 1300.0, 1380.0, 1400.0]
    flags_inago = detect_quality_flags(_hist(inago))
    assert flags_inago["inago_suspect"] is True, flags_inago
    assert "イナゴ疑い" in str(flags_inago["quality_flags"])

    # 8) 平常時の右肩上がりはどちらのフラグも立たない。
    normal = pd.Series([1000.0 * (1.001 ** i) for i in range(300)], index=dates)
    flags_normal = detect_quality_flags(_hist(normal))
    assert flags_normal["inago_suspect"] is False
    assert flags_normal["tob_suspect"] is False

    # 9) run_screening._collect_highs_row がフラグ・決算日欄付きの行を返す。
    from run_screening import _collect_highs_row
    up = pd.Series(range(1000, 1300), index=dates, dtype=float)
    hist_up = _hist(up)
    indicators = calculate_indicators(hist_up)
    assert indicators is not None
    high_info = classify_high_profile(hist_up)
    row = _collect_highs_row(
        {"code": "1111", "ticker": "1111.T", "name": "テスト", "market": "東証プライム", "sector": "情報・通信業"},
        indicators,
        high_info,
        hist_up,
    )
    assert row is not None
    for key in ("breaks_20d", "first_break_60d", "inago_suspect", "tob_suspect", "note_flags", "earnings_date"):
        assert key in row, key

    # 10) note_draft: 疑いフラグ銘柄はカードから除外し従来表に⚠️付きで残る。決算日列も出る。
    from note_draft import build_highs_note
    highs_df = pd.DataFrame(
        [
            {"code": "1111", "name": "健全", "high_type": "52W_NEW_HIGH", "current_price": 1000, "high_52w": 1000,
             "dist_to_high_pct": 0.0, "high_date": "2026-07-10", "turnover_20d": 500_000_000,
             "inago_suspect": False, "tob_suspect": False, "note_flags": "初回ブレイク", "earnings_date": "2026-08-08"},
            {"code": "2222", "name": "張付", "high_type": "52W_NEW_HIGH", "current_price": 1200, "high_52w": 1200,
             "dist_to_high_pct": 0.0, "high_date": "2026-07-10", "turnover_20d": 300_000_000,
             "inago_suspect": False, "tob_suspect": True, "note_flags": "TOB疑い", "earnings_date": ""},
        ]
    )
    note_text = build_highs_note(highs_df, Path("screening_highs_test.csv"))
    # T-K新形式: 到達(A)/接近(B)/参考掲載(C)。TOB疑いはCへ、決算日は表とカウントダウンで出る。
    assert "2026-08-08" in note_text or "2026年8月8日" in note_text
    assert "TOB疑い" in note_text
    assert "参考掲載" in note_text
    main_section = note_text.split("## 【C】")[0]
    assert "張付" not in main_section  # TOB疑いはA/B本体に出ない
    assert "健全" in main_section


def _test_track_record() -> None:
    """T-I(2026-07-10): バックテスト博士（実績トラッキング）の契約テスト。通信なし。"""
    import tempfile
    from datetime import date as _date

    import pandas as pd

    from track_record import (
        append_today_snapshot,
        build_track_record_lines,
        evaluate_track_record,
        load_record,
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        outputs = tmp_path / "outputs"
        outputs.mkdir()
        record_path = tmp_path / "data" / "highs_track_record.csv"
        summary_path = outputs / "track_record.json"

        highs = pd.DataFrame(
            [
                {
                    "code": "1111",
                    "ticker": "1111.T",
                    "name": "上昇テスト",
                    "high_type": "52W_NEW_HIGH",
                    "first_break_60d": True,
                    "inago_suspect": False,
                    "tob_suspect": False,
                    "current_price": 100.0,
                },
                {
                    "code": "2222",
                    "ticker": "2222.T",
                    "name": "下落テスト",
                    "high_type": "52W_NEAR_HIGH",
                    "first_break_60d": False,
                    "inago_suspect": False,
                    "tob_suspect": False,
                    "current_price": 200.0,
                },
            ]
        )
        highs.to_csv(outputs / "screening_highs_20260708.csv", index=False)

        entry_day = _date(2026, 7, 8)
        added = append_today_snapshot(output_dir=outputs, record_path=record_path, today=entry_day)
        assert added == 2, f"追記件数が想定外: {added}"
        # 再実行しても重複しない
        added_again = append_today_snapshot(output_dir=outputs, record_path=record_path, today=entry_day)
        assert added_again == 0, "同一(日付,コード)が重複追記された"
        record = load_record(record_path)
        assert len(record) == 2 and set(record["code"]) == {"1111", "2222"}

        # 合成価格: 1111 は掲載後上昇、2222 は下落。営業日インデックスで entry_day を含む。
        idx = pd.bdate_range("2026-07-08", periods=25)

        def fake_fetcher(ticker: str) -> pd.DataFrame:
            if ticker == "1111.T":
                closes = [100.0, 110.0, 112.0, 115.0, 118.0, 120.0] + [130.0] * 14 + [150.0] * 5
            else:
                closes = [200.0, 190.0, 188.0, 186.0, 184.0, 180.0] + [170.0] * 14 + [150.0] * 5
            return pd.DataFrame({"Close": closes}, index=idx)

        summary = evaluate_track_record(
            record_path=record_path,
            summary_path=summary_path,
            price_fetcher=fake_fetcher,
            today=_date(2026, 8, 10),
        )
        assert summary["evaluated"] == 2 and summary["price_missing"] == 0, summary
        h1 = summary["horizons"]["1"]
        # +1営業日: 1111=+10%, 2222=-5% → 勝率50%, 平均+2.5%
        assert h1["n"] == 2 and h1["win_rate_pct"] == 50.0, h1
        assert abs(h1["avg_return_pct"] - 2.5) < 0.01, h1
        h5 = summary["horizons"]["5"]
        # +5営業日: 1111=+20%, 2222=-10%
        assert h5["best_pct"] == 20.0 and h5["worst_pct"] == -10.0, h5
        h20 = summary["horizons"]["20"]
        # +20営業日: 1111=+50%, 2222=-25%
        assert h20["best_pct"] == 50.0 and h20["worst_pct"] == -25.0, h20
        fb5 = summary["first_break"].get("5")
        assert fb5 and fb5["n"] == 1 and fb5["win_rate_pct"] == 100.0, summary["first_break"]
        assert summary_path.exists(), "track_record.json が出力されていない"

        # note向け整形: データありなら表と初回ブレイクの一文、データ無しなら「データ不足」
        lines = "\n".join(build_track_record_lines(summary))
        assert "## 実績" in lines and "| 翌営業日 | 2 | 50.0% |" in lines, lines
        assert "初回ブレイク" in lines and "100.0%" in lines, lines
        empty_lines = "\n".join(build_track_record_lines(None))
        assert "データ不足" in empty_lines
        # 価格取得できない銘柄は除外し件数を明示（捏造しない）
        summary_missing = evaluate_track_record(
            record_path=record_path,
            summary_path=summary_path,
            price_fetcher=lambda ticker: pd.DataFrame(),
            today=_date(2026, 8, 10),
        )
        assert summary_missing["price_missing"] == 2 and summary_missing["evaluated"] == 0
        missing_lines = "\n".join(build_track_record_lines(summary_missing))
        assert "データ不足" in missing_lines
    print("self-test: track_record(バックテスト博士) OK")


def _test_kabutan_check() -> None:
    """T-J(2026-07-10): カブタン照合ツールの契約テスト。通信なし（fetchを差し替え）。"""
    import numpy as np
    import pandas as pd

    import kabutan_check

    # コード正規化: 区切り文字の混在・.T付き・英字コード・重複
    codes = kabutan_check.parse_codes("7203, 6758　9984\n130a.t、7203")
    assert codes == ["7203", "6758", "9984", "130A"], codes

    idx = pd.bdate_range("2025-07-01", periods=300)

    def _hist(closes: list[float]) -> pd.DataFrame:
        s = pd.Series(closes, index=idx[: len(closes)])
        return pd.DataFrame({"Close": s, "High": s * 1.005, "Low": s * 0.995, "Volume": 500000})

    rising = list(np.linspace(1000, 2000, 300))  # 高値更新中
    far = list(np.linspace(1000, 2000, 260)) + [1600.0] * 40  # 高値から2割下

    original = kabutan_check.fetch_price_history

    def fake_fetch(ticker: str) -> pd.DataFrame:
        if ticker == "1111.T":
            return _hist(rising)
        if ticker == "2222.T":
            return _hist(far)
        return pd.DataFrame()

    kabutan_check.fetch_price_history = fake_fetch
    try:
        highs = pd.DataFrame([{"code": "1111", "high_type": "52W_NEW_HIGH", "note_flags": ""}])

        d1 = kabutan_check.diagnose_code("1111", highs)
        assert str(d1["verdict"]).startswith("該当"), d1
        assert "掲載あり" in str(d1["in_our_list"]), d1

        d2 = kabutan_check.diagnose_code("2222", highs)
        assert "乖離>3%" in str(d2["verdict"]), d2
        assert "掲載なし" in str(d2["in_our_list"]), d2

        d3 = kabutan_check.diagnose_code("130A", highs)  # 英字コード: fetchせず対象外
        assert "ユニバース" in str(d3["verdict"]), d3

        d4 = kabutan_check.diagnose_code("3333", highs)  # データ取得不可 → 捏造せず診断不能
        assert d4["verdict"] == "診断不能", d4

        report = "\n".join(kabutan_check.build_report(["1111", "2222", "130A"], highs))
        assert "| 1111 |" in report and "| 2222 |" in report and "既知の制限" in report, report
        assert "接近(3%以内)" in report  # 読み方ガイドが必ず付く
    finally:
        kabutan_check.fetch_price_history = original
    print("self-test: kabutan_check(カブタン照合) OK")


def _test_note_highs_v2() -> None:
    """T-K(2026-07-12): note1本目（52週新高値 接近・到達）新形式の契約テスト。通信なし。"""
    import pandas as pd

    from note_draft import build_highs_note

    rows = [
        {"code": "1111", "ticker": "1111.T", "name": "到達テスト", "sector": "電気機器",
         "high_type": "52W_NEW_HIGH", "current_price": 1500.0, "prev_close": 1450.0, "change_pct": 3.45,
         "today_high": 1520.0, "high_price": 1490.0, "high_52w": 1490.0, "dist_to_high_pct": 0.0,
         "high_date": "2026-07-10", "turnover_20d": 2_500_000_000, "volume_ratio_today": 3.2,
         "intraday_range_pct": 4.1, "breaks_20d": 1, "first_break_60d": True,
         "inago_suspect": False, "tob_suspect": False, "data_anomaly": False, "anomaly_note": "",
         "note_flags": "初回ブレイク", "earnings_date": "2026-07-14", "data_date": "2026-07-10",
         "per_actual": 18.2, "pbr": 2.1, "roe_pct": 12.4, "sales_growth_pct": 12.3, "market_cap_oku": 3500},
        {"code": "2222", "ticker": "2222.T", "name": "接近テスト", "sector": "サービス業",
         "high_type": "52W_NEAR_HIGH", "current_price": 980.0, "change_pct": 1.03, "high_price": 1000.0,
         "dist_to_high_pct": 2.0, "turnover_20d": 300_000_000, "breaks_20d": 0, "first_break_60d": False,
         "inago_suspect": False, "tob_suspect": False, "data_anomaly": False, "anomaly_note": "",
         "note_flags": "", "earnings_date": "2026-01-15", "data_date": "2026-07-10",
         "per_actual": float("nan"), "market_cap_oku": None},
        {"code": "3333", "ticker": "3333.T", "name": "張付参考", "sector": "卸売業",
         "high_type": "52W_NEW_HIGH", "current_price": 1200.0, "dist_to_high_pct": 0.0,
         "turnover_20d": 500_000_000, "breaks_20d": 12, "first_break_60d": False,
         "inago_suspect": False, "tob_suspect": True, "data_anomaly": False, "anomaly_note": "",
         "note_flags": "TOB疑い", "earnings_date": "", "data_date": "2026-07-10"},
        {"code": "4444", "ticker": "4444.T", "name": "異常参考", "sector": "銀行業",
         "high_type": "52W_NEAR_HIGH", "current_price": 82270.0, "prev_close": 4100.0, "change_pct": 1.2,
         "dist_to_high_pct": 1.5, "turnover_20d": 200_000_000, "breaks_20d": 0, "first_break_60d": False,
         "inago_suspect": False, "tob_suspect": False, "data_anomaly": True,
         "anomaly_note": "前日比と前日終値から逆算した現在値が不整合（桁ずれ・分割の疑い）",
         "note_flags": "", "earnings_date": "", "data_date": "2026-07-10"},
    ]
    text = build_highs_note(pd.DataFrame(rows), Path("screening_highs_test.csv"))

    # 1) タイトルに対象取引日（データ最終日）が入る
    assert text.splitlines()[0] == "# 2026年7月10日 52週新高値 接近・到達銘柄", text.splitlines()[0]
    # 2) NaN/None/null が本文に残らない
    import re
    for token in ("nan", "NaN", "None", "null", "NULL", "inf"):
        hits = re.findall(rf"(?<![A-Za-z0-9_]){token}(?![A-Za-z0-9_])", text)
        assert not hits, f"{token} が本文に残存: {hits}"
    # 3) 到達(A)と接近(B)が別セクションで表示される
    assert "## 【A】52週新高値に本日到達した銘柄" in text
    assert "## 【B】52週新高値まで3%以内に接近している銘柄" in text
    a_section = text.split("## 【A】")[1].split("## 【B】")[0]
    b_section = text.split("## 【B】")[1].split("## 【C】")[0]
    assert "到達テスト" in a_section and "接近テスト" not in a_section
    assert "接近テスト" in b_section and "到達テスト" not in b_section
    # 4) TOB疑い・データ異常は参考掲載(C)へ。A/B本体には出ない
    assert "張付参考" not in a_section and "異常参考" not in b_section
    c_section = text.split("## 【C】")[1].split("## 本日の集計")[0]
    assert "張付参考" in c_section and "異常参考" in c_section
    assert "データ異常のため参考掲載" in c_section
    # 5) 決算カウントダウン: 未来日は「あとN日」(2026-07-10→07-14=4日)、過去日は次回として出さない
    assert "2026年7月14日／あと4日" in text
    assert "2026年1月15日" not in text and "未公表" in b_section
    # 6) 実績セクション・集計・免責が末尾にある
    assert "## 本日の集計" in text and "## 実績（過去に掲載した銘柄のその後）" in text
    assert "本記事は情報提供を目的としたもので、特定銘柄の売買を推奨するものではありません" in text
    # 7) 検索トレンドは取得していないので「急上昇」と書かない
    assert "急上昇" not in text
    # 8) 注目ポイントとチャートリンクがある（銘柄ごとの解説）
    assert "🔍 **注目ポイント**" in text
    assert "https://finance.yahoo.co.jp/quote/1111.T/chart" in text
    # 9) OpenWork行が通信なしで出力される（キャッシュ無し→取得できず）
    assert "OpenWork" in text
    # 10) validator互換: 一覧表と高値乖離%マーカー
    assert "### 一覧表" in text and "高値乖離%" in text

    # 空データ日: データ不足の明記＋4本構成を壊さない
    empty_text = build_highs_note(pd.DataFrame(), None)
    assert "データ不足" in empty_text and "該当なし" in empty_text
    assert "本記事は情報提供を目的としたもので" in empty_text
    print("self-test: note_highs_v2(52週新高値 記事新形式) OK")


def _test_fundamentals_contract() -> None:
    """T-K(2026-07-12): ファンダ指標の妥当性検証と株価・単位の異常値チェック。通信なし。"""
    from fundamentals import detect_price_anomalies, sanitize_fundamentals

    info = {
        "currency": "JPY", "trailingPE": 18.23, "forwardPE": 16.5, "priceToBook": 2.14,
        "dividendYield": 0.018, "returnOnEquity": 0.124, "operatingMargins": 0.152,
        "profitMargins": 0.108, "revenueGrowth": 0.123, "earningsGrowth": 0.205,
        "marketCap": 350_000_000_000, "currentPrice": 1500.0,
    }
    out = sanitize_fundamentals(info, current_price=1500.0)
    assert out["per_actual"] == 18.2 and out["pbr"] == 2.14, out
    assert out["dividend_yield_pct"] == 1.8 and out["roe_pct"] == 12.4, out
    assert out["market_cap_oku"] == 3500, out
    assert out["fundamentals_source"] == "yfinance"

    # 異常値は「使わない」: マイナスPER・通貨違いの時価総額・株価不整合
    bad = sanitize_fundamentals({"currency": "USD", "trailingPE": -5, "marketCap": 1e12}, 1500.0)
    assert "per_actual" not in bad and "market_cap_oku" not in bad, bad
    mismatch = sanitize_fundamentals(
        {"currency": "JPY", "marketCap": 350_000_000_000, "currentPrice": 82270.0}, current_price=4100.0
    )
    assert "market_cap_oku" not in mismatch, mismatch  # 桁ずれ・分割疑いは時価総額を信用しない
    assert sanitize_fundamentals({}, None) == {}

    # 株価異常値チェック: キオクシア型（前日比と現在値の不整合）を検出できる
    kioxia_like = {"current_price": 82270.0, "prev_close": 4100.0, "change_pct": 1.2, "turnover_20d": 5e9}
    issues = detect_price_anomalies(kioxia_like)
    assert any("不整合" in i for i in issues), issues
    assert any("値幅制限" in i for i in detect_price_anomalies({"current_price": 100.0, "prev_close": 60.0, "change_pct": 66.7}))
    assert any("本日高値" in i for i in detect_price_anomalies({"current_price": 1000.0, "today_high": 900.0}))
    normal = {"current_price": 1500.0, "prev_close": 1450.0, "change_pct": 3.45, "today_high": 1520.0, "turnover_20d": 2.5e9}
    assert detect_price_anomalies(normal) == [], detect_price_anomalies(normal)
    print("self-test: fundamentals(妥当性・異常値検証) OK")


def _test_openwork_cache_contract() -> None:
    """T-K(2026-07-12): OpenWork月次キャッシュ（30日ルール・失敗時前回値保持・捏造禁止）。通信なし。"""
    import tempfile
    from datetime import date as _date

    import pandas as pd

    import openwork_cache as ow

    today = _date(2026, 7, 12)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cache_path = tmp_path / "openwork_cache.csv"
        record_path = tmp_path / "highs_track_record.csv"
        pd.DataFrame([
            {"code": "1111", "name": "新鮮", "overall": 4.1, "respondents": 120,
             "fetched_at": "2026-07-01", "source_url": "", "status": "ok"},
            {"code": "2222", "name": "古株", "overall": 3.2, "respondents": 5,
             "fetched_at": "2026-05-01", "source_url": "", "status": "ok"},
        ]).to_csv(cache_path, index=False)
        pd.DataFrame([{"date": "2026-07-10", "code": "3333", "ticker": "3333.T", "name": "新顔"}]).to_csv(record_path, index=False)

        cache = ow.load_cache(cache_path)
        # 1) 30日未満は再取得しない / 2) 30日以上は更新対象
        fresh = cache[cache["code"] == "1111"].iloc[0]
        stale = cache[cache["code"] == "2222"].iloc[0]
        assert ow.needs_update(fresh, today) is False
        assert ow.needs_update(stale, today) is True

        # 3) 取得失敗（fetcherが返さない）→ 前回値と取得日を保持 / 4) 新規で取得できず→捏造せずunavailable
        stats = ow.update_cache(cache_path=cache_path, record_path=record_path, fetcher=lambda codes: {}, today=today)
        assert stats["targets"] == 2 and stats["kept"] == 1 and stats["missing"] == 1, stats
        updated = ow.load_cache(cache_path)
        kept_row = updated[updated["code"] == "2222"].iloc[0]
        assert float(kept_row["overall"]) == 3.2 and str(kept_row["fetched_at"])[:10] == "2026-05-01"
        new_row = updated[updated["code"] == "3333"].iloc[0]
        assert str(new_row["status"]) == "unavailable" and str(new_row.get("overall", "")).strip() in ("", "nan")

        # 取得成功時は更新される（fetcher差し替え）
        stats2 = ow.update_cache(
            cache_path=cache_path, record_path=record_path,
            fetcher=lambda codes: {"2222": {"overall": 3.5, "respondents": 6, "fetched_at": today.isoformat()}},
            today=today,
        )
        assert stats2["updated"] == 1, stats2
        updated2 = ow.load_cache(cache_path)
        assert float(updated2[updated2["code"] == "2222"].iloc[0]["overall"]) == 3.5

        # 5-7) note表示: 通信なしでキャッシュのみ参照。取得日を必ず表示。少人数は参考値。
        lines_fresh = "\n".join(ow.build_openwork_lines("1111", updated2, today))
        assert "総合評価：4.10" in lines_fresh and "取得日：2026-07-01" in lines_fresh
        lines_stale = "\n".join(ow.build_openwork_lines("2222", ow.load_cache(cache_path), _date(2026, 9, 1)))
        assert "更新待ち" in lines_stale and "参考値" in lines_stale
        lines_missing = "\n".join(ow.build_openwork_lines("3333", updated2, today))
        assert "取得できず" in lines_missing
        lines_none = "\n".join(ow.build_openwork_lines("9999", updated2, today))
        assert "取得できず" in lines_none
        # NaN等が表示に残らない
        for text in (lines_fresh, lines_stale, lines_missing):
            for token in ("nan", "None", "null"):
                assert token not in text, text
    print("self-test: openwork_cache(月次キャッシュ) OK")


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
    from intraday_high_alert import build_alert, build_body, build_subject, intraday_mail_enabled, load_watchlist_codes, status_mail_on_no_new_enabled

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

    old_env = {key: os.environ.get(key) for key in ("GITHUB_SHA", "GITHUB_RUN_ID", "ENABLE_INTRADAY_MAIL", "INTRADAY_STATUS_MAIL_ON_NO_NEW")}
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
        assert "検出アラート: 1件" in body
        assert "[7011 三菱重工](https://finance.yahoo.co.jp/quote/7011.T/chart" in body
        assert "https://finance.yahoo.co.jp/quote/7011.T/chart" in body
        status_body = build_body([], detected_count=19, status_note="手動確認")
        assert "検出アラート: 19件" in status_body
        assert "新規アラート: 0件" in status_body
        assert "手動確認" in status_body
        os.environ["ENABLE_INTRADAY_MAIL"] = "false"
        assert intraday_mail_enabled() is False
        os.environ["ENABLE_INTRADAY_MAIL"] = "true"
        assert intraday_mail_enabled() is True
        os.environ["INTRADAY_STATUS_MAIL_ON_NO_NEW"] = "true"
        assert status_mail_on_no_new_enabled() is True
        os.environ["INTRADAY_STATUS_MAIL_ON_NO_NEW"] = "false"
        assert status_mail_on_no_new_enabled() is False
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _test_intraday_cloud_workflow_contract() -> None:
    """リアルタイム監視はGitHub Actions本番scheduleでGmail送信し、通知済みstateをrun間で引き継ぐ。"""
    workflow = (Path(__file__).resolve().parent / ".github" / "workflows" / "intraday_high_alert.yml").read_text(encoding="utf-8")
    assert 'ENABLE_INTRADAY_MAIL: "false"' not in workflow
    assert "send_mail:" in workflow
    assert "status_mail_on_no_new:" in workflow
    assert "INTRADAY_STATUS_MAIL_ON_NO_NEW" in workflow
    assert "github.event_name == 'schedule' && 'true'" in workflow
    assert "actions/cache/restore@v4" in workflow
    assert "actions/cache/save@v4" in workflow
    assert "intraday-alert-state-${{ steps.holiday.outputs.jst_date }}" in workflow
    assert "Check Gmail secrets" in workflow
    assert "GMAIL_USER secret is missing" in workflow


def _test_cloud_digest_mail() -> None:
    """25MA/押し目などの引け後クラウド結果はGmailで届き、手動再送もできる。"""
    import tempfile
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from cloud_mail_digest import build_digest, collect_attachments

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        (out / "note_pullback_title.txt").write_text("25MAタッチ候補", encoding="utf-8")
        (out / "note_pullback.md").write_text("# 25MAタッチ候補\n\n7011 三菱重工\n", encoding="utf-8")
        (out / "note_highs_title.txt").write_text("52週新高値候補", encoding="utf-8")
        (out / "note_highs.md").write_text("# 52週新高値候補\n\n該当なし\n", encoding="utf-8")
        (out / "note_chatgpt.md").write_text("# ChatGPT案\n\nBUY なし\n", encoding="utf-8")
        (out / "note_claude.md").write_text("# Claude案\n\nWATCH なし\n", encoding="utf-8")
        (out / "metron_kpi_report.md").write_text("# メトロンKPI\n\n- note4本: OK\n", encoding="utf-8")
        (out / "screening_pullback_20260621_132309.csv").write_text("code,name\n1111,古い候補\n", encoding="utf-8")
        (out / "screening_pullback_20260716_160000.csv").write_text(
            "code,name,current_price,ma25,ma200,ma240,dist_25ma_pct,dist_200ma_pct,dist_52w_high_pct,turnover_20d,ma25_touch,ma200_touch\n"
            "7011,三菱重工,4380,4310,3900,3880,1.6,12.3,4.5,123456789,true,false\n"
            "1333,Ｕｍｉｏｓ,1307,1274,1291,1248,2.6,1.3,16.0,629260612,true,true\n",
            encoding="utf-8",
        )

        digest = build_digest(out, now=datetime(2026, 7, 16, 18, 40, tzinfo=ZoneInfo("Asia/Tokyo")))
        assert digest.subject == "【DUKEクラウド】25MA/200MA・本日のスクリーニング結果 2026-07-16"
        assert "25MA/押し目" in digest.body
        assert "25MAタッチ候補" in digest.body
        assert "## 25MA/200MA候補（本文で確認）" in digest.body
        assert "25MAタッチ（2件）" in digest.body
        assert "200MAタッチ（1件）" in digest.body
        assert "[1333](https://finance.yahoo.co.jp/quote/1333.T/chart" in digest.body
        assert "https://finance.yahoo.co.jp/quote/1333.T/chart" in digest.body
        assert "52週新高値" in digest.body
        assert "メトロンKPI" in digest.body
        assert "| nan" not in digest.body.lower()
        assert "nan |" not in digest.body.lower()
        assert any(path.name == "screening_pullback_20260716_160000.csv" for path in collect_attachments(out))
        assert not any(path.name == "screening_pullback_20260621_132309.csv" for path in collect_attachments(out))

    project_root = Path(__file__).resolve().parent
    note_workflow = (project_root / ".github" / "workflows" / "note_draft_cloud.yml").read_text(encoding="utf-8")
    daily_workflow = (project_root / ".github" / "workflows" / "daily-discipline.yml").read_text(encoding="utf-8")
    resend_workflow = (project_root / ".github" / "workflows" / "cloud_digest_mail.yml").read_text(encoding="utf-8")
    assert "send_mail:" in note_workflow
    assert "SEND_CLOUD_DIGEST" in note_workflow
    assert "Save cloud Note draft in note.com" in note_workflow
    assert "outputs/note_body.md" in note_workflow
    assert "note_draft_url_cloud.txt" in note_workflow
    assert "Build or send cloud digest mail" in note_workflow
    assert "cloud_mail_digest.py --output-dir outputs" in note_workflow
    assert "cloud_mail_digest.py --output-dir outputs --dry-run" in note_workflow
    assert "outputs/screening_pullback_*.csv" in note_workflow
    assert "send_mail:" in daily_workflow
    assert "SEND_GMAIL" in daily_workflow
    assert "python daily_discipline_run.py --send-gmail" in daily_workflow
    assert "GMAIL_USER secret is missing" in daily_workflow
    assert "workflow_dispatch:" in resend_workflow
    assert "send_mail:" in resend_workflow
    assert "rm -rf outputs" in resend_workflow
    assert "daily-discipline.yml" in resend_workflow
    assert "note_draft_cloud.yml" in resend_workflow
    assert "cloud_mail_digest.py --output-dir outputs" in resend_workflow
    assert "cloud_mail_digest.py --output-dir outputs --dry-run" in resend_workflow
    print("self-test: cloud_digest_mail(25MAメール・手動再送) OK")


def _test_metron_kpi() -> None:
    """メトロンはnote4本・運用・実績KPIを捏造せず日次レポートに集計する。"""
    import tempfile
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from metron_kpi import build_kpi, render_markdown, write_outputs

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        out = root / "outputs"
        data = root / "data"
        out.mkdir()
        data.mkdir()
        pd.DataFrame(
            [
                {"code": "1111", "name": "A社", "rank": "S", "score": 91, "dist_52w_high_pct": 1.2, "reason": "テスト"},
                {"code": "2222", "name": "B社", "rank": "A", "score": 80, "dist_52w_high_pct": 3.4, "reason": "テスト"},
            ]
        ).to_csv(out / "screening_result.csv", index=False)
        pd.DataFrame(
            [
                {"code": "1111", "name": "A社", "decision": "BUY", "score": 91, "rank": "S"},
                {"code": "2222", "name": "B社", "decision": "WATCH", "score": 80, "rank": "A"},
            ]
        ).to_csv(out / "decision_result.csv", index=False)
        pd.DataFrame(
            [
                {"slot": 1, "action": "BUY", "code": "1111", "name": "A社"},
                {"slot": 2, "action": "CASH", "cash_reason": "残りは現金"},
            ]
        ).to_csv(out / "discipline_result.csv", index=False)
        (out / "warren_summary.json").write_text(
            json.dumps(
                {
                    "capital": 3000000,
                    "regime": "NORMAL",
                    "buy_count": 1,
                    "watch_count": 1,
                    "skip_count": 0,
                    "cash_count": 1,
                    "selected_symbols": ["1111"],
                    "cash_reason": "残りは現金",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        manifest = []
        for key in ["chatgpt", "claude", "pullback", "highs"]:
            manifest.append({"key": key, "title": f"{key}記事", "md_file": f"note_{key}.md", "title_file": f"note_{key}_title.txt", "html_file": f"note_{key}.html"})
            (out / f"note_{key}.md").write_text("本文", encoding="utf-8")
            (out / f"note_{key}_title.txt").write_text("タイトル", encoding="utf-8")
            (out / f"note_{key}.html").write_text("<p>本文</p>", encoding="utf-8")
        (out / "note_drafts_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        (out / "performance_report.md").write_text("# 実績", encoding="utf-8")
        pd.DataFrame(
            [{"date": "2026-07-13", "code": "1111", "ticker": "1111.T", "name": "A社", "entry_close": 1000}]
        ).to_csv(data / "highs_track_record.csv", index=False)

        kpi = build_kpi(output_dir=out, data_dir=data, now=datetime(2026, 7, 14, 17, 45, tzinfo=ZoneInfo("Asia/Tokyo")))
        assert kpi["employee"] == "メトロン"
        assert kpi["overall_status"] == "OK"
        assert kpi["research"]["s_rank_count"] == 1
        assert kpi["operations"]["buy_count"] == 1
        assert kpi["editorial"]["ready"] == 4
        report = render_markdown(kpi)
        assert "メトロン日次KPIレポート" in report
        assert "note4本チェック" in report
        for banned in ("NaN", "None", "null", "inf"):
            assert banned not in report
        md_path, json_path = write_outputs(kpi, output_dir=out)
        assert md_path.exists()
        assert json_path.exists()

    project_root = Path(__file__).resolve().parent
    metron_workflow = (project_root / ".github" / "workflows" / "metron_kpi.yml").read_text(encoding="utf-8")
    assert "Metron KPI Report" in metron_workflow
    assert "python3 metron_kpi.py" in metron_workflow
    assert 'cron: "45 8 * * 1-5"' in metron_workflow
    assert "actions/upload-artifact@v7" in metron_workflow
    note_workflow = (project_root / ".github" / "workflows" / "note_draft_cloud.yml").read_text(encoding="utf-8")
    assert "Metron KPI report" in note_workflow
    assert "outputs/metron_kpi_report.md" in note_workflow
    assert "outputs/metron_kpi.json" in note_workflow


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


def _test_paper_open_fill() -> None:
    from datetime import date

    from paper_open_fill import fill_open_entries, is_jpx_business_day, mark_to_market, portfolio_view_for_note

    assert not is_jpx_business_day(date(2026, 7, 20))
    assert is_jpx_business_day(date(2026, 7, 21))

    discipline = pd.DataFrame(
        [
            {
                "slot": 1,
                "action": "BUY",
                "regime": "NORMAL",
                "code": "7453",
                "ticker": "7453.T",
                "name": "良品計画",
                "rank": "S",
                "score": 100,
                "entry_price": 4259,
                "rule": "Sランクのみ",
            }
        ]
    )
    journal, fills = fill_open_entries(
        discipline,
        pd.DataFrame(),
        date(2026, 7, 21),
        price_fetcher=lambda ticker, trading_date: 4300.0,
        fill_time_jst="2026-07-21T09:40:00+09:00",
    )
    assert fills[0]["status"] == "FILLED"
    assert float(journal.iloc[0]["entry_price"]) == 4300.0
    assert int(journal.iloc[0]["shares"]) == 200

    screening = pd.DataFrame([{"code": "7453", "current_price": 4400.0}])
    marked = mark_to_market(journal, screening)
    assert int(marked.iloc[0]["unrealized_pnl"]) == 20000

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "paper_trade_journal.csv"
        marked.to_csv(path, index=False)
        view = portfolio_view_for_note(discipline, screening, path)
        assert int(view.iloc[0]["market_value"]) == 880000
        assert int(view.iloc[0]["unrealized_pnl"]) == 20000

    project_root = Path(__file__).resolve().parent
    workflow = (project_root / ".github" / "workflows" / "paper-open-fill.yml").read_text(encoding="utf-8")
    note_workflow = (project_root / ".github" / "workflows" / "note_draft_cloud.yml").read_text(encoding="utf-8")
    assert 'cron: "40 0 * * 1-5"' in workflow
    assert "paper_open_fill.py --output-dir outputs --journal data/paper_trade_journal.csv" in workflow
    assert "Reflect paper open portfolio" in note_workflow
    print("self-test: paper_open_fill(寄り付き約定) OK")


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


def _test_jpx_business_day_calendar() -> None:
    """T-K修正: 日本の祝日・年末年始対応のJPX営業日カレンダー。"""
    from datetime import date

    from jptime import is_jpx_business_day, jp_holidays, prev_jpx_business_day

    holidays_2026 = jp_holidays(2026)
    # 固定祝日・ハッピーマンデー・春分/秋分
    assert date(2026, 1, 1) in holidays_2026        # 元日
    assert date(2026, 1, 12) in holidays_2026       # 成人の日（1月第2月曜）
    assert date(2026, 2, 11) in holidays_2026       # 建国記念の日
    assert date(2026, 2, 23) in holidays_2026       # 天皇誕生日
    assert date(2026, 3, 20) in holidays_2026       # 春分の日
    assert date(2026, 7, 20) in holidays_2026       # 海の日（7月第3月曜）
    assert date(2026, 9, 21) in holidays_2026       # 敬老の日
    assert date(2026, 9, 23) in holidays_2026       # 秋分の日
    assert date(2026, 9, 22) in holidays_2026       # 国民の休日（祝日に挟まれた平日）
    assert date(2026, 5, 6) in holidays_2026        # 振替休日（5/3憲法記念日が日曜）
    assert date(2026, 10, 12) in holidays_2026      # スポーツの日

    # 営業日判定: 祝日・年末年始は休場、通常平日は営業日
    assert not is_jpx_business_day(date(2026, 7, 20))   # 海の日（月曜だが休場）
    assert not is_jpx_business_day(date(2026, 12, 31))  # 大納会翌日（年末休場）
    assert not is_jpx_business_day(date(2027, 1, 2))    # 年始休場
    assert not is_jpx_business_day(date(2026, 7, 18))   # 土曜
    assert is_jpx_business_day(date(2026, 7, 17))       # 通常の金曜
    assert is_jpx_business_day(date(2026, 12, 30))      # 大納会（営業日）

    # 直近営業日: 祝日・年末年始をまたいで正しく遡る
    assert prev_jpx_business_day(date(2026, 7, 20)) == date(2026, 7, 17)   # 海の日→金曜
    assert prev_jpx_business_day(date(2026, 7, 19)) == date(2026, 7, 17)   # 日曜→金曜
    assert prev_jpx_business_day(date(2027, 1, 3)) == date(2026, 12, 30)   # 年始→大納会
    assert prev_jpx_business_day(date(2026, 5, 6)) == date(2026, 5, 1)     # GW連休→5/1
    print("self-test: jpx_business_day(祝日・年末年始カレンダー) OK")


def _test_holiday_target_date_and_skip_guard() -> None:
    """T-K修正: 祝日に記事日付が祝日当日にならない＋休場日の重複下書き防止ガード。"""
    from datetime import date

    import jptime
    import note_draft
    from note_autosave import should_skip_autosave

    # (1) 祝日（2026-07-20 海の日）に生成しても、タイトルは直近取引日 7/17 になる
    original_jst_today = jptime.jst_today
    jptime.jst_today = lambda: date(2026, 7, 20)
    try:
        # データなしフォールバック: 直近JPX営業日
        assert note_draft._prev_jst_business_day() == date(2026, 7, 17)
        empty = pd.DataFrame()
        text = note_draft.build_highs_note(empty, Path("screening_highs_test.csv"))
        first_line = text.splitlines()[0]
        assert first_line == "# 2026年7月17日 52週新高値 接近・到達銘柄", first_line
        assert "2026年7月20日 52週新高値" not in text  # 祝日当日がタイトルにならない
        assert "直近取引日" in text  # 生成日と対象日が違う旨を明記
        # データがある場合は data_date が最優先（既存挙動の回帰確認）
        row = {"code": "1111", "name": "テスト", "data_date": "2026-07-17"}
        with_data = pd.DataFrame([row])
        text2 = note_draft.build_highs_note(with_data, Path("screening_highs_test.csv"))
        assert text2.splitlines()[0] == "# 2026年7月17日 52週新高値 接近・到達銘柄"
    finally:
        jptime.jst_today = original_jst_today

    # (2) 休場日ガード: schedule実行×休場日のみスキップ。手動実行・営業日は保存する
    skip, reason = should_skip_autosave("schedule", date(2026, 7, 20))
    assert skip and "休場" in reason  # 祝日
    skip, _ = should_skip_autosave("schedule", date(2027, 1, 1))
    assert skip  # 年末年始
    skip, _ = should_skip_autosave("schedule", date(2026, 7, 17))
    assert not skip  # 通常営業日は保存
    skip, _ = should_skip_autosave("workflow_dispatch", date(2026, 7, 20))
    assert not skip  # 手動実行は祝日でも保存（検証用）
    print("self-test: holiday_target_date_and_skip_guard(祝日対応) OK")


def _test_openwork_manual_reflection_contract() -> None:
    """T-K修正: OpenWork月次workflowの契約テスト。
    ①空欄・異常値で既存正常値を消さない ②外部サイトへ自動アクセスしない
    ③時刻表記が9:00 JSTで統一されている。"""
    import tempfile
    from datetime import date

    import openwork_cache

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cache_path = tmp_path / "openwork_cache.csv"
        record_path = tmp_path / "highs_track_record.csv"
        scores_path = tmp_path / "openwork_scores.csv"
        today = date(2026, 7, 1)

        # 既存キャッシュ: 1111は正常値（古い=更新対象）
        pd.DataFrame([
            {"code": "1111", "name": "既存社", "overall": 4.0, "respondents": 120,
             "fetched_at": "2026-04-01", "status": "ok"},
        ]).to_csv(cache_path, index=False)
        record_path.write_text("date,code\n2026-06-20,1111\n2026-06-20,2222\n", encoding="utf-8")

        # scores.csv: 1111は空欄と異常値のみ（→既存値を消してはいけない）、2222は異常値のみ
        scores_path.write_text(
            "code,openwork_score,treatment,respondents\n"
            "1111,,9.9,\n"
            "2222,0.2,,-5\n",
            encoding="utf-8",
        )
        fetcher = lambda codes: openwork_cache.manual_source_fetcher(codes, scores_path=scores_path, today=today)  # noqa: E731
        stats = openwork_cache.update_cache(cache_path=cache_path, record_path=record_path, fetcher=fetcher, today=today)
        assert stats["updated"] == 0, stats  # 空欄・異常値は「取得成功」にしない
        after = openwork_cache.load_cache(cache_path)
        kept_row = after[after["code"] == "1111"].iloc[0]
        assert float(kept_row["overall"]) == 4.0          # 既存正常値を保持
        assert str(kept_row["fetched_at"]) == "2026-04-01"  # 取得日も前回のまま（古さが分かる）
        assert str(kept_row["status"]) == "ok"
        bad_row = after[after["code"] == "2222"].iloc[0]
        assert str(bad_row["status"]) == "unavailable"    # 異常値のみ→取得できず扱い

        # 妥当な値なら反映される（範囲1.0〜5.0）
        scores_path.write_text("code,openwork_score,respondents\n1111,4.3,150\n", encoding="utf-8")
        stats2 = openwork_cache.update_cache(cache_path=cache_path, record_path=record_path, fetcher=fetcher, today=today)
        assert stats2["updated"] == 1, stats2
        after2 = openwork_cache.load_cache(cache_path)
        assert float(after2[after2["code"] == "1111"].iloc[0]["overall"]) == 4.3

    # 外部アクセスなし契約: モジュールにも月次workflowにも通信手段が存在しない
    project_root = Path(__file__).resolve().parent
    module_src = (project_root / "openwork_cache.py").read_text(encoding="utf-8")
    for banned in ("import requests", "import urllib", "import httpx", "import aiohttp",
                   "playwright", "selenium", "http.client", "socket"):
        assert banned not in module_src, f"openwork_cache.py に通信手段が存在: {banned}"

    workflow_path = project_root / ".github" / "workflows" / "openwork_monthly.yml"
    if workflow_path.exists():
        workflow_text = workflow_path.read_text(encoding="utf-8")
        for banned in ("playwright", "curl ", "wget ", "requests", "yfinance"):
            assert banned not in workflow_text, f"openwork_monthly.yml に外部アクセス手段: {banned}"
        assert "openwork_cache.py --update" in workflow_text
        assert 'cron: "0 0 1 * *"' in workflow_text       # 毎月1日 0:00 UTC
        assert "9:00 JST" in workflow_text                # 表記は9:00 JSTで統一
        assert "6:00 JST" not in workflow_text            # 旧表記が残っていない
        assert "自動取得」ではありません" in workflow_text  # 方式の明記
    print("self-test: openwork_manual_reflection(空欄保持・外部アクセスなし・9:00JST統一) OK")


if __name__ == "__main__":
    main()
