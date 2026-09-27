# Claude向け指示：OpenWork共用データ運用

このリポジトリでは、OpenWorkの確認済み数値をCodex版・Claude版の300万円運用noteで共用します。以下を守って作業してください。

## 正本

- 共用フォルダー：`data/openwork_shared/`
- 共用CSV：`data/openwork_shared/openwork_scores.csv`
- 列名や保存場所を変更しないでください。
- 旧 `data/openwork_scores.csv` は移行用です。新規値は共用CSVへ保存してください。

## 対象

OpenWorkを確認するのは、当日の300万円運用noteに実際に掲載する銘柄だけです。

- 保有銘柄
- 翌営業日の売買宣言銘柄
- note掲載候補TOP10

同じ証券コードは1社として扱います。市場全体やメール掲載銘柄全件の巡回は行いません。

## 保存項目

次の列を使用してください。

```text
code,name,openwork_score,overall,treatment,morale,openness,growth_20s,longterm,compliance,evaluation,respondents,fetched_at,source_url,status
```

- `code`：証券コード
- `name`：会社名
- `openwork_score`：総合評価
- `overall`：総合評価
- `treatment`：待遇面の満足度
- `morale`：社員の士気
- `openness`：風通しの良さ
- `growth_20s`：20代成長環境
- `longterm`：人材の長期育成
- `compliance`：法令順守意識
- `evaluation`：人事評価の適正感
- `respondents`：回答者数
- `fetched_at`：確認日（`YYYY-MM-DD`）
- `source_url`：確認したOpenWork企業ページ
- `status`：正常に確認できた場合は `ok`

## 更新規則

1. OpenWorkで正規に表示・確認できた値だけを保存してください。推測・補完・捏造は禁止です。
2. 自動ログイン、アクセス制限の回避、CAPTCHA回避、大量巡回は行わないでください。
3. 新しい正常値を確認できた会社だけ更新してください。
4. 未取得、空欄、異常値で過去の正常値を消さないでください。
5. 評価値は1.0〜5.0、回答者数は1以上の整数だけを受け入れてください。
6. 同じ証券コードは1行に統合し、新しい確認済み値を残してください。
7. `fetched_at` と `source_url` を必ず残してください。
8. 既存コード、既存行、他者の変更を勝手に削除しないでください。

## noteへの掲載

- 共用CSVに確認済み値があれば、前回値を再利用してください。
- note本文に出すのは確認済みのOpenWork評価だけです。
- 未確認銘柄へ架空の値や「未取得」の大量表示を追加しないでください。
- Codex版とClaude版は同じ共用CSVを読みます。別のOpenWorkファイルを作らないでください。

## 作業後の報告

次を簡潔に報告してください。

- note掲載対象数
- 今回新たに確認できた数
- 過去値を再利用した数
- 未確認数
- 更新した証券コード一覧
- 変更したファイル一覧
- 実行した検査と結果

変更は差分を提示し、明示的な許可なしにmainへ直接pushしないでください。
