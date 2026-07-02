# 300万円運用向け日本株スクリーナー

日本株の個別株を対象に、300万円程度の資金で候補にしやすい銘柄を抽出するスクリーナーです。

## 条件

- ETF・ETN・REITを除外
- 東証プライム・スタンダード・グロースの個別株のみ
- 52週高値15%以内
- MA25 / MA75 / MA200を上回る
- 20日平均売買代金1億円以上
- 出来高比 5日平均 / 20日平均
- カップウィズハンドルは追加シグナル
- 決算14営業日前から決算翌営業日は除外
- 決算未確認は最大A
- S / A / B / 見送り判定
- 100株購入額と300万円内で買える候補銘柄数の目安を出力
- CSV保存
- Streamlit表示

## セットアップ

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## CLI

正式運用フォルダは`/Users/user/stock-news-monitor`です。`scan_kit_v3`ではなく、このフォルダから実行します。

まず少数銘柄で動作確認します。

```bash
python run_screening.py --limit 20
```

全市場を対象にする場合:

```bash
python run_screening.py
```

CSVは`outputs/`に保存されます。

### CSV保存先

生成CSVは正式運用フォルダ配下の`outputs/`に保存されます。`outputs/*.csv`はGit管理対象外です。

- スクリーニング結果: `outputs/screening_result_YYYYMMDD_HHMMSS.csv`
- Sランク精査用: `outputs/s_rank_candidates_YYYYMMDD_HHMMSS.csv`
- 規律版ポートフォリオ: `outputs/discipline_portfolio_YYYYMMDD_HHMMSS.csv`
- 売買履歴: `outputs/trade_journal.csv`
- パターン集計: `outputs/pattern_summary.csv`

Gmail通知なしでローカル確認する場合:

```bash
ls -lt outputs/*.csv | head
python daily_discipline_run.py --limit 20
python paper_portfolio_discipline.py
python pattern_learn.py
```

全銘柄を処理する場合は`--limit`を外します。

```bash
python daily_discipline_run.py
```

全銘柄処理はJPX/yfinanceへの通信量が多く、数十分以上かかる場合があります。途中でJPX銘柄一覧を取得できない場合は、不完全な過去CSVへフォールバックせずエラーで停止します。

日次実行ではS/A/B候補を最大20件に絞って保存します。件数を変える場合は`--max-candidates 10`から`--max-candidates 20`を目安に指定します。全件保存したい場合は`--max-candidates 0`を指定します。

strictモードではSランクをさらに絞ります。

```bash
python daily_discipline_run.py --limit 500 --max-candidates 20 --strict
```

strict Sゲート:

- 52週高値更新、または52週高値まで1%以内
- 出来高倍率1.5倍以上
- MA25上向き
- MA75上向き
- 株価がMA25/MA75/MA200より上
- 20日平均売買代金1億円以上

実行ログには候補件数を出します。

```text
candidate_summary total=... S=... A=... B=...
strict_mode=True/False
s_rank_summary 本日はSランクなし
s_rank_gate ma25_rising=Falseまたはma75_rising=Falseの銘柄はSランクになりません
s_rank_details
S <code> <name> score=<score> reason=<reason>
```

### スクリーニングCSV列

- `code` / `ticker` / `name`: 証券コード、yfinanceティッカー、銘柄名
- `market` / `sector`: 市場、業種
- `current_price`: 現在値
- `high_52w` / `dist_52w_high_pct` / `days_since_52w_high`: 52週高値、現在値との距離、52週高値からの経過営業日
- `ma25` / `ma75` / `ma200`: 移動平均
- `ma25_slope` / `ma75_slope`: 直近5営業日の移動平均変化量
- `ma25_rising` / `ma75_rising`: 移動平均が上向きか
- `ma25_gap_pct` / `ma75_gap_pct` / `ma200_gap_pct`: 現在値と各移動平均の乖離率
- `ma200_touch_pct`: MA200との距離
- `volume_ratio_5d_20d`: 5日平均出来高 / 20日平均出来高
- `turnover_20d`: 20日平均売買代金
- `lot_value_100`: 100株購入額
- `cwh_signal` / `breakout_price` / `pct_to_breakout` / `cup_depth_pct` / `handle_depth_pct`: カップウィズハンドル関連
- `earnings_status` / `earnings_date` / `exclude_for_earnings` / `earnings_note`: 決算確認と除外判定
- `score` / `rank` / `max_positions_3m` / `reason`: スコア、判定、300万円内での購入可能単元目安、理由

Sランクはスコア合計だけではなく、以下のゲートをすべて満たす場合のみ付与します。未達の場合は最大Aになります。

- 52週高値3%以内
- MA25/75/200上
- MA25上向き
- MA75上向き
- 当日出来高が20日平均超
- 20日平均売買代金1億円以上

### Sランク精査CSV列

- `code`: 証券コード
- `name`: 銘柄名
- `current`: 現在値
- `score`: スコア
- `distance_to_52w_high_pct`: 52週高値までの距離
- `ma25_rising`: MA25上向き
- `ma75_rising`: MA75上向き
- `volume_ratio`: 出来高倍率
- `turnover_20d_avg`: 20日平均売買代金
- `reason`: 判定理由

### 規律版CSV列

- `slot`: 1〜3の運用枠
- `action`: `BUY`または`CASH`
- `regime`: 地合い
- `code` / `ticker` / `name`: 銘柄情報
- `rank` / `score`: スクリーニング判定
- `entry_price` / `shares` / `position_value`: エントリー価格、株数、投入額
- `stop_loss` / `take_profit` / `timeout_date`: 損切り、利確、10営業日タイムアウト日
- `rule` / `cash_reason`: 適用ルール、現金保有理由

## 300万円規律版

地合いは`market_regime.py`に表示される`REGIME_TXT_URL`を優先し、取得できない場合は正式運用フォルダ直下の`regime.txt`を正本として参照します。

- `NORMAL`
- `CAUTION`
- `RISK`
- `STOP`

`STOP`の場合は新規買いを停止し、規律版ポートフォリオは現金保有になります。

```bash
python paper_portfolio_discipline.py
```

規律:

- 最大3銘柄
- 1枠100万円
- Sランクのみ
- Sランク不足は現金保有
- 損切7%
- 利確15%
- 10営業日タイムアウト

手動で一括確認する場合:

```bash
python daily_discipline_run.py --limit 20
```

売買記録と集計:

```bash
python trade_journal.py entry --csv outputs/discipline_portfolio_YYYYMMDD_HHMMSS.csv --row 0 --regime NORMAL --shares 100
python trade_journal.py exit --trade-id <trade_id> --exit-price 1150 --exit-reason take_profit
python pattern_learn.py
```

## バックテスト

実データバックテストは、300万円運用に合わせて資金管理を反映します。

- 初期資金300万円
- 最大3銘柄を同時保有
- 1銘柄100万円以内
- 100株単位
- S級エントリー条件のみ
- -7%損切り
- 同一銘柄の重複エントリー禁止

20営業日保有:

```bash
python backtest.py --run --limit 50 --timeout-bdays 20
```

40営業日保有:

```bash
python backtest.py --run --limit 50 --timeout-bdays 40
```

`^TPX`や個別銘柄の価格取得に失敗した場合は、取得できる代替データを使うか、その銘柄だけログ出力して処理を継続します。

## GitHub Actions

`.github/workflows/daily-discipline.yml`で、平日朝07:30 JSTに実行できます。GitHub ActionsのcronはUTCなので、`22:30 UTC`を指定しています。

生成CSVはGitへコミットせず、ActionsのArtifactsとしてアップロードします。手動実行もできます。

### Gmail通知

Gmail通知を使う場合は、GitHub Secretsに以下を設定します。

- `GMAIL_USER`: Gmailアドレス
- `GMAIL_APP_PASSWORD`: Gmailのアプリパスワード
- `MAIL_TO`: 通知先メールアドレス

Secretsが未設定の場合、Gmail通知はスキップされ、CSV生成だけ実行されます。

ローカルでGmail送信テストする場合:

```bash
GMAIL_USER="your@gmail.com" \
GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx" \
MAIL_TO="to@example.com" \
python daily_discipline_run.py --limit 20 --send-gmail
```

通知メールの件名:

```text
【DUKEシステム】本日のS/A/B候補 YYYY-MM-DD
```

メール本文にはS/A/B候補をスマホで見やすい短いブロック形式で表示し、末尾に以下を入れます。

- Sランク: 最大5件
- Aランク: 最大10件
- Bランク: 最大10件
- Sランクが0件の日は「本日はSランクなし」と表示

```text
※これは投資助言ではなく、スクリーニング結果です。売買判断は自己責任で行ってください。
```

GitHub Actionsのログには以下のいずれかが出ます。

- `gmail_notification=sent ...`
- `gmail_notification=skipped reason=missing_secrets ...`
- `gmail_notification=skipped reason=disabled`

## Streamlit

```bash
streamlit run app.py
```

## 注意

決算日は`yfinance`から取得できる場合のみ確認します。取得できない銘柄は「決算未確認」として最大Aに制限します。


## GitHub Actionsで日本株スクリーニングを確認する（Mac不要）

Macを使わずGitHub Actionsだけで毎日安定実行するための専用ワークフローを追加しました：
`.github/workflows/screening.yml`（ワークフロー名: **JP Screening (QUICK / FULL)**）。
全銘柄(約3,700)を1件ずつyfinanceで取得すると1時間近くかかるため、まず上位30銘柄だけの
**QUICK_MODE**で5分以内の安定完了を確認し、本番(全銘柄)はその後に分けて回します。

### 環境変数

| 変数 | 意味 | 既定 |
| --- | --- | --- |
| `QUICK_MODE` | `true`で上位 `MAX_SYMBOLS` 銘柄だけ処理（軽量テスト）。`false`で全銘柄（本番） | スケジュール実行は `true` |
| `MAX_SYMBOLS` | QUICK_MODE時に処理する銘柄数 | `30` |
| `PROGRESS_EVERY` | 何銘柄ごとに経過時間ログを出すか | `25` |

`QUICK_MODE` / `MAX_SYMBOLS` は `run_screening.py` 内の `resolve_symbol_limit()` が解釈します。
`daily_discipline_run.py`（本番経路）は常に `limit=None`（全銘柄）ですが、`QUICK_MODE=true` を
渡せば内部で自動的に `MAX_SYMBOLS` 件に絞られるため、同じコードのまま軽量テストできます。

### QUICKテスト（まず5分以内の成功を確認）

1. GitHubの `Actions` タブを開く
2. 左の `JP Screening (QUICK / FULL)` を選ぶ
3. `Run workflow` を押し、`quick_mode` を `true`（既定）のまま実行
4. ログで各ステップの所要時間を確認する：
   - `[timing] universe_load: ...s rows=...`
   - `[timing] progress N/30 elapsed=...s eta=...s`
   - `[timing] scan_loop: ...s symbols=30 ...`
   - `[timing] run_screening_total: ...s candidates=...`
5. `timeout-minutes: 30` を設定済み（QUICKは通常5分以内、安全弁として30分）
6. Artifacts の `screening-result` をダウンロードし、`outputs/screening_result.csv` が
   含まれることを確認する（候補0件でもヘッダー付きの空CSVが必ず保存されます）

QUICKワークフローは取得失敗やデータ不足があっても途中で止まらず（全例外を捕捉）、
`screening_result.csv` を必ず残してから終了します（Actionsが赤くならず安定）。

### 本番（全銘柄）

`JP Screening (QUICK / FULL)` を手動実行する際に `quick_mode` を `false` にすると全銘柄を処理します
（時間がかかります）。Gmail通知やnote.com保存まで含めた従来のフルパイプラインは
`.github/workflows/daily-discipline.yml`（平日朝07:30 JST）をそのまま使います。

### ローカルでの確認

```bash
python3 self_test.py                                  # ネット不要・常に通る
QUICK_MODE=true MAX_SYMBOLS=30 python3 run_screening.py  # 上位30銘柄で軽量実行
```


### GitHub Actions QUICK_MODE確認

Actionsの初期確認は全銘柄ではなく30銘柄で短時間完了させます。
workflowには以下を設定しています。

```yaml
env:
  QUICK_MODE: "true"
  MAX_SYMBOLS: "30"
timeout-minutes: 30
```

確認手順:

1. GitHubの `Actions` タブを開く
2. `Daily Discipline Screening` を選ぶ
3. `Run workflow` を押す
4. ログで `[TIMER] install_dependencies` / `[TIMER] daily_discipline_run` / `[TIMER] note_draft` を確認する
5. Artifacts の `stock-news-monitor-outputs` をダウンロードする
6. `screening_result.csv` または `screening_result_YYYYMMDD_HHMMSS.csv` が含まれることを確認する

QUICK_MODEではnote.com保存とGmail通知はスキップします。まず5分以内の安定完了を確認し、その後に `QUICK_MODE=false` または環境変数削除で全銘柄運用へ戻します。
