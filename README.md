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

## Streamlit

```bash
streamlit run app.py
```

## 注意

決算日は`yfinance`から取得できる場合のみ確認します。取得できない銘柄は「決算未確認」として最大Aに制限します。
