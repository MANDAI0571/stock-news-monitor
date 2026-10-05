# Claude 300万円 台帳完全照合

注文台帳・約定・保有台帳・現金を突合する安全ゲートです。記録は自動修正しません。

- 基準日: 2026-10-05
- 判定: **HEALTHY**
- CRITICAL: 0
- 新規買い: 許可
- CRITICALでも既存ポジションの損切り・利確・タイムアウト宣告は継続

| 重要度 | 検査 | 判定 | 対象 | 詳細 |
|---|---|---|---|---|
| INFO | schema_orders | PASS | orders | 7 rows |
| INFO | schema_journal | PASS | journal | 5 rows |
| INFO | duplicate_orders | PASS | orders | none |
| INFO | duplicate_journal_entries | PASS | journal | none |
| INFO | filled_buys_to_journal | PASS | all | 5 keys matched |
| INFO | filled_sells_to_journal | PASS | all | 2 keys matched |
| INFO | open_share_balance | PASS | all | 3 keys matched |
| INFO | journal_arithmetic | PASS | journal | 5 rows matched |
| INFO | cash_balance | PASS | paper_cash | cash_jpy=1498500 |
| INFO | open_position_limits | PASS | portfolio | open=3 max=3 |
| INFO | pending_rule_compatibility | PASS | pending_orders | count=0 |
