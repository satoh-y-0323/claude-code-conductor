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
- Claude Code の Agent ツールが `isolation: "worktree"` パラメータをサポートしていること（v2.1.x 以降）。v2.1.150 未満では Agent 完了時の worktree auto-cleanup が動作しないため、2-F-3 のフォールバック手順が毎回必要になる挙動差がある（利用者向け推奨設定は [`.claude/docs/parallel-agents-setup.md`](../../docs/parallel-agents-setup.md) を参照）

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
| `tester` | `wt_tester` | 並列 worktree 専用、`permissionMode: bypassPermissions`。**model: 指定対象外**（frontmatter 任せ・機械適用対象外） |
| `developer` | `wt_developer` | 同上。**起動時に PreToolUse hook（tier_autoapply）が推奨 Tier を `model:` へ自動適用する（機械適用・親 Claude の明示不要・2-C 参照）**。他バリアント（wt_tester / wt_systematic-debugger / reviewer）は対象外 |
| `systematic-debugger` | `wt_systematic-debugger` | 同上。**model: 指定対象外**（frontmatter 任せ・機械適用対象外） |
| `code-reviewer` | `code-reviewer` | レビュー専用（ソース編集なし）、`permissionMode: bypassPermissions` を元 agent に直接付与。**model: 指定対象外** |
| `security-reviewer` | `security-reviewer` | 同上。**model: 指定対象外** |

各 Agent ツール呼び出しに以下を指定:

- `subagent_type`: 上記マッピング表の値
- `model`: **親 Claude は `model:` を指定しない**。`wt_developer` タスクは起動時に PreToolUse hook（`tier_autoapply.py`）が `[tier-routing 推奨]`（developer 基準）の推奨 Tier を `model:` へ自動適用する（機械適用・親 Claude が model: を転記する必要はない）。この推奨 Tier の SSOT は `.claude/state/tier_selection.json` の `tier`（無ければ `suggested_model`）であり、kickoff の UserPromptSubmit で 1 度確定して以降 wave をまたいで安定する（`[tier-routing 推奨]` の表示テキストはその派生表示）。hook は実適用した model を `.claude/state/tier_autoapply.jsonl` に記録する（適用者=記録 SSOT）。並列 wave 内に複数の `wt_developer` が居る場合、**全 wt_developer は同一の推奨 Tier（単一 tier_selection.json.tier）で起動される。これは本 MVP の設計として明示的に許容する**（per-task complexity に応じて wt_developer ごとに tier を変える機能は本 MVP のスコープ外・フェーズ 3 以降）。推奨と異なる Tier を使いたい場合のみ `model:` を明示指定する（明示指定は hook に尊重され上書きされない）。`wt_tester` / `wt_systematic-debugger` / `code-reviewer` / `security-reviewer` は **model: 指定対象外**（frontmatter/元 agent 任せ・機械適用対象外）。fork は model 上書き不可のため対象外。
- `isolation`: **`read_only: false` タスクのみ `"worktree"` を指定する。`read_only: true`（code-reviewer / security-reviewer）はソースを変更しないため worktree 不要。`isolation` を省略して main リポジトリで直接実行し、レポートを main の `.claude/reports/` に直接書かせる。**
  > **R5 hook による機械強制**: 上記ルールに違反して `read_only: true` のレビュータスクに `isolation: "worktree"` を指定した場合、`.claude/hooks/check_agent_invocation.py`（PreToolUse Agent hook）が exit 2 でブロックする。詳細は `.claude/skills/dev-workflow/references/plan-design-guidelines.md` R5 参照。
- `run_in_background`: `true`
- `description`: タスク id（5 単語以内）
- `prompt`: 以下を含める（ペルソナ採用は不要、frontmatter / system prompt で自動適用される）:
  - **`read_only: false` タスクのみ: 先頭に `PO_WORKTREE_GUARD=1` を export する Bash 1 行を必須で含める** [SR-V-002]:
    ```
    Bash でまず以下を実行: `export PO_WORKTREE_GUARD=1`（worktree_guard.py PreToolUse が worktree 外書き込みをブロックする条件）
    ```
    worktree_guard.py はこの env 未設定時 `sys.exit(0)` で完全無効化されるため、wt_* agent 起動プロンプトの先頭で必ず設定すること。
  - **全 `read_only: false` タスク（wt_developer / wt_tester）: プロンプト先頭付近（PO_WORKTREE_GUARD の Bash 行の直後・タスク本文の前）に機械可読マーカー行を必須で 1 行含める**:
    ```
    C3_TASK_ID: {task_id}
    ```
    - `{task_id}` は `description`（タスク id）および record の `--task` に渡す値と**完全一致**させる（英数と `.` `_` `-` のみ・200 字以内）。
    - この行を PreToolUse hook（`tier_autoapply.py`）が抽出し applied-state（`tier_autoapply.jsonl`）の `task_id` に記録する。record が `--task` と突合して適用 tier を機械解決するための入力キー。
    - **三者一致の責務は親 Claude が負う**: マーカー値 `C3_TASK_ID: {task_id}`・`description`（タスク id）・record `--task {task_id}` の三者は、**親 Claude が同一の `task_id` 変数から 3 箇所（description / C3_TASK_ID マーカー / record `--task`）へ転記**し、大小・末尾空白・省略の表記ゆれを生じさせない（by convention の保証者を明文化）。転記ミス時の静かな優先2b フォールバックは record 側の stderr 警告で検知する二段構え。
    - **不均質 wave ではマーカー注入が必須**: escape hatch として一部タスクに `model:` を明示し wave 内で tier が不均質になる場合、マーカー欠落で優先2b に落ちると 2b は task を無視して session+role 最新の 1 行を拾うため、**別タスクの tier を誤帰属しうる**。均質 wave では顕在化しないが、`model:` 明示混在の**不均質 wave ではマーカー欠落＝誤帰属リスクのためマーカー注入を必須**とする。
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

- 「リトライ」 → 失敗タスクのみ 2-C を再実行（失敗 worktree は auto-cleanup 済みのことが多いが、2-F-3 の残留チェックを実施してから再開すること。残留があった場合のみ事前に `git worktree remove -f -f` で削除）
- 「スキップして次の wave へ」 → 失敗内容をセッションファイルの `## 試みたが失敗したアプローチ` に追記して次の wave へ
- 「中断」 → セッションファイルの該当 wave 行は `- [ ]` のままにしてスキル終了

**tier-routing 結果記録（失敗タスク）**: 選択肢に関わらず、この wave で失敗した各タスクのうち `wt_developer`/`wt_tester` で起動したものについて、1 タスク = 1 記録で failure を記録する（`wt_developer`→`--role developer`、`wt_tester`→`--role tester`。`--execution subagent`）。`code-reviewer`/`security-reviewer`/`wt_systematic-debugger` のタスクは記録対象外。この記録は親 Claude が **main リポジトリ（2-F-0 の `cd <ROOT>` 後）で実行**する。**「スキップして次の wave へ」を選んだ場合も、次の wave に進む前に必ずこの記録を済ませること**（`--complexity` は dev-workflow 開始時の `[tier-routing 推奨]` 表示の複雑度をそのまま渡す。「リトライ」を選び再実行が成功した場合は 1 タスク 1 記録の原則により 2-F-4 で改めて success を記録する＝リトライ結果は新たな別記録になる）。

**tier 記録ルール（ADR-AS-4 解消・applied-state task_id 突合）**:
- `wt_developer`→`developer` の記録は **`--tier` を付けない**。起動時に PreToolUse hook（`tier_autoapply`）が applied-state（`tier_autoapply.jsonl`）に記録した実適用 tier を、record が `(session_id, role, task_id)` 突合で機械解決する（優先2a）。**`--task {task_id}` は突合キーとして必須**であり、2-C のマーカー `C3_TASK_ID: {task_id}` と完全一致させる。**従来 `--task` は dedupe 専用の任意引数だったが、T8 で突合の必須キーへ役割が変わる**（未注入だと優先2b フォールバック＝session_id 一致だけの曖昧解決に戻り、並列で誤 tier 帰属リスクが残る）。applied-state の `task_id` 突合により、並列 wave 内の複数 wt_developer が同一 session_id でも task 単位で**一意**に実適用 tier を解決できる（ADR-AS-4 解消・T8）。hook の書き込み先が main の `.claude/state/` であること（cwd リーク下でも `__file__` 基準で不変）は T4 E2E で実測確認済み（2026-07-07・並列 wt_developer×2 で worktree 側 jsonl 0 件・main 側 2 行・record が `--tier` なしで正解 tier を機械解決）。
- `wt_tester`→`tester` の記録は **`--tier` を付けない**（frontmatter 解決のまま不変。tester は機械適用対象外）。

```bash
# wt_developer→developer（tier フラグは付けない・applied-state task 突合で機械解決）
c3 run .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
  --role developer --outcome failure --gate 2-E \
  --execution subagent --complexity {セッションファイルの tier-routing複雑度: 行の値} \
  --task {task_id}   # ← 2-C の C3_TASK_ID マーカーと完全一致・突合の必須キー

# wt_tester→tester（tier フラグは付けない・frontmatter 解決）
c3 run .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
  --role tester --outcome failure --gate 2-E \
  --execution subagent --complexity {セッションファイルの tier-routing複雑度: 行の値} \
  --task {task_id}
```

### 2-F: wave 完了処理（成功時のみ）

全タスク成功した wave に対して以下を順に実行する。

#### 2-F-0: 親 cwd をプロジェクトルートへ復帰（無条件・必須）

worktree Agent 完了後、Claude Code の既知バグ（[Issue #28017](https://github.com/anthropics/claude-code/issues/28017) "Task tool with isolation=worktree leaks CWD to parent session"・closed as duplicate）により、**親 Claude の Bash cwd が `.claude/worktrees/agent-*` 内へ移動したまま戻らない**ことがある（発生はバージョン・環境・タイミング依存）。この状態だと 2-F-1〜2-F-3 の取り込み・コミット・削除が worktree 内で走って正しく行われない（Issue でも "Subsequent Bash commands run in the wrong directory" と報告）。とくに cwd が worktree 内のままだと worktree ディレクトリの削除に失敗しやすい（OS によっては cwd 配下を削除できない）。

これを防ぐため、**2-F の最初に無条件でプロジェクトルートへ戻す**:

```bash
cd <ROOT>
```

- `<ROOT>` = プロジェクトルートの絶対パス。最初の worktree Agent を起動する前（cwd がまだルート）の `pwd` の値で、セッション開始時の作業ディレクトリと同じ。worktree 内から `git rev-parse --show-toplevel` を打つと worktree のルートが返るため、ルート判定には使わず **事前に控えた絶対パス** を使う。
- cwd が漏れていなくても `cd <ROOT>` は無害。判定せず毎 wave 無条件で実行する（Issue が挙げる公式ワークアラウンド "manually cd back" に準拠）。`cd` は C3 の pre_tool hook の検査対象外のためブロックされない。

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

#### 2-F-3: worktree クリーンアップ（残留チェックのみ）

Claude Code 2.1.x（少なくとも 2.1.150 で実測確認、2026-05-23）以降、`isolation:"worktree"` 付き Agent は完了時に **物理ディレクトリ・worktree 登録・`worktree-agent-*` ブランチが自動削除** される（foreground / background / 並列 / 失敗ケース全パターンで検証済み。詳細: `.claude/reports/worktree-cleanup-verification-20260523-234110.md`）。

そのため明示的な `git worktree remove` は **不要**。auto-cleanup が race や障害でスキップされた場合のセーフティとして残留チェックのみ行う:

```bash
# 念のため worktree 登録の残留を確認、あれば prune（物理ディレクトリも片付く）
git worktree list --porcelain
git worktree prune

# 念のため worktree-agent-* ブランチの残留チェック（あれば手動削除を検討）
# Windows PowerShell では grep 未インストール時に動作しないため、代替として
# `git branch --list "worktree-agent-*"` を使う:
git branch -a | grep -E "worktree-agent-" || true
# Windows 代替: git branch --list "worktree-agent-*"
```

残留があった場合のみ手動 cleanup（古い Claude Code バージョン互換 or auto-cleanup 失敗ケース）:

```bash
git worktree remove -f -f .claude/worktrees/agent-{id}  # -f -f は claude agent lock 残留対策
git branch -D worktree-agent-{id}
```

#### 2-F-4: セッション記録

- `- [ ] Wave {N}` を `- [x] Wave {N}` に Edit
- `現在地:` を以下のルールで Edit（`dev-workflow/SKILL.md` の「セッションファイル運用総則」参照）:
  - Wave N 成功時: `現在地: Wave {N} 完了 / 次: Wave {N+1}`
  - 最終 Wave 完了時: `現在地: 完了`（レビューへ遷移する場合は `現在地: フェーズE レビュー中`）
  - Wave をスキップした時: `現在地: Wave {N} skipped / 次: Wave {N+1}`
- `session_utils.append_checkpoint()` を呼び出して checkpoint ブロックを追記。自由記述サマリ（`{要約}`）を Python 文字列リテラルにも bash heredoc にも直接埋め込まない。**親 Claude が `Write` ツールでサマリ本文を固定パス `<ROOT>/.claude/tmp/wave-checkpoint-summary.txt` へ書き込み**、bash 側は固定コード + 固定ファイルパス引数で `append_checkpoint` を呼ぶだけにする。`Write` ツールはテキストをそのまま書き込むだけで bash/heredoc のような区切り子解釈（終端行衝突・変数展開・コマンド置換）を持たないため、この注入クラスを構造的に排除できる。さらに固定パス方式により、**bash に可変内容（パス文字列の置換）を渡さないため、クォート脱出トリガ文字（`'` や `"` など）の混入を完全に無効化できる**（周回5 SR-INJ-002 指摘の構造的対処）。同一リポで複数の親 Claude セッションを並行させた場合、サマリが上書きされうるが、上書きで起きるのは checkpoint 本文の取り違え（コマンド実行には至らない）に限られ、C3 の想定運用（1 リポ 1 親セッション）では発生しないため許容：

  1. **親 Claude が `Write` ツール**でサマリ本文を **`<ROOT>/.claude/tmp/wave-checkpoint-summary.txt`** へ書き込む。本文例:
     ```
     - 成功タスク: {M}件
     - 残 wave: {K}/{TOTAL}
     - 成果物: {要約}
     ```
     `.claude/tmp/` は既存の一時ファイル置き場であり、`Write` ツールは親ディレクトリを自動作成する。
  2. **bash は固定コード + 固定ファイルパス引数で `append_checkpoint` を呼ぶだけ**（bash 側にサマリ本文を一切渡さない）。可変内容（パス置換）をプレースホルダー経由で bash に渡さず、ファイルパスは固定リテラルにする（クォート脱出クラスを構造的に排除）。cwd が `.claude/hooks` のため相対パス `../tmp/wave-checkpoint-summary.txt` で解決:
     ```bash
     (cd .claude/hooks && c3 run -c "
import sys
import os
from session_utils import append_checkpoint, SESSIONS_DIR

summary_file = '../tmp/wave-checkpoint-summary.txt'
if os.path.isfile(summary_file):
    with open(summary_file, 'r', encoding='utf-8') as f:
        summary = f.read()
else:
    summary = '[summary file not found]'

append_checkpoint(os.path.join(SESSIONS_DIR, '{YYYYMMDD}.tmp'),
  'Wave {N} success', summary)
" "../tmp/wave-checkpoint-summary.txt")
     ```
  3. `{要約}` の内容（サマリ本文）を bash コマンドのどの位置にも直接埋め込んではならない（heredoc の復元・固定コード文字列の改変・パス引数位置への本文貼り付けは禁止）。本文は必ず `Write` ツールでファイルへ書き、bash には固定リテラルのコードとパス引数のみを渡す。NG 例: `append_checkpoint(..., 'Wave {N} success', '{要約の本文をここに貼る}')`、`c3 run -c "..."` の固定コード部分の変更・パス引数の変更。
  4. 記録後、一時ファイルは不要なら削除してよい（`rm -f .claude/tmp/wave-checkpoint-summary.txt`）。

**tier-routing 結果記録（成功タスクのみ）**: この wave で成功した各タスクのうち `wt_developer`/`wt_tester` で起動したものについて、1 タスク = 1 記録で success を記録する（`wt_developer`→`--role developer`、`wt_tester`→`--role tester`。`--execution subagent`）。`code-reviewer`/`security-reviewer`/`wt_systematic-debugger` のタスクは記録対象外（`--complexity` は dev-workflow 開始時の `[tier-routing 推奨]` 表示の複雑度をそのまま渡す）。この記録は親 Claude が **main リポジトリ（2-F-0 の `cd <ROOT>` 後）で実行**する。

**tier 記録ルール（ADR-AS-4 解消・2-E と同一）**: `wt_developer`→`developer` は **`--tier` を付けない**。起動時に hook（`tier_autoapply`）が applied-state（`tier_autoapply.jsonl`）に記録した実適用 tier を、record が `(session_id, role, task_id)` 突合で機械解決する（優先2a）。**`--task {task_id}` は突合キーとして必須**であり、2-C のマーカー `C3_TASK_ID: {task_id}` と完全一致させる（従来 `--task` は dedupe 専用の任意引数だったが、T8 で突合の必須キーへ役割が変わる）。applied-state の `task_id` 突合により、同一 session_id の複数 wt_developer を task 単位で**一意**に分離できる（ADR-AS-4 解消・T8）。hook の書き込み先が main の `.claude/state/` であること（cwd リーク下でも `__file__` 基準で不変）は T4 E2E で実測確認済み（2026-07-07・並列 wt_developer×2 で worktree 側 jsonl 0 件・main 側 2 行・record が `--tier` なしで正解 tier を機械解決）。 `wt_tester`→`tester` は **`--tier` を付けない**（frontmatter 解決のまま不変・機械適用対象外）。

```bash
# wt_developer→developer（tier フラグは付けない・applied-state task 突合で機械解決）
c3 run .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
  --role developer --outcome success --gate 2-D \
  --execution subagent --complexity {セッションファイルの tier-routing複雑度: 行の値} \
  --task {task_id}   # ← 2-C の C3_TASK_ID マーカーと完全一致・突合の必須キー

# wt_tester→tester（tier フラグは付けない・frontmatter 解決）
c3 run .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
  --role tester --outcome success --gate 2-D \
  --execution subagent --complexity {セッションファイルの tier-routing複雑度: 行の値} \
  --task {task_id}
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
- Claude Code 2.1.x 以降は Agent 完了時に worktree auto-cleanup されるため、明示的な `git worktree remove` は基本不要（2-F-3 参照）。`.claude/worktrees/` に dead worktree が残る場合は古い Claude Code バージョンで作成された残骸の可能性が高い

