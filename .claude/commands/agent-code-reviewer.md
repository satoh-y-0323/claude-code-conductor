# /agent-code-reviewer コマンド

コードの品質・保守性・パフォーマンスをレビューし code-review-report を出力する。

## 実行前確認

```
ワークフローに沿って進めますか？
  [yes] 完了後に /agent-security-reviewer へ連携する
  [no]  このエージェントの作業のみ実施して終了する
```

## 実行手順

1. Agent ツールで `code-reviewer` エージェントを起動する
   （エージェント自身が git diff で変更ファイルを確認する）
2. code-review-report をユーザーに報告し、承認を求める
3. 否認された場合はフィードバックを確認して code-reviewer を再起動する

## 次フェーズ（yes かつ承認された場合のみ）

`/agent-security-reviewer` を実行する。
