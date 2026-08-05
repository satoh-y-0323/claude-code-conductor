# C3 仕様目次 — agent 層

対象: `.claude/agents/*.md`（**14 ファイル**・`ls` 実測 14 件 / 本書の `##` 見出し 14 件で一致）
作成日: 2026-08-05 / 調査方法: 全ファイル Read + 起動元は `grep` 実測（推測記述なし）

---

## サマリ

| 指標 | 件数 |
|---|---|
| agent 定義ファイル数 | 14 |
| 本書に載せた agent 数 | 14（`ls` 件数と一致） |
| `[起動元なし]` | **4 件**（`interviewer` / `architect` / `planner` / `wt_systematic-debugger`） |
| `[規範衝突]` | **7 件**（下記 §`[規範衝突]` 一覧） |
| `[未文書化]` | **9 件**（下記 §`[未文書化]` 一覧） |
| `memory: project` を持つ agent | 9（うち `.claude/agent-memory/` に実ディレクトリがあるのは 7） |
| `permissionMode: bypassPermissions` を持つ agent | 6（`code-reviewer.md:5` / `design-critic.md:5` / `security-reviewer.md:5` / `wt_developer.md:5` / `wt_systematic-debugger.md:5` / `wt_tester.md:5`） |
| `model: opus` の agent | 5（`architect.md:3` / `design-critic.md:3` / `doc-writer.md:3` / `planner.md:3` / `project-setup.md:3`。残り 9 は `model: sonnet`） |

### `[起動元なし]` の内訳（本命の発見）

| agent | 種別 | 実測 |
|---|---|---|
| `interviewer` | persona 専用（定義と整合） | Agent ツール起動指示 0 件。`.claude/skills/dev-workflow/SKILL.md:75` が定義ファイルを Read して親 Claude がペルソナ採用するのみ（`interviewer.md:14` の「サブエージェントとして起動しない」と整合） |
| `architect` | persona 専用（定義と整合） | 同上・`.claude/skills/dev-workflow/SKILL.md:140`（`architect.md:15` と整合） |
| `planner` | persona 専用（定義と整合） | 同上・`.claude/skills/dev-workflow/SKILL.md:206`（`planner.md:15` と整合） |
| `wt_systematic-debugger` | **実効ゼロ** | 起動経路は `.claude/skills/parallel-agents/SKILL.md:151` のマッピング表 1 行のみ。plan-report に `agent: systematic-debugger` タスクを生成させる指示は grep 0 件（`plan-design-guidelines.md:28-31` の 3-wave 分解表は `wt_tester` / `wt_developer` のみ・同 `:46` は「親 Claude が後続 wave で `systematic-debugger`（素の名前）を呼ぶ」と書く）。`.claude/agent-memory/wt_systematic-debugger/` も未作成 |

---

## architect

| 項目 | 内容 |
|---|---|
| frontmatter | `name: architect` / `model: opus` / `tools: Read, Write, Edit, Glob, Grep, Skill`（`memory` なし・`permissionMode` なし） |
| 入力 | requirements-report を Read／既存コードがあれば Glob・Grep（直接開始時は `.claude/reports/requirements-report-*.md` の最新を Glob → Read） |
| 出力 | `.claude/reports/architecture-report-{timestamp}.md`（timestamp は `report-timestamp` skill 取得） |
| 起動元 | **Agent ツール起動 0 件** `[起動元なし]`。`.claude/skills/dev-workflow/SKILL.md:140` フェーズ B で親 Claude が定義を Read してペルソナ採用 |
| 禁止事項 | ソースファイルの編集・書き込みを行わない／タスク分解・工数見積もり・実装・コーディング |
| 根拠 | `.claude/agents/architect.md:2-11`（frontmatter）`:15`（persona 宣言）`:29-32`（禁止）`:37-38`（入力）`:46`（出力）`:49`（制限） / `.claude/skills/dev-workflow/SKILL.md:140`・`:142-143`（起動元・直接開始時の入力）・`:189`（subagent 起動は条件節のみ） |

---

## code-reviewer

| 項目 | 内容 |
|---|---|
| frontmatter | `name: code-reviewer` / `model: sonnet` / `memory: project` / `permissionMode: bypassPermissions` / `tools: Read, Write, Bash, Glob, Grep, Skill` |
| 入力 | `git diff` または変更ファイル一覧（Bash）・関連テストコード・`.claude/skills/dev-workflow/references/code-review-checklist.md`（起動プロンプトでの入力指定は E-1 側に**なし**） |
| 出力 | `.claude/reports/code-review-report-{timestamp}.md` ＋ `.claude/agent-memory/code-reviewer/MEMORY.md` 追記（200 行 / 25KB 上限） |
| 起動元 | `.claude/skills/dev-workflow/SKILL.md:938`（フェーズ E-1）／`.claude/skills/parallel-agents/SKILL.md:152` マッピング表（`read_only: true` タスク・`isolation` 省略で main 直接） |
| 禁止事項 | ソースファイルの編集・書き込みを行わない／セキュリティ脆弱性診断（security-reviewer の担当）／`isolation: "worktree"` 併用（hook が exit 2 ブロック） |
| 根拠 | `.claude/agents/code-reviewer.md:2-13`（frontmatter）`:22-26`（memory）`:36-38`（禁止）`:43-45`（入力）`:50-51`（`[CR-XX-NNN]`/`[CR-NEW]` 規約）`:56`（出力）`:59`（制限） / `.claude/skills/dev-workflow/SKILL.md:938` / `.claude/skills/parallel-agents/SKILL.md:152`・`:159` / `.claude/hooks/check_agent_invocation.py:50`・`:86-98`（R5 ブロック） |

---

## design-critic

| 項目 | 内容 |
|---|---|
| frontmatter | `name: design-critic` / `model: opus` / `memory: project` / `permissionMode: bypassPermissions` / `tools: Read, Write, Glob, Grep, Skill` |
| 入力 | `design-critic-rubric.md` ＋ `requirements-report-*.md` / `architecture-report-*.md` / `plan-report-*.md` の各最新 1 件（自分で Glob → Read。起動プロンプトは起動指示のみ） |
| 出力 | `.claude/reports/design-review-report-{timestamp}.md` ＋ `.claude/agent-memory/design-critic/MEMORY.md` 追記 |
| 起動元 | `.claude/skills/dev-workflow/SKILL.md:394`（フェーズ C-3・opt-in ゲート。`subagent_type: "design-critic"` 固有名明示・`isolation: worktree` 不使用） |
| 禁止事項 | design-review-report の新規 Write **以外**のファイル編集・書き込み全面禁止／ソース編集／コード品質レビュー／セキュリティ診断／実装方針の決定 |
| 根拠 | `.claude/agents/design-critic.md:2-12`（frontmatter）`:21-25`（memory）`:35-39`（禁止）`:44-48`（入力）`:51-59`（3 レンズ・finding 必須項目）`:62`（出力）`:65`（制限） / `.claude/skills/dev-workflow/SKILL.md:392-399`（起動元・プロンプト内容） |

---

## developer

| 項目 | 内容 |
|---|---|
| frontmatter | `name: developer` / `model: sonnet` / `memory: project` / `tools: Read, Write, Edit, Bash, Glob, Grep, TodoWrite, Skill`（`permissionMode` なし） |
| 入力 | plan-report を Read・既存コードを Glob/Grep／bug-fix・stuck 再実行時は debug-analysis の**ファイルパスのみ**を受領し agent 側で Read |
| 出力 | ソース実装（1 タスク = 1 コミット）／stuck 時のみ `.claude/reports/debug-needed-{timestamp}.md`／`.claude/agent-memory/developer/MEMORY.md`（定常レポート出力は**なし**） |
| 起動元 | `.claude/skills/dev-workflow/SKILL.md:598`（D-2 bug-fix）・`:663`（D-2 Green）・`:679`（D-2.5 の再実行）・`:767`（D-4 Refactor） |
| 禁止事項 | テスト仕様の設計・テストコードの新規作成／セキュリティ診断・品質レビュー／設計の根本変更／秘密鍵・APIキー・パスワードの直書き |
| 根拠 | `.claude/agents/developer.md:2-14`（frontmatter）`:23-27`（memory）`:37-40`（禁止）`:45-46`（入力）`:49-56`（During 規範）`:59-64`（基本検証）`:68-75`（Stuck Signal 出力）`:78-80`（制限） / `.claude/skills/dev-workflow/SKILL.md:598`・`:663`・`:677-679`・`:767` / `.claude/hooks/tier_autoapply.py:99`（APPLY_ROLES 自動 model 注入） |

---

## doc-writer

| 項目 | 内容 |
|---|---|
| frontmatter | `name: doc-writer` / `model: opus` / `tools: Read, Write, Glob, Grep, Bash, Skill`（`memory` なし・`permissionMode` なし） |
| 入力 | 起動プロンプトの 6 項目（ドキュメント種類 / 対象ファイル・ディレクトリ / 読み手 / 目的 / 粒度 / 出力先）＋自分で Glob・Grep・Read した対象コード |
| 出力 | 3 分岐: (a) 指定パスへ直 Write（ディレクトリ不在なら Bash で作成） (b) `.claude/reports/doc-{名前}-{timestamp}.md` (c) 表示のみ（Write なし） |
| 起動元 | `.claude/skills/doc/SKILL.md:167`（Step 8）。**定義本文が書く起動元 `.claude/commands/doc.md` は存在しない**（`commands/` ディレクトリ自体がリポジトリ内に 0 件） |
| 禁止事項 | ソースコードの編集・修正／ドキュメント要件のヒアリング／セキュリティ診断・コードレビュー／読み取れない背景は「〜と考えられる」と明記 |
| 根拠 | `.claude/agents/doc-writer.md:2-11`（frontmatter）`:30-33`（禁止）`:38-40`（入力）`:52`（推測禁止）`:55-58`（出力）`:61`（制限）`:64`（**誤った起動元**） / `.claude/skills/doc/SKILL.md:167`・`:170-179`（実際の起動元とプロンプト） / `find . -type d -name commands` 実測 0 件 |

---

## interviewer

| 項目 | 内容 |
|---|---|
| frontmatter | `name: interviewer` / `model: sonnet` / `tools: Read, Write, Glob, Grep, Skill`（`memory` なし・`permissionMode` なし） |
| 入力 | ユーザーとの対話／既存 `requirements-report` があれば Read（差分ヒアリング用） |
| 出力 | `.claude/reports/requirements-report-{timestamp}.md` |
| 起動元 | **Agent ツール起動 0 件** `[起動元なし]`。`.claude/skills/dev-workflow/SKILL.md:75` フェーズ A で親 Claude が定義を Read してペルソナ採用 |
| 禁止事項 | 設計・技術選定／**ソースコードの読み込み・編集**／実現可能性の判断／ソースファイルの編集・書き込み |
| 根拠 | `.claude/agents/interviewer.md:2-10`（frontmatter）`:14`（persona 宣言）`:26-29`（禁止）`:34`（入力）`:42`（出力）`:45`（制限） / `.claude/skills/dev-workflow/SKILL.md:75`・`:123`（subagent 起動は条件節のみ） |

---

## planner

| 項目 | 内容 |
|---|---|
| frontmatter | `name: planner` / `model: opus` / `tools: Read, Write, Edit, Glob, Grep, Skill`（`memory` なし・`permissionMode` なし） |
| 入力 | **必読** `.claude/skills/dev-workflow/references/plan-design-guidelines.md` ＋ 利用可能な全レポート（requirements / architecture / test / review。不在フェーズはスキップして正常） |
| 出力 | `.claude/reports/plan-report-{timestamp}.md`（YAML フロントマター必須: `po_plan_version`（`"0.1"` \| `"sequential"`）/ `name` / `cwd: "../.."` / `tasks[]`（`id`/`agent`/`read_only`/`prompt` 必須）） |
| 起動元 | **Agent ツール起動 0 件** `[起動元なし]`。`.claude/skills/dev-workflow/SKILL.md:206` フェーズ C で親 Claude が定義を Read してペルソナ採用 |
| 禁止事項 | 設計判断／ソース編集／テスト・レビュー実施／`tasks[].id` 重複・未定義 `depends_on`・agent 名 typo の出力／guidelines ルール 1〜15・R2〜R6 違反 plan の出力 |
| 根拠 | `.claude/agents/planner.md:2-11`（frontmatter）`:15`（persona 宣言）`:29-32`（禁止）`:37-39`（入力）`:47-55`（出力仕様）`:58-65`（制限・自動検査対象） / `.claude/skills/dev-workflow/SKILL.md:206`・`:332`（subagent 起動は条件節のみ） / `.claude/hooks/planner_check.py`・`.dev/hooks/_planner_check.py`・`.claude/hooks/check_agent_invocation.py`（R2〜R6 の検査主体） |

---

## project-setup

| 項目 | 内容 |
|---|---|
| frontmatter | `name: project-setup` / `model: opus` / `tools: Read, Write, Glob, WebSearch, WebFetch`（`Edit` / `Bash` / `Grep` / `Skill` **なし**・`memory` なし・`permissionMode` なし） |
| 入力 | 親プロンプトのスタック情報・チーム規約ヒアリング結果 ＋ `.claude/skills/setup/templates/{coding-standards,project-conventions}-template.md` ＋ `.claude/skills/setup/reference.md` ＋ WebSearch/WebFetch の調査結果 |
| 出力 | `.claude/rules/coding-standards.md` と `.claude/rules/project-conventions.md`（レポートは出力しない・完了報告は本文出力のみ） |
| 起動元 | `.claude/skills/setup/SKILL.md:184`（Phase 3）。setup skill 自体の起動経路は `/setup` 明示 or `init-session` ガード G-2 の `SETUP_NEEDED` の 2 経路のみに制限 |
| 禁止事項 | ユーザーへの質問・ヒアリング／規約ファイル以外のソースファイルの編集／プロジェクトの設計・アーキテクチャ判断 |
| 根拠 | `.claude/agents/project-setup.md:2-11`（frontmatter）`:18`（対話禁止）`:27-30`（禁止）`:34-39`（テンプレ Read）`:41-44`（既存ファイル差分反映）`:56-66`（置換ルール・出力先）`:73`（制限） / `.claude/skills/setup/SKILL.md:8-11`（起動経路制約）・`:184-205`（起動元とプロンプト） |

---

## security-reviewer

| 項目 | 内容 |
|---|---|
| frontmatter | `name: security-reviewer` / `model: sonnet` / `memory: project` / `permissionMode: bypassPermissions` / `tools: Read, Write, Bash, Glob, Grep, Skill` |
| 入力 | 変更ファイルと依存関係（Bash/Glob/Grep）・認証/外部入力/DB アクセスのコード・`.claude/skills/dev-workflow/references/security-review-checklist.md`（起動プロンプトでの入力指定は E-2 側に**なし**） |
| 出力 | `.claude/reports/security-review-report-{timestamp}.md` ＋ `.claude/agent-memory/security-reviewer/MEMORY.md` 追記 |
| 起動元 | `.claude/skills/dev-workflow/SKILL.md:1076`（フェーズ E-2）／`.claude/skills/parallel-agents/SKILL.md:153` マッピング表（`read_only: true`・`isolation` 省略） |
| 禁止事項 | コード品質・保守性レビュー（code-reviewer の担当）／ソースコードの編集・修正／`isolation: "worktree"` 併用（hook が exit 2 ブロック） |
| 根拠 | `.claude/agents/security-reviewer.md:2-13`（frontmatter）`:22-26`（memory）`:37-39`（禁止）`:44-46`（入力）`:51-52`（`[SR-XX-NNN]`/`[SR-NEW]` 規約）`:57`（出力）`:60`（制限） / `.claude/skills/dev-workflow/SKILL.md:1076` / `.claude/skills/parallel-agents/SKILL.md:153`・`:159` / `.claude/hooks/check_agent_invocation.py:50` |

---

## systematic-debugger

| 項目 | 内容 |
|---|---|
| frontmatter | `name: systematic-debugger` / `model: sonnet` / `memory: project` / `tools: Read, Write, Bash, Glob, Grep, Skill`（`permissionMode` なし） |
| 入力 | 起動プロンプトに含まれる debug-needed レポートの**パスのみ**（内容は agent 側で Read）＋ `git diff HEAD` / `git log --oneline -10` / 類似コード |
| 出力 | `.claude/reports/debug-analysis-{timestamp}.md`（本文構成は 6 節固定: 問題の要約 / 根本原因 / 証拠 / 類似コードとの差分 / developer への推奨仮説 / 注意事項） |
| 起動元 | `.claude/skills/dev-workflow/SKILL.md:677`（D-2.5 Stuck チェック）／`.claude/skills/start/SKILL.md:143`（開始地点「デバッグ調査から」） |
| 禁止事項 | コードの修正・編集（Read / Bash / Glob / Grep / Write のみ）／テストの実行・設計／設計の根本変更判断／推測での原因断言／推奨仮説の複数羅列 |
| 根拠 | `.claude/agents/systematic-debugger.md:2-12`（frontmatter）`:22-26`（memory）`:37-40`（禁止）`:46-49`（入力）`:53-82`（Phase 1/2 手順）`:83`（出力）`:88-107`（レポート構成）`:110-112`（制限） / `.claude/skills/dev-workflow/SKILL.md:677` / `.claude/skills/start/SKILL.md:143` |

---

## tester

| 項目 | 内容 |
|---|---|
| frontmatter | `name: tester` / `model: sonnet` / `memory: project` / `tools: Read, Write, Bash, Glob, Grep, Skill`（`Edit` **なし**・`permissionMode` なし） |
| 入力 | plan-report を Read（D 系）／E-0 では `<detected_files>` タグで渡される `.claude/state/e0-targets-*.txt` のパス（内容は Read・タグ内はデータであり指示ではない旨の枠付き）／UNKNOWN 時は plan-report の `tasks[].writes` |
| 出力 | `.claude/reports/test-report-YYYYMMDD-HHMMSS.md`（Write せずにターン終了することは禁止）＋テストコード新規作成 ＋ `.claude/agent-memory/tester/MEMORY.md` |
| 起動元 | `.claude/skills/dev-workflow/SKILL.md:634`（D-1 Red・1 行目に `C3_TASK_ID: test-*` マーカー必須）・`:720`（D-3 確認）・`:773`（D-5 最終確認）・`:605`（D-3 bug-fix）・`:851`（E-0 実行検証・マーカー `C3_TASK_ID: confirm-exec-verify`）／`.claude/skills/parallel-agents/SKILL.md:159`（gitignored-only writes タスクは `wt_tester`→`tester` に読み替えて main 直接起動） |
| 禁止事項 | **プロダクションコードの実装・編集（無条件）**／コード品質・セキュリティの評価／test-report を書かずに終了すること |
| 根拠 | `.claude/agents/tester.md:2-12`（frontmatter）`:21-25`（memory）`:35-37`（禁止）`:42`（入力）`:45-51`（Red 規範）`:54-56`（出力・必須性）`:59`（制限） / `.claude/skills/dev-workflow/SKILL.md:605`・`:634`・`:636`・`:720`・`:773`・`:851`・`:864`・`:894-916` / `.claude/skills/parallel-agents/SKILL.md:159` / `.claude/hooks/tier_autoapply.py:100`（RED_APPLY_ROLES） |

---

## wt_developer

| 項目 | 内容 |
|---|---|
| frontmatter | `name: wt_developer` / `model: sonnet` / `memory: project` / `permissionMode: bypassPermissions` / `tools: Read, Write, Edit, Bash, Glob, Grep, TodoWrite, Skill` |
| 入力 | plan-report を Read ＋ 起動プロンプト 1 行目の `C3_TASK_ID: {task_id}` マーカー ＋ 2 行目以降の `export PO_WORKTREE_GUARD=1` 指示（task_id は description / record `--task` と三者一致） |
| 出力 | ソース実装（worktree 内）／stuck 時 `.claude/reports/debug-needed-{task_id}.md`（保険経路のみ `-{timestamp}`）／`.claude/agent-memory/wt_developer/MEMORY.md` |
| 起動元 | `.claude/skills/parallel-agents/SKILL.md:150` マッピング表（plan-report の `agent: developer`・`isolation: "worktree"` 付き並列起動）。単発起動では**使わない**と定義本文が明示 |
| 禁止事項 | テスト仕様の設計・テストコードの新規作成／セキュリティ診断・品質レビュー／設計の根本変更／秘密情報の直書き／worktree 外への書き込み（`worktree_guard.py` PreToolUse がガード） |
| 根拠 | `.claude/agents/wt_developer.md:2-15`（frontmatter）`:20-22`（用途と worktree ガード）`:28-32`（memory）`:42-45`（禁止）`:50-51`（入力）`:73-80`（Stuck Signal 出力）`:83-85`（制限） / `.claude/skills/parallel-agents/SKILL.md:150`・`:155-193`（起動パラメータ・プロンプト規約） / `.claude/hooks/tier_autoapply.py:99` |

---

## wt_systematic-debugger

| 項目 | 内容 |
|---|---|
| frontmatter | `name: wt_systematic-debugger` / `model: sonnet` / `memory: project` / `permissionMode: bypassPermissions` / `tools: Read, Write, Bash, Glob, Grep, Skill` |
| 入力 | 起動プロンプトに含まれる debug-needed レポートのパス ＋ `task_id`（`.claude/reports/debug-analysis-{task_id}.md` を writes 宣言と一致させるための追加注入） |
| 出力 | `.claude/reports/debug-analysis-{task_id}.md`（保険経路のみ `-{timestamp}`）＋ `.claude/agent-memory/wt_systematic-debugger/MEMORY.md`（ディレクトリ未作成） |
| 起動元 | **実効ゼロ** `[起動元なし]`。`.claude/skills/parallel-agents/SKILL.md:151` のマッピング表 1 行のみが経路で、その前提となる `agent: systematic-debugger` タスクを plan に生成させる指示は grep 0 件 |
| 禁止事項 | コードの修正・編集（Read / Bash / Glob / Grep / Write のみ）／テストの実行・設計／設計の根本変更判断／推測での断言／仮説の複数羅列 |
| 根拠 | `.claude/agents/wt_systematic-debugger.md:2-14`（frontmatter）`:18-20`（用途）`:27-31`（memory）`:42-45`（禁止）`:51`（入力）`:86-88`（出力）`:115-117`（制限） / `.claude/skills/parallel-agents/SKILL.md:151`・`:191` / `.claude/skills/dev-workflow/references/plan-design-guidelines.md:28-31`（3-wave 表に不在）・`:46`（素の名前で呼ぶと記載） |

---

## wt_tester

| 項目 | 内容 |
|---|---|
| frontmatter | `name: wt_tester` / `model: sonnet` / `memory: project` / `permissionMode: bypassPermissions` / `tools: Read, Write, Bash, Glob, Grep, Skill`（`Edit` **なし**） |
| 入力 | plan-report を Read ＋ 起動プロンプト 1 行目の `C3_TASK_ID: {task_id}` マーカー ＋ `export PO_WORKTREE_GUARD=1` 指示 ＋ 「`test-report-{task_id}.md` を Write し writes 宣言と一致させること」の追加注入 |
| 出力 | `.claude/reports/test-report-{task_id}.md`（保険経路のみ `-{timestamp}`）＋テストコード ＋ `.claude/agent-memory/wt_tester/MEMORY.md` |
| 起動元 | `.claude/skills/parallel-agents/SKILL.md:149` マッピング表（plan-report の `agent: tester`）。ただし `writes` が全て gitignored のタスクは `:159` により素の `tester` へ読み替えられ本 agent は起動されない |
| 禁止事項 | プロダクションコードのソースファイルを編集・書き込みしない／test-report を書かずに終了すること／worktree 外への書き込み |
| 根拠 | `.claude/agents/wt_tester.md:2-13`（frontmatter）`:18-20`（用途と worktree ガード）`:26-30`（memory）`:40-42`（禁止）`:47`（入力）`:59-62`（出力・必須性）`:65`（制限） / `.claude/skills/parallel-agents/SKILL.md:149`・`:159`・`:164-171`・`:189` / `.claude/hooks/tier_autoapply.py:100` |

---

# `[規範衝突]` 一覧（7 件）

| # | 衝突 | agent 側（禁止事項） | 実際に課される作業 |
|---|---|---|---|
| 1 | tester にプロダクションコード改変を課す | `tester.md:36`・`tester.md:59`「プロダクションコードのソースファイルを編集・書き込みしない」（無条件） | `scripts/` 配下の一時改変・一時作成を tester タスクに割り当てる計画が実在（`.claude/reports/architecture-report-20260805-125532.md:232`「`scripts/*.py` … AC-4/AC-4b/AC-4c/AC-4d/AC-5 の一時改変のみ」・`.claude/reports/design-review-report-20260805-101605.md:321`）。同衝突は `.claude/agent-memory/design-critic/feedback_recycle_audit_focus_on_fix_induced_defects.md:475-476` で既知として記録済み |
| 2 | test-report のファイル名規約が二重 | `tester.md:54`「**必ず** `.claude/reports/test-report-YYYYMMDD-HHMMSS.md` に Write」（無条件）／同旨 `dev-workflow/SKILL.md:634`・`:720`・`:773` | `plan-design-guidelines.md:45`「`writes` には task_id ベースのファイル名を宣言する。**逐次経路でも**…本ルールは両モード共通」＋ `parallel-agents/SKILL.md:159` により gitignored-only writes タスクは素の `tester` で起動される（＝task_id 規約を持たない agent に task_id 名を要求する構図） |
| 3 | design-critic の MEMORY 追記 | `design-critic.md:65`「design-review-report の新規 Write **以外**のファイル編集・書き込みは行わない（…その他ファイルへの Edit / Write は禁止）」 | 同ファイル `:21`「`.claude/agent-memory/design-critic/MEMORY.md` に追記する」（同一定義ファイル内で矛盾。実ディレクトリも存在し書き込み実績あり） |
| 4 | project-setup の差分更新 | `project-setup.md:5-11` tools に `Edit` なし（`Read` / `Write` / `Glob` / `WebSearch` / `WebFetch` のみ） | 同ファイル `:44`「存在する場合は Read して、**上書きではなく更新として差分を反映**する」（`Write` 全文置換しか手段がない） |
| 5 | tester の既存テスト修正 | `tester.md:5-12` tools に `Edit` なし | 同ファイル `:49`「テストが最初から Pass する場合は…**修正する**」（既存ファイルの部分修正手段がない） |
| 6 | interviewer のソース読み込み | `interviewer.md:28`「ソースコードの読み込み・編集」を担当しない | 同ファイル `:6-10` tools に `Read` / `Glob` / `Grep` を付与（禁止と付与ツールが不一致） |
| 7 | persona 専用宣言と subagent 起動の想定 | `interviewer.md:14`・`architect.md:15`・`planner.md:15`「サブエージェントとして起動しない」 | `dev-workflow/SKILL.md:53`・`:123`・`:189`・`:332` が「Agent ツールでサブエージェント起動した場合は `--execution subagent` を渡す」と起動され得る前提で分岐を用意（起動を指示する箇所は 0 件だが、規範としては相反） |

---

# `[未文書化]` 一覧（9 件）

| # | 実際の挙動 | どこに書かれているか | agent 定義側 |
|---|---|---|---|
| 1 | `developer` / `wt_developer` は起動時に PreToolUse hook が `model:` を自動注入し frontmatter の `model: sonnet` が上書きされうる | `.claude/hooks/tier_autoapply.py:99`（`APPLY_ROLES`）・`.claude/skills/dev-workflow/SKILL.md:61` | `developer.md` / `wt_developer.md` に記載なし |
| 2 | `tester` / `wt_tester` は `C3_TASK_ID` が `test-` 開始のときのみ同じ機械適用対象になる | `.claude/hooks/tier_autoapply.py:100`（`RED_APPLY_ROLES`）・`.claude/skills/parallel-agents/SKILL.md:149` | `tester.md` / `wt_tester.md` に記載なし |
| 3 | `code-reviewer` / `security-reviewer` を `isolation: "worktree"` 付きで起動すると exit 2 でブロックされる | `.claude/hooks/check_agent_invocation.py:50`・`:86-98`・`plan-design-guidelines.md:168-170` | 両 agent 定義に記載なし |
| 4 | `doc-writer` の実際の起動元は `.claude/skills/doc/SKILL.md:167` | `.claude/skills/doc/SKILL.md:165-179` | `doc-writer.md:32`・`:64` は存在しない `.claude/commands/doc.md` を起動元と記載（`commands/` ディレクトリはリポジトリ内に 0 件） |
| 5 | CR/SR のレポートは生成後に親 Claude が `review_hint_inject.py` で末尾へ過去判断ヒントを追記する（SR 実行時には CR レポートも上書きされる） | `.claude/skills/dev-workflow/SKILL.md:941-947`・`:1078-1086` | `code-reviewer.md` / `security-reviewer.md` に記載なし |
| 6 | `tester` は E-0 で `.claude/state/e0-targets-*.txt` を `<detected_files>` タグ経由で受け取り、実行検証（直積・境界値）を課される | `.claude/skills/dev-workflow/SKILL.md:851-916` | `tester.md` の Workflow は plan-report 入力しか書いていない |
| 7 | `tester` の E-0 起動には `C3_TASK_ID: confirm-exec-verify` マーカーが必須（Red 限定注入の不変則を壊さないため） | `.claude/skills/dev-workflow/SKILL.md:864` | `tester.md` に記載なし |
| 8 | `systematic-debugger` / `wt_systematic-debugger` は `memory: project` を宣言するが `.claude/agent-memory/` に該当ディレクトリが存在しない（書き込み実績なし） | `ls .claude/agent-memory/` 実測（存在するのは code-reviewer / design-critic / developer / security-reviewer / tester / wt_developer / wt_tester の 7 件） | `systematic-debugger.md:4`・`:22` / `wt_systematic-debugger.md:4`・`:27` |
| 9 | OpenCode adapter の `@mention` 一覧には `design-critic` / `project-setup` / `wt_*` が含まれない（写像されない agent がある） | `src/c3/adapters.py:474-478`（設計意図コメント）・`:486-494`（一覧） | 各 agent 定義に記載なし |

---

# 未確認の項目

| 項目 | 未確認の理由 |
|---|---|
| `memory: project` frontmatter が実際に付与するツール権限（`Read`/`Write`/`Edit` の自動付与の有無） | `.claude/docs/taxonomy.md:78` は「Claude Code がエージェント停止時に自動更新する」とだけ書く。ツール権限への影響を規定した記述はリポジトリ内に見つからず、Claude Code 本体仕様のため本調査（実ファイル読解のみ）では確定できない。§`[規範衝突]` #4・#5（`Edit` 不在）の実害の有無はこれに依存する |
| `permissionMode: bypassPermissions` の実効範囲 | agent 定義・skill は「worktree 内で permission プロンプトをスキップ」と書くが（`wt_tester.md:18` 等）、`code-reviewer` / `security-reviewer` は worktree を使わず main で動く（`parallel-agents/SKILL.md:159`）。main 直接実行時に何が bypass されるかを規定した記述は見つからなかった |
| `wt_systematic-debugger` の過去の起動実績 | agent 起動は本調査の禁止事項であり、`.claude/state/c3.db` の読み取りも禁止のため、静的な grep 以上の確認をしていない。`.claude/agent-memory/wt_systematic-debugger/` 不在は傍証にとどまる |
| 各 agent の「起動プロンプトで渡されるもの」の完全な逐語内容 | `dev-workflow/SKILL.md` の E-1 / E-2 / D-2 / D-4 は「Agent ツールで起動する」とのみ書きプロンプト本文を規定していない（`:663`・`:767`・`:938`・`:1076`）。実際に何が渡るかは親 Claude の裁量で、仕様として確定していない |
| `design-critic` の `severity` 語彙が `critical` を含まない根拠の実装側裏取り | `dev-workflow/SKILL.md:429` が「design-critic は `critical` を供給しない」と規定するが、これを機械強制する実装は本調査では確認していない |
| `interviewer` / `architect` / `planner` を Agent ツールで起動した場合の実挙動 | 起動指示が 0 件のため経路自体が未使用。`dev-workflow/SKILL.md:53` は `--execution subagent` 分岐を用意するが、実際に起動された記録は本調査（静的 grep のみ）では確認していない |
