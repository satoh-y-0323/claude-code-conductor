---
description: ヒアリングフェーズ。interviewer ペルソナで背景・制約・非機能要件を聴取し requirements-report を生成する。dev-workflow フェーズ A 専用 skill。
disable-model-invocation: false
user-invocable: false
---

# Phase A: ヒアリング

`.claude/agents/interviewer.md` を Read してペルソナを採用する。

今日のセッションファイルに以下を追記する（未登録の場合のみ）:
- `- [ ] ヒアリング` / `- [ ] 設計` / `- [ ] 計画`

---

## A-1: 背景・きっかけ

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "背景・きっかけを教えてください（なぜ今これが必要ですか？）",
    "options": [
      { "label": "ユーザーからの要望", "description": "具体的な声があった" },
      { "label": "ビジネス上の要件", "description": "事業的な理由がある" },
      { "label": "技術的な負債解消", "description": "将来のために今直したい" },
      { "label": "パフォーマンス問題", "description": "遅い・重いを解決したい" }
    ]
  }]
}
```

## A-2: 制約・前提条件

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "制約や前提条件はありますか？（複数選択可）",
    "options": [
      { "label": "納期がある", "description": "期日を後で教えてください" },
      { "label": "既存APIを壊せない", "description": "後方互換性が必要" },
      { "label": "特定の技術スタックに限定", "description": "使える技術が決まっている" },
      { "label": "特になし" }
    ],
    "multiSelect": true
  }]
}
```

制約を選んだ場合は補足情報（納期の日付など）を追加で確認する。

## A-3: 非機能要件

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "特に重視したい品質特性はありますか？",
    "options": [
      { "label": "セキュリティ", "description": "認証・認可・データ保護を重視" },
      { "label": "パフォーマンス", "description": "速度・スループットを重視" },
      { "label": "保守性", "description": "読みやすさ・変更しやすさを重視" },
      { "label": "特になし・バランスよく" }
    ],
    "multiSelect": true
  }]
}
```

## A-4: requirements-report の生成と承認

収集した内容をもとに `.claude/reports/requirements-report-YYYYMMDD-HHMMSS.md` に Write する。

内容を提示した後、AskUserQuestion で確認する:

```json
{
  "questions": [{
    "question": "requirements-report の内容を確認してください。どうしますか？",
    "options": [
      { "label": "承認", "description": "設計フェーズへ進む" },
      { "label": "否認・修正を依頼する", "description": "フィードバックを入力してヒアリングをやり直す" },
      { "label": "否認・自分でファイルを編集する", "description": "reports/ のファイルを直接編集してから続ける" }
    ]
  }]
}
```

承認後 → セッションファイルの `- [ ] ヒアリング` を `- [x] ヒアリング` に Edit する。

**知識蓄積:**
- 「否認・修正を依頼する」: `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する
- 承認かつ非自明なアプローチが有効だった場合: `## うまくいったアプローチ` に追記し `patterns` にも追加する

---

## 次フェーズへの遷移

承認後、次は **phase-b-architecture** へ進む。
LLM は Skill ツールで `phase-b-architecture` を呼び出すか、
`.claude/skills/phase-b-architecture/SKILL.md` を Read してフェーズ B を開始する。
