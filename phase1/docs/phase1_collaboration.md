# フェーズ1 協働仕様書（Claude × Codex）

対象: 配信（X／LINE）＋登録導線＋メトロン最小起動
作成: 2026-07-20（Fable／Claude）｜合意ベース: Codex返信 2026-07-20
ステータス: 合意済み・今週分の成果物を本書に集約

---

## 0. 目的

毎日作れている4本のnoteを、実際に読者へ届け（配信）→ 手元にリスト化し（登録導線）→ 効果を数字で見る（測定）ループを立ち上げる。実投稿は当面「半自動（自動下書き→高重さんが確認して投稿）」。

## 1. 役割分担（合意済み）

### Claude（Fable）担当
- SNS・LINE向け配信文の生成
- 媒体ごとの文体調整
- メトロン最小版の設計
- KPI集計と改善案
- 配信文の公開前校閲（compliance_check.py ゲート）

### Codex担当
- メール登録導線
- 300万円運用メールとの接続
- GitHub Actionsによるクラウド実行
- 重複送信防止、テスト文除外、障害記録
- 配信文に銘柄チャートリンクを付与

## 2. 重要ルール（両者厳守）

- 実投稿は当面**半自動**。SNS／LINEへの完全自動投稿は、内容確認と高重さんの承認後に導入。
- メールとnoteは**Codex側の既存クラウド基盤**を使う。
- 同じ配信をClaude系とCodex系の**両方から送らない**（重複禁止）。
- 本番変更前に最新の `main` を取り込む。
- Codexの**300万円運用3ファイルは削除・置換しない**。
- テストメール・テンプレート文（プレースホルダ `{{...}}` 含む）は**外部送信しない**。
- 高重さん確認前にGitHubへ直接pushしない。変更はClaude専用ブランチ（`claude/phase1`）で作り、**PRで共有**。
- 捏造しない（不足は「取得できず／未公表／算出不可」と明記、NaN/None/null/inf禁止）。
- noteは必ず4本、31役割は追加・削除・改名しない。カブタンはスクレイピング禁止・答え合わせのみ。

> 補足（Fableの実行制約）: Claude側の実行環境からは直接 push／PR作成ができません。Claudeは repo 配置そのままの形でファイルを用意し、高重さんが `claude/phase1` ブランチにコミット→PRを作成します。これによりmain直上書き禁止ルールを満たします。

## 3. 受け皿（登録導線）

第1候補は**メール**（既存Gmail配信基盤を活用、配信履歴とクリック導線を管理しやすい）。**LINEは第2段階**で追加。

## 4. UTM規約（合意済み）

すべての登録導線リンクに付与する。

| パラメータ | 値 |
|---|---|
| `utm_source` | x / line / note / email |
| `utm_medium` | social / messaging / article / newsletter |
| `utm_campaign` | phase1_YYYYMM（例: phase1_202607） |
| `utm_content` | morning / ma25 / ma200 / close / chatgpt300 / claude300 ※ |

リンク形式:
`{{BASE_URL}}?utm_source={source}&utm_medium={medium}&utm_campaign=phase1_YYYYMM&utm_content={content}`

チャネル→source/medium 対応:
- X: source=x / medium=social
- LINE: source=line / medium=messaging
- note: source=note / medium=article
- メール: source=email / medium=newsletter

※ `utm_content` は Codex 回答で確定（2026-07-20）。300万円運用本体は `chatgpt300`、Claudeが作る紹介・誘導文は `claude300` として分離する。

## 5. 最初のKPI（メトロン最小版・合意済み）

主要3指標:
1. 登録数
2. リンククリック数・クリック率
3. note閲覧または登録への転換数

補助指標: 配信数（入力側）。詳細な項目定義は `metron/kpi_min_schema.md` を参照。初期は手動記録から始め、後でAPI接続。

## 6. 配信トーン（合意済み）

- **SNS（X）**: 短く、結論と銘柄を先に表示。
- **LINE**: 要点3行＋詳細リンク。
- **メール**: 根拠・価格・チャート・注意事項まで掲載。
- **note**: 運用記録として読み物形式。

テンプレート実物は `templates/distribution_templates.md`。

## 7. 今週の成果物（Claude→本PRで提出）

- [x] 4種類の配信文テンプレート … `templates/distribution_templates.md`
- [x] メトロン最小版の項目定義 … `metron/kpi_min_schema.md`
- [x] 本番投稿前の確認チェック … `docs/pre_publish_checklist.md`（校閲ツール compliance_check.py と併用）
- [x] 仕様と担当範囲の記録 … 本書 `docs/phase1_collaboration.md`

Codexは本内容を受けて、メール導線・重複防止・クラウド実行（GitHub Actions）へ接続。

## 8. 確定事項（2026-07-20・Codex回答で合意）

1. **`utm_content=claude300` を追加**。300万円運用本体は `chatgpt300`、Claudeが作る紹介・誘導文は `claude300`。
2. **半自動配信の一本化ルート（確定）**:
   Claudeが配信文生成 → `compliance_check.py` で機械確認 → GitHub上の成果物へ保存 → 人が内容確認 → Codex側のメール基盤から本番配信。
   SNS・LINEは当面、確認後に手動投稿。Claude系とCodex系から同じ内容を二重送信しない。
3. **クリック計測は手動記録から開始**。CSVへ手入力 → 実データが貯まってから自動取得へ。最初から複雑なAPI連携は入れない。

一本化フロー:
```
Claude: 配信文生成
   → phase1/tools/compliance_check.py（exit 0 必須）
   → GitHub成果物へ保存
   → 人が内容確認（pre_publish_checklist.md）
   → Codexのメール基盤から本番配信
   （SNS/LINEは確認後に手動投稿）
```

## 9. 担当割り当て（2026-07-20・ダッシュボードv11に反映）

| 対象 | 担当AI | 稼働状態 | 配信権限 |
|---|---|---|---|
| ツイート（X） | Claude | 半自動 | 人の承認後 |
| LINE | Claude | 設計中 | 下書きのみ |
| メトロン | Claude | 半自動 | — |
| メール／ポスト | Codex | クラウド稼働 | — |
| 300万円運用 | Codex | クラウド稼働 | — |
| シールド（校閲） | Codex | 半自動（クラウド接続前） | — |
| 協働仕様・投稿前チェック | 共同 | — | — |

表示項目（各役割カード）: 担当AI／稼働状態／配信権限／最終成功日時／次の実行予定／エラー状態。
実測のない項目（最終成功日時・エラー状態など）は「未記録」と表示し、値を捏造しない。

## 10. セキュリティ原則（両者厳守）

- メールアドレス・パスワード・APIキー等の秘密情報を **HTML／Markdown／CSV に書かない**。
- 認証情報は各実行環境のシークレット管理（GitHub Secrets等）に置き、成果物ファイルには載せない。
