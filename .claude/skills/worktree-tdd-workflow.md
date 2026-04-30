# Worktree TDD Workflow

ヘッドレス専用の TDD 1サイクル定義。AskUserQuestion を含まない。
`.claude/agents/tdd-develop.md` から呼び出されることを前提とする。

---

## 前提条件

- `.claude/reports/plan-report-*.md` が存在すること
- プロダクションコードが worktree 内に配置済みであること

---

## Step 1: tester（現状確認フェーズ）

以下を順番に実行する:

1. `.claude/agents/tester.md` を Read してペルソナ・制約・手順を把握する
2. Glob で `.claude/reports/plan-report-*.md` の最新ファイルパスを取得し、Read する
3. Agent ツールを起動する。**subagent_type は指定しない**。プロンプトに以下を注入する:
   - tester.md の内容（ペルソナ・制約・手順）
   - plan-report の内容（テスト対象・受け入れ条件）
   - 「テストが存在しない場合は Red フェーズとして失敗するテストを先に作成してから実行すること」
   - 以下の手順を必ず守ること:
     1. Bash ツールでタイムスタンプを取得する:
        `python -c "from datetime import datetime; print(datetime.now().strftime('%Y%m%d-%H%M%S'))"`
     2. **Write ツールを呼び出して** `.claude/reports/test-report-{タイムスタンプ}.md` を作成する。テキストで返すだけでは不十分。合格/不合格/スキップ件数・不合格テストのエラーを記載すること

完了後: Glob で `.claude/reports/test-report-*.md` の最新ファイルパスを取得する。

---

## Step 2: 全合格チェック

取得した test-report を Read して合否を確認する。

- **全合格の場合**: 呼び出し元（tdd-develop）に `PASS` と test-report パスを返してワークフローを終了する
- **不合格あり**: Step 3 へ進む

---

## Step 3: developer（Green フェーズ）

以下を順番に実行する:

1. `.claude/agents/developer.md` を Read してペルソナ・制約・手順を把握する
2. Agent ツールを起動する。**subagent_type は指定しない**。プロンプトに以下を注入する:
   - developer.md の内容（ペルソナ・制約・手順）
   - plan-report の内容
   - test-report の内容（不合格テストの詳細）
   - 「test-report の不合格テストをすべて通過させるよう実装すること。テストコードは編集しないこと」

---

## Step 4: tester（Green 確認フェーズ）

以下を順番に実行する:

1. `.claude/agents/tester.md` を Read してペルソナ・制約・手順を把握する（Step 1 のコンテキストが残っていれば省略可）
2. Agent ツールを起動する。**subagent_type は指定しない**。プロンプトに以下を注入する:
   - tester.md の内容（ペルソナ・制約・手順）
   - plan-report の内容
   - 「developer の実装後のテストを全件実行すること」
   - 以下の手順を必ず守ること:
     1. Bash ツールでタイムスタンプを取得する:
        `python -c "from datetime import datetime; print(datetime.now().strftime('%Y%m%d-%H%M%S'))"`
     2. **Write ツールを呼び出して** `.claude/reports/test-report-{タイムスタンプ}.md` を作成する。テキストで返すだけでは不十分。合格/不合格/スキップ件数・不合格テストのエラーを記載すること

完了後: Glob で最新 test-report を取得し、合否を確認する。
呼び出し元（tdd-develop）に結果（`PASS` または `FAIL`）と test-report パスを返してワークフローを終了する。
