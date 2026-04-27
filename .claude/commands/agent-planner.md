# /agent-planner コマンド

全レポートを統合してタスク分解した plan-report を出力する。

## 実行前確認

```
ワークフローに沿って進めますか？
  [yes] 完了後に /agent-tester → /agent-developer へ連携する
  [no]  このエージェントの作業のみ実施して終了する
```

## 実行手順

1. 利用可能な全レポートを Glob で検索して最新を Read する:
   - `.claude/reports/requirements-report-*.md`
   - `.claude/reports/architecture-report-*.md`
   - `.claude/reports/test-report-*.md`
   - `.claude/reports/code-review-report-*.md`
   - `.claude/reports/security-review-report-*.md`
   （存在しないレポートはスキップする）
2. Agent ツールで `planner` エージェントを起動する
3. 出力レポートをユーザーに報告し、承認を求める
4. 否認された場合はフィードバックを確認して planner を再起動する

## 次フェーズ（yes かつ承認された場合のみ）

1. `/agent-tester` を実行する（Red フェーズ: 失敗テスト作成）
2. tester 完了後、`/agent-developer` を実行する
