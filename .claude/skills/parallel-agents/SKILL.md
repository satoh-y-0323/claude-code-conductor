---
description: plan-report の wave 単位で親 Claude の Agent ツール並列起動 + isolation:worktree で実装する手順。develop skill がフェーズ D で参照する PO（Parallel Orchestra）後継。
user-invocable: false
---

# Parallel Agents（PO 後継、v1.12.0+）

`/develop` のフェーズ D で plan-report に YAML フロントマター（`po_plan_version: "0.1"`）が含まれるときに Read される手順。

C3 の親 Claude が plan-report の DAG を **wave 単位** で歩く:

1. 各 wave 内のタスクを 1 ターン内で複数 Agent ツール並列起動
2. 各 Agent は `isolation: "worktree"` 付きで独立した git worktree 内で完結
3. 親 Claude が wave 完了後に各 worktree の成果物を取り込み、一括コミット

permission race の構造的修正（2026-05-11 PoC で 15 並列・101 tool 呼び出し 0 失敗確認）と公式 `isolation: "worktree"` フロントマターにより、旧 PO（Parallel Orchestra）の並列実行レイヤを Claude Code 公式機能で代替する。

---

## 重要な制約: depth 1 制限

Claude Code のサブエージェントは **更にサブエージェントを spawn できない**（公式仕様 depth 1 制限）。これにより:

| agent 種別 | 並列起動可否 | 理由 |
|---|---|---|
| `developer` / `tester` / `code-reviewer` / `security-reviewer` 等 | **可** | 内部で Agent ツールを使わない |
| `tdd-develop` | **不可** | 内部で tester / developer を Agent ツールで spawn する設計 |

`tdd-develop` を含むタスクは次のいずれかで実行する:

- **ペルソナ採用パターン**: 親 Claude が `.claude/agents/tdd-develop.md` を Read して直接実行（並列度 1）
- **PO 委譲フォールバック**: `c3 po run-wave` で claude -p --agent 起動（v1.14.0 までの暫定）

planner エージェントが plan-report を生成する時点で「tdd-develop を含む wave は 1 タスクのみ」と粒度を制御することが望ましい。

---

## 前提条件

- plan-report が `.claude/reports/plan-report-*.md` の形式で配置され、YAML フロントマターを持つこと
- フロントマターが無ければ `.claude/skills/dev-workflow/SKILL.md` の D-1〜D-5 ceremony へフォールバック
- Claude Code の Agent ツールが `isolation: "worktree"` パラメータをサポートしていること（v2.1.x 以降）

---

## Step 0: 妥当性チェック

1. Glob で `.claude/reports/plan-report-*.md` の最新ファイルパスを取得
2. Read で内容（フロントマター含む）を確認
3. Bash で以下を実行する（v1.14.0 まで PO の dry-run を流用、v1.14.0 以降は親 Claude が直接 YAML 検証）:

   ```
   c3 po dry-run <plan-report-path>
   ```

   | exit | 意味 | 次のアクション |
   |---|---|---|
   | `0` | マニフェスト妥当 | Step 1 へ |
   | `2` | フロントマター不正 | stderr を整形して提示、`/start` フェーズ C 再実行を案内、スキル終了 |
   | `3` | 環境エラー | stderr を提示してスキル終了 |

---

## Step 1: wave 分解

Bash で以下を実行する（v1.14.0 まで PO の wave 分解を流用）:

```
c3 po waves <plan-report-path>
```

stdout の JSON 形式:

```json
{
  "waves": [
    {
      "index": 0,
      "tasks": [
        { "id": "dev-login", "agent": "developer", "read_only": false, "writes": ["src/auth/login.py"], "prompt": "..." },
        { "id": "dev-logout", "agent": "developer", "read_only": false, "writes": ["src/auth/logout.py"], "prompt": "..." }
      ]
    },
    {
      "index": 1,
      "tasks": [
        { "id": "review-auth", "agent": "code-reviewer", "read_only": true, "writes": [], "prompt": "..." }
      ]
    }
  ]
}
```

セッションファイル（`.claude/memory/sessions/YYYYMMDD.tmp`）に未登録の場合のみ以下を追記:
- 各 wave につき `- [ ] Wave {N} ({task_count} tasks, parallel={M})` を 1 行ずつ
  - `M` は wave 内で `tdd-develop` 以外の並列化可能タスク数

---

## Step 2: wave ごとに実行する

`waves` 配列を index 順にループする。各 wave で以下を順に行う。

### 2-A: wave 内容を提示する

親 Claude が wave のタスク一覧を Markdown 表で提示する:

| id | agent | parallelizable | read_only | writes |
|---|---|---|---|---|
| dev-login | developer | yes | false | src/auth/login.py |
| dev-logout | developer | yes | false | src/auth/logout.py |
| tdd-mfa | tdd-develop | **no (depth 1)** | false | src/auth/mfa.py |

`parallelizable` は agent 名で判定:
- `tdd-develop` → `no (depth 1)`
- それ以外 → `yes`

### 2-B: 実行可否をユーザーに確認する

AskUserQuestion ツール:

```json
{
  "questions": [{
    "question": "Wave {N} を実行してよいですか？並列度 {M}、tdd-develop {K} 件は逐次実行。",
    "options": [
      { "label": "承認・進む", "description": "この wave を実行する" },
      { "label": "中断", "description": "ここで wave 実行を停止する。完了済みの wave はそのまま残る" }
    ]
  }]
}
```

「中断」の場合、セッションファイルの `## 試みたが失敗したアプローチ` に中断理由を追記してスキル終了。

### 2-C: タスクを実行する

タスクごとに分岐:

#### 2-C-1: `agent == "tdd-develop"` のとき（ペルソナ採用パターン、並列度 1）

depth 1 制限により Agent ツール起動不可。親 Claude が直接実行する:

1. `.claude/agents/tdd-develop.md` を Read してペルソナを採用する
2. `.claude/skills/worktree-tdd-workflow/SKILL.md` を Read して TDD ループ手順（tester→developer→tester）を取得する
3. タスクの `prompt` を実装内容として、worktree-tdd-workflow/SKILL.md の Step 1〜4 を **親 Claude が直接** 実行する。tester / developer は Agent ツールでスポーン可能（親 Claude depth 0 から depth 1 として完結）
4. ループ完了後、結果を 2-D の集約に渡す

#### 2-C-2: それ以外（並列化対象、Agent ツール並列起動）

並列化可能タスクを **1 ターン内で複数 Agent ツール呼び出し** として発行する。並列度の上限は **デフォルト 5、上限 15**（PoC で検証済み）。タスク数がそれ以上なら 5 件ずつのバッチに分割する。

各 Agent ツール呼び出しに以下を指定:

- `subagent_type`: 指定しない（カスタム agent は subagent_type 不可、ペルソナ採用は prompt 経由）
- `isolation`: `"worktree"`
- `run_in_background`: `true`
- `description`: タスク id（5 単語以内）
- `prompt`: 以下を含める
  - 「**Step 1: `.claude/agents/{agent_name}.md` を Read してペルソナを採用すること**」
  - タスクの `prompt` 本文
  - 「**禁止事項: git add / git commit / git push を実行しないこと**。コミットは親 Claude がユーザー承認後に行う」
  - 「**返り値フォーマット厳守**:
    ```
    [Result]
    - task_id: {id}
    - status: success | failure
    - writes_files: {ファイルパス一覧、改行区切り}
    - error_summary: {失敗時のエラー要約、無ければ「なし」}
    ```
    」

全タスクを 1 メッセージ内で発行したあと、各 Agent の完了通知が `<task-notification>` で順次届く。全件揃うまで待つ。

### 2-D: 結果集約

各 Agent の返答から task_id / status / worktree path / writes / error を Markdown 表に整形:

| id | agent | status | worktree | writes 取り込み | last_error |
|---|---|---|---|---|---|
| dev-login | developer | success | agent-{id} | src/auth/login.py | - |
| dev-logout | developer | failure | agent-{id} | - | TypeError in line 42 |

worktree path は Agent ツール返り値の `<worktree><worktreePath>...</worktreePath></worktree>` ブロックから取得する。

### 2-E: 失敗があったら方針を確認する

並列タスクで 1 件以上失敗した場合（2-C-1 の tdd-develop が不合格の場合も含む）、AskUserQuestion で次を確認する:

```json
{
  "questions": [{
    "question": "Wave {N} に失敗があります。どうしますか？",
    "options": [
      { "label": "リトライ", "description": "失敗したタスクのみもう一度実行する" },
      { "label": "スキップして次の wave へ", "description": "失敗を残したまま次の wave に進む" },
      { "label": "中断", "description": "ここで wave 実行を停止する。完了済みの wave はそのまま残る" }
    ]
  }]
}
```

- 「リトライ」 → 失敗タスクのみ 2-C を再実行（成功 worktree はそのまま残し、失敗 worktree は事前に `git worktree remove -f -f` で削除）
- 「スキップして次の wave へ」 → 失敗内容をセッションファイルの `## 試みたが失敗したアプローチ` に追記して次の wave へ
- 「中断」 → セッションファイルの該当 wave 行は `- [ ]` のままにしてスキル終了

### 2-F: wave 完了処理（成功時のみ）

全タスク成功した wave に対して以下を順に実行する。

#### 2-F-1: 成果物の取り込み

各 worktree の `writes` ファイルを main に取り込む。**親 Claude が一括で行う**:

```bash
# 各 worktree ブランチから writes ファイルだけ checkout
git checkout worktree-agent-{id} -- src/auth/login.py
git checkout worktree-agent-{id2} -- src/auth/logout.py
```

注意:
- `writes` フィールドに列挙されたファイルのみを取り込む
- worktree が touch したが本タスクの責務でない周辺ファイル（`CLAUDE.md` / `package.json` / `.claude/settings.local.json` / `.claude/reports/` 配下）は取り込まない
- `tdd-develop` の場合は worktree-tdd-workflow.md 内で既に作業ツリーで実装されているため、追加の checkout は不要

#### 2-F-2: 親 Claude が一括コミット

親 Claude が `git status --short` を確認し、wave の成果物だけがステージングされていることを確認してから:

```bash
git add {writes ファイル一覧}
git commit -m "Wave {N}: {要約}"
```

メッセージには各タスクの目的を簡潔にまとめる（例: 「Wave 1: auth ログイン/ログアウト実装」）。

#### 2-F-3: worktree クリーンアップ

各 worktree を削除する。**`-f -f` フラグが必須**（Claude Code が worktree を `claude agent` lock で残すため）:

```bash
git worktree remove -f -f .claude/worktrees/agent-{id}
git worktree remove -f -f .claude/worktrees/agent-{id2}
git worktree prune
git branch -D worktree-agent-{id} worktree-agent-{id2}
```

#### 2-F-4: セッション記録

- `- [ ] Wave {N}` を `- [x] Wave {N}` に Edit
- `session_utils.append_checkpoint()` を呼び出して checkpoint ブロックを追記:

  ```bash
  python -c "from session_utils import append_checkpoint, SESSIONS_DIR; import os; \
    append_checkpoint(os.path.join(SESSIONS_DIR, '{YYYYMMDD}.tmp'), \
      'Wave {N} success', \
      '- 成功タスク: {M}件\n- 残 wave: {K}/{TOTAL}\n- 成果物: {要約}')" \
    --hooks-dir .claude/hooks
  ```

checkpoint の summary には KEEP ルール（設計判断・決定事項・解決済みのハマりどころ）に該当する情報のみ書く。雑談・進捗報告はセッションファイル本体（`## うまくいったアプローチ` 等）へ。

失敗 wave を「スキップして次の wave へ」した場合も同じ仕組みで記録する。label は `Wave {N} skipped`、summary に失敗内容と判断理由を書く。

---

## Step 3: フェーズ E への遷移

全 wave 完了後、フェーズ E（レビュー）への遷移を案内する。

- plan-report に reviewer タスク（agent が `code-reviewer` / `security-reviewer`）が含まれている場合は wave で既に実行済みなので、二重レビューを避けるためにユーザーへ「wave 内で reviewer タスクが完了済みなので E をスキップしてもよい」と提示する
- reviewer タスクが含まれていない場合は通常通りフェーズ E へ進む

---

## 知識蓄積

- 並列実行で **特定パターンが詰まりがち** と気付いたら、セッションファイルの `## 試みたが失敗したアプローチ` に追記し `patterns` に登録する
- 並列度を増やして race の兆候が出た場合は本 skill の上限値 15 を見直す（PoC では 15 並列まで 0 失敗確認済み）
- agent ツール並列起動の `<task-notification>` の到着順序は保証されないため、結果集約の表は task_id でソートして提示する
- worktree クリーンアップを忘れると `.claude/worktrees/` に dead worktree が積もる。`-f -f` フラグの必要性は PoC で実証済み

---

## PO 廃止移行期の注意（v1.12.0〜v1.14.0）

- 本 skill は v1.12.0 で導入された。`wave-execution.md` は当面残るが case B（複数 wave / PO 委譲）は deprecated 扱い
- v1.14.0 で `c3 po dry-run` / `c3 po waves` が削除される予定。その時点で Step 0 / Step 1 を「親 Claude が plan-report の YAML フロントマターを直接読んで DAG 分解」するロジックに置き換える
- v2.0.0 で `parallel_orchestra` パッケージ本体が削除される。本 skill の Step 0/1 を完全に PO 非依存に切り替えるのは v1.14.0 のタイミング
- 詳細計画: `~/.claude/plans/atomic-foraging-sprout.md`
