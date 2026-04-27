# /develop コマンド

plan-report に基づき TDD サイクルで実装を進める。
developer・tester は Agent ツールで起動する。

## Step 1: plan-report の確認

Glob で `.claude/reports/plan-report-*.md` の最新を Read する。
存在しない場合は `/start` から始めるよう案内して終了する。

## Step 2: tester（Red フェーズ）

Agent ツールで `tester` エージェントを起動する。
→ 失敗するテストを先に作成する。

## Step 3: developer（Green フェーズ）

Agent ツールで `developer` エージェントを起動する。
→ テストが通る実装を行う。

## Step 4: tester（確認）

Agent ツールで `tester` エージェントを起動する。
→ 全テストの合否を確認する。

**不合格がある場合:** Step 3（developer）に戻る。合格するまで繰り返す。
**全合格の場合:** Step 5 へ。

## Step 5: developer（Refactor フェーズ）

Agent ツールで `developer` エージェントを起動する。
→ テストを壊さずにコードを整理する。

## Step 6: tester（最終確認）

Agent ツールで `tester` エージェントを起動する。
test-report をユーザーに報告して承認を求める。

承認後 → `/review` を案内する。
