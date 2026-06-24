---
description: 開発ワークフローの入口。開始地点（標準ワークフローの各フェーズ / 実装 / デバッグ調査 / レビュー）を選び、対応する dev-workflow フェーズに遷移する。
---

# start

開発ワークフローの入口。フロー順序: **セッション初期化ガード（最優先）→ Step 0（レポート整理）→ Step 1（開始地点選択）→ Step 2（フェーズ遷移）**。

ワークフローは 2 つだけ:

- **標準ワークフロー**: ヒアリング → 設計 → 計画 → 実装(TDD) → レビュー → 指摘ありで計画へ戻る、なしで終了
- **デバッグワークフロー**: systematic-debugger → 実装(developer + tester Green) → レビュー → 指摘ありで標準ワークフローの計画へ、なしで終了

その他の単発タスク（ドキュメント作成など）は対応する個別コマンド（例: `/doc`）を直接使う。

---

## 最優先: セッション初期化ガード（Step 0 より前に必ず実行）

本体（Step 0 以降）に入る前に、**当該セッションで `/init-session` が実行済みかを判定し、未実行なら自動実行する**。
Bash で判定する:

```bash
python .claude/skills/init-session/scripts/session_guard.py check
```

- **`INIT_DONE`**（フラグの中身が現在の `CLAUDE_CODE_SESSION_ID` と一致）→ 既に初期化済み。そのまま Step 0 へ進む。
- **`INIT_NEEDED`**（不一致 / フラグ不在 / `CLAUDE_CODE_SESSION_ID` が空）→ Skill ツールで `init-session` を **`from-start` 引数付き**で呼ぶ。
  - `init-session` が（必要なら `/setup` を連鎖実行し）前回状態を復元し、`init_session.flag` を書く。`from-start` 付きのため `init-session` 側は開始方法の選択（Step 4）や陳腐化タスク確認などの対話プロンプトをスキップして復元サマリのみ提示する。
  - `init-session` の Skill 実行が完了したら本スキルに戻り、続けて Step 0（レポート整理）以降を実行する。

> **ループ回避**: `init-session` は本体開始直後に `init_session.flag` を現セッション ID で書く。そのため `init-session`（Step 5「ワークフローで始める」）が `/start` を呼び戻しても、本ガードは `INIT_DONE` と判定し再帰呼び出ししない。

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
      { "label": "レビュー", "description": "code-review-report-*.md / security-review-report-*.md / design-review-report-*.md" }
    ],
    "multiSelect": true
  }]
}
```

選択されたフェーズに対応するファイルを Bash ツールで移動する（ファイルが存在しない場合はスキップ）:
- 要件定義: `mkdir -p .claude/reports/archive && mv .claude/reports/requirements-report-*.md .claude/reports/archive/ 2>/dev/null || true`
- 設計: `mkdir -p .claude/reports/archive && mv .claude/reports/architecture-report-*.md .claude/reports/archive/ 2>/dev/null || true`
- 計画: `mkdir -p .claude/reports/archive && mv .claude/reports/plan-report-*.md .claude/reports/archive/ 2>/dev/null || true`
- レビュー: `mkdir -p .claude/reports/archive && mv .claude/reports/code-review-report-*.md .claude/reports/archive/ 2>/dev/null || true && mv .claude/reports/security-review-report-*.md .claude/reports/archive/ 2>/dev/null || true && mv .claude/reports/design-review-report-*.md .claude/reports/archive/ 2>/dev/null || true`

---

## Step 1: 開始地点の選択

AskUserQuestion ツールで 4 択を提示する:

```json
{
  "questions": [{
    "question": "どこから始めますか？",
    "header": "開始地点",
    "options": [
      { "label": "標準ワークフロー", "description": "ヒアリング/設計/計画のどれかから始める（新機能・リファクタ・改善など）" },
      { "label": "実装から", "description": "既存 plan-report を使って実装フェーズへ" },
      { "label": "デバッグ調査から", "description": "不具合の原因調査から始める" },
      { "label": "レビューから", "description": "既存コードをレビューする（指摘があれば修正サイクルへ）" }
    ]
  }]
}
```

「標準ワークフロー」を選んだ場合は続けて Step 1.5 のサブ選択を行う。
それ以外を選んだ場合は Step 1.5 をスキップして Step 2 へ進む。

---

## Step 1.5: 標準ワークフローのサブ選択

Step 1 で「標準ワークフロー」を選んだ場合のみ実行する。
AskUserQuestion で 3 択を提示する:

```json
{
  "questions": [{
    "question": "標準ワークフローのどこから始めますか？",
    "header": "標準サブ選択",
    "options": [
      { "label": "ヒアリング", "description": "要件を整理するところから始める（新規・大きな変更）" },
      { "label": "設計", "description": "要件は明確なので設計から始める" },
      { "label": "計画", "description": "設計済みなのでタスク計画から始める" }
    ]
  }]
}
```

---

## Step 2: フェーズ遷移

確定した開始地点に応じて以下のマッピングで遷移する。

| 開始地点 | 遷移先 |
|---|---|
| 標準ワークフロー・ヒアリング | `.claude/skills/dev-workflow/SKILL.md` を Read して **フェーズ A** から |
| 標準ワークフロー・設計 | `.claude/skills/dev-workflow/SKILL.md` を Read して **フェーズ B** から |
| 標準ワークフロー・計画 | `.claude/skills/dev-workflow/SKILL.md` を Read して **フェーズ C** から |
| 実装から | `.claude/skills/dev-workflow/SKILL.md` を Read して **フェーズ D** から（[注 1]） |
| デバッグ調査から | Agent ツールで `systematic-debugger` を起動 → `.claude/skills/dev-workflow/SKILL.md` を Read して **フェーズ D** へ（[注 2]） |
| レビューから | `.claude/skills/dev-workflow/SKILL.md` を Read して **フェーズ E** から |

[注 1] **実装から** の D-0 判定: plan-report-*.md の YAML フロントマターに `po_plan_version` があれば parallel-agents モード、なければ legacy TDD モードで動作する。plan-report が無い場合はフェーズ C へ案内される。

[注 2] **デバッグ調査から** の D-0 判定: systematic-debugger が当日タイムスタンプの debug-analysis-*.md を出力するため、D-0 がそれを検出して bug-fix モードで動作する。D-1（Red tester）と D-4（Refactor）はスキップされる。

**最初に必ず** 遷移先の `dev-workflow/SKILL.md` を Read してから実行する。記憶・推測で進めず、各フェーズに記述された AskUserQuestion・Edit・セッションファイル更新の手順を省略しないこと。

各エージェント完了後は通常の Approval Flow に従う。

レビューサイクルの loop-back（指摘ありで計画フェーズへ戻る）は dev-workflow フェーズ E が担う。`/start` 側で個別に管理する必要はない。
