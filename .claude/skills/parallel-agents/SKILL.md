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

## depth 1 制限について

Claude Code のサブエージェントは **更にサブエージェントを spawn できない**（公式仕様 depth 1 制限）。v2.2.0 時点で配布されている全 agent は内部で Agent ツールを使わない設計のため、**すべて並列起動可能**。

将来的に「内部で Agent ツールを使う agent」を追加する場合は、その agent を含む wave のタスク数を 1 に絞る運用ガードが必要になる（v2.0.0 まで存在した `tdd-develop` agent はこのパターンだった）。

## subagent_type 明示指定と wt_* 名前空間（v2.2.0+）

`subagent_type` パラメータには **カスタム agent (`.claude/agents/*.md`) も指定可能**（公式仕様）。これにより frontmatter の `permissionMode` / `tools` / `model` / `memory` が subagent 起動レイヤーで自動適用される。

並列実行で permission プロンプトに詰まらないよう、v2.2.0 から **worktree 専用の `wt_*` プレフィックス agent** を導入した:

- `wt_tester` / `wt_developer` / `wt_systematic-debugger`: frontmatter に `permissionMode: bypassPermissions` を持つ並列専用バリアント
- 本体ロジックはオリジナルの `tester` / `developer` / `systematic-debugger` と同等。差分はレポート出力のファイル名規約のみで、並列専用 agent は `test-report-{task_id}.md` / `debug-needed-{task_id}.md` / `debug-analysis-{task_id}.md` を主経路とする（タイムスタンプ形式は task_id 不在時の保険）
- worktree 内のみで動作するため、`worktree_guard.py` (PreToolUse, `PO_WORKTREE_GUARD=1`) が worktree 外への書き込みをブロックする保護下にある

直接起動経路（worktree なし）では元の agent を使い、main リポジトリでの bypass を防ぐ。

---

## 前提条件

- plan-report が `.claude/reports/plan-report-*.md` の形式で配置され、YAML フロントマターを持つこと
- フロントマターが無ければ `.claude/skills/dev-workflow/SKILL.md` の D-1〜D-5 ceremony へフォールバック
- Claude Code の Agent ツールが `isolation: "worktree"` パラメータをサポートしていること（v2.1.x 以降）

---

## Step 0: 妥当性チェック

1. Glob で `.claude/reports/plan-report-*.md` の最新ファイルパスを取得
2. Read で内容（フロントマター含む）を確認
3. Bash で以下を実行する:

   ```
   c3 plan validate <plan-report-path>
   ```

   | exit | 意味 | 次のアクション |
   |---|---|---|
   | `0` | マニフェスト妥当 | Step 1 へ |
   | `2` | フロントマター不正・agent ファイル不在・循環依存等 | stderr を整形して提示、`/start` フェーズ C 再実行を案内、スキル終了 |

---

## Step 1: wave 分解

Bash で以下を実行する:

```
c3 plan waves <plan-report-path>
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
  - `M` は wave 内のタスク数（全 agent が並列起動可能）

---

## Step 2: wave ごとに実行する

`waves` 配列を index 順にループする。各 wave で以下を順に行う。

### 2-A: wave 内容を提示する

親 Claude が wave のタスク一覧を Markdown 表で提示する。plan-report の `agent` フィールドはそのまま表示し、実際に起動する subagent_type（並列専用バリアント）を補足する:

| id | agent (plan-report 表記) | 起動する subagent_type | read_only | writes |
|---|---|---|---|---|
| test-login | tester | `wt_tester` | false | tests/auth/test_login.py, .claude/reports/test-report-test-login.md |
| impl-login | developer | `wt_developer` | false | src/auth/login.py |
| confirm-login | tester | `wt_tester` | false | .claude/reports/test-report-confirm-login.md |

v2.2.0 以降、全 agent が並列起動可能のため `parallelizable` 列は省略する。subagent_type マッピングは 2-C 参照。

### 2-B: マイルストーン確認（設定時のみ）

dev-workflow フェーズ C の C-1 で「マイルストーンを設ける」を選んだ場合のみ、**指定した wave の直前**で AskUserQuestion を出す。それ以外の wave はスキップして 2-C へ直行する。

plan-report 承認時点で全タスク・agent・writes・prompt が確認済みのため、wave ごとの承認は不要。

マイルストーン wave の確認 UI（該当 wave のみ）:

```json
{
  "questions": [{
    "question": "Wave {N}（マイルストーン）に到達しました。続行しますか？",
    "options": [
      { "label": "続行する", "description": "この wave を実行する" },
      { "label": "中断", "description": "ここで wave 実行を停止する。完了済みの wave はそのまま残る" }
    ]
  }]
}
```

「中断」の場合、セッションファイルの `## 試みたが失敗したアプローチ` に中断理由を追記してスキル終了。

### 2-C: タスクを実行する（並列起動）

並列化可能タスクを **1 ターン内で複数 Agent ツール呼び出し** として発行する。並列度の上限は **デフォルト 5、上限 15**（PoC で検証済み）。タスク数がそれ以上なら 5 件ずつのバッチに分割する。

**重要**: 各タスクの `agent` 名は plan-report で `tester` / `developer` 等と書かれているが、**Agent ツール呼び出し時の `subagent_type` には並列専用バリアント (`wt_*`) を指定する**。マッピング表:

| plan-report の agent | 実際に指定する subagent_type | 補足 |
|---|---|---|
| `tester` | `wt_tester` | 並列 worktree 専用、`permissionMode: bypassPermissions` |
| `developer` | `wt_developer` | 同上 |
| `systematic-debugger` | `wt_systematic-debugger` | 同上 |
| `code-reviewer` | `code-reviewer` | レビュー専用（ソース編集なし）、`permissionMode: bypassPermissions` を元 agent に直接付与 |
| `security-reviewer` | `security-reviewer` | 同上 |

各 Agent ツール呼び出しに以下を指定:

- `subagent_type`: 上記マッピング表の値
- `isolation`: **`read_only: false` タスクのみ `"worktree"` を指定する。`read_only: true`（code-reviewer / security-reviewer）はソースを変更しないため worktree 不要。`isolation` を省略して main リポジトリで直接実行し、レポートを main の `.claude/reports/` に直接書かせる。**
  > **R5 hook による機械強制**: 上記ルールに違反して `read_only: true` のレビュータスクに `isolation: "worktree"` を指定した場合、`.claude/hooks/check_agent_invocation.py`（PreToolUse Agent hook）が exit 2 でブロックする。詳細は `.claude/rules/plan-design-guidelines.md` R5 参照。
- `run_in_background`: `true`
- `description`: タスク id（5 単語以内）
- `prompt`: 以下を含める（ペルソナ採用は不要、frontmatter / system prompt で自動適用される）:
  - **`read_only: false` タスクのみ: 先頭に `PO_WORKTREE_GUARD=1` を export する Bash 1 行を必須で含める** [SR-V-002]:
    ```
    Bash でまず以下を実行: `export PO_WORKTREE_GUARD=1`（worktree_guard.py PreToolUse が worktree 外書き込みをブロックする条件）
    ```
    worktree_guard.py はこの env 未設定時 `sys.exit(0)` で完全無効化されるため、wt_* agent 起動プロンプトの先頭で必ず設定すること。
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
  - **tester タスクの場合の追加注入**: 「`.claude/reports/test-report-{task_id}.md` を Write し、writes 宣言と一致させること」
  - **developer タスクの場合の追加注入**: 「Stuck Signal を返す場合は `.claude/reports/debug-needed-{task_id}.md` を Write する（task_id を含めることで親 Claude が後続 wave で systematic-debugger を呼ぶ際に対象 task を特定できる）。systematic-debugger は親 Claude が後続 wave で呼ぶ」
  - **systematic-debugger タスクの場合の追加注入**: 「`.claude/reports/debug-analysis-{task_id}.md` を Write し、writes 宣言と一致させること」

全タスクを 1 メッセージ内で発行したあと、各 Agent の完了通知が `<task-notification>` で順次届く。全件揃うまで待つ。

### 2-D: 結果集約

各 Agent の返答から task_id / status / worktree path / writes / error を Markdown 表に整形:

| id | agent | status | worktree | writes 取り込み | last_error |
|---|---|---|---|---|---|
| dev-login | developer | success | agent-{id} | src/auth/login.py | - |
| dev-logout | developer | failure | agent-{id} | - | TypeError in line 42 |

worktree path は Agent ツール返り値の `<worktree><worktreePath>...</worktreePath></worktree>` ブロックから取得する。

### 2-E: 失敗があったら方針を確認する

並列タスクで 1 件以上失敗した場合、AskUserQuestion で次を確認する（Green wave の失敗もここで吸収する）:

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
  # bash
  (cd .claude/hooks && python -c "from session_utils import append_checkpoint, SESSIONS_DIR; import os; \
    append_checkpoint(os.path.join(SESSIONS_DIR, '{YYYYMMDD}.tmp'), \
      'Wave {N} success', \
      '- 成功タスク: {M}件\n- 残 wave: {K}/{TOTAL}\n- 成果物: {要約}')")
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

