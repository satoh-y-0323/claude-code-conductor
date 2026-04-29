# Worktree TDD Workflow

ヘッドレス専用の TDD 1サイクル定義。AskUserQuestion を含まない。
`agents/tdd-develop.md` から呼び出されることを前提とする。

---

## 前提条件

- `.claude/reports/plan-report-*.md` が存在すること
- プロダクションコードが worktree 内に配置済みであること

---

## Step 1: tester（現状確認フェーズ）

Glob で `.claude/reports/plan-report-*.md` の最新ファイルパスを取得する。

Agent ツールで `tester` エージェントを起動する。プロンプトに以下を含める:
- 最新 plan-report のパス
- 「現在のテスト状況を確認してください。テストが存在しない場合は Red フェーズとして失敗するテストを先に作成してください。完了後 test-report を出力してください」

完了後: Glob で `.claude/reports/test-report-*.md` の最新ファイルパスを取得する。

---

## Step 2: 全合格チェック

取得した test-report を Read して合否を確認する。

- **全合格の場合**: 呼び出し元（tdd-develop）に `PASS` と test-report パスを返してワークフローを終了する
- **不合格あり**: Step 3 へ進む

---

## Step 3: developer（Green フェーズ）

Agent ツールで `developer` エージェントを起動する。プロンプトに以下を含める:
- 最新 plan-report のパス
- 最新 test-report のパス
- 「test-report の不合格テストを全て通過させるように実装してください。テストコードは編集しないこと」

---

## Step 4: tester（Green 確認フェーズ）

Agent ツールで `tester` エージェントを起動する。プロンプトに以下を含める:
- 最新 plan-report のパス
- 「developer の実装後のテストを全件実行し、結果を test-report として出力してください」

完了後: Glob で最新 test-report を取得し、合否を確認する。
呼び出し元（tdd-develop）に結果（`PASS` または `FAIL`）と test-report パスを返してワークフローを終了する。
