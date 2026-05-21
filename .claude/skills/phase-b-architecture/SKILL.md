---
description: 設計フェーズ。architect ペルソナで技術スタック確認・設計案作成・architecture-report 生成を行う。dev-workflow フェーズ B 専用 skill。
disable-model-invocation: false
user-invocable: false
---

# Phase B: 設計

`.claude/agents/architect.md` を Read してペルソナを採用する。

**フェーズ A から続いている場合:** 要件はコンテキスト内にあるため読み直し不要。
**直接開始の場合:** Glob で `.claude/reports/requirements-report-*.md` の最新を Read する。

今日のセッションファイルに以下を追記する（未登録の場合のみ）:
- `- [ ] 設計` / `- [ ] 計画`

---

## B-1: 技術スタックの確認

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "技術スタックについて制約はありますか？",
    "options": [
      { "label": "既存スタックに合わせる", "description": "使用中の言語・FWに統一する" },
      { "label": "最適なものを選んでほしい", "description": "推薦に任せる" },
      { "label": "指定がある", "description": "使う技術を具体的に伝えます" }
    ]
  }]
}
```

## B-2: 設計と不明点の確認

要件をもとに設計案を作成する。不明点があれば AskUserQuestion ツールで確認する。

## B-3: architecture-report の生成と承認

`.claude/reports/architecture-report-YYYYMMDD-HHMMSS.md` に Write する。
内容を提示した後、AskUserQuestion で確認する:

```json
{
  "questions": [{
    "question": "architecture-report の内容を確認してください。どうしますか？",
    "options": [
      { "label": "承認", "description": "計画フェーズへ進む" },
      { "label": "否認・修正を依頼する", "description": "フィードバックを入力して設計をやり直す" },
      { "label": "否認・自分でファイルを編集する", "description": "reports/ のファイルを直接編集してから続ける" }
    ]
  }]
}
```

承認後 → セッションファイルの `- [ ] 設計` を `- [x] 設計` に Edit する。

**知識蓄積:**
- 「否認・修正を依頼する」: `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する
- 承認かつ非自明なアプローチが有効だった場合: `## うまくいったアプローチ` に追記し `patterns` にも追加する

---

## 次フェーズへの遷移

承認後、次は **phase-c-plan** へ進む。
LLM は Skill ツールで `phase-c-plan` を呼び出すか、
`.claude/skills/phase-c-plan/SKILL.md` を Read してフェーズ C を開始する。
