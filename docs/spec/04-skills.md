# C3 仕様目次（skill 層）

> **⛔ 凍結: 2026-08-05 時点のスナップショット。**
> 実装を変更しても更新しない（2026-08-05 裁定）。**現行仕様として読まないこと。**
> 理由と、現在の姿を知る方法は [`00-index.md`](00-index.md) の冒頭を参照。

対象: `.claude/skills/*/SKILL.md`（17 ディレクトリ）と各 skill 配下の `scripts/` / `references/` / `templates/`。
根拠はすべて `file:line` を添える。読み取れない項目は「未確認」と明記する。

---

## サマリ（実測）

| 指標 | 値 | 実測方法 |
|---|---|---|
| skill ディレクトリ数 | **17** | `ls -d .claude/skills/*/ \| wc -l` → 17 |
| 本ドキュメントの `## <skill 名>` 見出し数 | **17** | 下記の全 17 節（突き合わせ一致） |
| SKILL.md 合計行数 | **4,213** | `wc -l .claude/skills/*/SKILL.md` の total |
| 最大 SKILL.md | `dev-workflow` **1,246 行**（全体の 29.6%） | 同上 |
| `AskUserQuestion` の JSON ブロック総数 | **72** | `grep -c '"questions"'` の全 skill 合計 |
| JSON を伴わない散文の承認ゲート | **1**（`recall/SKILL.md:56` の 3 択） | grep 実測 |
| 人間が止まる箇所の総数（上限） | **73** = 72 + 1 | 同上（分岐で排他のものを含む理論最大） |
| `dev-workflow` が「承認ゲート」と定義する数 | **10**（A-4 / B-3 / C-1 / C-2 / C-3 / D-2.5 / D-3 / D-5 / E-1 / E-2） | `.claude/skills/dev-workflow/SKILL.md:34` |
| `[起動元なし]` | **1 件** | `autonomous-mode`（下記 §起動元なし） |
| `[起動元=ユーザーの `/` 入力のみ]` | **6 件** | `brainstorm` / `codex-review` / `doc` / `extract-lib` / `mcp-config` / `pattern-status` |
| `[重複]` | **16 件** | 下記 §重複一覧 |
| `[実体なし]` | **5 件** | 下記 §実体なし一覧 |
| 付属スクリプト | **7 本**（`__pycache__` 除く） | `find .claude/skills -type f` |
| 付属 references / templates | **9 本** | 同上 |

### 標準ワークフロー 1 周で人間が止まる回数（`/start` 経路）

| skill | JSON ブロック数 | 根拠 |
|---|---|---|
| `start` | 4 | `start/SKILL.md:45,67,94,119` |
| `dev-workflow` | 23 | `dev-workflow/SKILL.md:103,169,218,290,312,379,417,450,472,526,610,687,725,778,956,979,1018,1051,1096,1120,1159,1192,1232` |
| `parallel-agents` | 2 | `parallel-agents/SKILL.md:129,212` |
| 小計 | **29** | 分岐で排他のものを含む理論最大 |

---

## autonomous-mode

| 項目 | 内容 |
|---|---|
| 行数 | 356 |
| 起動方法 | **`[起動元なし]`** — `user-invocable: false`（`autonomous-mode/SKILL.md:3`）かつ `disable-model-invocation: true`（同 `:4`）で `/` 入力も Skill ツールも不可。grep 実測で「Skill ツールで `autonomous-mode`」は 0 件。実際は `Read` されるだけの規約文書（`init-session/SKILL.md:187`・`dev-workflow/SKILL.md:38,44`） |
| 入力 | セッションファイルの `モード:` 行（`autonomous-mode/SKILL.md:19-35` の BNF）、委任プラン（`~/.claude/plans/` 配下・`:51`）、`cycles=` トークン（`:243-253`） |
| 出力 | ファイル生成なし。規約として `モード:` 行の挿入・更新・削除を親 Claude に指示（`:176-179`）、`loop-impl-gaps-*.md` への 1 行追記（`:274`） |
| 承認ゲート | **JSON ブロック 0 個**。ただしゲート対応表として承認ゲート **10 個**の HITL/自律の挙動を定義（`:60-74`）、人間の関所 2 類型を不変則化（`:109-117`） |
| 付属スクリプト | `scripts/mode_line.py`（188 行）— モード行の有効性判定・realpath 正規化＋`~/.claude/plans/` 封じ込め検査。`VALID<TAB>path`(exit 0)／`INVALID<TAB>理由`(exit 1) を返す（`:335-339`） |
| 根拠 | `.claude/skills/autonomous-mode/SKILL.md:1-356` |

---

## brainstorm

| 項目 | 内容 |
|---|---|
| 行数 | 112 |
| 起動方法 | `/brainstorm`（`brainstorm/SKILL.md:23`）＋ LLM 自律判断（`:24-25`）。`user-invocable: true`（`:4`）。**他ファイルからの起動は grep 0 件**（`.claude/commands` / `agents` / `hooks` / `rules` / `src/c3` を走査） |
| 入力 | ユーザーの相談テーマ、PDF／画像を Read（`:49`）。Excel は非対応で PDF 化を案内（`:50-54`） |
| 出力 | 任意で `.claude/reports/brainstorm-{timestamp}.md`（`:95`）。Skill ツールで `report-timestamp` を呼ぶ（`:94`）。agent 起動なし（`:9` で親 Claude ペルソナと明記） |
| 承認ゲート | **JSON ブロック 0 個**。「AskUserQuestion で軽く選択肢を出すのは可」という許可のみ（`:85`）＝個数は動的で未確定 |
| 付属スクリプト | なし |
| 根拠 | `.claude/skills/brainstorm/SKILL.md:1-112` |

---

## codex-review

| 項目 | 内容 |
|---|---|
| 行数 | 211 |
| 起動方法 | `/codex-review` または自然文トリガー（`codex-review/SKILL.md:17-21`）。frontmatter に `user-invocable` / `disable-model-invocation` の指定なし＝既定。**他ファイルからの起動は grep 0 件** |
| 入力 | args（`:49-53`）、`.codex/agents/{reviewer_type}.toml`（`:135`）、`git diff HEAD`（`:115`）または指定ファイルの Read（`:128`） |
| 出力 | `.claude/reports/code-review-report-{timestamp}.md` / `security-review-report-{timestamp}.md`（`:144-145,174`）。`codex exec` を Bash 起動（`:154-166`）。選択次第で Skill ツールで `review-phase` へ連鎖（`:211`） |
| 承認ゲート | **3 個**（`:59`＝種別/対象、`:86`＝ファイルパス、`:198`＝フォローアップ） |
| 付属スクリプト | なし |
| 根拠 | `.claude/skills/codex-review/SKILL.md:1-211` |

---

## develop

| 項目 | 内容 |
|---|---|
| 行数 | **15**（最小の実体 skill） |
| 起動方法 | `/develop`（frontmatter に制限指定なし・`develop/SKILL.md:1-3`）。`init-session/SKILL.md:194` が「フェーズ D 実装中 → `develop` skill を使う」と指示。`hooks/restore_session.py:271` も skill 名を提示 |
| 入力 | `.claude/skills/dev-workflow/SKILL.md` を Read（`:11`）、plan-report の `po_plan_version`（`:14-15`） |
| 出力 | 自身は何も生成しない。**dev-workflow フェーズ D への転送のみ**。`"0.1"` で `parallel-agents/SKILL.md` を Read（`:14`） |
| 承認ゲート | **0 個**（JSON なし）。「dev-workflow の AskUserQuestion 手順を省略しない」という参照のみ（`:13`） |
| 付属スクリプト | なし |
| 根拠 | `.claude/skills/develop/SKILL.md:1-15` |

---

## dev-workflow

| 項目 | 内容 |
|---|---|
| 行数 | **1,246**（全 skill 合計 4,213 の 29.6%） |
| 起動方法 | **`/` 起動不可**（`user-invocable: false`・`dev-workflow/SKILL.md:4`）。`start/SKILL.md:139-144`・`develop/SKILL.md:11`・`review-phase/SKILL.md:11` が **Read** して指定フェーズから実行する（Skill ツール呼び出しではなくファイル Read） |
| 入力 | `agents/interviewer.md`(`:75`) / `architect.md`(`:140`) / `planner.md`(`:206`)、`references/interview-rubric.md`(`:83`)・`design-rubric.md`(`:150`)、セッションファイルの `モード:` 行(`:36`)・`tier-routing複雑度:` 行(`:52`)、plan-report フロントマター(`:550-560`)、`.claude/reports/` 各種 |
| 出力 | requirements-report(`:97`) / architecture-report(`:164`) / plan-report(`:230`) / design-review-report(`:401`) / test-report(`:634,720,773`) / code-review-report(`:944`) / security-review-report(`:1083`)。起動 agent: interviewer・architect・planner（ペルソナ）／design-critic(`:394`)・tester(`:634,720,773,851`)・developer(`:663,767`)・systematic-debugger(`:677`)・code-reviewer(`:938`)・security-reviewer(`:1076`)。c3.db へ `record_agent_outcome.py` / `record_review_decision.py` を多数実行 |
| 承認ゲート | **23 個**（JSON 実測: `:103,169,218,290,312,379,417,450,472,526,610,687,725,778,956,979,1018,1051,1096,1120,1159,1192,1232`）。ただし本文の定義上の「承認ゲート」は **10 個**（`:34`）＝定義と実装数が一致しない（分岐先の追い質問が 13 個） |
| 付属スクリプト | `scripts/record_agent_outcome.py`(967 行, tier-routing 学習記録)／`record_review_decision.py`(173 行, review 判断を c3.db へ)／`review_hint_inject.py`(310 行, 過去判断ヒントをレポート末尾に追記)／`detect_execution_verification.py`(528 行, E-0 実行検証判定) |
| 付属 references | `interview-rubric.md`(79)／`design-rubric.md`(63)／`design-critic-rubric.md`(160)／`plan-design-guidelines.md`(180)／`code-review-checklist.md`(96)／`security-review-checklist.md`(89)。**うち 3 本（plan-design-guidelines / code-review-checklist / security-review-checklist）は SKILL.md から一度も参照されず、`.claude/agents/*.md` だけが参照する**（`agents/planner.md:37,61`・`code-reviewer.md:45,50`・`security-reviewer.md:46,51`） |
| 根拠 | `.claude/skills/dev-workflow/SKILL.md:1-1246` |

---

## doc

| 項目 | 内容 |
|---|---|
| 行数 | 179 |
| 起動方法 | `/doc` のみ（`disable-model-invocation: true`・`doc/SKILL.md:3`）。**他ファイルからの起動は grep 0 件**。`start/SKILL.md:14` は「例: `/doc`」と言及するだけで起動しない |
| 入力 | ユーザーへの質問 8 問（種類・対象・読み手・目的・粒度・出力先・パス・確認） |
| 出力 | Agent ツールで `doc-writer` を起動（`:167`）。出力先は `.claude/reports/doc-{名前}.md` / 指定パス / チャット表示の 3 択（`:118-120`） |
| 承認ゲート | **8 個**（`:18,39,52,73,97,115,129,153`） |
| 付属スクリプト | なし |
| 根拠 | `.claude/skills/doc/SKILL.md:1-179` |

---

## extract-lib

| 項目 | 内容 |
|---|---|
| 行数 | 297 |
| 起動方法 | `/extract-lib` のみ（`disable-model-invocation: true`・`extract-lib/SKILL.md:3`）。**他ファイルからの起動は grep 0 件** |
| 入力 | `.claude/rules/coding-standards.md` / `project-conventions.md` を Read（`:14`。無ければ `/setup` を案内して終了・`:18`）、解析対象プロジェクトの絶対パス（最低 2 件・`:61`） |
| 出力 | `.claude/reports/lib-extract-{YYYYMMDD}.md`（`:187`）、任意でライブラリスケルトン（`:238-260`）、セッションファイルの `## うまくいったアプローチ` / `## 試みたが失敗したアプローチ` / `patterns` へ追記（`:211,225,268`）。**agent は名前指定でなく「Agent ツールで解析を実行」の無名プロンプト起動**（`:94,238`）＝ `.claude/agents/*.md` を使わない唯一の skill |
| 承認ゲート | **6 個**（`:36,49,70,84,198,220`） |
| 付属スクリプト | なし |
| 根拠 | `.claude/skills/extract-lib/SKILL.md:1-297` |

---

## init-session

| 項目 | 内容 |
|---|---|
| 行数 | 242 |
| 起動方法 | `/init-session`（frontmatter に制限指定なし・`init-session/SKILL.md:1-3`）＋ `start/SKILL.md:28` が Skill ツールで `from-start` 引数付き起動 |
| 入力 | `.claude/memory/sessions/*.tmp` の最新（`:54`）、`モード:` 行（`:57`）、`.claude/memory/patterns.json`（`:91`）、`git log --since=`（`:78`） |
| 出力 | `.claude/state/init_session.flag`（`:23` の `session_guard.py mark`）、セッションファイルの `- [ ]`→`- [x]` Edit（`:142`）。連鎖起動: `setup`（`:33`）／`start`（`:202`）／`develop`・`review-phase`・`start`（`:194-196`）。`autonomous-mode/SKILL.md` を Read（`:187`） |
| 承認ゲート | **2 個**（`:131`＝陳腐化タスク確認、`:154`＝作業の開始方法）。`from-start` 起動時は両方スキップ（`:40-44`） |
| 付属スクリプト | `scripts/session_guard.py`（163 行）— `mark`／`check`／`setup-mark` の 3 サブコマンド。`SETUP_DONE`/`SETUP_NEEDED`・`INIT_DONE`/`INIT_NEEDED` を print |
| 根拠 | `.claude/skills/init-session/SKILL.md:1-242` |

---

## mcp-config

| 項目 | 内容 |
|---|---|
| 行数 | 332 |
| 起動方法 | `/mcp-config` のみ（`disable-model-invocation: true`・`mcp-config/SKILL.md:3`）。**他ファイルからの起動は grep 0 件** |
| 入力 | `.claude/settings.json` を Read（`:39,48,288`）、WebSearch で公開 MCP サーバー候補を調査（`:99-101`）、ユーザーの入力（識別名・コマンド・URL・env） |
| 出力 | `.claude/settings.json` の `mcpServers` を Edit（`:61` 削除／`:331` 追加）。agent 起動なし |
| 承認ゲート | **13 個**（`:23,55,71,91,131,143,175,194,203,214,225,236,275`）— skill 単体では dev-workflow に次ぐ 2 番目の多さ |
| 付属スクリプト | なし |
| 根拠 | `.claude/skills/mcp-config/SKILL.md:1-332` |

---

## parallel-agents

| 項目 | 内容 |
|---|---|
| 行数 | 418 |
| 起動方法 | **`/` 起動不可**（`user-invocable: false`・`parallel-agents/SKILL.md:3`）。`develop/SKILL.md:14` と `dev-workflow/SKILL.md:573` が **Read** する（`po_plan_version: "0.1"` の場合のみ・`:8`） |
| 入力 | `.claude/reports/plan-report-*.md` の最新（`:50`）、`c3 plan validate`（`:56`）・`c3 plan waves`（`:70`）の出力、Agent 返り値の `<worktree><worktreePath>` ブロック（`:204`） |
| 出力 | 各 worktree の成果物を main へ `cp` 取り込み（`:282-283`）→ `git add` / `git commit`（`:299-300`）、セッションファイルの Wave 行 `[x]` 化と `現在地:` 更新（`:332-336`）、`.claude/tmp/wave-checkpoint-summary.txt`（`:339`）、`record_agent_outcome.py` を多数実行（`:241,247,253,379,385,391`）。起動 agent: `wt_tester` / `wt_developer` / `wt_systematic-debugger` / `code-reviewer` / `security-reviewer`（`:147-153`） |
| 承認ゲート | **2 個**（`:129`＝マイルストーン wave の続行確認・C-1 で「設ける」を選んだ場合のみ、`:212`＝wave 失敗時の方針） |
| 付属スクリプト | なし（`dev-workflow/scripts/record_agent_outcome.py` を借用・`:241`） |
| 根拠 | `.claude/skills/parallel-agents/SKILL.md:1-418` |

---

## pattern-status

| 項目 | 内容 |
|---|---|
| 行数 | 103 |
| 起動方法 | `/pattern-status` のみ（`disable-model-invocation: true`・`pattern-status/SKILL.md:3`）。**他ファイルからの起動は grep 0 件** |
| 入力 | `.claude/memory/patterns.json` を Read（`:14`） |
| 出力 | **ファイル出力なし**（読み取り専用・`:101`）。チャットへ表形式サマリを表示（`:46-82`） |
| 承認ゲート | **0 個**（AskUserQuestion の記載自体が 0 件） |
| 付属スクリプト | なし |
| 根拠 | `.claude/skills/pattern-status/SKILL.md:1-103` |

---

## promote-pattern

| 項目 | 内容 |
|---|---|
| 行数 | 152 |
| 起動方法 | `/promote-pattern`（frontmatter に制限指定なし・`promote-pattern/SKILL.md:1-3`）。`hooks/consolidate_memory.py:440` と `hooks/patterns_guard.py:116` が案内文で提示、`pattern-status/SKILL.md:55,92` も案内する（いずれも自動起動ではなく文言による誘導） |
| 入力 | `.claude/memory/patterns.json` を Read（`:13`） |
| 出力 | `.claude/rules/promoted/YYYYMMDD-{id}.md`（`:68`）または `.claude/skills/promoted-YYYYMMDD-{id}/SKILL.md`（`:82`）、`.claude/rules/promoted/index.md` のマーカー間へ追記（`:106`）、`.claude/state/patterns_guard_allow.flag` 作成後に `patterns.json` を Edit（`:119-122`） |
| 承認ゲート | **2 個**（`:28`＝昇格するパターン選択（multiSelect）、`:50`＝昇格先 rule/skill。`:60` により選択パターン数だけ `:50` が繰り返される＝実効回数は可変） |
| 付属スクリプト | なし |
| 根拠 | `.claude/skills/promote-pattern/SKILL.md:1-152` |

---

## recall

| 項目 | 内容 |
|---|---|
| 行数 | 113 |
| 起動方法 | `/recall`（`recall/SKILL.md:26`）＋ LLM 自律判断（`:19-24`）。`allowed-tools: Bash, Read`（`:4`）。hooks の `recall_inject.py` / `recall_autorebuild.py` は `c3 recall` CLI を直接叩くのみで **skill は起動しない** |
| 入力 | ユーザーのタスクを 30〜60 文字のクエリに要約（`:40`）、`c3 recall search --json` の出力（`:45`） |
| 出力 | **ファイル出力なし**。ヒットした `path` を Read してコンテキストへ反映（`:50-51`）。応答内に検索件数と参照 path を明示（`:77-79`） |
| 承認ゲート | **JSON ブロック 0 個**。散文で **3 択の AskUserQuestion を 1 箇所**規定（`:56`＝インデックスが古いときの rebuild 確認）。同一セッション内の再提示は禁止（`:60`） |
| 付属スクリプト | なし |
| 根拠 | `.claude/skills/recall/SKILL.md:1-113` |

---

## report-timestamp

| 項目 | 内容 |
|---|---|
| 行数 | 33 |
| 起動方法 | **`/` 起動不可**（`user-invocable: false`・`report-timestamp/SKILL.md:3`）。Skill ツール経由でのみ起動。実測 11 箇所の起動元: `agents/architect.md:46`・`code-reviewer.md:56`・`design-critic.md:62`・`developer.md:70`・`doc-writer.md:56`・`interviewer.md:42`・`planner.md:47`・`security-reviewer.md:57`・`systematic-debugger.md:81`・`tester.md:54`・`wt_developer.md:79`・`wt_systematic-debugger.md:88`・`wt_tester.md:60`、および `brainstorm/SKILL.md:94`・`codex-review/SKILL.md:141` |
| 入力 | なし（引数なし） |
| 出力 | 標準出力に `YYYYMMDD-HHMMSS` 1 行（`:18`）。ファイル出力・agent 起動なし |
| 承認ゲート | **0 個** |
| 付属スクリプト | `scripts/get_timestamp.py`（**2 行**）— `datetime.now().strftime('%Y%m%d-%H%M%S')` を print するだけ |
| 根拠 | `.claude/skills/report-timestamp/SKILL.md:1-33` |

---

## review-phase

| 項目 | 内容 |
|---|---|
| 行数 | **13**（最小） |
| 起動方法 | `/review-phase`（frontmatter に制限指定なし・`review-phase/SKILL.md:1-3`）。`init-session/SKILL.md:195`（フェーズ E レビュー中 → `review-phase` skill）、`codex-review/SKILL.md:211`（Skill ツールで呼び出し）、`hooks/restore_session.py:271`（案内文） |
| 入力 | `.claude/skills/dev-workflow/SKILL.md` を Read（`:11`） |
| 出力 | 自身は何も生成しない。**dev-workflow フェーズ E への転送のみ**（`:12`） |
| 承認ゲート | **0 個**（JSON なし）。`:13` に「AskUserQuestion 手順を省略しない」の参照のみ |
| 付属スクリプト | なし |
| 根拠 | `.claude/skills/review-phase/SKILL.md:1-13` |

---

## setup

| 項目 | 内容 |
|---|---|
| 行数 | 237 |
| 起動方法 | `/setup` 明示、または `init-session` ガード G-2 の `SETUP_NEEDED` によるチェーン起動の **2 経路のみ**（`setup/SKILL.md:10-13`）。実起動元: `init-session/SKILL.md:33`。frontmatter で model invocation を禁止していない理由も同 `:13` に明記 |
| 入力 | ユーザーへの質問 9 問（言語・FW・実行環境・DB・テスト FW・スタイルガイド・コメント方針・カバレッジ・その他） |
| 出力 | Agent ツールで `project-setup` を起動（`:184`）→ `.claude/rules/coding-standards.md` / `project-conventions.md`（`:232-233`）。`session_guard.py setup-mark` で `.claude/state/setup_done.flag` を作成（`:223`） |
| 承認ゲート | **9 個**（`:24,45,61,79,98,117,134,153,170`）— 全て情報収集で、承認/否認の分岐は 0 |
| 付属スクリプト | なし（`init-session/scripts/session_guard.py` を借用・`:223`） |
| 付属 references | `reference.md`(57 行, 言語→拡張子マッピング・公式スタイルガイド)／`templates/coding-standards-template.md`(29)／`templates/project-conventions-template.md`(23)。参照は `:191-193` および `agents/project-setup.md:37-39` |
| 根拠 | `.claude/skills/setup/SKILL.md:1-237` |

---

## start

| 項目 | 内容 |
|---|---|
| 行数 | 154 |
| 起動方法 | `/start`（frontmatter に制限指定なし・`start/SKILL.md:1-3`）。`init-session/SKILL.md:202`（Skill ツールで `start` を呼ぶ）、`init-session/SKILL.md:196`（それ以外 → `start` skill）、`hooks/restore_session.py:271`（案内文） |
| 入力 | `session_guard.py check` の `INIT_DONE`/`INIT_NEEDED`（`:24`）、Glob で `.claude/reports/*.md`（`:38`）、plan-report のフロントマター（`:146`） |
| 出力 | レポートの `archive/` 移動（`:59,81-84`）、`dev-workflow/SKILL.md` を Read してフェーズ A〜E へ遷移（`:139-144`）。`init-session` を `from-start` 付きで Skill 起動（`:28`）、`systematic-debugger` を Agent 起動（`:143`） |
| 承認ゲート | **4 個**（`:45`＝レポート整理、`:67`＝アーカイブ対象フェーズ（multiSelect）、`:94`＝開始地点 4 択、`:119`＝標準ワークフローのサブ選択 3 択） |
| 付属スクリプト | なし（`init-session/scripts/session_guard.py` を借用・`:24`） |
| 根拠 | `.claude/skills/start/SKILL.md:1-154` |

---

## `[起動元なし]` 一覧（1 件）

| skill | 状況 | 根拠 |
|---|---|---|
| `autonomous-mode` | `user-invocable: false` かつ `disable-model-invocation: true` の両方が付いており、`/` 入力でも Skill ツールでも起動できない。`.claude/` 全域と `src/c3` の grep で「Skill ツールで autonomous-mode」は 0 件。実体は `Read` されるだけの規約文書。**skill ディレクトリに置かれているが skill として一度も実行されない** | `autonomous-mode/SKILL.md:3-4`／参照側は `init-session/SKILL.md:187`・`dev-workflow/SKILL.md:38,44` |

### 参考: `[起動元=ユーザーの `/` 入力のみ]`（6 件）

`.claude/commands/` / `.claude/agents/` / `.claude/hooks/` / `.claude/rules/` / `src/c3` の grep で **プログラム的な起動元が 0 件**（ユーザーが `/` を打つ以外に発火経路がない）:

| skill | 根拠 |
|---|---|
| `brainstorm` | grep 0 件（自ファイル内の `/brainstorm` 言及のみ） |
| `codex-review` | grep 0 件（自ファイル内の `/codex-review` 言及のみ） |
| `doc` | grep 0 件。`start/SKILL.md:14` は例示のみ |
| `extract-lib` | grep 0 件 |
| `mcp-config` | grep 0 件 |
| `pattern-status` | grep 0 件（`promote-pattern` からの逆参照もなし） |

なお `.claude/commands/` ディレクトリは**存在しない**（`ls .claude/` 実測）。したがって C3 のスラッシュコマンドはすべて `.claude/skills/` の自動露出に依存する。

---

## `[重複]` 一覧（16 件）

| # | 重複している内容 | 箇所 A | 箇所 B（以降） |
|---|---|---|---|
| 1 | 承認ゲート 10 個の列挙と「D-0 は承認ゲートに含めない」注記 | `dev-workflow/SKILL.md:33-40` | `autonomous-mode/SKILL.md:60-61,78-79` |
| 2 | tier-routing の機械適用ルール（`tier_autoapply.py` が `model:` を自動注入・opus 5 体は対象外・明示指定は尊重） | `dev-workflow/SKILL.md:61-64` | `parallel-agents/SKILL.md:158` |
| 3 | E-1/E-2 の帰属判定パラグラフ（`帰属根拠:明確` / `帰属根拠:要判断` トークンの説明、約 700 字）が **同一ファイル内に 4 回** | `dev-workflow/SKILL.md:1004` | 同 `:1037`／`:1145`／`:1178` |
| 4 | 「C-3 のステップ 0（転記行の評価）と C-2 の宣言生成は HITL 専用」注記 | `dev-workflow/SKILL.md:252` | 同 `:353`／`autonomous-mode/SKILL.md:93` |
| 5 | tier 記録ルール（`--tier` を付けない・`--task` は突合の必須キー・T4 E2E 実測の説明、約 500 字） | `parallel-agents/SKILL.md:236` | 同 `:375` |
| 6 | `mode_line.py` の Bash 呼び出し例と「文字列の目視比較で代替しない」規定 | `autonomous-mode/SKILL.md:335-340` | `init-session/SKILL.md:172-179` |
| 7 | SR L-2 サニタイズ（C0/C1・DEL・改行・`-->`・U+2028/U+2029 の除去） | `init-session/SKILL.md:59-62` | `autonomous-mode/SKILL.md:342-346`／`dev-workflow/SKILL.md`（`現在地:` 表示規約 `:23`） |
| 8 | パス用 choke point（改行/NUL 除去 → realpath → `~/.claude/plans/` 封じ込め） | `init-session/SKILL.md:64-67` | `autonomous-mode/SKILL.md:322-333` |
| 9 | D-0 実行モード判別（`po_plan_version` の `"0.1"` / `"sequential"` / 無し / 不正値の 4 分岐） | `dev-workflow/SKILL.md:550-560` | `develop/SKILL.md:14-15`／`start/SKILL.md:146`／`parallel-agents/SKILL.md:8,42-43` |
| 10 | 「最初に必ず dev-workflow/SKILL.md を Read する／AskUserQuestion・Edit の手順を省略しない」3 項目 | `develop/SKILL.md:11-13` | `review-phase/SKILL.md:11-13`（15 行と 13 行の skill が同じ文面） |
| 11 | 人間の関所 2 類型（非可逆操作・情報不足の質問）と「検証用ビルド足場は含まない」 | `autonomous-mode/SKILL.md:109-117` | `.claude/docs/autonomous-mode-onboarding.md:49-57` |
| 12 | 資源同意＝費用同意ではない／ハード上限はサイクル回数のみ担保 | `autonomous-mode/SKILL.md:221` | `.claude/docs/autonomous-mode-onboarding.md:96-104` |
| 13 | worktree auto-cleanup と 2.1.150 未満のフォールバック手順の説明 | `parallel-agents/SKILL.md:44,307-309` | `.claude/docs/parallel-agents-setup.md:86-97` |
| 14 | 昇格候補の定義（`promotion_candidate: true` かつ `promoted: true` でない） | `pattern-status/SKILL.md:34` | `promote-pattern/SKILL.md:14`／`init-session/SKILL.md:92` |
| 15 | Approval Flow の 3 択（承認／否認・修正を依頼／否認・自分で修正） | `.claude/CLAUDE.md`（Approval Flow 節） | `dev-workflow/SKILL.md:103-112,169-178,312-321` の 3 択 JSON |
| 16 | セッションファイルの更新ルール（`- [ ]`→`[x]` と `現在地:` の同時更新） | `dev-workflow/SKILL.md:15-29` | `init-session/SKILL.md:212-232` |

---

## `[実体なし]` 一覧（5 件）

| # | 参照元 | 参照先 | 状況 |
|---|---|---|---|
| 1 | `codex-review/SKILL.md:37,135` | `.codex/agents/code-reviewer.toml` / `{reviewer_type}.toml` | **本リポジトリに `.codex/` ディレクトリが存在しない**（`ls .codex/agents/` → No such file）。skill 側は前提確認で終了する導線を持つ（`:39-43`）が、配布元では常に不成立 |
| 2 | `agents/doc-writer.md:32,64`（`doc` skill の下流） | `.claude/commands/doc.md` | **`.claude/commands/` ディレクトリ自体が存在しない**。doc-writer は「起動元: `.claude/commands/doc.md`」と書くが実体は `doc/SKILL.md` |
| 3 | `promote-pattern/SKILL.md:82,137` | `.claude/skills/promoted-YYYYMMDD-{id}/SKILL.md` | 生成物のため現状 **0 件**（`find .claude/skills -type f` に該当なし）。昇格経路が一度も使われていないことを示す |
| 4 | `src/c3/_excludes.py:66` | `skills/worktree-tdd-workflow/*` | 除外パターンが残っているが**当該 skill ディレクトリは存在しない**（v2.1.0 で廃止済み）。死んだ除外エントリ |
| 5 | `autonomous-mode/scripts/mode_line.py`（docstring） | — | ファイルは実在するが、docstring が「**本関数は実運用のデータフローからは呼ばれない**」と明記する一方、`autonomous-mode/SKILL.md:335-336` と `init-session/SKILL.md:172-174` は「Bash 経由で**実際に呼び出すこと**・目視比較で代替しない」と機械実行を必須化している。**契約が正面から矛盾**（実体はあるが仕様の実体がない） |

---

## その他の構造的発見（棚卸しの直接入力）

| # | 発見 | 根拠 |
|---|---|---|
| 1 | `dev-workflow/SKILL.md:40` は「`autonomous-mode` skill は配布元限定のため配布物には含まれず」と書くが、`src/c3/_excludes.py` に `skills/autonomous-mode/*` の除外エントリは**存在しない**（grep 実測 0 件）。`src/c3/adapters.py:28` の除外は Codex/Cursor/OpenCode の **adapter 生成物のみ**が射程で wheel には効かない。記述と配布実態の整合は**要確認** | `dev-workflow/SKILL.md:40`／`src/c3/_excludes.py`（該当なし）／`src/c3/adapters.py:23-28` |
| 2 | `dev-workflow/references/` 6 本のうち **3 本（`plan-design-guidelines.md` 180 行・`code-review-checklist.md` 96 行・`security-review-checklist.md` 89 行＝計 365 行）は SKILL.md から一度も参照されない**。参照するのは `.claude/agents/*.md` のみ。skill 配下に置かれているが所有者は agent 層 | `agents/planner.md:37,61`／`code-reviewer.md:45,50`／`security-reviewer.md:46,51`。SKILL.md 側の grep は 0 件 |
| 3 | `develop`(15 行)＋`review-phase`(13 行) は合計 28 行で、**中身はほぼ同一の「dev-workflow を Read せよ」だけ**。skill としての固有ロジックは `develop:14-15` の `po_plan_version` 分岐 2 行のみ | `develop/SKILL.md:1-15`／`review-phase/SKILL.md:1-13` |
| 4 | `dev-workflow` は本文で承認ゲートを **10 個**と定義するが、実際の `AskUserQuestion` JSON は **23 個**。差分 13 個は「否認後のフィードバック入力」「許容理由の入力」「対応する指摘の選択」など分岐先の追い質問 | `dev-workflow/SKILL.md:34` vs JSON 実測 23 個 |
| 5 | `mcp-config` 単体で **13 個**の AskUserQuestion を持ち、`dev-workflow`(23) に次いで多い。MCP サーバー 1 台を登録するのに最大 8 回停止する（`:23,71,91,131,143,175,236,275` 経路） | `mcp-config/SKILL.md` JSON 実測 |
| 6 | `setup` の 9 ゲートは**全て情報収集**で承認/否認の分岐がない。9 回の停止が単一の入力フォームとして機能している | `setup/SKILL.md:24,45,61,79,98,117,134,153,170` |
| 7 | `parallel-agents/SKILL.md:337-365` の checkpoint 記録手順は、シェルインジェクション対策のため「Write ツールで固定パスへ書き→bash は固定リテラルのみ」という **29 行の回避手順**を skill 本文に持つ。防御ロジックが文書として肥大している例 | `parallel-agents/SKILL.md:337-365` |
| 8 | `dev-workflow/SKILL.md:866-918`（E-0 の対象一覧受け渡し）は **53 行**を一時ファイル生成・後始末・プロンプト枠付けの手順に費やす。同様に防御手順が本文を占める | `dev-workflow/SKILL.md:866-918` |
| 9 | skill の起動方式が **3 種混在**する: (a) Skill ツール呼び出し（`setup`/`start`/`init-session`/`report-timestamp`/`review-phase`）、(b) ファイル Read（`dev-workflow`/`parallel-agents`/`autonomous-mode`）、(c) `/` 入力のみ（6 件）。同じ `.claude/skills/` 配下に別物が同居している | `init-session/SKILL.md:33,202`（a）／`develop/SKILL.md:11,14`（b）／§起動元なし の表（c） |
| 10 | `extract-lib` だけが `.claude/agents/*.md` を使わず**無名プロンプトで Agent を起動**する（`:94,238`）。agent 契約（レポート名・記録・tier-routing）の外にある唯一の実行経路 | `extract-lib/SKILL.md:94,238` |

---

## 未確認の項目

| # | 項目 | 未確認の理由 |
|---|---|---|
| 1 | `codex-review` の実挙動（`codex exec` の出力形式・レビュー品質） | `.codex/agents/` が存在せず、skill を起動しない制約のため実行検証していない |
| 2 | `promote-pattern` が生成する `skills/promoted-YYYYMMDD-{id}/` の実物 | 生成実績が 0 件で実体を観察できない |
| 3 | `brainstorm` の AskUserQuestion 実効回数 | `:85` が「出すのは可」と許可するのみで固定数を規定していない。LLM 判断に依存し静的には確定不能 |
| 4 | `promote-pattern:50` の実効ゲート数 | `:60` により選択パターン数だけ繰り返されるため、静的には 1 回分しか数えられない |
| 5 | `dev-workflow` 23 ゲートのうち 1 ワークフローで実際に到達する数 | 分岐（承認/否認・findings 有無・bug-fix モード・parallel/sequential）で排他になり、静的には最大値しか出せない |
| 6 | `autonomous-mode` skill の配布可否の最終判定 | `_excludes.py` に除外エントリが無いことは実測したが、wheel 実体（`dist/*.whl`）の中身は未検証（ビルドを実行していないため） |
| 7 | `mode_line.py` の docstring と SKILL.md の矛盾のうち、どちらが現行仕様か | 両者の記述だけでは決定できない。実行時に呼ばれているかの動的確認が必要 |
