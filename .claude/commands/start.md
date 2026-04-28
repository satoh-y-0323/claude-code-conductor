# /start コマンド

開発ワークフローの入口。

---

## Step 0: レポートの整理

Glob で `.claude/reports/*.md` を検索する（`archive/` 配下は含まない）。
レポートが存在しない場合はこの Step をスキップして Step 1 へ進む。

レポートが存在する場合はファイル名の一覧をテキストで提示してから AskUserQuestion で確認する:

```json
{
  "questions": [{
    "question": "既存のレポートがあります。どうしますか？",
    "options": [
      { "label": "全てアーカイブして新しく始める", "description": "全レポートを reports/archive/ に移動する" },
      { "label": "アーカイブするフェーズを選ぶ", "description": "フェーズ単位で選んで一部だけ移動する" },
      { "label": "そのまま引き継ぐ", "description": "レポートを変更せずに続ける" }
    ]
  }]
}
```

**「全てアーカイブして新しく始める」の場合:**
Bash ツールで実行する:
```bash
mkdir -p .claude/reports/archive && mv .claude/reports/*.md .claude/reports/archive/
```

**「アーカイブするフェーズを選ぶ」の場合:**
AskUserQuestion で対象フェーズを確認する:

```json
{
  "questions": [{
    "question": "アーカイブするフェーズを選んでください（複数選択可）",
    "options": [
      { "label": "要件定義", "description": "requirements-report-*.md" },
      { "label": "設計", "description": "architecture-report-*.md" },
      { "label": "計画", "description": "plan-report-*.md" },
      { "label": "レビュー", "description": "code-review-report-*.md / security-review-report-*.md" }
    ],
    "multiSelect": true
  }]
}
```

選択されたフェーズに対応するファイルを Bash ツールで移動する（ファイルが存在しない場合はスキップ）:
- 要件定義: `mkdir -p .claude/reports/archive && mv .claude/reports/requirements-report-*.md .claude/reports/archive/ 2>/dev/null || true`
- 設計: `mkdir -p .claude/reports/archive && mv .claude/reports/architecture-report-*.md .claude/reports/archive/ 2>/dev/null || true`
- 計画: `mkdir -p .claude/reports/archive && mv .claude/reports/plan-report-*.md .claude/reports/archive/ 2>/dev/null || true`
- レビュー: `mkdir -p .claude/reports/archive && mv .claude/reports/code-review-report-*.md .claude/reports/archive/ 2>/dev/null || true && mv .claude/reports/security-review-report-*.md .claude/reports/archive/ 2>/dev/null || true`

---

## Step 1: 開始地点の選択

AskUserQuestion ツールで以下を提示する:

```json
{
  "questions": [{
    "question": "どこから始めますか？",
    "options": [
      { "label": "ヒアリング", "description": "要件を整理するところから始める（新規・大きな変更）" },
      { "label": "設計", "description": "要件は明確なので設計から始める" },
      { "label": "計画", "description": "設計済みなのでタスク計画から始める" },
      { "label": "実装", "description": "計画済みなので実装から始める" }
    ]
  }]
}
```

---

## Step 2: dev-workflow の実行

`.claude/skills/dev-workflow.md` を Read して、選択した開始地点に対応するフェーズから実行する:

- **ヒアリング** → フェーズ A から実行
- **設計** → フェーズ B から実行
- **計画** → フェーズ C から実行
- **実装** → フェーズ D から実行
