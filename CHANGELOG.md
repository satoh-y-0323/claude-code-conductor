# Changelog

## [1.1.1] - 2026-05-09

### 修正

- v1.1.0 の wheel に開発元リポジトリの `.claude/state/tier_selection.json`（hooks が実行時に書き出す一時ファイル）が混入していた問題を修正。`c3 update` 実行時に利用先プロジェクトに「開発元の最後の Tier 選択結果」がコピーされてしまっていた。
- 恒久対策として `src/c3/_excludes.py` / `hatch_build.py` の `EXCLUDE_PATTERNS` を `state/c3.db*` から `state/*`（`KEEP_PATTERNS` の `state/.gitkeep` のみ残す）に変更。今後 `state/` 配下に新規ファイルが増えても自動的に配布除外される。
- `.gitignore` も同様に `.claude/state/c3.db*` 3 行から `.claude/state/*` + `!.claude/state/.gitkeep` に変更。

### 内部

- `.claude/settings.json` の permission allow 一覧に `git tag*` / `git push origin main` / `git push origin v*` / `gh release create v*` / `gh release view v*` を追加（リリース作業の自動化）。

### 既存利用先への対応

- v1.1.0 を `c3 update` で導入済みの場合、`.claude/state/tier_selection.json` が残っている可能性がある。次回プロンプトで上書きされるか、`record_tier_outcome.py` 実行時に削除されるため副作用は無いが、気になる場合は手動で削除可。

## [1.1.0] - 2026-05-09

### マイルストーン

第 5 波として F-005（Tier 自動ルーティング）の Phase 2 を A/B/C すべて完成。1.0.0 までは「推奨 Tier を `additionalContext` で提示するだけ」の MVP だったが、本リリースで PO 経由のサブエージェントについては **起動時の model を Tier 推奨で動的上書き** するレコメンダーから自動オペレーターへ進化した。さらに過去類似タスクからの complexity 補正と、Haiku 連続失敗時の自動エスカレーションを追加し、F-005 全体の精度・コスト最適化が機能するようになった。

### 追加（第 5 波）

#### F-005 Phase 2-A: model 動的切替（PO 経由）

- `src/parallel_orchestra/manifest.py`: `Task` dataclass に `model_override: str | None`、`Defaults` に `model: str | None` を追加。許可値は `haiku`/`sonnet`/`opus`。`_KNOWN_TASK_KEYS` / `_KNOWN_DEFAULTS_KEYS` に `model` を許可。
- `src/parallel_orchestra/runner.py`: `_resolve_effective_model()` / `_read_tier_selection()` を新規追加。優先順位は (1) `tier_selection.json` の `suggested_model` → (2) `Task.model_override` → (3) `Defaults.model` → (4) frontmatter。値がある場合は `claude --agents '{"<agent>":{"model":"<tier>"}}'` で起動、無ければ既存の `--agent <name>` で後方互換。
- `.claude/hooks/select_tier.py`: `tier_selection.json` payload に `suggested_model` フィールドを追加（tier 名と model 短縮名は同一）。
- 新規テスト: `tests/parallel_orchestra/test_runner_model_override.py`（14 ケース、優先順位 4 パターン + フォールバック含む）。
- スコープ: PO 経由のサブエージェントのみ。親 Claude の Agent ツール経由は公式 API に動的 model 指定手段が無く対象外（CHANGELOG / dev-workflow.md で明示）。

#### F-005 Phase 2-B: Haiku 失敗フォールバック（次回補正のみ）

- `.claude/hooks/schema.sql` に `tier_recent_outcomes` テーブルを新規追加（直近 outcome を保持）。`SCHEMA_VERSION` を 1 → 2 に bump。
- `src/parallel_orchestra/c3_db.py` に `record_tier_recent_outcome()` / `read_tier_failure_rate()` を追加。直近 N 件（既定 10、最小サンプル数 5）から failure rate を計算。
- `.claude/hooks/select_tier.py` に `maybe_escalate()` を実装。Beta サンプリング後、failure rate ≥ 0.5 で 1 段昇格（haiku → sonnet, sonnet → opus）。opus は最上位なので昇格なし。`tier_selection.json` に `escalated: true` / `escalation_reason` を残す。
- `.claude/hooks/record_tier_outcome.py`: tier_bandit 更新と並行して `tier_recent_outcomes` にも 1 件追記。
- 新規テスト: `tests/hooks/test_select_tier_escalation.py`（10 ケース、境界値・サンプル不足・opus 抑止を含む）。
- 設計判断: 同一プロンプト retry はせず「次回プロンプトの選択補正」のみ。倍コストと PO retry 機構との二重化を避けるため案 A を採用。

#### F-005 Phase 2-C: 類似度推定（complexity 推定の精度向上）

- 新規 `.claude/logs/prompt-history.jsonl`（実行時生成、`.gitignore` / `EXCLUDE_PATTERNS` で配布除外済み）。
- `.claude/hooks/select_tier.py`: `difflib.SequenceMatcher` で過去プロンプトとの類似度を計算。threshold 0.8 以上で complexity を上書き、0.6-0.8 は信頼度補強のみ。`tier_selection.json` に prompt 情報（prefix 200 文字 + SHA256 先頭 16 文字）を含める。末尾 1000 行のみ `collections.deque` で読み込み（O(n) のスキャンを抑制）。
- `.claude/hooks/record_tier_outcome.py`: tier_selection.json から prompt 情報を読み、`prompt-history.jsonl` に 1 行追記。旧フォーマット（prompt 情報なし）はスキップする後方互換。
- 新規テスト: `tests/hooks/test_similarity_boost.py`（14 ケース）+ `tests/hooks/test_record_tier_outcome.py` に 4 ケース追加。
- プライバシー対策: prefix 200 文字 + SHA256 16 文字のみ保存し、フルプロンプトは保存しない。

### 注意（既存利用先への影響）

- PO の動的 model 切替は **PO 経由のサブエージェントのみ** 対象。dev-workflow フェーズ A/B/C/E や対話中の Agent ツール経由は依然 frontmatter 指定が優先される。コスト最適化したい場合は手動切替が必要。
- `.claude/state/c3.db` は `SCHEMA_VERSION` 2 に bump されるが、`CREATE TABLE IF NOT EXISTS` のみで既存 DB を破壊しない（マイグレーション不要）。
- `.claude/logs/prompt-history.jsonl` は新規生成される。`.gitignore` / `EXCLUDE_PATTERNS` で除外済みのため利用先プロジェクトに git 汚染は出ない。

### 内部

- 新規テスト追加: 14（Phase 2-A）+ 10（Phase 2-B）+ 14 + 4（Phase 2-C）= **42 ケース**。
- 全体テスト: **705 passed / 3 skipped / 0 failed**（前バージョン 663 → 705、+42）。

## [1.0.0] - 2026-05-09

### マイルストーン

c3 追加予定機能リスト（10 機能 F-001 〜 F-010）を全て実装完了。第 1 波（F-006/007/008）→ 第 2 波（F-009 基盤）→ 第 3 波（F-001/002/004/010）→ 第 4 波（F-003/005）の段階的リリースが完了し、Ruflo 調査から派生した「自己観測 + 自己学習」C3 が 1.0 として一区切り。

### 追加（第 4 波）

#### F-003: PO 並列処理の状況可視化

- `src/parallel_orchestra/c3_db.py` に `upsert_po_status()` / `fetch_po_status()` を追加（SQLite 3.24+ の `INSERT ... ON CONFLICT ... DO UPDATE` で UPSERT、未知 state は警告のみで通過）。
- `src/parallel_orchestra/runner.py` の `_Dashboard` に `snapshot_states()`（thread-safe コピー）を追加。`_PO_STATUS_STATE_MAPPING` で内部 `_TaskStatus` → schema state の語彙変換を定義。新規 `_heartbeat_po_status_loop()` 関数で 30 秒ごとに状態を UPSERT（waiting タスクは除外）。
- `run_manifest()` で session_id を上部に移動（F-002 と F-003 で共有）、dashboard が enabled な場合のみ heartbeat スレッドを起動。`dashboard.stop()` の前に最終状態を 1 回 UPSERT してからスレッド停止。
- 新規 `.claude/skills/po-status/SKILL.md`（4 種の SELECT パターン: 直近 active / session 別 / stale 検出 / 全履歴）。
- `tests/parallel_orchestra/test_po_status_visibility.py` 新規追加（19 ケース）。

#### F-005: Tier 自動ルーティング（MVP）

- `src/parallel_orchestra/c3_db.py` に `read_tier_params()` / `update_tier_params()` を追加。各 Tier の Beta(α, β) を行が無ければ `Beta(1,1)` で初期化扱い、UPSERT で α または β を加算、trials+=1。
- 新規 `.claude/hooks/select_tier.py`（UserPromptSubmit hook）: 複雑度を文字数 + キーワードで simple/medium/complex に推定。合計 trials < 30 は uniform random（学習データ収集期）、≥ 30 は `random.betavariate()` による純 Thompson Sampling で Tier 選定。結果を `.claude/state/tier_selection.json` に保存し、`additionalContext` で「推奨 Tier: sonnet（信頼度 trials=12）」を返す。
- 新規 `.claude/hooks/record_tier_outcome.py`（CLI）: dev-workflow から `--outcome success/failure` で呼ばれて α/β を更新、json を削除（DB 不在時は json 維持してリトライ可能）。
- `.claude/settings.json` の `UserPromptSubmit` セクションを新規追加し `select_tier.py` を登録。
- `.claude/skills/dev-workflow/SKILL.md` フェーズ E-2（最終承認）のみに `record_tier_outcome.py` 呼び出しを統合（多重カウント防止のため E-1 では呼ばない）。
- 新規テスト 30 ケース（`test_select_tier.py` 24 ケース + `test_record_tier_outcome.py` 6 ケース）。
- 依存ライブラリ追加なし（`random.betavariate()` で stdlib 完結）。
- **MVP スコープ**: 推奨提示のみ（agent の `model:` フロントマター動的書き換えは次フェーズ）。

### MVP として残した拡張ポイント

- F-002 Phase 2: PO の worktree 内からの直接 SQLite 書き込み（環境変数で DB パス共有）。
- F-004: patterns.json の自動 promotion 判定変更、auto-memory への直接書き込み、LLM 要約による集約。
- F-005: agent の `model:` フロントマター動的書き換え、Haiku 失敗時の Sonnet 自動昇格、過去類似タスクからの complexity 類推。

### 注意（既存利用先への影響）

- `UserPromptSubmit` hook が新規登録される。プロンプト送信時に毎回 `additionalContext` で推奨 Tier が表示されるようになる。
- `Stop` hook は既に F-004 で追加された `consolidate_memory.py` がいる。第 4 波で Stop 周りの追加変更はない。
- レビューフロー（dev-workflow フェーズ E-2）の最終承認時に `record_tier_outcome.py` が呼ばれて c3.db に記録される。記録失敗時は `tier_selection.json` が残るが、次回プロンプトで上書きされるため副作用なし。

### 内部

- 新規テスト追加: 19（F-003）+ 30（F-005）= **49 ケース**。
- 全体テスト: **663 passed / 3 skipped / 0 failed**（前バージョン 614 → 663、+49）。

## [0.9.1] - 2026-05-09

### 修正

- F-004 で生成される `.claude/memory/consolidated_summary.md` が `.gitignore` および `EXCLUDE_PATTERNS` に登録されていなかったため、利用先プロジェクトで余計な untracked ファイルが残ってしまう問題を修正。`.gitignore` / `src/c3/_excludes.py` / `hatch_build.py` の 3 箇所に追加。

## [0.9.0] - 2026-05-09

### 追加（第 2 波・第 3 波）

#### F-009: DuckDB ハイブリッド構成（基盤）
- `pyproject.toml` の `dependencies` に `duckdb>=0.10` を追加（必須化）。
- 新規 `.claude/hooks/schema.sql`: 6 テーブル（`schema_version` / `review_decisions` / `po_results` / `po_status` / `tier_bandit` / `agent_runs`）の DDL を定義。
- 新規 `.claude/hooks/init_c3_db.py`: `.claude/state/c3.db` を WAL モードで初期化し、`CREATE TABLE IF NOT EXISTS` で冪等にスキーマ適用。失敗してもセッションを止めない。
- `.claude/settings.json` の `SessionStart` に登録（matcher 空で全セッション開始時に実行）。
- 配布版にも `.claude/state/.gitkeep` でディレクトリ確保。`c3.db` 本体は `.gitignore` / `EXCLUDE_PATTERNS` で除外。
- 書き込みは Python 標準 `sqlite3`、読み・分析は DuckDB の `sqlite_scanner` で ATTACH するハイブリッド構成。

#### F-010: タスク種別 → エージェント編成 skill
- 新規 `.claude/skills/task-routing/SKILL.md`: bug-fix / feature / refactor / security-audit / docs の 5 種別 × 対応エージェント編成テーブルを定義。
- AskUserQuestion での選択 → 編成提示 → 承認 → 起動の 4 Step フロー。新エージェント追加時は本 SKILL.md の編成テーブルも更新する旨を注記。

#### F-002: PO 集約レイヤ SQLite 化（Phase 1）
- 新規 `src/parallel_orchestra/c3_db.py`: `locate_c3_db()` / `record_task_results()` で TaskResult を `po_results` テーブルに INSERT。DB 不在時 / SQL エラー時は警告ログのみで PO 本体を止めない。
- `src/parallel_orchestra/runner.py`: `run_manifest()` 終了時に `record_task_results` を呼ぶ。`session_id` は `{manifest.name}_{started_at}` 形式で自動生成。
- Phase 2（worktree 内からの直接書き込み）は要望が出るまで保留。

#### F-001: レビュー判断ヒント機能
- `.claude/rules/code-review-checklist.md` / `security-review-checklist.md` の全項目（CR: 58 / SR: 47）に `[CR-XX-NNN]` / `[SR-XX-NNN]` 形式の ID を付与。
- `.claude/agents/code-reviewer.md` / `security-reviewer.md` の Workflow に「指摘時に該当 ID を併記」の指示を追加。
- `src/parallel_orchestra/c3_db.py` に `fetch_review_decisions` / `insert_review_decision` / `aggregate_decisions` ヘルパーを追加。
- 新規 `.claude/hooks/review_hint_inject.py`: レビューレポートから ID を抽出し、`c3.db.review_decisions` の過去判断を「## 過去判断ヒント」として末尾に追記。6 ヶ月超は `[要再評価]` ラベル、両レポートで同一 ID は ⚠ 重複指摘フラグを付与。
- 新規 `.claude/hooks/record_review_decision.py`: dev-workflow から判断を `c3.db` に INSERT する CLI ラッパー。
- `.claude/skills/dev-workflow/SKILL.md` フェーズ E に上記スクリプトの呼び出しを統合。

#### F-004: MemoryConsolidation 集約フック（MVP）
- `.claude/hooks/session_utils.py` に `extract_section()` を共通化（既存 `restore_session.py` の独自実装は後方互換のため残置）。
- 新規 `.claude/hooks/consolidate_memory.py`: 過去 7 日分の `.claude/memory/sessions/*.tmp` から「うまくいったアプローチ」「試みたが失敗したアプローチ」を抽出し、重複行除去 + 出現順保持で `.claude/memory/consolidated_summary.md` に出力。LLM 要約は使わず決定論的な単純行マージ。
- `.claude/settings.json` の `Stop` hook 配列に登録（既存 `stop.py` と並列実行）。
- patterns.json の自動 promotion 判定変更や auto-memory への直接書き込みは次フェーズで検討。

### 内部（テスト・運用ドキュメント）

- 新規テストファイル 4 件 / 計 47 ケース追加:
  - `tests/hooks/test_init_c3_db.py`（9 ケース）
  - `tests/parallel_orchestra/test_po_results_recording.py`（12 ケース）
  - `tests/hooks/test_review_hint_inject.py`(16 ケース）
  - `tests/hooks/test_consolidate_memory.py`（10 ケース）
- 全体テスト: **614 passed / 3 skipped / 0 failed**（前バージョン 567 → 614、+47）。
- 機能検討ドキュメント 4 件（`ruflo_research_result.md` / `c3候補機能への質問に対する回答.md` / `c3候補機能採用.md` / `c3追加予定機能リスト.md`）はローカル管理のみ（git / 配布物から除外）。

### 注意（既存利用先への影響）

- `pyproject.toml` の依存に DuckDB が追加された。`pip install --upgrade claude-code-conductor` で自動的に取り込まれる。Python 3.10 以上で動作する軽量パッケージ（wheel 10-30MB）。
- `SessionStart` 時に `.claude/state/c3.db` が自動生成される（無ければ作成、既存なら no-op）。利用先プロジェクトでは `.gitignore` に `.claude/state/c3.db*` を追加することを推奨。
- レビュー時の `code-reviewer` / `security-reviewer` の出力に `[CR-XX-NNN]` / `[SR-XX-NNN]` 形式の ID 表記が含まれるようになる。
- `Stop` 時に `.claude/memory/consolidated_summary.md` が自動更新される。

## [0.8.0] - 2026-05-08

### 追加（フック・第 1 波）

- **F-006: Bash 秘密情報検出フック**: `hooks/pre_tool.py` に正規表現ベースの判定を追加。`password=` / `api_key=` / `Bearer` / `token=` / `secret=` / `aws_secret_access_key=` / PEM 秘密鍵の 7 パターンを検出してブロックする。誤爆時は環境変数 `C3_SKIP_SECRET_CHECK=1` で bypass 可能。警告メッセージには検出値そのものを含めず、二次漏洩を防ぐ設計。これは既存 `security-review-checklist.md` の項目「秘密情報がログに出力されていないか」を実行前に自動化するもの。
- **F-007: Edit 後コード品質スキャンフック**: 新規 `hooks/post_tool.py` を追加し、Write / Edit 完了後にコード品質スキャンを実行する。`console.log` / `print(` / `TODO` / `FIXME` / `XXX` を検出して警告（**非ブロッキング**、`exit 0`）。対象拡張子は `.py` / `.js` / `.ts` / `.tsx` / `.jsx` / `.cs` / `.go` / `.rs`。バイナリ（先頭 8 KB に NUL バイトを含む）と 256 KB 超は先頭のみスキャン。`settings.json` の `PostToolUse` に既存 `validate_skill_change.py` と並列で登録。配布版にも含める。
- **F-008: SubagentStop メトリクス拡張**: `hooks/subagent_log.py` の `_SAFE_PAYLOAD_FIELDS` に `total_tokens` / `status` / `token_usage` / `model` を追加。Tier 自動ルーティング（学習ベースのモデル選択）の学習データ収集の前提となる。`result` 系は応答本文・コード断片の混入リスクがあるため引き続き除外。

### 注意（既存利用先への影響）

- F-006 は既存 Bash 実行に対して **新規ブロック動作** を導入する。`echo password=...` のような書式は今後ブロックされるため、誤爆した場合は `C3_SKIP_SECRET_CHECK=1` を設定して回避すること。
- F-007 は警告のみで非ブロッキング。既存 `validate_skill_change.py` と並列実行されるため、`.claude/skills/` 配下のファイル変更時は両方の hook が動く（出力重複は許容、責務分離優先）。

### 内部（テスト・除外設定）

- `tests/hooks/test_pre_tool.py` を新規追加（14 ケース）。既存 `rm -rf` 等のリグレッション防止 + F-006 各種検出 / 偽陽性回避 / bypass 動作を網羅。
- `tests/hooks/test_post_tool.py` を新規追加（15 ケース）。各パターン検出 / 対象外拡張子スキップ / バイナリスキップ / 大ファイル制限 / 言語制限を網羅。
- `tests/hooks/test_subagent_log.py` に `TestF008MetricsFieldsExtended` クラス（4 ケース）を追加。新フィールド記録 / `result` 除外維持 / 並列ペアリングの整合を検証。
- 全体テスト: **567 passed / 3 skipped / 0 failed**（既存 534 + 新規 33）。
- 機能検討ドキュメント 4 件（`ruflo_research_result.md` / `c3候補機能への質問に対する回答.md` / `c3候補機能採用.md` / `c3追加予定機能リスト.md`）を `.gitignore` と `EXCLUDE_PATTERNS`（`src/c3/_excludes.py` と `hatch_build.py` の両方）に追加し、git 追跡と wheel 配布の両方から除外。

## [0.7.1] - 2026-05-08

### 追加（開発者向け）

- `agents/`: developer / tester / code-reviewer / security-reviewer / systematic-debugger の 5 サブエージェントに `memory: project` フロントマターを付与。`.claude/agent-memory/<エージェント名>/MEMORY.md` が起動時にシステムプロンプトへ自動注入され、セッションをまたいだ知見蓄積が可能になった。配布版（`_template/`）には `agent-memory/` を含めず、利用側はゼロから蓄積する方針。
- `hooks/subagent_log.py`（C3 開発版専用）: SubagentStart / SubagentStop イベントの payload を `.claude/logs/agent-runs.jsonl` に追記する hook を追加。`payload.agent_id` ベースで Start / Stop をペアリングし `duration_seconds` を計算。配布版（`_template/`）からは `EXCLUDE_PATTERNS` で除外。

### 修正（`subagent_log.py` 堅牢化）

- U+2028 / U+2029 を JSON ASCII エスケープして JSONL 構造破壊を防止。
- payload をホワイトリスト方式でサニタイズし、`last_assistant_message` 等の長文・任意コンテンツが永続化されないように変更。
- `stdin` を 1 MB 上限・ログ走査を末尾 10,000 行に制限してメモリ DoS を防止。
- ファイル作成パーミッションを `0o700` / `0o600` に明示設定（POSIX）。
- `stdin` / `json.loads` 失敗時の `Exception` catch と record 非書き込みでフェイルセーフ化。
- `_append_log` の catch を `OSError` から `Exception` に拡大（`json.dumps` の `TypeError` 等も対応）。
- `collections.deque` ベースのペアリングで `popleft()` を `O(1)` 化、走査コストを削減。

### 内部（テスト・ドキュメント整備）

- `tests/hooks/test_subagent_log.py` を 6 → 19 ケースに拡張（U+2028 エスケープ・残留 Start・Stop 先着・サニタイズ・巨大 payload・`TypeError` 耐性・`main()` 戻り値検証など）。
- `tests/hooks/test_restore_session.py` を新規追加（13 ケース、`find_latest_session` / `extract_section` / `main` subprocess）。
- `tests/hooks/test_permission_handler.py` を新規追加（29 ケース、`load_rules` / `_glob_to_regex` / `matches_pattern` / `describe_tool` / `main` subprocess / `notify_on_auto`）。
- `.claude/docs/taxonomy.md`: `.claude/rules/` フロントマターの `description` キー記述を削除（公式仕様に存在しないため）。
- `.claude/CLAUDE.md`: コミットメッセージ・チェンジログ・リリースページの日本語記述ルールを追記。
- `.claude/settings.local.json`: SubagentStart / SubagentStop hook 登録と開発作業用 Bash 許可を追加。

## [0.6.4] - 2026-05-07

### Changed
- `CLAUDE.md`: 不要・重複・抽象的なセクションを削除・整理（Startup Protocol / Session Update Rules / Pattern Recording / Rule Compliance を削除、Communication Style・User Interaction Rules・Approval Flow・Compact Instructions・Available Commands・Directory Structure を整理・圧縮）
- `agents/`: `report-timestamp` スキル呼び出しとファイル出力の記述を全エージェントで統一（architect・code-reviewer・doc-writer・interviewer・planner・security-reviewer）
- `skills/`: `.claude/skills/` プレフィックスなしのパス参照を修正（code-review・dev-workflow・develop・start・wave-execution）
- `skills/dev-workflow/SKILL.md`: `commands/` の記述を `skills/` に更新、description に wave-execution を参照元として追記
- `README.md`: パターン昇格パス・c3 po コマンド一覧・PO 並列実行の説明・エージェント一覧（tdd-develop・systematic-debugger 追加）を現状に合わせて修正

## [0.6.3] - 2026-05-07

### Fixed
- `read_only=true` tasks (e.g. `code-reviewer`, `security-reviewer`) were
  launched with `--read-only`, a flag that does not exist in Claude Code CLI,
  causing immediate failure. All tasks now use `--dangerously-skip-permissions`
  regardless of `read_only`. `read_only` controls worktree creation only and
  is never passed to the `claude` binary.
- `wave-execution/SKILL.md`: update timeout note from 900 s (15 min) to
  1200 s (20 min) to reflect the v0.6.1 change.

## [0.6.2] - 2026-05-07

### Fixed
- `c3 update` no longer copies `.claude/logs/` files into the destination
  project. `logs/*` is now listed in `EXCLUDE_PATTERNS` in both
  `hatch_build.py` and `src/c3/_excludes.py`.

## [0.6.1] - 2026-05-07

### Changed
- Raise default agent timeout from 900 s to 1200 s (`_INTERNAL_TIMEOUT_SEC`).
- Raise default parallel worker count from 3 to 5 (`_DEFAULT_MAX_WORKERS`).

## [0.6.0] - 2026-05-07

### Changed
- **Bundled `parallel-orchestra` (PO) into the C3 distribution.** PO is now
  part of the same wheel as C3; users no longer need to `pip install
  parallel-orchestra` separately. The `parallel-orchestra` CLI command is
  still exposed as a console script for backward compatibility.
- `c3.po.run.run_manifest` now calls `parallel_orchestra.run_manifest`
  directly via the Python API instead of spawning a `parallel-orchestra`
  subprocess. The `subprocess` round-trip and ANSI-stream parsing are
  removed; PO failures surface as typed `ManifestError` / `RunnerError`
  exceptions and are mapped to C3 exit codes.
- `c3.po.manifest` now uses `yaml.safe_load` (via the new `PyYAML>=6.0`
  dependency) for frontmatter parsing. The ~200-line homegrown YAML
  subset parser is removed.
- `c3 doctor` now reports the bundled PO version instead of probing for a
  separately installed `parallel-orchestra` binary on PATH.

### Removed (post-bundling cleanup)
- **Webhook notifications** (`on_complete` / `on_failure` / `webhook_url`)
  removed from the manifest schema, parser, and runner. C3 never used
  webhooks (they were dropped explicitly in `build_wave_manifest_text`).
  Roughly 160 LOC of dispatch/SSRF-mitigation code and `urllib.request`
  imports are gone.
- **`--resume` and `RunState`**: `parallel_orchestra/run_state.py`
  deleted. The `parallel-orchestra run --resume` flag is removed; C3
  commits each wave on completion so partial-run restoration is
  unnecessary. Roughly 240 LOC and the `resumed` field on `TaskResult`
  are gone.
- **`parallel_orchestra/__main__.py`** removed. Use the
  `parallel-orchestra` console script (or `c3 po run`) instead of
  `python -m parallel_orchestra`.
- **PO CLI options** that C3 doesn't expose: `--log-dir` / `--no-log`
  / `--dashboard` / `--no-dashboard` / `--dry-run` (C3 has its own
  `c3 po dry-run` subcommand). The `format_dry_run` helper is
  removed.
- `src/c3/po/detect.py` and the `RunStatus="not_installed"` branch — PO
  is always available now that it ships with C3.
- The "PO is not installed" guidance in `wave-execution` Step 0 and the
  "optional install" section of the README; replaced with a note that
  PO is bundled.
- **PO public API trimmed**: `parallel_orchestra` now re-exports only
  `run_manifest`, `load_manifest`, `RunResult`, `ManifestError`,
  `RunnerError`, and `ParallelOrchestraError`. `Defaults`, `Manifest`,
  `Task`, `TaskResult`, `WebhookConfig`, `SUPPORTED_PLAN_VERSIONS`,
  and `generate_report` are no longer top-level exports (still
  accessible via `parallel_orchestra.manifest` /
  `parallel_orchestra.runner` for advanced callers).

### Added
- `PyYAML>=6.0` runtime dependency.
- `tests/parallel_orchestra/` — PO's test suite is now run as part of
  the C3 test run (`pytest tests/`).
- `[tool.pytest.ini_options]` block declaring `testpaths` and the
  `slow` marker (carried over from PO's pyproject.toml).
- Explicit `_check_duplicate_task_ids` validation in
  `parallel_orchestra.load_manifest`. Previously duplicate IDs were
  caught only indirectly via `depends_on` reference checks.

### Changed (post-bundling cleanup)
- `c3.po.manifest.validate_manifest` now delegates structural
  validation to `parallel_orchestra.load_manifest` and only adds the
  C3-specific check that each task's `agent` resolves to a file under
  `.claude/agents/`. The duplicated po_plan_version / name / cwd /
  task-field checks are removed (~70 LOC).
- `c3.po.manifest._yaml_quote` reuses `json.dumps(..., ensure_ascii=False)`
  for double-quoted YAML scalars instead of hand-rolled escaping.

## [0.5.1] - 2026-05-05

### Added
- New `/pattern-status` skill (read-only) that visualizes `patterns.json`:
  trust score distribution, promotion candidates, expiry-near patterns,
  and already-promoted patterns. Use it before `/promote-pattern` to
  inspect the current state without modifying the file.
- `session_utils.append_checkpoint(session_file, label, summary)` helper
  for milestone state snapshots. Safely handles non-existent and empty
  session files by writing the template before appending. Used by both
  `wave-execution` (success/skipped-failure waves) and `pre_compact.py`.
- `CLAUDE.md` "When to use /compact" guideline — decision flow for
  `/compact` vs session restart, aimed at clarifying the choice for
  power users (restart) vs casual users (`/compact`).

### Changed
- `pre_compact.py` now emits `hookSpecificOutput.additionalContext` to
  inject KEEP/DISCARD save instructions into Claude's context just
  before compaction. Previously the hook only wrote a timestamp marker
  to the session file. Claude now writes important state (remaining
  tasks, key decisions, resolved gotchas) to the session before the
  context shrinks.
- `wave-execution` Step 2-F now records a checkpoint block to the
  session file on every wave completion (success or skipped failure),
  in addition to flipping `[ ]` → `[x]`. This gives a time-stamped
  trail of milestones for `/init-session` and `/pattern-status`.
- Added `WebSearch` to `permissions.allow` in `settings.json` so
  research subagents can use it without prompting.

## [0.5.0] - 2026-05-05

### Added
- New `systematic-debugger` agent: dedicated investigation phase for
  root-cause analysis and pattern matching when `developer` gets stuck.
  Runs in a separate phase from implementation, preserving C3's
  multi-agent separation.
- `developer` agent: Stuck Signal — after 3 failed attempts at the
  same problem, write a `debug-needed` report and stop, letting the
  orchestrator dispatch `systematic-debugger`.
- `dev-workflow` D-2.5 and `worktree-tdd-workflow` Step 3.5: detect
  Stuck Signal, run systematic-debugger, re-invoke developer with the
  debug analysis injected.
- `tester` agent: Verify RED rule — before handing off to developer,
  confirm tests fail for the right reason (missing feature, not
  syntax errors), and document the verification in the test-report.
- `developer` agent: minimal code principle — Green phase writes
  only what tests require, no premature extensions or speculative
  abstractions.
- `developer` agent: lightweight verification before tester handoff
  (syntax/build check) drawn from superpowers' verification ideas
  while keeping C3's agent-separated structure.

## [0.4.0] - 2026-05-04

### Breaking Changes
- **Skill renamed**: `/review` → `/code-review` — avoids conflict with the
  official Claude Code `/review [PR]` command (which reviews pull requests).
  C3's `/code-review` runs `code-reviewer` + `security-reviewer` agents
  as dev-workflow phase E.
- **Skill renamed**: `/mcp` → `/mcp-config` — avoids conflict with the
  official Claude Code `/mcp` command (which manages live MCP connections).
  C3's `/mcp-config` manages `mcpServers` entries in `.claude/settings.json`.
- **Skill structure**: `commands/` directory migrated to `skills/` following
  the Claude Code 2026 skills standard. All skills are now under
  `.claude/skills/{name}/SKILL.md` with YAML frontmatter.

### Added
- `stop.py`: Records `last_assistant_message` from Stop hook payload into the
  session file's 事実ログ section as `- 最終応答: ...` (truncated at 500 chars).
  The next session's init-session can now read what Claude last accomplished.
- `session_utils.py`: New shared module exporting `SESSIONS_DIR`,
  `SESSION_JSON_MARKER`, `is_worktree()`, and `create_session_template()`.
  Eliminates duplicate definitions across `stop.py` and `pre_compact.py`.

### Fixed
- `settings.local.json` had a duplicate `hooks` section identical to
  `settings.json`, causing all hooks to fire twice per event. Removed.
- Hook commands now use `"$CLAUDE_PROJECT_DIR/.claude/hooks/…"` (absolute
  path via env var) so hooks remain findable even after `cd` changes CWD.
  The `cd` block in `pre_tool.py` has been removed as it is no longer needed.
- `UserPromptSubmit` hook for `statusline.py` removed — the hook input has no
  `context_window` field, so it always displayed `0%`. The `statusLine`
  setting handles display correctly on its own.
- `stop.py`: Reads and respects `stop_hook_active` flag — skips processing
  on re-entrant Stop calls to prevent duplicate session updates.
- `pre_compact.py`: Uses `__file__`-based paths instead of `os.getcwd()` so
  the session file is always found regardless of working directory.
- `pre_compact.py`: Records `trigger` (manual/auto) and `context_items_before`
  in checkpoint output for richer context.
- `stop.py`: Sanitizes surrogate characters (`\udc80`–`\udcff`) in
  `last_assistant_message` before writing to avoid `UnicodeEncodeError`.
- `settings.json`: Added missing `Write`/`Edit` permissions for
  `.claude/reports/archive/**`, `.claude/rules/**`, `.claude/settings.json`,
  `Edit(.claude/memory/**)`, `Edit(.claude/rules/**)`, `Edit(.claude/skills/**)`.
- Bash permissions for hook scripts now include both relative-path and
  `$CLAUDE_PROJECT_DIR`-prefixed forms for full coverage.

## [0.3.4] - 2026-05-02

### Security
- `pre_tool.py`: Hardened `rm -rf` detection — flags are now collected
  only from tokens immediately following the `rm` command, preventing
  false-negatives when earlier commands in a pipeline carry `-r`/`-f`
  flags (e.g. `grep -rf … && rm file`). Also added detection of
  `--recursive --force` long-option combinations.
- `pre_tool.py`: Extended `cd` block to cover subshell `$()`, backtick,
  newline, and `eval "cd …"` bypass paths that the previous regex missed.
- `stop.py`: Field whitelist on `patterns.json` writes — only
  allow-listed keys are written and `promoted` can never be injected
  via a session JSON block. Added `MAX_ID_LENGTH = 64` and
  `MAX_DESCRIPTION_LENGTH = 500` guards.
- `manifest.py`: `writes`, `agent`, and `concurrency_group` values in
  generated wave manifests are now passed through `_yaml_quote` to
  prevent newline injection into the ephemeral YAML.

### Fixed
- `run.py`: Replaced `assert process.stderr is not None` (silently
  removed by `-O` optimised bytecode) with an explicit
  `if … is None: raise RuntimeError(…)` guard.
- `pre_compact.py`: Replaced `os.path.exists()` + `open('w')` TOCTOU
  with `open('x')` + `except FileExistsError` — matches the pattern
  already used in `stop.py`.
- `stop.py`: `update_patterns` called `os.listdir` inside the pattern
  loop, causing O(N×M) file-system reads. A single `_build_sessions_by_date`
  call outside the loop reduces this to O(N+M).
- `manifest.py`: Removed dead branch `rest is None` (always `False`
  for `str.partition` return values). Double-quoted YAML scalars now
  handle `\\`, `\"`, `\n`, `\t`, and `\r` escape sequences.
- `cli_po.py`: `run-wave` temp manifest now uses `tempfile.NamedTemporaryFile`
  (unpredictable name) and is deleted in a `try/finally` block regardless
  of outcome.
- `cli_list.py`: `OSError` when reading a file in `_summary` is caught
  and returns `"(unreadable)"` instead of propagating and breaking the
  entire listing.
- `run.py`: Replaced `__import__("sys").stderr` idiom with `sys.stderr`.
- `manifest.py`: `validate_manifest` local `version` renamed to
  `plan_version` to avoid shadowing a potential future import.
  `build_wave_manifest_text` accepts an optional `waves` argument to
  avoid recomputing the wave graph when the caller already has it.

### Changed
- `pre_compact.py` / `stop.py`: `SESSION_JSON_MARKER = 'C3:SESSION:JSON'`
  constant is now defined in both files — eliminates the hard-coded
  string in `pre_compact.py` and makes the two files consistent.
- `stop.py`: Import block reordered to comply with PEP 8 (all imports
  before module-level statements).
- `validate_skill_change.py`: Early-exit paths changed from
  `sys.exit(0)` to `return`; `__main__` block uses
  `sys.exit(main() or 0)` pattern, consistent with `pre_tool.py`.
- `clear_file_history.py`: Added `os.path.islink` pre-check so
  symbolic links are removed with `os.unlink` rather than
  `shutil.rmtree`, preventing accidental recursive deletion of a
  symlink target on some platforms.
- `worktree_guard.py`: Removed noisy `stderr` log on every tool call
  when `PO_WORKTREE_GUARD` is unset; the hook now exits silently when
  the guard is disabled.
- Template sync: all seven files under `src/c3/_template/.claude/hooks/`
  are now identical to their counterparts under `.claude/hooks/`, so
  `c3 init` / `c3 update` distribute the corrected implementations.

## [0.3.3] - 2026-05-01

### Fixed
- `__pycache__/` and `.pyc`/`.pyo` artefacts no longer ship in the
  wheel and no longer leak into user projects via `c3 init` /
  `c3 update`. Previous releases shipped Python bytecode caches at
  any path under `.claude/` whenever the dev had run hooks before
  the build (notably `.claude/hooks/__pycache__/*.pyc`). The
  `should_skip` predicate in both `c3._excludes` and `hatch_build.py`
  now short-circuits on any path component named `__pycache__` or
  any `.pyc` / `.pyo` suffix.
- `tests/test_excludes.py`: regression test
  `test_excludes_pycache_at_any_depth` asserts the new behaviour at
  multiple directory depths and confirms `.py` source files remain
  framework files.

## [0.3.2] - 2026-05-01

### Fixed
- `c3 update` and `c3 init` no longer overwrite the user's
  `.claude/settings.local.json`. This file is per-machine permission
  state that Claude Code edits when granting tool permissions; the
  bundled template should never replace it. `settings.local.json`
  is now in `EXCLUDE_PATTERNS` in both `c3._excludes` (used at
  runtime by `c3 init` / `c3 update`) and `hatch_build.py` (used at
  wheel-staging time so the file no longer ships in the wheel at
  all). The companion `settings.json` (project-shared permissions)
  remains a framework file and continues to be updated by
  `c3 update`.
- `tests/test_excludes.py`: regression test asserting the new
  exclusion.

## [0.3.1] - 2026-05-01

### Docs
- Operational rules captured from a 17-tasks / 7-stages C3+PO verification
  run in `c3_pip_test`:
  - `.claude/skills/wave-execution.md`: new **Step 0-pre** that requires a
    clean working tree before invoking PO (PO's auto-merge re-creates
    same-named files in worktrees and conflicts on dirty main — most
    commonly via `.claude/settings.local.json`, which Claude Code auto-edits
    when granting permissions). Adds an explicit **"do not git
    add/commit/push"** rule to case A-2 Agent-tool prompts (a developer
    sub-agent was committing implementation files while leaving Red tests
    and test-reports untracked). Adds an **auto-merge conflict (exit code
    3) recovery** sub-section under case B with a selective-checkout
    procedure that rescues only declared `writes` and discards worktree-
    side edits to surrounding files. Adds a per-wave commit reminder under
    Step 2-F. Notes PO's hardcoded 15-minute per-task timeout
    (`_INTERNAL_TIMEOUT_SEC = 900`, no manifest-level override) so the
    parent Claude can route exit-code-1 timeouts back to planner sizing
    rather than agent debugging.
  - `.claude/agents/planner.md`: documents the `depends_on: []` pitfall
    (`c3 po dry-run` rejects empty arrays — omit the field instead) and
    the `writes` collision detection. Adds a per-task time budget rule
    (≤15 min, matching PO's internal timeout) with a self-check item.
    Adds an **"alternating parallel/serial pattern"** section that
    authorises ordering `depends_on` between stages when the user
    explicitly wants intermediate review/sync points, while preserving
    in-stage parallelism ≥ 2.
  - `.claude/docs/parallel-orchestra-manifest.md`: adds an "alternating
    parallel/serial pattern" section describing the structure with a
    pointer to the planner rule.

No code changes — `c3 update` after `pip install -U claude-code-conductor`
brings these into existing projects.

## [0.3.0] - 2026-04-30

### Changed (breaking)
- `/develop` now auto-detects YAML frontmatter on the latest plan-report and
  switches between two modes:
  - **frontmatter present** → new "C3 main + PO spot" workflow. C3 walks the
    DAG wave-by-wave, asks for user approval before each wave, and dispatches
    each wave to the right runner: solo waves run on the C3 host (Agent-tool
    spawn for `code-reviewer` / `developer` / `tester`, parent-Claude persona
    adoption for `tdd-develop` to avoid the depth-1 nested-spawn limit), and
    multi-task waves are delegated to parallel-orchestra via an ephemeral
    wave-only manifest under `.claude/tmp/`.
  - **no frontmatter** → legacy D-1〜D-5 sequential TDD ceremony, unchanged.
- The previous "PO 全委譲" model (D-0 two-choice prompt) and
  `.claude/skills/parallel-execution.md` are removed. The new flow is
  documented in `.claude/skills/wave-execution.md`.

### Added
- `c3 po waves <plan-report>` — prints the topological wave decomposition of
  a manifest as JSON. Used by `wave-execution.md` to drive the per-wave loop.
- `c3 po run-wave <plan-report> --wave-index N` — generates a wave-only
  ephemeral manifest under `.claude/tmp/po-manifest-wave-{N}-{ts}.md` and
  hands it to parallel-orchestra.
- `c3.po.manifest.compute_waves(frontmatter)` — Kahn's-algorithm topological
  wave decomposition. Detects cycles, unknown dependency ids, and duplicate
  task ids.
- `c3.po.manifest.build_wave_manifest_text(frontmatter, wave_index)` —
  emits a parseable plan-report Markdown for one wave, dropping `depends_on`
  and webhook fields and decorating the manifest name with ` - wave N`.
- `tests/test_po_waves.py` (16 tests) and `tests/test_cli_po.py` (5 tests)
  covering wave decomposition, ephemeral-manifest generation, CLI exit
  codes, and frontmatter round-trip.

### Notes
- The persona-adoption pattern for `tdd-develop` in solo waves is the direct
  consequence of Claude Code's depth-1 nested-spawn limit (a sub-agent
  spawned via the Agent tool cannot itself spawn another sub-agent). For
  agents that internally spawn sub-agents (today: `tdd-develop`), the parent
  Claude reads the agent definition and adopts its persona instead. Other
  agents (`code-reviewer`, `security-reviewer`, `developer`, `tester`) keep
  using the standard Agent-tool spawn path.

## [0.2.4] - 2026-05-01

### Changed
- `planner` agent now produces plan-reports designed for actual parallel
  execution. Previously the agent only knew "emit YAML frontmatter"; without
  rules for how to design the dependency graph it tended to write conservative
  serial chains where every task depended on the previous one, defeating the
  point of parallel-orchestra. Added a "並列実行のための設計指針" section
  with eight concrete rules:
  - depends_on only for true dependencies (not "just to be safe")
  - serialization self-check: chain length ≦ tasks/2
  - reviews go to the end via depends_on covering all dev tasks
  - decompose at file/module boundaries, not function-level or module-level
  - 1 TDD task = test + production + correction loop (do not split)
  - default granularity: file / feature
  - `writes` field is mandatory for collision detection
  - duplicate writes must be merged, sequenced via depends_on, or grouped
- `.claude/docs/parallel-orchestra-manifest.md`: example expanded to three
  dev tasks + a depends-on-all reviewer (showing real parallelism), plus
  inline comments and an "アンチパターン" section that calls out
  serialized chains, splitting TDD into separate tester/developer tasks,
  and empty/duplicate `writes` fields.

## [0.2.3] - 2026-05-01

### Added
- `name` field on every agent definition under `.claude/agents/*.md`. The
  `description` field was already present on all agents, so this fills the
  remaining frontmatter gap. Values match the file stem (e.g. `architect`,
  `tdd-develop`).

## [0.2.2] - 2026-05-01

### Fixed
- `c3 po run` no longer crashes on Windows when parallel-orchestra emits UTF-8
  characters on stderr. The `subprocess.Popen` call previously paired
  `text=True` with no explicit `encoding`, so Python decoded the pipe with the
  platform's locale (cp932 on JP Windows) and raised `UnicodeDecodeError` on
  the first non-ASCII byte. The Popen now passes `encoding="utf-8",
  errors="replace"` so PO's output decodes regardless of locale and a stray
  byte cannot tear down the stream mid-run.

### Added
- `tests/test_po_run.py::test_run_manifest_decodes_stderr_as_utf8` —
  regression test that asserts the Popen kwargs include `encoding="utf-8"`
  and `errors="replace"`.

## [0.2.1] - 2026-05-01

### Fixed
- `c3 init` no longer copies personal/working files when run against the live
  development tree. Two regressions in 0.2.0 caused this:
  1. `templates_dir()` walked up from `__file__` looking for any ancestor with
     `.claude/` + `pyproject.toml`. A wheel install in a venv that happened to
     live inside the C3 source tree (e.g. `claude-code-conductor/.venv/...`)
     therefore resolved to the dirty live `.claude/` instead of the bundled
     `_template/`. The dev fallback is now anchored to `<root>/src/c3/` ancestry
     so site-packages-loaded copies always use `importlib.resources`.
  2. `_copytree` did not apply the same exclusion rules as the build hook,
     so even legitimate editable installs (which intentionally serve the live
     `.claude/`) could leak personal files. `cli_init` and `cli_update` now
     share `c3._excludes` with `hatch_build.py`.

### Added
- `src/c3/_excludes.py` — single source of truth for excluded paths
  (reports/, memory/sessions/, memory/patterns.json, docs/decisions.md, etc.).
- Regression tests:
  - `tests/test_paths.py` — `_resolve_dev_template` rejects site-packages paths.
  - `tests/test_excludes.py` — KEEP_PATTERNS override EXCLUDE_PATTERNS.
  - `tests/test_cli_init.py::test_init_excludes_personal_files` — init does not
    leak personal files even when given a "dirty" template tree.

## [0.2.0] - 2026-05-01

### Added
- PyPI distribution as `claude-code-conductor` (`pip install claude-code-conductor`)
- `c3` command-line interface with subcommands:
  - `c3 init` — scaffold `.claude/` into a project (refuses to overwrite without `--force`)
  - `c3 update` — refresh framework files; preserves user-managed files (reports/, memory/sessions/, founding docs)
  - `c3 list-agents` / `list-skills` / `list-commands` — inspect installed assets
  - `c3 doctor` — diagnose `.claude/`, `settings.json`, claude binary, parallel-orchestra availability
  - `c3 po dry-run <plan-report>` / `c3 po run <plan-report>` — invoke parallel-orchestra via subprocess
- Optional `parallel-orchestra` integration (loose coupling; PO is *not* in dependencies):
  - Runtime detection via `shutil.which` + `importlib.metadata`
  - `.claude/skills/parallel-execution.md` skill orchestrates D-0 → preflight → user approval → run → report
  - `planner` agent now emits required YAML frontmatter on plan-reports per `.claude/docs/parallel-orchestra-manifest.md`
  - `/develop` Phase D adds **D-0: 実行モード選択** (TDD 逐次 vs PO 並列)

### Changed
- Recommended install path is now `pip install claude-code-conductor` + `c3 init`. Manual `cp -r .claude/` still documented as an alternative.
- `worktree_guard.py` docstring: `C3_WORKTREE_GUARD` → `PO_WORKTREE_GUARD` (matches the implementation).

### Internal
- `src/c3/` package layout (hatchling build backend)
- Hatch custom build hook stages distributable subset of `.claude/` into `src/c3/_template/.claude/`
- Test suite under `tests/` (28 tests including loose-coupling guards and an opt-in `parallel-orchestra --dry-run` smoke)

## [0.1.0] - 2026-04-29

### Added
- Initial Claude Code Conductor (C3) framework structure
- Multi-agent orchestration with parent-Claude-persona pattern
- Structured approval flow using `AskUserQuestion` tool
- `/init-session` — session initialization and state restoration
- `/start` — development workflow entry (interviewing → design → planning)
- `/develop` — implementation phase with TDD (tester → developer → tester)
- `/review` — review phase (code-reviewer + security-reviewer)
- `/promote-pattern` — promote candidate patterns to rules/skills
- `/doc` — architecture diagram and documentation generation
- `/mcp` — MCP server management (add / list / remove)
- `/extract-lib` — cross-project common code extraction and library design
- Code review checklist (`rules/code-review-checklist.md`)
- Security review checklist (`rules/security-review-checklist.md`)
- Hooks: `pre_tool.py`, `stop.py`, `log_agent.py`, `validate_skill_change.py`, `pre_compact.py`, `statusline.py`
- Session memory system with pattern trust scoring

### Fixed
- Force UTF-8 encoding on stdout/stderr for all hooks (Windows compatibility)
- Block `cd` commands in `pre_tool` hook to prevent CWD drift that breaks hook resolution
- Exclude all report/tmp file types from git tracking
