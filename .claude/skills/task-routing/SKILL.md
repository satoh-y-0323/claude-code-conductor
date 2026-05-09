---
description: タスク種別から推奨エージェント編成を選び、確認後に各エージェントを起動するルーティング skill。bug-fix / feature / refactor / security-audit / docs の 5 種別に対応。
disable-model-invocation: false
---

# task-routing

タスクの種別を選び、それに対応するエージェント編成を提示する skill。
Ruflo の「タスク種別 → エージェント編成テンプレ」発想を C3 の既存エージェント
構成に当てはめたもの。`/start` から自動呼び出し可能（Skill ツール `args` に
`from_start=true` が渡される）。単独利用（`/task-routing` 直接実行）も継続サポートする。

## 動作モード

呼び出し元は Skill ツールの `args` パラメータで動作モードを指定する。
LLM はこの skill が起動された際、文脈や呼び出し情報から `from_start=true` の有無を判定する。

- **/start 経由（args に `from_start=true`）**: Step 1 で種別を確定したら
  そのまま終了し、種別を `/start` 側に返却する。Step 2〜4 はスキップする。
  TASK_TYPE 書き込みも `/start` 側で行うため、本 skill では行わない。
  編成詳細は `/start` 後段（Step 1 開始地点選択 / Step 2 フェーズ遷移）で
  扱うため、ここでは出さない。
- **単独利用（args 指定なし、または `from_start=false`）**: 従来通り Step 1 → 2 → 3 → 4 を
  順に実行する。Step 4 完了時に TASK_TYPE 書き込みも本 skill が行う。

---

## Step 1: タスク種別を選択する

Skill ツールの `args` に `from_start=true` が含まれているときは「/start 経由モード」とみなし、
Step 1 で種別を確定したら **Step 2〜4 はスキップ** して種別を呼び出し元（/start）に
返却する（編成詳細は /start 側で表示するため）。

AskUserQuestion ツール:

```json
{
  "questions": [{
    "question": "今回のタスクの種別を選んでください",
    "header": "タスク種別",
    "options": [
      { "label": "bug-fix", "description": "既存機能の不具合を直す（原因調査が中心）" },
      { "label": "feature", "description": "新機能を追加する（要件定義から実装・レビューまで）" },
      { "label": "refactor", "description": "動作を変えずに内部構造を改善する" },
      { "label": "security-audit", "description": "セキュリティ観点で既存コードを監査する" },
      { "label": "docs", "description": "ドキュメント整備のみ（コード変更を伴わない）" }
    ]
  }]
}
```

---

## Step 2: 選択された種別に対応する編成を提示する

選ばれた種別に応じて、以下の編成案を **テキストで提示** する（実行はまだしない）。

### bug-fix（直列・最小構成）

| 順 | エージェント | 役割 |
|---|---|---|
| 1 | `systematic-debugger` | 不具合の根本原因を特定 |
| 2 | `developer` | 修正実装 |
| 3 | `tester` | リグレッション確認 |
| 4 | `code-reviewer` + `security-reviewer` | 最終レビュー |

意図: 原因不明の不具合は systematic-debugger で先に切り分けてから developer に渡す。
修正後のコードに新たな脆弱性が生まれる可能性があるため、最終レビューは code-reviewer と security-reviewer の並列起動で両面を確認する。

### feature（直列・dev-workflow フルパス）

| 順 | エージェント | フェーズ |
|---|---|---|
| 1 | `interviewer` | A: ヒアリング |
| 2 | `architect` | B: 設計 |
| 3 | `planner` | C: 計画 |
| 4 | `tester` → `developer` → `tester` → `developer` → `tester` | D: TDD |
| 5 | `code-reviewer` → `security-reviewer` | E: レビュー |

意図: 新機能追加は要件定義から計画まで全フェーズが必要。
**この場合は `/start` を直接呼ぶ方がシンプル。** 本 skill は提示のみ。

### refactor（並列・PO 推奨）

| 役割 | エージェント | 並列 |
|---|---|---|
| タスク分解 | `planner` | — |
| 各リファクタタスク | `developer` + `tester` | PO で並列 |
| 最終レビュー | `code-reviewer` | — |

意図: 動作を変えないため、各リファクタを worktree で並列化できる。
PO（Parallel Orchestra）の `c3 po run` で時間短縮を狙う。
最終レビューは `code-reviewer` のみ（リファクタは動作変更を伴わないため新たな攻撃面が生まれにくく、security-reviewer は省略可）。

### security-audit（並列・レビュアー 2 体）

| エージェント | 並列 |
|---|---|
| `security-reviewer` | 並列起動 |
| `code-reviewer` | 並列起動 |

意図: 既存コードへの監査のため、レビュアー 2 体を同時に当てる。
検出指摘の対応は `/start` の security-audit フェーズ F/G/H（修正計画 → TDD 実装 → 最終レビュー）で行う。

### docs（直列・軽量）

| 順 | エージェント | 役割 |
|---|---|---|
| 1 | `doc-writer` | ドキュメント作成・更新 |

意図: コード変更を伴わないため、tester / reviewer は不要。

---

## Step 3: 編成案を承認するか確認する

AskUserQuestion ツール:

```json
{
  "questions": [{
    "question": "提示した編成案で進めますか？",
    "header": "編成承認",
    "options": [
      { "label": "この編成で進める", "description": "提示順にエージェントを起動して作業を始める" },
      { "label": "編成を調整する", "description": "エージェントを追加・削除・並び替えしたい" },
      { "label": "中止する", "description": "編成案だけ参考にして今は実行しない" }
    ]
  }]
}
```

---

## Step 4: 選択に応じて分岐する

### 「この編成で進める」の場合

**まず最初に** 当日のセッション tmp（`.claude/memory/sessions/YYYYMMDD.tmp`）の
冒頭に `TASK_TYPE: {種別}` 行を Edit で書き込む（既存行があれば置換、空欄なら埋める）。
これは `args` に `from_start=true` が **無い** 場合のみ task-routing 側で行う
（`from_start=true` の場合は /start 側が書き込むため、ここでは書かない）。

その後、選択された種別に応じて以下を実行する:

- **feature**:
  - `args` に `from_start=true` が含まれているとき: 種別を返却するのみ（制御を /start に返す。再帰呼び出しを避ける）
  - `args` 指定なし/`from_start=false` のとき: `.claude/skills/start/SKILL.md` を Read して `/start` フローに合流する
- **bug-fix**: `systematic-debugger` → `developer` → `tester` の順に Agent ツールで順次起動し、完了後に `code-reviewer` と `security-reviewer` を 1 メッセージ内で並列起動する
- **docs**: Agent ツールで `doc-writer` を起動する
- **refactor**: planner で `po_plan_version` 付き plan-report を生成 → `.claude/skills/wave-execution/SKILL.md` を Read して PO 並列実行に合流する
- **security-audit**: code-reviewer と security-reviewer を **1 メッセージ内で並列起動**（複数 Agent ツール呼び出し）

各エージェント完了後は通常の Approval Flow（AskUserQuestion で承認 / 否認・修正依頼 / 否認・自分で修正）に従う。

### 「編成を調整する」の場合

追加の AskUserQuestion で調整内容を確認する:

```json
{
  "questions": [{
    "question": "編成をどう調整しますか？",
    "header": "調整内容",
    "options": [
      { "label": "エージェントを追加する", "description": "編成に存在しないエージェントを足す" },
      { "label": "エージェントを削除する", "description": "不要なエージェントを抜く" },
      { "label": "順序を変える", "description": "並列/直列や順番を調整する" }
    ]
  }]
}
```

調整後、改めて Step 3 の承認確認に戻る。

### 「中止する」の場合

編成案だけテキストで提示して終了する。後から手動で各エージェントを呼べるよう
編成情報は破棄せず最終応答に含める。

---

## 参考: 既存エージェント一覧

`.claude/agents/` 配下の利用可能エージェント（2026-05-08 時点）:

- `architect` — 設計
- `code-reviewer` — コード品質レビュー
- `developer` — 実装・デバッグ
- `doc-writer` — ドキュメント作成
- `interviewer` — 要件ヒアリング
- `planner` — タスク計画
- `project-setup` — プロジェクト初期設定
- `security-reviewer` — セキュリティ診断
- `systematic-debugger` — デバッグ調査
- `tdd-develop` — ヘッドレス TDD コンダクター（PO 専用）
- `tester` — テスト設計・実行

新しいエージェントが `.claude/agents/` に追加された場合は、この skill の編成
テーブルにも反映すること。
