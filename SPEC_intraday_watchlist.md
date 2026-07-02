# SPEC: 日中15分監視の軽量化（200〜500銘柄ウォッチリスト方式）

目的: `intraday_high_alert.py` を全銘柄（約3,567）ではなく、前日EODで選んだ
**200〜500銘柄のウォッチリスト**だけザラ場中に監視する。15分ごとの実行を30分制限内に確実に収める。
既存の全銘柄経路は消さず、ウォッチリストが無い日はフォールバックで全銘柄に戻す（フェイルセーフ・捏造しない）。

## 1. 選定ロジック（前日EODの結果だけで作る＝新規の通信ゼロ）

入力 = 前日の `run_screening.py --include-rejected` が出した全銘柄CSV
（`outputs/screening_result_<日付>.csv`。全銘柄・各指標つき）。

3条件の和集合（重複は除去）で選ぶ:

- A. 前日候補: `rank in {S, A, B}`
- B. 52週高値接近: `dist_52w_high_pct <= INTRADAY_NEAR_52W_PCT`（既定 5.0%）
- C. 売買代金上位: `turnover_20d` 降順の上位 `INTRADAY_TURNOVER_TOP` 件（既定 200）

上限 `INTRADAY_WATCH_MAX`（既定 300、200〜500にクリップ）を超えたら
**A → B → C の優先順**で埋める（高重さんの優先順位: 出来高＞52週＞候補ランク を踏まえ、
確度の高い前日候補を必ず残し、残り枠を高値接近・売買代金上位で埋める）。

出力: `intraday_watchlist.csv`（列: `code, name, market, reason, score, rank, dist_52w_high_pct, turnover_20d`）。
`reason` はどの条件で入ったか（例: `S候補` / `52週-2.1%` / `売買代金上位`）。
前日CSVが見つからない日は**何も書かない**（警告ログのみ）。

### 前提の小改修（EOD側）
現在 `outputs/screening_result_*.csv` に `turnover_20d` 列が無い（`turnover_20d` は
`scanner/highs.py` で内部計算済み・1億円ゲートに使用）。C条件のため、
`run_screening.py` の `DISPLAY_COLUMNS` に `turnover_20d`（と参考に `volume_ratio_5_20`）を
追加してEOD CSVに出力する。これは追加列なので既存の読み手を壊さない。

## 2. `intraday_high_alert.py` への組み込み

- `scan(markets, limit=None, watchlist_codes: set[str] | None = None)` を追加。
  `watchlist_codes` があれば `load_jpx_listed(...)` を `code in watchlist_codes` で絞ってから回す。
- `load_intraday_watchlist(path) -> set[str]` を追加（英数字4桁コードもそのまま保持＝`[0-9A-Z]{4}`）。
- 環境変数 `INTRADAY_USE_WATCHLIST`（既定 `1`）:
  - `1` かつ ウォッチリストが存在 → その銘柄だけ監視。
  - ウォッチリストが無い / `0` → **従来どおり全銘柄**（フェイルセーフ）。
- `--limit` / QUICK_MODE は手動テスト専用のまま（本番scheduleは全ウォッチリスト）。

## 3. スケジューリング（重要: GitHub Actions はジョブ間でファイル共有できない）

Mac（launchd）は同じフォルダを共有するのでEOD生成→日中読み込みがそのまま動く。
GitHub Actions は EODジョブと日中ジョブでファイルシステムが別 + `outputs/` は `.gitignore` 済み。
そのため日中ジョブが前日のEODを読めない。方式は次の2択（要・高重さん判断）:

- 方式X（推奨・自動コミット）: EODジョブ（`daily-discipline.yml`）の最後に
  `build_intraday_watchlist.py` を実行し、**追跡対象の固定パス**
  `screening/intraday_watchlist.csv`（`.gitignore` の対象外）に書いて、
  `GITHUB_TOKEN`（`permissions: contents: write`）で bot コミット&プッシュ。
  日中ジョブは checkout でこのファイルを取得して使う。1日1回の小さなコミットのみ。
- 方式Y（アーティファクト取得）: 日中ジョブが `dawidd6/action-download-artifact` 等で
  直近EODジョブの成果物CSVを取得し、`build_intraday_watchlist.py` をその場で実行。
  自動コミットは不要だが、ワークフロー間アーティファクト取得の設定が要る。

どちらも投稿・公開はしない（メール通知のみ）。まずMacで方式そのものを検証 → Actionsは方式Xで実装が素直。

## 4. 環境変数（すべて既定値ありで安全側）

| 変数 | 既定 | 意味 |
|---|---|---|
| `INTRADAY_USE_WATCHLIST` | `1` | 0でウォッチリスト無効（全銘柄に戻す） |
| `INTRADAY_WATCH_MAX` | `300` | ウォッチリスト最大件数（200〜500にクリップ） |
| `INTRADAY_NEAR_52W_PCT` | `5.0` | 52週高値からの距離しきい値(%) |
| `INTRADAY_TURNOVER_TOP` | `200` | 売買代金上位の採用件数 |

## 5. 検証（self_test に追加する不変条件）

- ウォッチリスト生成: ダミーEOD DataFrame → A/B/C の和集合・上限クリップ・優先順・重複除去を確認。
- 英数字コード（例 285A）がウォッチリストに残ること。
- 前日CSV無し → 空 + 警告（例外で落ちない）。
- `INTRADAY_USE_WATCHLIST=0` / ファイル無し → 全銘柄フォールバック。
- `intraday_high_alert.py` の scheduled 本番経路に `--limit` が無いこと（既存不変条件を踏襲）。
- `python3 -m py_compile` + `python3 self_test.py` が通ること。

## 6. 想定効果

3,567銘柄→約300銘柄で日中の取得が約1/12。15分間隔・30分制限に余裕。
確度の高い前日S/A/B候補と高値接近・売買代金上位を取りこぼさない。
全銘柄経路は残すので、ウォッチリストが壊れた日も自動で従来動作に戻る。
