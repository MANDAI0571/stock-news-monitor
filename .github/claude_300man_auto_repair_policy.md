# Claude 300万円 安全な自動修復方針

対象は300万円Claudeペーパー口座のコードとGitHub Actionsだけです。実在口座へ注文せず、修復をmainへ直接反映しません。

## 起動条件

- 同じClaude workflowが同じ原因で2回連続失敗した
- 外部価格配信の一時障害、GitHub側の遅延、JPX休場だけでは起動しない
- ローカルまたは隔離worktreeで再現できる決定的な不具合だけを修復候補にする
- 同じ障害について開いている自動修復PRは1本まで

## 許可する処理

1. `origin/main`から`codex/claude-auto-repair-*`ブランチまたは隔離worktreeを作る
2. 原因を再現する回帰テストを先に追加する
3. 必要最小限のコード、workflow、テスト、説明だけを修正する
4. `self_test_groups.py --group critical`、対象テスト、workflow YAML解析、`git diff --check`を通す
5. `python3 claude_300man_repair_guard.py --base-ref origin/main`を通す
6. ブランチをpushし、根拠・再現方法・検査結果・残るリスクを書いたPRを作る
7. ユーザーへPRを報告し、人の承認を待つ

## 禁止事項

- mainへの直接push、自動マージ、自動承認
- `data/`配下（注文、約定、資金、設定、監視履歴を含む）の変更
- `docs/archive/`配下の過去記録の変更
- 売買ルール、資金、株数、期、rule hashの変更
- 失敗を隠すためのテスト削除、検査緩和、例外握り潰し
- Claude以外の注文や実在口座への操作
- secret、token、個人情報の表示またはPRへの保存

`claude_300man_declare.py`、`claude_300man_fill.py`、共有約定経路を変更するPRは、ガード通過後も重点レビュー対象です。
