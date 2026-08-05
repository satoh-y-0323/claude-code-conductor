# C3 仕様目次 — hook 層

対象: `.claude/hooks/*.py`（24 ファイル）+ `.dev/hooks/*.py`（5 ファイル・配布元専用 / gitignored）
登録先: `.claude/settings.json` / `.claude/settings.local.json`
作成方法: 全ファイルを静的に Read（hook は一切発火させていない）。実 `c3.db` は読んでいない。

---

## 0. サマリ

| 指標 | 件数 |
|---|---|
| `.claude/hooks/*.py` の総ファイル数（`ls` 実測） | 24 |
| 本書に節を持つ `.claude/hooks/*.py` | 24（差分 0） |
| `.dev/hooks/*.py` の総ファイル数（`ls` 実測） | 5 |
| 本書に節を持つ `.dev/hooks/*.py` | 5（差分 0） |
| **[未登録]**（ファイルは在るが `settings*.json` の `hooks` にも `statusLine` にも登録が無い） | **6**（うち「一度も呼ばれない死んだコード」は **0** — 全て他 hook からの内部呼び出し経路あり） |
| **[実体なし]**（規約文書・設定に書かれているが実装ファイルが存在しない） | **6** |
| **[未文書化]**（実装はあるが `CLAUDE.md` / `.claude/CLAUDE.md` / `.claude/docs/` のどこにも記載が無い） | **5** |
| **[登録only]**（登録があるのに対応ファイルが無い hook 登録エントリ） | **0** |
| 登録エントリ総数 | 27（`settings.json` 22 = hooks 21 + statusLine 1 / `settings.local.json` 5） |

### [未登録] 6 件（死んだコードではない — 呼び出し元を併記）

| ファイル | 呼び出し元（根拠） |
|---|---|
| `_hook_utils.py` | `check_agent_invocation.py:41` / `patterns_guard.py:30` / `planner_check.py:41` / `report_contract_check.py:38` / `session_mode_watch.py:52` が `from _hook_utils import` |
| `session_utils.py` | `stop.py:14` / `pre_compact.py:22` / `session_start.py:28` が直 import、`restore_session.py:59` / `consolidate_memory.py:97` / `session_stop.py:97` が importlib 動的ロード |
| `stop.py` | `session_stop.py:80`（`_load_module("stop")` → `stop.run(payload)`） |
| `consolidate_memory.py` | `session_stop.py:87`（`run_sync(today=today)`） |
| `tier_gap_check.py` | `session_stop.py:111`（`run(payload)`） |
| `permission_handler_toast.py` | `permission_handler.py:318,328,338`（`subprocess.run` で同期起動） |

### [実体なし] 6 件

| 参照されている名前 | 参照元（根拠） | 実態 |
|---|---|---|
| `.claude/hooks/clear_file_history.py` | `.claude/settings.local.json:5`（permissions.allow） | ファイル不在。機能は `session_start.py:47 _run_clear_file_history()` に統合済み |
| `.claude/hooks/enable_sandbox.py` | `.claude/settings.local.json:6` | ファイル不在。機能は `session_start.py:103 _run_enable_sandbox()` に統合済み |
| `.claude/hooks/validate_skill_change.py` | `.claude/settings.local.json:10`、`.claude/docs/decisions.md:78` | ファイル不在。機能は `post_tool.py:95 _check_skills_change()` に統合済み（`decisions.md:197` に改名記録） |
| `.claude/hooks/schema.sql` | `.claude/docs/decisions.md:186`（co-location 一覧に現行として記載） | ファイル不在。`session_start.py:15-16` が「v2.20.0 で `c3.migrate.apply_pending_migrations()` に委譲、schema.sql は廃止」と明記 |
| `.claude/hooks/init_c3_db.py` | `.claude/docs/c3追加予定機能リスト.md:708,738,752,753`、`.claude/docs/C3_hnsw_機能追加詳細設計.md:556` | ファイル不在。`session_start.py:7` が「旧 init_c3_db.py」を統合と明記 |
| `record_tier_outcome.py` | `.claude/docs/decisions.md:192`（「現在の配置: … → `.claude/skills/dev-workflow/scripts/` 配下」）、`.claude/docs/c3追加予定機能リスト.md:522` | `.claude/hooks/` にも `.claude/skills/dev-workflow/scripts/` にも不在（実在は `record_agent_outcome.py`） |

> 参考（[実体なし] に**数えなかった**もの）: `.claude/docs/C3_利用状況可視化.md:159,185,232,266` の `_analytics_db.py` / `analytics_stop.py` / `analytics_post_tool.py` / `analytics_subagent_stop.py` は同ファイル `:436` が「不要になる方向」と結論している未実装の設計案。`.claude/docs/C3_tier_routing_cost_integration_設計.md:172` の `subagent_stop.py` も「新規で」と書く設計案。`.claude/docs/c3追加予定機能リスト.md:313` の `po_heartbeat.py` は PO 廃止に伴う過去記録。いずれも「規約」ではないため除外した。

### [未文書化] 5 件

| ファイル | 状況（根拠） |
|---|---|
| `.claude/hooks/_hook_utils.py` | `CLAUDE.md` / `.claude/CLAUDE.md` / `.claude/docs/` に一切記載なし（grep 0 件）。5 hook が import する共有基盤 |
| `.claude/hooks/permission_handler_toast.py` | 同上 0 件。`permission_handler` の Windows トーストワーカー |
| `.claude/hooks/report_contract_check.py` | 同上 0 件。`settings.json:139` に登録済みの現役 PostToolUse hook |
| `.claude/hooks/tier_gap_check.py` | 同上 0 件（`.claude/skills/dev-workflow/scripts/record_agent_outcome.py` のコメントにのみ言及） |
| `.dev/hooks/_version_watch.py` | `CLAUDE.md:59-64` の「配布元専用 hook 一覧」表に**行が無い**（表は 4 本のみ）。実際は `settings.local.json:67` に登録された現役 SessionStart hook |

> 併せて検出した文書側の陳腐化（[未文書化] には数えず注記のみ）:
> - `.claude/docs/decisions.md:170-182` の「現在の登録済みフック一覧（settings.json）」表は 11 行しかなく、実登録の `patterns_guard` / `check_agent_invocation` / `tier_autoapply` / `planner_check` / `report_contract_check` / `session_mode_watch` / `recall_autorebuild` の 7 本が欠落している。
> - `CLAUDE.md:64` は `_planner_check.py` を「R1〜R4 ルール」と書くが、実装は R3 単独（`.dev/hooks/_planner_check.py:11,18-19,24`。R2/R4/R6 は配布側 `planner_check.py` へ移管、R1 は廃止）。

---

## 1. 登録マップ（`settings*.json` の全エントリ → ファイル対応）

すべての登録エントリが実在ファイルに対応する（**[登録only] は 0 件**）。

| # | 設定ファイル:行 | イベント / matcher | 起動コマンド | 対応ファイル |
|---|---|---|---|---|
| 1 | `settings.json:62` | statusLine（`type: command`） | `c3 run "${CLAUDE_PROJECT_DIR}/.claude/hooks/statusline.py"` | `statusline.py` |
| 2 | `settings.json:66,72` | PreToolUse / `Bash` | `c3 run …/pre_tool.py` | `pre_tool.py` |
| 3 | `settings.json:77,82` | PreToolUse / `Write` | `c3 run …/worktree_guard.py` | `worktree_guard.py` |
| 4 | `settings.json:77,87` | PreToolUse / `Write` | `c3 run …/patterns_guard.py` | `patterns_guard.py` |
| 5 | `settings.json:92,97` | PreToolUse / `Edit` | `c3 run …/worktree_guard.py` | `worktree_guard.py` |
| 6 | `settings.json:92,102` | PreToolUse / `Edit` | `c3 run …/patterns_guard.py` | `patterns_guard.py` |
| 7 | `settings.json:107,112` | PreToolUse / `Agent` | `c3 run …/check_agent_invocation.py` | `check_agent_invocation.py` |
| 8 | `settings.json:107,117` | PreToolUse / `Agent` | `c3 run …/tier_autoapply.py` | `tier_autoapply.py` |
| 9 | `settings.json:124,129` | PostToolUse / `Write` | `c3 run …/post_tool.py` | `post_tool.py` |
| 10 | `settings.json:124,134` | PostToolUse / `Write` | `c3 run …/planner_check.py` | `planner_check.py` |
| 11 | `settings.json:124,139` | PostToolUse / `Write` | `c3 run …/report_contract_check.py` | `report_contract_check.py` |
| 12 | `settings.json:144,149` | PostToolUse / `Edit` | `c3 run …/post_tool.py` | `post_tool.py` |
| 13 | `settings.json:144,154` | PostToolUse / `Edit` | `c3 run …/planner_check.py` | `planner_check.py` |
| 14 | `settings.json:144,159` | PostToolUse / `Edit` | `c3 run …/session_mode_watch.py` | `session_mode_watch.py` |
| 15 | `settings.json:166,171` | PermissionRequest / `""` | `c3 run …/permission_handler.py` | `permission_handler.py` |
| 16 | `settings.json:178,183` | UserPromptSubmit / `""` | `c3 run …/select_tier.py` | `select_tier.py` |
| 17 | `settings.json:178,188` | UserPromptSubmit / `""` | `c3 run …/recall_inject.py` | `recall_inject.py` |
| 18 | `settings.json:195,200` | SessionStart / `compact` | `c3 run …/restore_session.py` | `restore_session.py` |
| 19 | `settings.json:205,210` | SessionStart / `""` | `c3 run …/session_start.py` | `session_start.py` |
| 20 | `settings.json:217,222` | PreCompact / `""` | `c3 run …/pre_compact.py` | `pre_compact.py` |
| 21 | `settings.json:229,234` | Stop / `""` | `c3 run …/session_stop.py` | `session_stop.py` |
| 22 | `settings.json:229,239` | Stop / `""` | `c3 run …/recall_autorebuild.py` | `recall_autorebuild.py` |
| 23 | `settings.local.json:61,67` | SessionStart / `""` | `python …/.dev/hooks/_version_watch.py` | `.dev/hooks/_version_watch.py` |
| 24 | `settings.local.json:75,81` | PreToolUse / `Write\|Edit` | `python …/.dev/hooks/_template_guard.py` | `.dev/hooks/_template_guard.py` |
| 25 | `settings.local.json:89,95` | PostToolUse / `Write\|Edit` | `python …/.dev/hooks/_sync_check.py` | `.dev/hooks/_sync_check.py` |
| 26 | `settings.local.json:89,102` | PostToolUse / `Write\|Edit` | `python …/.dev/hooks/_pip_reinstall_reminder.py` | `.dev/hooks/_pip_reinstall_reminder.py` |
| 27 | `settings.local.json:89,109` | PostToolUse / `Write\|Edit` | `python …/.dev/hooks/_planner_check.py` | `.dev/hooks/_planner_check.py` |

補足:
- 配布 hook は全て `command: "c3"` + `args: ["run", …]`（例 `settings.json:71-72`）。配布元専用 hook は `command: "python"`（例 `settings.local.json:65-68`）。`c3 run` ランチャ自体の挙動は `src/c3/` を未読のため**未確認**。
- `settings.json` と `settings.local.json` の `hooks` がマージされる前提は `.claude/docs/settings.json.md:7` が「公式 docs 未明記・本リポの実機事実」と記す**未確認**事項。

---

## 2. `.claude/hooks/*.py`（24 ファイル）

### 2-1. 登録済み（18 ファイル）

## pre_tool.py

| 項目 | 内容 |
|---|---|
| イベント | PreToolUse（`pre_tool.py:2` docstring / `settings.json:66`） |
| 発火条件 | matcher `Bash`（`settings.json:66-67`）。hook 内でも `tool_name != 'Bash'` は exit 0（`pre_tool.py:77-78`） |
| 入力 | stdin JSON の `tool_name`（`:77`）と `tool_input.command`（`:80`）／環境変数 `C3_SKIP_SECRET_CHECK`（`:113`） |
| 出力 | exit 2 = ブロック（`rm -rf` 相当 `:104-109` / 秘密情報代入 `:113-120`）、exit 0 = 通過（`:122`）。stderr に `[PreToolUse WARNING]`（force push `:85-87`、DROP/TRUNCATE `:91-93`）と `[PreToolUse BLOCK]`。ファイル書き込みなし |
| 無効化手段 | `C3_SKIP_SECRET_CHECK=1` で秘密情報検出のみスキップ（`:112-113`）。rm -rf ブロックの bypass は無し |
| 根拠 | `.claude/hooks/pre_tool.py:71-122` / `.claude/settings.json:65-75` |

## worktree_guard.py

| 項目 | 内容 |
|---|---|
| イベント | PreToolUse（`worktree_guard.py:2` / `settings.json:77,92`） |
| 発火条件 | matcher `Write` と `Edit` の 2 箇所に登録（`settings.json:82,97`）。実行は `PO_WORKTREE_GUARD=1` のときのみ（`:44`）かつ CWD が `.claude/worktrees/…` 構造のときのみ（`:69-75`） |
| 入力 | 環境変数 `PO_WORKTREE_GUARD`（`:44`）／stdin JSON の `tool_name`（`:52`）・`tool_input.file_path`（`:56`）／`os.getcwd()`（`:60`） |
| 出力 | exit 2 = worktree 外への Write/Edit をブロック + stderr `[WorktreeGuard BLOCK]`（`:81-89`）、それ以外 exit 0。ファイル書き込みなし |
| 無効化手段 | `PO_WORKTREE_GUARD` を 1 以外にする（既定で無効。`:44`。`SR-V-002` として `:9-11` に既知リスク明記） |
| 根拠 | `.claude/hooks/worktree_guard.py:43-91` / `.claude/settings.json:76-105` |

## patterns_guard.py

| 項目 | 内容 |
|---|---|
| イベント | PreToolUse（`patterns_guard.py:2` / `settings.json:77,92`） |
| 発火条件 | matcher `Write` / `Edit`（`settings.json:87,102`）。`file_path` の realpath が `.claude/memory/patterns.json` に一致するときのみ作用（`:72-85`） |
| 入力 | 環境変数 `C3_PATTERNS_GUARD_DISABLE`（`:45`）／stdin の `tool_name`・`tool_input.file_path`（`:55-60`）／フラグファイル `.claude/state/patterns_guard_allow.flag` の mtime（`:67,93-95`） |
| 出力 | exit 2 = ブロック + stderr `[PatternGuard BLOCK]`（`:114-123`）、exit 0 = 許可。副作用として TTL 超過フラグを削除する（`:100-104`） |
| 無効化手段 | `C3_PATTERNS_GUARD_DISABLE=1`（恒久・`:45`）／`.claude/state/patterns_guard_allow.flag`（TTL 600 秒・`:40,96`） |
| 根拠 | `.claude/hooks/patterns_guard.py:43-123` / `.claude/settings.json:76-105` / `CLAUDE.md:10-4`（§10-4 三段構え） |

## check_agent_invocation.py

| 項目 | 内容 |
|---|---|
| イベント | PreToolUse（`check_agent_invocation.py:2` / `settings.json:107`） |
| 発火条件 | matcher `Agent`（`settings.json:107,112`）。`subagent_type ∈ {code-reviewer, security-reviewer}` かつ `isolation == "worktree"`（R5・`:50,86-92`） |
| 入力 | stdin JSON の `tool_name`・`tool_input.subagent_type`・`tool_input.isolation`（`:68-84`、1M 文字上限 `:58`）／環境変数 `C3_HOOK_DEBUG`（`_hook_utils.py:24,72`） |
| 出力 | exit 2 = ブロック + stderr `[CheckAgentInvocation BLOCK] R5`（`:97-104`）、他は exit 0。`C3_HOOK_DEBUG=1` のとき `.claude/tmp/agent_hook_debug.log` へ 1 行追記（`:55,93-95,106-109`） |
| 無効化手段 | なし（キー名不一致時は検出できず exit 0 へ fail-open。`:10-14`） |
| 根拠 | `.claude/hooks/check_agent_invocation.py:50-110` / `.claude/settings.json:106-120` |

## tier_autoapply.py

| 項目 | 内容 |
|---|---|
| イベント | PreToolUse（`tier_autoapply.py:2` / `settings.json:107`） |
| 発火条件 | matcher `Agent`（`settings.json:117`）。`subagent_type ∈ {developer, wt_developer, tester, wt_tester}` のみ処理（`:101,394`）。注入は developer 系は無条件、tester 系は `C3_TASK_ID: test-…` マーカー時のみ（`:415-442`） |
| 入力 | 環境変数 `C3_TIER_AUTOAPPLY_DISABLE`（`:107,372`）／stdin の `tool_name`・`tool_input.{subagent_type,model,prompt}`・`session_id`（`:376-408`）／`.claude/state/tier_selection.json`（`:84,216-225`）／`c3.pricing` モジュール（`:128-135`） |
| 出力 | exit 0 固定（`:474,484`）。注入時は stdout に `{"hookSpecificOutput":{"hookEventName":"PreToolUse","updatedInput":{…"model":tier}}}`（`:464-472`）。`.claude/state/tier_autoapply.jsonl` へ 1 行追記（+ 1MB 超で末尾 500 行にローテート `:275-291`、ロックファイル `…jsonl.lock` 作成 `:328,345`）。失敗時 stderr `[tier_autoapply] …`（`:340,361,483`） |
| 無効化手段 | `C3_TIER_AUTOAPPLY_DISABLE=1`（注入も記録も行わない・`:107,370-373`） |
| 根拠 | `.claude/hooks/tier_autoapply.py:369-484` / `.claude/settings.json:106-120` |

## post_tool.py

| 項目 | 内容 |
|---|---|
| イベント | PostToolUse（`post_tool.py:2` / `settings.json:124,144`） |
| 発火条件 | matcher `Write` と `Edit` の 2 箇所（`settings.json:129,149`）。skills 通知はパスに `.claude/skills/` を含むとき（`:101-103`）、品質スキャンは拡張子が `.py/.js/.ts/.tsx/.jsx/.cs/.go/.rs` のとき（`:32-34,61-63`） |
| 入力 | stdin の `tool_name`・`tool_input.file_path`（`:135-140`）／対象ファイル本体を先頭 256KB まで読む（`:37,76-78`。先頭 8KB に NUL があればバイナリ扱いで中止 `:69-73`） |
| 出力 | 常に exit 0（`:15,147`）。stdout に `[C3] .claude/skills/… を変更しました…`（`:107`）、stderr に `[C3 quality] <file>:<line> <pattern> を検出`（console.log / print / TODO / FIXME / XXX・`:45-52,120-126`）。ファイル書き込みなし |
| 無効化手段 | なし（コード上に env / フラグの分岐が存在しない） |
| 根拠 | `.claude/hooks/post_tool.py:129-147` / `.claude/settings.json:122-162` |

## planner_check.py

| 項目 | 内容 |
|---|---|
| イベント | PostToolUse（`planner_check.py:2` / `settings.json:124,144`） |
| 発火条件 | matcher `Write` / `Edit`（`settings.json:134,154`）。basename が `plan-report-*.md` かつ `..` を含まないとき（`:98-108`）。PyYAML 未導入なら即 exit 0（`:50-54`） |
| 入力 | stdin（1M 文字上限 `:63,234`）の `tool_name`・`tool_input.file_path`／対象ファイル先頭 512KiB の YAML frontmatter `tasks`（`:64,250-262`）／環境変数 `C3_HOOK_DEBUG`（`_hook_utils.py:72`） |
| 出力 | 常に exit 0（`:304`）。stderr `[PlannerCheck WARN]` + stdout JSON `hookSpecificOutput.additionalContext`（`:277-300`）。`C3_HOOK_DEBUG=1` のとき `.claude/tmp/planner_check_debug.log` へ追記（`:60,276,302`） |
| 無効化手段 | なし（PyYAML 不在時のみ黙って無効化 `:50-54`） |
| 根拠 | `.claude/hooks/planner_check.py:232-304`（検査 R2 `:124-142` / R4 `:173-208` / R6 `:211-229`）/ `.claude/settings.json:122-162` |

## report_contract_check.py

| 項目 | 内容 |
|---|---|
| イベント | PostToolUse（`report_contract_check.py:2` / `settings.json:124`） |
| 発火条件 | matcher `Write` のみ（`settings.json:139`。Edit 側には未登録＝`settings.json:144-161` に本 hook の行が無い）。hook 内でも `tool_name != 'Write'` は exit 0（`:56-58`）。対象は `.claude/reports/` 直下（`:77`）で prefix が `requirements-report-` / `architecture-report-` / `plan-report-` / `design-review-report-` のもの（`:81-93`） |
| 入力 | stdin の `tool_name`・`tool_input.file_path`（`:56-62`）のみ。ファイル本体は読まない |
| 出力 | 常に exit 0（`:101,127`）。契約違反時は stderr `[ReportContract WARN]`（`:105-111`）+ stdout JSON `additionalContext`（`:114-125`）。ファイル書き込みなし |
| 無効化手段 | なし |
| 根拠 | `.claude/hooks/report_contract_check.py:48-127` / `.claude/settings.json:136-140` |

## session_mode_watch.py

| 項目 | 内容 |
|---|---|
| イベント | PostToolUse（`session_mode_watch.py:2` / `settings.json:144`） |
| 発火条件 | matcher `Edit` のみ（`settings.json:159`。Write 側には未登録＝`settings.json:124-141` に本 hook の行が無い）。hook 内でも `tool_name != 'Edit'` は exit 0（`:136-138`）。パス成分に `sessions` を含み basename が `.tmp` で終わるとき（ケース非依存・`:155`） |
| 入力 | stdin の `tool_name`・`tool_input.{file_path,old_string,new_string}`（`:132-161`）。ファイル本体は読まない |
| 出力 | 常に exit 0（`:248,272`）。警告時は stderr `[SessionModeWatch WARN] … に 挿入/差し替え が検出されました`（`:252-256`）+ stdout JSON `additionalContext`（`:259-270`）。ファイル書き込みなし |
| 無効化手段 | なし |
| 根拠 | `.claude/hooks/session_mode_watch.py:129-272`（状態遷移表 `:26-35`）/ `.claude/settings.json:157-160` |

## permission_handler.py

| 項目 | 内容 |
|---|---|
| イベント | PermissionRequest（`permission_handler.py:3` / `settings.json:164-166`） |
| 発火条件 | matcher `""`（全ツール・`settings.json:166`）。`AskUserQuestion` は通知のみで自動承認対象外（`:373-375`） |
| 入力 | stdin の `tool_name`・`tool_input`（`:365-367`）／`.claude/permission_rules.json` の `auto_allow` / `notify_on_auto`（`:26,83-101`）／`platform.system()`（`:44,314`） |
| 出力 | 明示 exit なし（正常時 return = exit 0。パース失敗時 `sys.exit(0)` `:363`）。承認時は stdout JSON `{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}`（`:381-386,393-398`）。副作用: OS 通知（osascript / notify-send / PowerShell balloon・`:44-78`）と `permission_handler_toast.py` の subprocess 起動（`:328-338`）。エラーは stderr `[permission_handler] …` |
| 無効化手段 | なし（`permission_rules.json` の内容で挙動が変わるのみ。不在時は `DEFAULT_RULES` `:28,89-90`） |
| 根拠 | `.claude/hooks/permission_handler.py:359-398` / `.claude/settings.json:164-175` |

## select_tier.py

| 項目 | 内容 |
|---|---|
| イベント | UserPromptSubmit（`select_tier.py:1` / `settings.json:176-178`） |
| 発火条件 | matcher `""`（全プロンプト・`settings.json:178,183`）。`prompt` が非文字列 / 空白のみなら return 0（`:802-804`） |
| 入力 | stdin の `prompt`・`session_id`（`:802-806`）／`.claude/logs/prompt-history.jsonl` 末尾 1000 行（`:100,145,277-294`）／`c3.db`（`read_agent_tier_params` / `read_agent_failure_rate` / `read_tier_cost_rate_for_complexity`・`:828,838,475`）／`c3.pricing`（`:837`）／環境変数 `C3_TIER_EPSILON`（`:694`）・`C3_ESCALATION_THRESHOLD`（`:730`）・`C3_TIER_COST_LAMBDA`（`:767`） |
| 出力 | 常に exit 0（`:800,907,911`）。stdout に JSON `hookSpecificOutput.additionalContext`（`[tier-routing 推奨] …`・`:900-906`）。`.claude/state/tier_selection.json` を上書き（最新 1 件のみ・`:96,576-606`）。不正 env / DB import 失敗は stderr 警告（`:684,700-719,736-755,774-792`） |
| 無効化手段 | 専用の kill-switch は無し。env 3 種は挙動調整のみ（`:688-793`） |
| 根拠 | `.claude/hooks/select_tier.py:796-907` / `.claude/settings.json:176-190` |

## recall_inject.py

| 項目 | 内容 |
|---|---|
| イベント | UserPromptSubmit（`recall_inject.py:1` / `settings.json:176`） |
| 発火条件 | matcher `""`（`settings.json:188`）。prompt 15 文字未満 / `/` 始まり / `@` 始まりはスキップ（`:49,94-105`）。`.claude/state/recall_meta.json` と `recall.hnsw` の両方が無ければスキップ（`:162-165`） |
| 入力 | 環境変数 `C3_RECALL_HOOK_DISABLE`（`:67,259`）・`CLAUDE_PROJECT_DIR`（`:155`）／stdin の `prompt`（`:267`）／`recall.hnsw`・`recall_meta.json` の存在と mtime（`:163-211`）／`.claude/memory/sessions/*.tmp`・`.claude/agent-memory/*.md`・`.claude/reports/archive/*.md`・`.claude/memory/patterns.json` の mtime（`:175-211`） |
| 出力 | 常に exit 0（`:259-288,292`）。stdout に JSON `additionalContext`（`[recall] 過去の類似情報の検索結果…`、index 陳腐化時は rebuild 提案の 3 択指示を前置・`:108-150,281-287`）。副作用は `python -m c3.cli recall search --json`（timeout 8 秒）の subprocess 実行のみ（`:214-255`）。ファイル書き込みなし |
| 無効化手段 | `C3_RECALL_HOOK_DISABLE=1`（`:67,259-260`） |
| 根拠 | `.claude/hooks/recall_inject.py:258-288` / `.claude/settings.json:185-189` |

## restore_session.py

| 項目 | 内容 |
|---|---|
| イベント | SessionStart（`restore_session.py:3` / `settings.json:193-195`） |
| 発火条件 | matcher `compact`（コンパクション後のみ・`settings.json:195,200`）。最新 `.tmp` が無い / ファイル名が `^\d{8}$` でない / 現在地・残タスク・成功・失敗が全て空なら exit 0（`:216-232,252-253`） |
| 入力 | stdin は読まない（`main()` に payload 参照が無い・`:215-232`）。`.claude/memory/sessions/` の最大ファイル名 `.tmp` を読む（`:20,72-78,220-221`） |
| 出力 | exit 0（`:218,233,253` の early exit / 正常終了）。stdout に復元メッセージ（`⚠️ dev-workflow 進行中（現在地: …）` + `[C3 セッション復元: …]` + 残タスク + アプローチ、上限 10000 文字・`:31,269-357`）。ファイル書き込みなし |
| 無効化手段 | なし |
| 根拠 | `.claude/hooks/restore_session.py:215-357` / `.claude/settings.json:193-203` |

## session_start.py

| 項目 | 内容 |
|---|---|
| イベント | SessionStart（`session_start.py:2` / `settings.json:204-205`） |
| 発火条件 | matcher `""`（全 SessionStart・`settings.json:205,210`）。sandbox 処理のみ worktree ではスキップ（`:107-110`） |
| 入力 | stdin は読まない（`main()` に payload 参照なし・`:212-236`）。`~/.claude/file-history/` の一覧（`:44,49-57`）／`<cwd>/.claude/settings.json`（`:112-124`）／`c3.migrate.apply_pending_migrations`（`:183-185`） |
| 出力 | 常に exit 0（`:236,240`）。stdout に `[clear-file-history] N 件削除しました。` / `[enable-sandbox] …`（`:50,81,109,114,127,157`）、失敗時 stderr `[session_start:<label>] failed: <型名>`（`:235`）。**副作用が大きい**: `~/.claude/file-history/` 配下を全削除（`:56-71`）、`.claude/settings.json` に `sandbox` 設定を atomic 上書き（`:130-157`）、`.claude/state/c3.db` へ migration 適用（`:167,175-204`） |
| 無効化手段 | なし（3 処理とも env / フラグ分岐が無い） |
| 根拠 | `.claude/hooks/session_start.py:212-240` / `.claude/settings.json:204-213` |

## pre_compact.py

| 項目 | 内容 |
|---|---|
| イベント | PreCompact（`pre_compact.py:2` / `settings.json:215-217`） |
| 発火条件 | matcher `""`（`settings.json:217,222`）。worktree 内なら exit 0（`:173-175`）。直近 10 秒以内に PreCompact checkpoint があればスキップ（デバウンス・`:36,190-193`） |
| 入力 | stdin の `trigger`・`context_items_before`（`:168-179`）／当日 `.claude/memory/sessions/YYYYMMDD.tmp` の既存 checkpoint 行（`:96-164,188`）／`os.getcwd()`（`:173`） |
| 出力 | 明示 exit なし（正常時 return = exit 0、worktree 時 `sys.exit(0)` `:175`）。`sessions/YYYYMMDD.tmp` に `## [Checkpoint: PreCompact: <trigger> - <ISO8601>]` ブロックを追記（`:207-212` → `session_utils.py:124-161`）。stderr に `[PreCompact] セッション状態を … に保存しました`（`:214`）／デバウンス時 `[PreCompact] debounce: …`（`:192`）／timestamp 破損時の診断 1 行（`:148-152`） |
| 無効化手段 | なし |
| 根拠 | `.claude/hooks/pre_compact.py:167-214` / `.claude/settings.json:215-225` |

## session_stop.py

| 項目 | 内容 |
|---|---|
| イベント | Stop（`session_stop.py:1` / `settings.json:227-229`） |
| 発火条件 | matcher `""`（`settings.json:229,234`）。stdin が 1MB 超なら stderr 警告して即 return（`:34,64-70`） |
| 入力 | stdin 全文（`:64`）→ `session_id` / `transcript_path`（`:99-100`）と、そのまま `stop.run(payload)` / `tier_gap_check.run(payload)` へ渡す（`:81,113`） |
| 出力 | 常に exit 0（`:116,120`）。4 フェーズを順に実行し、各失敗は stderr `[session_stop:<phase>] failed: …`（`:83,90,107,114`）。書き込みは各フェーズの担当（下記 stop.py / consolidate_memory.py / `c3.usage_ingester.ingest_session` `:103-105`） |
| 無効化手段 | なし |
| 根拠 | `.claude/hooks/session_stop.py:54-120` / `.claude/settings.json:227-242` |

## recall_autorebuild.py

| 項目 | 内容 |
|---|---|
| イベント | Stop（`recall_autorebuild.py:2` / `settings.json:227`）。加えて `--rebuild-worker` 引数付きの自己再起動モード（`:430-442`） |
| 発火条件 | matcher `""`（`settings.json:239`）。worktree はスキップ（`:397`）、index 不在はスキップ（`:401`）、stale でなければスキップ（`:405`）、ロック競合はスキップ（`:410-412`） |
| 入力 | 環境変数 `C3_RECALL_AUTOREBUILD_DISABLE`（`:41,380`）・`CLAUDE_PROJECT_DIR`（`:82`）・`C3_RECALL_AUTOREBUILD_DEBUG`（`:326`）／stdin は読み捨て（`:386`）／`recall.hnsw` と recall ソース群の mtime（`:118-167`） |
| 出力 | 常に exit 0（`:423,439,442`）。`.claude/state/recall_rebuild.lock` を作成/削除（TTL 600 秒・`:45,187-254`）。detached 子プロセスを起動し、そこで `python -m c3.cli recall rebuild --target <root>`（timeout 600 秒・`:313-358`）を実行して `recall.hnsw` 等を再生成する。stdout 出力なし。worker エラーは stderr（`:356`） |
| 無効化手段 | `C3_RECALL_AUTOREBUILD_DISABLE=1`（`:41,380-381`） |
| 根拠 | `.claude/hooks/recall_autorebuild.py:366-442` / `.claude/settings.json:236-240` |

## statusline.py

| 項目 | 内容 |
|---|---|
| イベント | `statusLine`（hooks ではなく `settings.json:60-63` の `statusLine.type=command`） |
| 発火条件 | matcher の概念なし。Claude Code がステータス行を描画するたびに起動（`settings.json:60-63`） |
| 入力 | stdin の JSON（`model.display_name` `:83-85` / `effort.level` `:89-92` / `context_window.used_percentage` `:95-97` / `rate_limits.five_hour|seven_day` `:100-126`）。最大 64KB で打ち切り（`:20,157-163`）、5 秒でタイムアウト描画（`:149-151`） |
| 出力 | 明示 exit なし（= exit 0）。stdout 1 行 `[<model>] <effort> | ctx used N% | 5h lim N% (…) | 7d lim N% (…)`（ANSI 色付き・`:128-133`）。ファイル書き込みなし |
| 無効化手段 | なし（`settings.json:60-63` の削除のみ） |
| 根拠 | `.claude/hooks/statusline.py:71-172` / `.claude/settings.json:60-63` |

### 2-2. [未登録]（6 ファイル・内部呼び出しあり）

## stop.py 〔未登録〕

| 項目 | 内容 |
|---|---|
| イベント | 実質 Stop（`session_stop.py:80-81` 経由。`settings*.json` に直接の登録は無い） |
| 発火条件 | `session_stop.py` Phase 1 から `stop.run(payload)`（`session_stop.py:80-81`）。`payload.stop_hook_active` が真なら即 return（`:534-535`）、worktree なら即 return（`:537-539`） |
| 入力 | payload の `stop_hook_active`・`last_assistant_message`（`:534,544`）／`.claude/memory/sessions/*.tmp`（`:80,110-131`）／`.claude/memory/patterns.json`（`:25,339-348`）／`.claude/agent-memory/*/MEMORY.md`（`:43,408-418`） |
| 出力 | 戻り値 0 固定（`:556`。単独実行時は `main()` が `sys.exit(run(...))` `:559-569`）。当日 `sessions/YYYYMMDD.tmp` を作成し前日の `- [ ]` を引き継ぐ（`:176-193`）、`- 記録時刻:` と `- 最終応答:` を atomic 更新（`:195-256`）、`patterns.json` を atomic 上書き（trust_score / promotion_candidate / 30 日 expiry・`:351-368,447-518`）。stderr に `[Stop] …` 各種警告（`:188,345,390,438,520`） |
| 無効化手段 | なし |
| 根拠 | `.claude/hooks/stop.py:523-573` / 呼び出し元 `.claude/hooks/session_stop.py:79-83` |

## consolidate_memory.py 〔未登録〕

| 項目 | 内容 |
|---|---|
| イベント | 実質 Stop（`session_stop.py:86-88` 経由。docstring `:20` は「settings.json の Stop hook 配列に登録される」と書くが**現行 `settings.json` に登録行は無い**） |
| 発火条件 | `session_stop.py` Phase 2 から `run_sync(today=today)`（`session_stop.py:87-88`）。条件分岐なし（3 処理を順に実行・`:659-690`） |
| 入力 | 環境変数 `C3_CONSOLIDATE_ARCHIVE_TTL_DAYS`（`:49,597`）／`.claude/memory/sessions/YYYYMMDD.tmp` 直近 7 日（`:44,110-142`）／`.claude/memory/patterns.json`（`:73,263-290`）。単独実行時のみ stdin を読むが内容は未使用（`:701-712`） |
| 出力 | 常に 0（`:692,713`）。`.claude/memory/consolidated_summary.md` を atomic 書き込み（`:53,71,211,486`）、`.claude/memory/promotion-candidates.md` を書き込み（`:56,74,410`）、`.claude/memory/archive/` へ古い `.tmp` を移動（既定 TTL 21 日・`:50,72,522-590`）。失敗時 stderr `[consolidate_memory…] unexpected error`（`:666,678,688`） |
| 無効化手段 | なし（TTL 変更のみ `C3_CONSOLIDATE_ARCHIVE_TTL_DAYS`・`:592-617`） |
| 根拠 | `.claude/hooks/consolidate_memory.py:640-713` / 呼び出し元 `.claude/hooks/session_stop.py:86-90` |

## tier_gap_check.py 〔未登録〕〔未文書化〕

| 項目 | 内容 |
|---|---|
| イベント | 実質 Stop（`session_stop.py:110-112` 経由の Phase 4。`settings*.json` に登録なし） |
| 発火条件 | `tier_gap_check.run(payload)`（`session_stop.py:111`）。`session_id` が確定できない / `tier_autoapply.jsonl` 不在 or symlink / 起動数 N が全 role 0 なら沈黙（`:136-157`）。評価 role は `developer` のみ（`:52`） |
| 入力 | payload の `session_id`（`:122`）／fallback で `.claude/state/tier_selection.json`（`:43,124-130`）／`.claude/state/tier_autoapply.jsonl`（末尾優先 5MB 上限・`:42,72-99`）／`c3.db` の `agent_outcomes` テーブル（`c3_db.locate_c3_db()` → sqlite3 read-only SELECT・`:160-188,279-315`） |
| 出力 | 戻り値なし・例外は全て握り潰す（`:110-114`）。K' = N - M - Z > 0 のとき stderr に `[tier_gap_check] 学習記録の欠落の可能性: …`（`:318-347`）。**書き込みなし・副作用なし**（`:11`） |
| 無効化手段 | なし（`tier_autoapply.jsonl` の不在が実質的な kill-switch・`:147`） |
| 根拠 | `.claude/hooks/tier_gap_check.py:102-347` / 呼び出し元 `.claude/hooks/session_stop.py:109-114` |

## permission_handler_toast.py 〔未登録〕〔未文書化〕

| 項目 | 内容 |
|---|---|
| イベント | なし（hook ではなく CLI ワーカー。`permission_handler.py:328-338` が `subprocess.run` で同期起動） |
| 発火条件 | `permission_handler.py` が Windows かつ auto_allow 未マッチのときのみ起動（`permission_handler.py:314-338`） |
| 入力 | コマンドライン引数 `--message`（必須）/ `--pattern`（任意）/ `--rules-file`（必須）（`:195-201`）／`permission_rules.json` の既存 `auto_allow`（`:48-63`）。stdin は読まない |
| 出力 | exit 10 = ユーザーが許可ボタンをクリック（`:36,204`）、exit 3 = `windows-toasts` 未インストール（`:37,122`）、exit 0 = タイムアウト（60 秒 `:33,172`）/ 無視。副作用: `permission_rules.json` の `auto_allow` へ atomic append（上限 100 件・`:34,40-95`）、Windows トースト表示（`:147-173,176-191`）。エラーは stderr `[permission_handler_toast] …` |
| 無効化手段 | なし（`windows-toasts` 未導入なら exit 3 で呼び元がバルーン通知にフォールバック・`permission_handler.py:344-346`） |
| 根拠 | `.claude/hooks/permission_handler_toast.py:194-208` / 呼び出し元 `.claude/hooks/permission_handler.py:318-348` |

## session_utils.py 〔未登録〕

| 項目 | 内容 |
|---|---|
| イベント | なし（共有ライブラリ。`:2` docstring も "Shared utilities" と明記） |
| 発火条件 | import 時のみ。`stop.py:14` / `pre_compact.py:22,29` / `session_start.py:28` が直 import、`restore_session.py:59-69` / `consolidate_memory.py:97-107` / `session_stop.py:97` が importlib で動的ロード |
| 入力 | 引数のみ（stdin・env の参照なし）。パスは `__file__` から `.claude/memory/sessions/` を導出（`:47-49`） |
| 出力 | exit code なし。`append_checkpoint()` が `sessions/*.tmp` にテンプレート作成 + checkpoint ブロック追記（`:124-161`）、`ensure_session_initialized()` が空ファイルをテンプレートで再初期化（`:87-96`） |
| 無効化手段 | なし |
| 根拠 | `.claude/hooks/session_utils.py:1-161` / 呼び出し元 `.claude/hooks/stop.py:14`・`.claude/hooks/session_stop.py:97` |

## _hook_utils.py 〔未登録〕〔未文書化〕

| 項目 | 内容 |
|---|---|
| イベント | なし（共有ライブラリ。`:2` docstring "Shared utilities for .claude/hooks/ scripts"） |
| 発火条件 | import 時のみ。`check_agent_invocation.py:41` / `patterns_guard.py:30` / `planner_check.py:41` / `report_contract_check.py:38` / `session_mode_watch.py:52` が `sys.path.insert` 後に import |
| 入力 | 引数のみ。ただし `write_debug_log()` は環境変数 `C3_HOOK_DEBUG`（`:24,72`）を参照 |
| 出力 | exit code なし。`write_debug_log()` が `C3_HOOK_DEBUG=1` のときのみ引数の `log_path` へ 1 行追記（失敗は握り潰し・`:62-82`）。`norm_component()` / `sanitize_for_terminal()` は純関数（`:41-59`） |
| 無効化手段 | `C3_HOOK_DEBUG` を 1 以外にする（既定でログ出力なし・`:72-73`） |
| 根拠 | `.claude/hooks/_hook_utils.py:1-82` / 利用元 `.claude/hooks/patterns_guard.py:29-30` |

---

## 3. `.dev/hooks/*.py`（配布元専用・5 ファイル / gitignored）

## _template_guard.py

| 項目 | 内容 |
|---|---|
| イベント | PreToolUse（`_template_guard.py:2` / `settings.local.json:73-75`） |
| 発火条件 | matcher `Write|Edit`（`settings.local.json:75,81`）。解決後パスが `<cwd>/src/c3/_template` 配下のとき（`:50-56`） |
| 入力 | 環境変数 `C3_TEMPLATE_GUARD_DISABLE`（`:35`）／stdin の `tool_name`・`tool_input.file_path`（`:43-48`）／`os.getcwd()`（`:50`） |
| 出力 | exit 2 = ブロック + stderr `[TemplateGuard BLOCK] …`（`:57-65`）、それ以外 exit 0（`:67`）。ファイル書き込みなし |
| 無効化手段 | `C3_TEMPLATE_GUARD_DISABLE=1`（`:11,35-36`） |
| 根拠 | `.dev/hooks/_template_guard.py:34-67` / `.claude/settings.local.json:73-86` / `CLAUDE.md:15,61` |

## _sync_check.py

| 項目 | 内容 |
|---|---|
| イベント | PostToolUse（`_sync_check.py:2` / `settings.local.json:87-89`） |
| 発火条件 | matcher `Write|Edit`（`settings.local.json:89,95`）。cwd 相対パスが `.gitignore` / `src/c3/_excludes.py` / `hatch_build.py` のいずれかのとき（`:30,62`） |
| 入力 | stdin の `tool_name`・`tool_input.file_path`（`:44-49`）／`os.getcwd()`（`:51`） |
| 出力 | 常に exit 0（`:71`）。該当時のみ stderr `[SyncCheck WARN] … 同期を必ず確認してください: …`（`:63-68`）。ファイル書き込みなし |
| 無効化手段 | なし |
| 根拠 | `.dev/hooks/_sync_check.py:38-71` / `.claude/settings.local.json:87-97` / `CLAUDE.md:29,62` |

## _pip_reinstall_reminder.py

| 項目 | 内容 |
|---|---|
| イベント | PostToolUse（`_pip_reinstall_reminder.py:2` / `settings.local.json:87`） |
| 発火条件 | matcher `Write|Edit`（`settings.local.json:102`）。cwd 相対パスが `src/c3/__init__.py` / `pyproject.toml` のとき（`:29,60`） |
| 入力 | stdin の `tool_name`・`tool_input.file_path`（`:43-48`）／`os.getcwd()`（`:50`） |
| 出力 | 常に exit 0（`:68`）。該当時のみ stderr `[PipReinstallReminder] … pip install -e . --no-deps --quiet を再実行してください`（`:61-66`）。ファイル書き込みなし |
| 無効化手段 | なし |
| 根拠 | `.dev/hooks/_pip_reinstall_reminder.py:37-68` / `.claude/settings.local.json:98-104` / `CLAUDE.md:63` |

## _planner_check.py

| 項目 | 内容 |
|---|---|
| イベント | PostToolUse（`_planner_check.py:2` / `settings.local.json:87`） |
| 発火条件 | matcher `Write|Edit`（`settings.local.json:109`）。basename が `plan-report-*.md`（`:56-58`）かつ frontmatter の `tasks[].writes` に `src/c3/_template/` を含むとき（R3・`:45,74-93`）。PyYAML 未導入なら即 exit 0（`:38-41`） |
| 入力 | stdin の `tool_name`・`tool_input.file_path`（`:102-106`）／対象ファイル全文の YAML frontmatter（`:112-124`。配布側 `planner_check.py:252` と違い読み取りバイト上限なし） |
| 出力 | exit 2 = ブロック + stderr `[PlannerCheck BLOCK] …`（`:127-132`）、それ以外 exit 0（`:134`）。stdout 出力・ファイル書き込みなし |
| 無効化手段 | なし |
| 根拠 | `.dev/hooks/_planner_check.py:96-134` / `.claude/settings.local.json:105-111` / `CLAUDE.md:64`（表の「R1〜R4」記述は実装 R3 単独と不一致） |

## _version_watch.py 〔未文書化〕

| 項目 | 内容 |
|---|---|
| イベント | SessionStart（`_version_watch.py:2` / `settings.local.json:58-61`） |
| 発火条件 | matcher `""`（`settings.local.json:61,67`）。`claude` CLI が PATH にあり、`--version` の X.Y.Z が `.dev/state/last_verified_version` と異なる（または未記録）とき（`:63-85,140-153`） |
| 入力 | `shutil.which("claude")` + `claude --version` の stdout（timeout 5 秒・`:112-137`）／`.dev/state/last_verified_version`（`:140-150`）。stdin は読まない |
| 出力 | 明示 exit なし（= exit 0、全経路 fail-open `:107-159`）。通知時のみ stdout に「Claude Code バージョン X を検知しました。… `python .dev/smoke/run_smoke.py`」（`:88-96,153-155`）。ファイル書き込みなし（state 更新は `run_smoke.py` 側・`:5`） |
| 無効化手段 | なし |
| 根拠 | `.dev/hooks/_version_watch.py:104-163` / `.claude/settings.local.json:58-71` |

---

## 4. 未確認の項目

| # | 未確認事項 | 根拠 / 制約 |
|---|---|---|
| 1 | `c3 run <script>` ランチャ自体の挙動（cwd・env・タイムアウト・exit code の透過性） | 配布 hook 全 22 エントリがこの経路（`.claude/settings.json:71-72` 等）。実装 `src/c3/` を本タスクでは未読 |
| 2 | `settings.json` と `settings.local.json` の `hooks` がマージされる保証 | `.claude/docs/settings.json.md:7-8` が「公式 docs 未明記・本リポの実機事実」と自認 |
| 3 | Agent ツール `tool_input` のキー名（`subagent_type` / `isolation` / `model` / `prompt`） | `.claude/hooks/check_agent_invocation.py:10-14` が「公式仕様にドキュメント化されていない（2026-05-21 時点）」と明記。`tier_autoapply.py:389,402,407` も同キーに依存 |
| 4 | PreToolUse の `updatedInput` による model 上書きの実効 | `.claude/hooks/tier_autoapply.py:12-14` が「T0 実測により省略形を正とした」と書くのみで、公式仕様の裏取りは本書では未実施 |
| 5 | PostToolUse で exit 2 を返した場合の扱い | 配布 hook はいずれも PostToolUse では exit 0 のみ（`planner_check.py:304` / `report_contract_check.py:127` / `session_mode_watch.py:272` / `post_tool.py:147`）。`.dev/hooks/_planner_check.py:132` だけが PostToolUse で exit 2 を返すが、その効果は未確認 |
| 6 | PermissionRequest hook の `decision` スキーマ（`{"behavior":"allow"}`） | `.claude/hooks/permission_handler.py:381-386` の実装のみが根拠。公式仕様の照合は未実施 |
| 7 | `statusLine` に渡される stdin JSON のキー名の正式定義 | `.claude/hooks/statusline.py:100-126` は `five_hour` / `5h` / `fiveHour` の 3 通りを総当たりしており、実装側も確定していない |
| 8 | `session_start.py` の `~/.claude/file-history/` 全削除がハーネスに与える影響 | `.claude/hooks/session_start.py:47-81` は削除を実行するが、file-history の用途・削除の副作用は本書では未検証 |
| 9 | `restore_session.py` / `session_start.py` が stdin payload を読まないことの是非 | 両者とも `main()` 内に stdin 参照が無い（`restore_session.py:215-232` / `session_start.py:212-236`）。ハーネスが payload を送っているかは未確認 |
| 10 | `.claude/permission_rules.json` の実ファイル存在と内容 | `.claude/hooks/permission_handler.py:26,89-90` は不在時 `DEFAULT_RULES` にフォールバックする。実ファイルは本タスクで未読 |
| 11 | `tier_gap_check.py` が読む `agent_outcomes` テーブルの実在とスキーマ | `.claude/hooks/tier_gap_check.py:287-313` の SQL のみが根拠。実 `c3.db` は読み書き禁止のため未確認 |
