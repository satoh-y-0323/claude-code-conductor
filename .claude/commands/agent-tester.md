# /agent-tester コマンド

テスト仕様の設計・実行・test-report の出力を行う。

## 実行前確認

```
ワークフローに沿って進めますか？
  [yes] 完了後に /agent-code-reviewer へ連携する
  [no]  このエージェントの作業のみ実施して終了する
```

## 実行手順

1. Glob で `.claude/reports/plan-report-*.md` を検索して最新を Read する
2. Agent ツールで `tester` エージェントを起動する
3. test-report をユーザーに報告し、承認を求める
4. 否認された場合はフィードバックを確認して tester を再起動する

## 次フェーズ（yes かつ承認された場合のみ）

全テスト合格の場合: `/agent-code-reviewer` を実行する。

不合格がある場合: `/agent-developer` を実行して修正し、再度 tester を起動する。
合格するまで developer ↔ tester のサイクルを繰り返す。
