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

まず少数銘柄で動作確認します。

```bash
python run_screening.py --limit 20
```

全市場を対象にする場合:

```bash
python run_screening.py
```

CSVは`outputs/`に保存されます。

## Streamlit

```bash
streamlit run app.py
```

## 注意

決算日は`yfinance`から取得できる場合のみ確認します。取得できない銘柄は「決算未確認」として最大Aに制限します。
