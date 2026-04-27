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

完了後、AskUserQuestion で確認する:

```json
{
  "questions": [{
    "question": "実装内容を確認してください。どうしますか？",
    "options": [
      { "label": "承認", "description": "テスト実行フェーズへ進む" },
      { "label": "否認・再実装を依頼する", "description": "フィードバックを入力して developer を再起動する" },
      { "label": "否認・自分でコードを修正する", "description": "自分でコードを修正してから続ける" }
    ]
  }]
}
```

## Step 4: tester（確認）

Agent ツールで `tester` エージェントを起動する。
→ 全テストの合否を確認する。

AskUserQuestion で確認する:

```json
{
  "questions": [{
    "question": "テスト結果を確認してください。どうしますか？",
    "options": [
      { "label": "全合格・次へ進む", "description": "Refactor フェーズへ進む" },
      { "label": "不合格あり・再実装を依頼する", "description": "フィードバックを入力して developer を再起動する" },
      { "label": "不合格あり・自分でコードを修正する", "description": "自分で修正してから tester を再実行する" }
    ]
  }]
}
```

**不合格の場合:** Step 3（developer）に戻る。合格するまで繰り返す。

## Step 5: developer（Refactor フェーズ）

Agent ツールで `developer` エージェントを起動する。
→ テストを壊さずにコードを整理する。

## Step 6: tester（最終確認）

Agent ツールで `tester` エージェントを起動する。

AskUserQuestion で確認する:

```json
{
  "questions": [{
    "question": "最終テスト結果と実装内容を確認してください。どうしますか？",
    "options": [
      { "label": "承認・レビューへ進む", "description": "/review でレビューフェーズへ進む" },
      { "label": "否認・修正を依頼する", "description": "フィードバックを入力して再修正を依頼する" },
      { "label": "否認・自分でコードを修正する", "description": "自分で修正してから再テストする" }
    ]
  }]
}
```

承認後 → `/review` を案内する。
