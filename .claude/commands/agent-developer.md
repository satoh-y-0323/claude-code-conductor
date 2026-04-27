# /agent-developer コマンド

plan-report に基づき実装・デバッグを行う。

## 実行前確認

```
ワークフローに沿って進めますか？
  [yes] 完了後に /agent-tester へ連携する
  [no]  このエージェントの作業のみ実施して終了する
```

## 実行手順

1. Glob で `.claude/reports/plan-report-*.md` を検索して最新を Read する
   （存在しない場合はユーザーに確認して続けるか判断する）
2. Agent ツールで `developer` エージェントを起動する
3. 実装完了をユーザーに報告し、確認を求める
4. 問題があれば developer を再起動する

## 次フェーズ（yes かつ確認された場合のみ）

`/agent-tester` を実行する（テスト再実行・Green 確認）。

## TDD サイクルについて

tester から不合格が報告された場合、developer を再起動して修正する。
合格するまで developer ↔ tester のサイクルを繰り返す。
