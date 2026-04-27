# /review コマンド

code-reviewer と security-reviewer を順番に Agent ツールで起動してレビューを行う。

## Step 1: code-reviewer エージェントの起動

Agent ツールで `code-reviewer` エージェントを起動する。

AskUserQuestion で確認する:

```json
{
  "questions": [{
    "question": "code-review-report の内容を確認してください。どうしますか？",
    "options": [
      { "label": "承認・セキュリティレビューへ進む", "description": "指摘なし、またはLowのみ" },
      { "label": "否認・再レビューを依頼する", "description": "フィードバックを入力して再実行する" },
      { "label": "指摘を自分で対応する", "description": "自分でコードを修正してからセキュリティレビューへ進む" }
    ]
  }]
}
```

## Step 2: security-reviewer エージェントの起動

Agent ツールで `security-reviewer` エージェントを起動する。

AskUserQuestion で確認する:

```json
{
  "questions": [{
    "question": "security-review-report の内容を確認してください。どうしますか？",
    "options": [
      { "label": "承認・完了", "description": "指摘なし、またはLowのみ。コミットを提案する" },
      { "label": "否認・再診断を依頼する", "description": "フィードバックを入力して再実行する" },
      { "label": "指摘を自分で対応する", "description": "自分でコードを修正してから再診断する" },
      { "label": "High/Critical あり・計画を見直す", "description": "/start の計画フェーズから再開する" }
    ]
  }]
}
```

## Step 3: 結果に応じた処理

**「承認・完了」または「指摘を自分で対応する（対応済み）」:**
コミットを提案する。

**「High/Critical あり・計画を見直す」:**
`/start` の計画フェーズ（plan）から再開するよう案内する。
→ planner が全レポートを読み込んで plan-report を更新する。
