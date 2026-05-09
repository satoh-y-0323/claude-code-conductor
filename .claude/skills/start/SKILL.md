---
description: 開発ワークフローの入口。既存レポートの整理後、ヒアリング・設計・計画・実装のどこから始めるかを選んで dev-workflow を実行する。
---

# start

開発ワークフローの入口。フロー順序: **Step 0（レポート整理）→ Step 0.5（タスク種別確認）→ Step 1（開始地点選択）→ Step 2（フェーズ遷移）**。

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

## Step 0.5: タスク種別の確認

タスクの種別（feature / bug-fix / refactor / security-audit / docs）を確定して、
当日セッション tmp の冒頭 `TASK_TYPE:` 行に書き込む。

### 0.5-A: 当日 tmp の準備

当日セッションファイル（`.claude/memory/sessions/{今日のYYYYMMDD}.tmp`）が存在しない場合は、
Bash で `session_utils.create_session_template` を使ってテンプレートを生成する:

```bash
python -c "
import sys
from pathlib import Path
sys.path.insert(0, '.claude/hooks')
from session_utils import create_session_template
date_str = '{今日のYYYYMMDD}'
target = Path(f'.claude/memory/sessions/{date_str}.tmp')
if not target.exists():
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(create_session_template(date_str), encoding='utf-8')
"
```

これによりテンプレート全体が一度に書かれ、`TASK_TYPE: ` 行も冒頭に含まれる。

### 0.5-B: 前回 TASK_TYPE の確認とショートカット

Glob で `.claude/memory/sessions/*.tmp` を検索し、当日より前で最大日付のファイルを Read する。
冒頭から `^TASK_TYPE: (\S+)$` を抽出して `prev_type` とする。

**ホワイトリスト検証**: 抽出した `prev_type` が
`feature / bug-fix / refactor / security-audit / docs` のいずれでもない場合
（空欄、enum 外の文字列、不正な値など）は `prev_type=None` として 0.5-C に進む
（プロンプト汚染対策、`[SR-V-001]` 対応）。

`prev_type` が確定種別（上記 5 種のいずれか）の場合、AskUserQuestion で 2 択ショートカットを提示する:

```json
{
  "questions": [{
    "question": "前回と同じ種別 ({prev_type}) で進めますか？",
    "header": "種別ショートカット",
    "options": [
      { "label": "前回と同じ ({prev_type}) で進める", "description": "task-routing をスキップして種別を引き継ぐ" },
      { "label": "別の種別を選ぶ", "description": "task-routing で 5 択から選び直す" }
    ]
  }]
}
```

「前回と同じ」を選んだ場合は `task_type = prev_type` として 0.5-D へ進む。
「別の種別を選ぶ」または `prev_type=None` の場合は 0.5-C へ進む。

### 0.5-C: task-routing の呼び出し

Skill ツールで `task-routing` を呼ぶ。`args` パラメータに `from_start=true` を渡すことで、
task-routing 側に「/start 経由での呼び出しなので Step 1 のみ実行して種別を返す」ことを伝える:

```
Skill(skill="task-routing", args="from_start=true")
```

task-routing は args に `from_start=true` が含まれていることをコンテキストから読み取り、
Step 1（種別の 5 択）のみ実行して種別を返す。Step 2〜4 はスキップされる。

戻ってきた種別を `task_type` とする。

### 0.5-D: TASK_TYPE 行の書き込み

当日 tmp の冒頭 `TASK_TYPE:` 行を Edit で `TASK_TYPE: {task_type}` に置換する。
0.5-A で生成したテンプレートには `TASK_TYPE: ` のような空欄行が冒頭にあるため、そこを置換する:

```
old: TASK_TYPE: 
new: TASK_TYPE: {task_type}
```

`{task_type}` には Step 0.5-B または 0.5-C で確定した種別の文字列
（feature / bug-fix / refactor / security-audit / docs のいずれか）を埋める。

---

## Step 1: 開始地点の選択

確定した `task_type` に応じて、種別ごとに異なる選択肢を AskUserQuestion で提示する。

### feature の場合（4 択・現状維持）

```json
{
  "questions": [{
    "question": "feature の作業をどこから始めますか？",
    "header": "開始地点",
    "options": [
      { "label": "ヒアリング", "description": "要件を整理するところから始める（新規・大きな変更）" },
      { "label": "設計", "description": "要件は明確なので設計から始める" },
      { "label": "計画", "description": "設計済みなのでタスク計画から始める" },
      { "label": "実装", "description": "計画済みなので実装から始める" }
    ]
  }]
}
```

### bug-fix の場合（最大 2 択）

既存 `.claude/reports/plan-report-*.md` が存在するかを Glob で確認し、選択肢を組み立てる:

```json
{
  "questions": [{
    "question": "bug-fix の作業をどこから始めますか？",
    "header": "開始地点",
    "options": [
      { "label": "systematic-debugger 直起動", "description": "原因調査から始める（推奨）" },
      { "label": "計画から", "description": "既存 plan-report がある場合のみ。計画フェーズから入って修正タスクとして整える" }
    ]
  }]
}
```

`plan-report-*.md` が無い場合は「計画から」の選択肢を外して 1 択（自動的に systematic-debugger）にする。

### refactor の場合（2 択）

```json
{
  "questions": [{
    "question": "refactor の作業をどこから始めますか？",
    "header": "開始地点",
    "options": [
      { "label": "計画", "description": "planner で po_plan_version 付き plan-report を生成して PO 並列実行に備える" },
      { "label": "実装", "description": "既存 po_plan_version 付き plan-report を使って wave-execution に直接入る" }
    ]
  }]
}
```

### security-audit の場合（即実行のみ）

選択肢は 1 つのみ。AskUserQuestion で確認のみ取る:

```json
{
  "questions": [{
    "question": "security-audit を実行します。code-reviewer と security-reviewer を並列起動してよいですか？",
    "header": "実行確認",
    "options": [
      { "label": "実行する", "description": "code-reviewer + security-reviewer を 1 メッセージで並列起動" },
      { "label": "中止する", "description": "ここで停止" }
    ]
  }]
}
```

### docs の場合（即実行のみ）

```json
{
  "questions": [{
    "question": "docs 作業を実行します。doc-writer を起動してよいですか？",
    "header": "実行確認",
    "options": [
      { "label": "実行する", "description": "doc-writer を起動してドキュメント作業を進める" },
      { "label": "中止する", "description": "ここで停止" }
    ]
  }]
}
```

---

## Step 2: 種別 × 開始地点 → フェーズ遷移

| 種別 | 開始地点 | 遷移先 |
|---|---|---|
| feature | ヒアリング | `.claude/skills/dev-workflow/SKILL.md` を Read してフェーズ A から |
| feature | 設計 | `.claude/skills/dev-workflow/SKILL.md` を Read してフェーズ B から |
| feature | 計画 | `.claude/skills/dev-workflow/SKILL.md` を Read してフェーズ C から |
| feature | 実装 | `.claude/skills/dev-workflow/SKILL.md` を Read してフェーズ D から |
| bug-fix | systematic-debugger 直起動 | Agent ツールで `systematic-debugger` を起動 → `developer` → `tester` → `code-reviewer` の順 |
| bug-fix | 計画から | `.claude/skills/dev-workflow/SKILL.md` を Read してフェーズ C から（既存 plan-report を利用） |
| refactor | 計画 | Agent ツールで `planner` を起動して `po_plan_version` 付き plan-report を生成 → `.claude/skills/wave-execution/SKILL.md` を Read |
| refactor | 実装 | `.claude/skills/wave-execution/SKILL.md` を Read して PO 並列実行に直接入る |
| security-audit | 即実行 | Agent ツールで `code-reviewer` と `security-reviewer` を **1 メッセージ内で並列起動** |
| docs | 即実行 | Agent ツールで `doc-writer` を単独起動 |

**最初に必ず** 遷移先の SKILL.md を Read してから実行する。記憶・推測で進めず、AskUserQuestion・Edit・セッションファイル更新の手順を省略しないこと。

各エージェント完了後は通常の Approval Flow に従う。
