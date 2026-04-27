# /agent-security-reviewer コマンド

セキュリティ脆弱性を診断し security-review-report を出力する。

## 実行前確認

```
ワークフローに沿って進めますか？
  [yes] 完了後に結果に応じて次フェーズを案内する
  [no]  このエージェントの作業のみ実施して終了する
```

## 実行手順

1. Agent ツールで `security-reviewer` エージェントを起動する
   （エージェント自身が変更ファイルと依存関係を確認する）
2. security-review-report をユーザーに報告し、承認を求める
3. 否認された場合はフィードバックを確認して security-reviewer を再起動する

## 次フェーズ（yes かつ承認された場合のみ）

**指摘がない、または Low のみの場合:**
このフェーズで完了。必要に応じてコミットを提案する。

**High / Critical の指摘がある場合:**
`/agent-planner` を実行して指摘を plan-report に反映し、
developer → tester → reviewer のサイクルを再度回す。
