# /agent-interviewer コマンド

要件ヒアリングを実施し requirements-report を出力する。

## 実行前確認

```
ワークフローに沿って進めますか？
  [yes] 完了後に /agent-architect へ連携する
  [no]  このエージェントの作業のみ実施して終了する
```

## 実行手順

1. 既存の requirements-report があれば Glob で `.claude/reports/requirements-report-*.md` を検索して最新を Read する
2. Agent ツールで `interviewer` エージェントを起動する
3. 出力レポートをユーザーに報告し、承認を求める
4. 否認された場合はフィードバックを確認して interviewer を再起動する

## 次フェーズ（yes かつ承認された場合のみ）

`/agent-architect` を実行する。
