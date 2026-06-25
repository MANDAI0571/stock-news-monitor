# agents/ ― stock-news-monitor 運用エージェント

スマホから**短い一言**で回すための役割分担。各エージェントは役割を1つに絞り、危険操作は1人に集約する。

## 5体
| # | ファイル | 名前 | 一言で |
|---|---|---|---|
| 1 | `01_hunter_screening.md` | ハンター | スクリーニング条件の改善（仕様まで）|
| 2 | `02_miss_checker.md` | ミス検出官 | 取り逃し理由を miss_check.py で1行報告 |
| 3 | `03_implementer_git.md` | 職人 | 実装・self_test・commit・push（唯一の書込み者）|
| 4 | `04_note_editor.md` | 編集者 | note原稿生成→下書き保存（公開しない）|
| 5 | `05_quality_gate.md` | 門番 | 品質チェック、NGなら下書き保存停止 |

各 md に「役割 / 禁止事項 / 入力 / 出力 / 最初に渡すプロンプト / スマホからの短い呼び出し例」を記載。

## スマホ運用チート（短い指示 → 動くエージェント）
| 高重さんの一言 | 動く | 中身 |
|---|---|---|
| 「雑魚減らして」「条件改善して」 | 1 ハンター | 改善仕様を作る（→実装は3へ）|
| 「なぜ285A出なかった?」「この銘柄調べて」 | 2 ミス検出官 | miss_check で除外理由1行 |
| 「実装して」「直して push して」 | 3 職人 | 仕様どおり実装→test→commit/push、3行報告 |
| 「note作って」「下書きにして」 | 4 編集者 | 原稿生成→QC→下書き保存（公開しない）|
| 「チェックして」「これ出して平気?」 | 5 門番 | PASS/NG判定、NGなら保存停止 |

## 毎日の標準フロー
1. （自動）`screening_result_*.csv` が出る → **4 編集者**「note作って」
2. **4 → 5** 編集者が門番にチェック依頼
3. 5 PASS なら 4 が下書き保存 / NG なら 4 に差し戻し
4. 取り逃し疑いがあれば **2 ミス検出官**「なぜ◯◯出なかった」
5. 条件を直したくなったら **1 ハンター**「改善して」→ **3 職人**「実装して」

## 安全境界（重要・全員厳守）
- **commit / push**: `03_implementer_git` だけ。他は仕様・指摘までで止める。
- **note公開**: 誰も自動でしない。高重さんが手動で行う。
- **下書き保存の停止権**: `05_quality_gate` が握る。NG なら 04 は保存しない。
- **backtest.py（売買ロジック）**: 原則不可侵。変更は高重さんの明示許可＋03職人のみ。
- **self_test.py**: 実装系は必ず `python3 self_test.py` を通してから commit。

## 関連実ファイル
- スクリーニング: `run_screening.py` / `scanner/`（highs.py, patterns.py, scoring.py, indicators.py, universe.py, openwork.py, prices.py）
- 取り逃し: `miss_check.py`（`--codes` / `--codes-file codes.txt`）
- note: `note_draft.py`（note_daily.md / note_title.txt / note_daily.html 生成）/ `note_autosave.py`（下書き保存）
- 通知: `gmail_notify.py` / `daily_note_mail.py`
- テスト: `self_test.py`
- 運用: `daily_discipline_run.py`、`.github/workflows/`
