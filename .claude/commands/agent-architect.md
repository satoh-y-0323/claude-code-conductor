# /agent-architect コマンド

システム設計・技術選定を行い architecture-report を出力する。

## 実行前確認

```
ワークフローに沿って進めますか？
  [yes] 完了後に /agent-planner へ連携する
  [no]  このエージェントの作業のみ実施して終了する
```

## 実行手順

1. Glob で `.claude/reports/requirements-report-*.md` を検索して最新を Read する
   （存在しない場合はユーザーに確認して続けるか判断する）
2. Agent ツールで `architect` エージェントを起動する
3. 出力レポートをユーザーに報告し、承認を求める
4. 否認された場合はフィードバックを確認して architect を再起動する

## 次フェーズ（yes かつ承認された場合のみ）

`/agent-planner` を実行する。
