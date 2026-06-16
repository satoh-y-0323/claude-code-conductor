# C3 アーキテクチャ

Claude Code Conductor（C3）が **何でできていて・実行時にどう動き・どうビルドされ配布されるか** を 1 枚で示す全体地図。

> **このドキュメントの立ち位置**: ここは「地図」であり、各領域の詳細は専門ドキュメントに委譲する（重複を作らない）。
> 詳細の所在は [§8 正典ドキュメントの地図](#8-正典ドキュメントの地図) を参照。
>
> **対象読者**: C3 本体をフォーク・拡張する開発者、コントリビューター、未来の自分。
> C3 の *利用先* ユーザーには本ファイルは不要（`c3 init` では配布されない。[§5](#5-ビルド配布パイプライン) 参照）。

---

## 1. C3 の二層構造

C3 は性質の異なる **2 つの層** からなる。この区別が全体理解の起点になる。

```
┌─────────────────────────────────────────────────────────────┐
│ 層 A: c3 Python パッケージ（PyPI: claude-code-conductor）       │
│   ターミナルで動く CLI / インストーラ / 知能基盤              │
│   実体: src/c3/*.py                                          │
│   役割: .claude/ を利用先へ配置・更新し、recall/tier 等の     │
│         「Claude Code の外で走る計算」を担う                   │
└─────────────────────────────────────────────────────────────┘
                          │ c3 init / c3 update で配置
                          ▼
┌─────────────────────────────────────────────────────────────┐
│ 層 B: .claude/ フレームワーク本体                             │
│   Claude Code の *内側* で動くオーケストレーション定義         │
│   実体: .claude/{agents,skills,rules,hooks,memory,...}        │
│   役割: 親 Claude が専門エージェントを指揮する仕組み           │
└─────────────────────────────────────────────────────────────┘
```

| | 層 A（`c3` パッケージ） | 層 B（`.claude/` フレームワーク） |
|---|---|---|
| 実行環境 | ターミナル（Python プロセス） | Claude Code セッション内 |
| 言語 | Python | Markdown（+ hook は Python） |
| 配布 | `pip install claude-code-conductor` | `c3 init` が層 A から展開 |
| エントリ | `c3 = "c3.cli:main"`（pyproject.toml） | スラッシュコマンド・Agent ツール |
| 依存 | PyYAML / duckdb / numpy / fastembed | Claude Code ランタイム |

**配布元リポジトリ内の物理対応**:

```
claude-code-conductor/
├── src/c3/              ← 層 A の実体（Python パッケージ）
│   └── _template/.claude/  ← 層 B の「配布スナップショット」（ビルド時自動生成・直接編集禁止）
├── .claude/             ← 層 B の canonical source（開発時はここを編集する）
├── hatch_build.py       ← .claude/ → _template/ への staging（§5）
└── pyproject.toml       ← 層 A のパッケージ定義
```

> 層 B を変更したいときは **`.claude/` を編集する**。`src/c3/_template/` はビルド時に再生成される配布物実体なので直接編集しても消える（`.dev/hooks/_template_guard.py` がブロックする）。

---

## 2. ランタイム・オーケストレーションモデル

C3 の中心思想は「**親 Claude（オーケストレーター）が複数の専門エージェントを指揮する**」。

```
ユーザー
  │ /start /develop /review-phase /doc ...（= skills）
  ▼
親 Claude（オーケストレーター）
  ├─ ペルソナ採用で動く（対話が必要）: interviewer / architect / planner
  └─ Agent ツールで起動する（実装・検証）: developer / tester /
        code-reviewer / security-reviewer / doc-writer / systematic-debugger
  │
  ├─ 受け渡し: reports/*.md（requirements / architecture / plan / *-review / test）
  ├─ 記憶:     memory/（sessions/*.tmp・patterns.json）/ agent-memory/
  └─ 制約・知識: rules/（常時注入）・CLAUDE.md（行動規範）
```

### 構成要素と責務

| 要素 | 置き場所 | 役割 | 詳細 |
|---|---|---|---|
| **agents** | `.claude/agents/*.md` | 「誰か」（ペルソナ・スコープ・出力契約）のみ定義 | [taxonomy.md](.claude/docs/taxonomy.md) |
| **skills** | `.claude/skills/<name>/SKILL.md` | 「何をするか」（手順・フェーズ構成）。スラッシュコマンドの実体 | taxonomy.md |
| **rules** | `.claude/rules/*.md` | 「これを知っておけ」（知識・制約）。全文常時注入 | taxonomy.md |
| **hooks** | `.claude/hooks/*.py` | ライフサイクルイベントで自動実行 | [§3](#3-hook-ライフサイクル) |
| **memory** | `.claude/memory/` | セッション間の記憶。hook が骨格、LLM が中身を更新 | taxonomy.md |
| **reports** | `.claude/reports/` | エージェント間の受け渡し成果物 | — |

### 起動方式の使い分け

- **親 Claude がペルソナ採用**（interviewer / architect / planner）: ユーザーとの対話が必要なため、サブエージェント化せず親が役を演じる。対話 skill は frontmatter に `allowed-tools` を**付けない**（絞ると `AskUserQuestion` が落ちる）。
- **Agent ツールで起動**（developer / tester / reviewer 系 / doc-writer / systematic-debugger）: 独立スコープで動かしコンテキスト汚染を防ぐ。
- **`wt_*` 派生**（`wt_developer` / `wt_tester` / `wt_systematic-debugger`）: `parallel-agents` skill が `isolation: "worktree"` で並列起動する専用版。`permissionMode: bypassPermissions` とレポートの task_id 命名だけが通常版と異なる。

### 開発ワークフロー（dev-workflow skill）

```
A ヒアリング → B 設計 → C 計画 → D 実装(TDD) → E レビュー
（requirements）（architecture）（plan）（tester⇄developer）（code/security-reviewer）
   各フェーズ間でユーザー承認。E で指摘が出たら C へ戻る（内部遷移）
```

`/start`（入口）→ `develop`（D 以降のガード／legacy 逐次 TDD と `parallel-agents` を分岐）→ `parallel-agents`（plan-report の YAML フロントマターを wave 分解し並列起動）という連携で動く。

---

## 3. hook ライフサイクル

層 B の自動化は Claude Code のライフサイクル hook で実現する。**以下は `.claude/settings.json` に登録された実際のマッピング**（配布される hook）。

| イベント | matcher | hook | 役割 |
|---|---|---|---|
| `SessionStart` | `compact` | `restore_session.py` | compact 後のセッション復元 |
| `SessionStart` | （全て） | `session_start.py` | file-history クリア・sandbox 有効化・`c3.db` 初期化 |
| `UserPromptSubmit` | （全て） | `select_tier.py` | 複雑度判定 → Tier ルーティング推奨（[§4](#4-知能基盤)） |
| `UserPromptSubmit` | （全て） | `recall_inject.py` | 過去類似情報を意味検索しコンテキスト注入（[§4](#4-知能基盤)） |
| `PreToolUse` | `Bash` | `pre_tool.py` | 破壊的コマンド（`rm -rf` 等）ブロック・secret スキャン |
| `PreToolUse` | `Write` / `Edit` | `worktree_guard.py` | worktree 外への書き込み禁止（`PO_WORKTREE_GUARD=1` 設定時のみ作動。parallel-agents が並列実行時に有効化） |
| `PreToolUse` | `Agent` | `check_agent_invocation.py` | エージェント起動の検査 |
| `PostToolUse` | `Write` / `Edit` | `post_tool.py` | skills 変更通知・品質パターン（TODO 等）スキャン |
| `PostToolUse` | `Write` / `Edit` | `planner_check.py` | plan-report の YAML/タイムスタンプ/reviewer 規約検査 |
| `PermissionRequest` | （全て） | `permission_handler.py` | `permission_rules.json` の `auto_allow` で自動承認（[config-policy §2 レイヤー B](.claude/docs/config-policy.md)） |
| `PreCompact` | （全て） | `pre_compact.py` | compact 前にチェックポイントマーカー注入 |
| `Stop` | （全て） | `session_stop.py` | セッション終了処理のオーケストレータ |

### hook ワーカー（イベント登録なし・内部呼び出し）

`session_stop.py` は 3 段で他モジュールを呼ぶ:

- Phase 1 `stop.py` — session ファイル生成・最終応答記録・パターン信用度再計算
- Phase 2 `consolidate_memory.py` — セッション要約・昇格候補ログ・アーカイブ
- Phase 3 `usage_ingester.py` + `db.sync_tier_bandit_cost()` — セッションログからコスト集計（worktree セッションでは起動しない。§4-1 参照）
- 共通: `session_utils.py` / `_hook_utils.py`（複数 hook が共有するヘルパー）

### 設計原則

- **非ブロッキング**: 観測系 hook は失敗しても `exit 0` でセッションを止めない（DB 不在・index 不在でも本体は動く）。
- **`exit 2` は自己修正用**: PostToolUse の `exit 2` は LLM の system reminder に block error として surface される（「自動修正させたい違反」は exit 2、「ログだけ」は exit 0 + stderr で書き分ける）。例は配布元専用 `.dev/hooks/_planner_check.py` の R3。なお PreToolUse の `exit 2`（`check_agent_invocation.py` の R5 等）は動作をブロックするが LLM コンテキストへは注入されず、設計が異なる。
- **配布元専用 hook** は `.dev/hooks/` に分離（`settings.local.json` で登録・配布されない）: `_template_guard.py` / `_sync_check.py` / `_pip_reinstall_reminder.py` / `_planner_check.py`。

---

## 4. 知能基盤（層 A が担う計算）

「Claude Code の外で走らせた方がよい計算」を層 A が担う。2 つの独立サブシステムがある。

### 4-1. `c3.db`（SQLite + DuckDB ハイブリッド）

書き込みは標準 `sqlite3`（WAL モード）、読み・分析は DuckDB の `sqlite_scanner` で ATTACH する想定。スキーマは `src/c3/migrations/*.sql` を `migrate.py` が冪等適用する。

| テーブル | 用途 | 書き込み元 |
|---|---|---|
| `schema_migrations` | マイグレーション適用記録 | `migrate.py` |
| `review_decisions` | レビュー指摘への過去判断（対応/許容） | `record_review_decision.py`（dev-workflow/scripts） |
| `tier_bandit` | Tier ルーティングの Thompson Sampling 統計 | `select_tier.py` / `record_tier_outcome.py` |
| `tier_recent_outcomes` | 直近 outcome 履歴（escalation 判定用） | 同上 |
| `agent_runs` / `agent_cost_runs` | エージェント実行・コスト集計 | `usage_ingester.py` |
| `usage_ingest_state` | セッションログ取り込みの offset 管理 | `usage_ingester.py` |

- **review-hint ループ**: code-reviewer が `[CR-XX-NNN]` ID を付与 → `review_hint_inject.py` が過去判断を次回レビューに注入し、一貫性を高める。
- **tier-routing**: `select_tier.py`（`UserPromptSubmit` hook）がプロンプト複雑度を判定し Tier（haiku/sonnet/opus）を推奨。`LEARNING_THRESHOLD`（=30）試行までは uniform、以降は cost-weighted Thompson Sampling。環境変数 `C3_TIER_COST_LAMBDA` / `C3_TIER_EPSILON` / `C3_ESCALATION_THRESHOLD` で調整（解決は `db.py` の `resolve_*` → 内部ヘルパー `_resolve_float_env` が SSOT）。CLI は `c3 tier stats`。
- **cost tracking**: `usage_ingester.py` が `<session>.jsonl`（mainline）と `<session>/subagents/agent-*.jsonl`（subagent）を読み、`pricing.py` の単価表で USD 集計する。

### 4-2. `recall`（意味検索）

過去のセッション・レポート・パターンを意味検索して現タスクに「記憶補完」する。

```
recall_chunker.py  →  embedding.py            →  recall_index.py
（sessions/reports/    （fastembed: paraphrase-     （numpy cosine ブルートフォース・
  patterns をチャンク化）  multilingual-MiniLM-L12-v2    .claude/state/ に保存）
                          384次元・約220MB・多言語）
```

- CLI: `c3 recall search "<query>"` / `c3 recall rebuild` / `c3 recall stats`。
- 自動注入: `recall_inject.py`（`UserPromptSubmit` hook）が score 0.4 以上の上位 3 件を裏で注入（α 案・最終採否は LLM 判断）。index が古い（ソース mtime > index mtime）と検出したら rebuild 確認を促す。
- `C3_RECALL_HOOK_DISABLE=1` で停止可。初回 rebuild はモデル取得のため `FASTEMBED_CACHE_PATH` でミラー指定可。

---

## 5. ビルド・配布パイプライン

層 B（`.claude/`）を層 A（wheel）に焼き込み、利用先へ届けるまで。

### ビルド時の staging

`hatch_build.py` の `StageTemplateHook.initialize()` が、wheel/sdist ビルドの最初に `.claude/` を `src/c3/_template/.claude/` へ **フィルタコピー**する。pyproject の `force-include` がこの staging ツリーを wheel に同梱する。

```
.claude/  ──（_should_skip でフィルタ）──▶  src/c3/_template/.claude/  ──▶  wheel
            EXCLUDE_PATTERNS で除外
            KEEP_PATTERNS が優先で救済（.gitkeep / deletions.txt / breaking-changes.txt）
```

### 3 ファイル同期ルール（最重要の保守制約）

配布除外パターンは **3 ファイルに重複定義**されており、変更時は必ず同期する:

| ファイル | 役割 |
|---|---|
| `.gitignore` | git 追跡からの除外 |
| `src/c3/_excludes.py` | `c3 init` / `c3 update` の除外判断（`should_skip`） |
| `hatch_build.py` | wheel ビルド時の除外判断（`_excludes.py` の重複） |

> `hatch_build.py` が重複を持つのは、build hook が **package import 前**に走り `_excludes.py` を import できないため。`.dev/hooks/_sync_check.py`（PostToolUse）が同期漏れを警告する。過去 defect: v1.1.0 / v2.14.1 で wheel 混入。

### 配置と更新

- `c3 init` — `_template/.claude/` を利用先へ展開（個人ファイルは `should_skip` で除外）。
- `c3 update` — 差分更新（`reports/`・`memory/sessions/` 等は保持。`rules/promoted/` は触らない）。
- `deletions.txt` — `c3 update` が読み、利用先から旧ファイルを削除（`c3 update` は削除を自動検出しないため、リリースで消したファイルはここに明記）。
- `breaking-changes.txt` — `c3 update` が利用先の `state/c3_version.txt` と diff し破壊的変更を提示。

### リリース前チェック（配布元 `/CLAUDE.md` に規定）

`python -m build --wheel` で wheel 実体検証 → `scripts/extract_breaking_changes.py --check` → `scripts/check_deletions.py --check`。詳細は配布元 `/CLAUDE.md` と [config-policy.md](.claude/docs/config-policy.md) §5–6。

---

## 6. クロスプラットフォーム adapter

`.claude/` を canonical source としたまま、Codex / Cursor / OpenCode 向けの派生生成物を作る。

| プラットフォーム | 生成物 | 生成コマンド |
|---|---|---|
| Claude Code | `.claude/`（primary） | — |
| Codex | `/AGENTS.md` / `.codex/` / `.agents/skills/` | `c3 init --platform codex` |
| Cursor | `.cursor/rules/c3-core.mdc` / `.cursor/mcp.json` | `c3 init --platform cursor` |
| OpenCode | `/AGENTS.md` / `.opencode/agents/` | `c3 init --platform opencode` |

- 生成ロジック: `adapters.py` + `platforms.py`。adapter 生成物は **派生物**（直接編集すると再生成で上書き・配布元 `.gitignore` で除外）。
- `AskUserQuestion` 互換: MCP tool `c3_ask_user_question`（`mcp_server.py`）、非対応時の fallback は `c3 ask`（`cli_ask.py`）。
- 詳細は `.claude/docs/platform-adapters.md`。

---

## 7. テスト・CI

- `tests/` に約 73 ファイル・約 1300 テスト関数（unit 中心 + hook 挙動の統合テスト）。`pyproject.toml` の `addopts = "-m 'not slow'"` で重い embedding テストを既定除外。
- CI（`.github/workflows/`）: `test.yml`（Python 3.10/3.11/3.12）、`publish.yml`（PyPI 公開）、Pages（mkdocs ドキュメント）。
- **テストの穴（既知）**: hook の実ファイル e2e（多くは importlib ロードで実 subprocess 実行と乖離し得る）、worktree 並列実行の競合テストが未整備。

---

## 8. 正典ドキュメントの地図

C3 のドキュメントは領域ごとに権威が分かれている。**重複させず、各領域の唯一の出典を以下に固定する**。

| 領域 | 正典ドキュメント | 扱う内容 |
|---|---|---|
| **全体像（本書）** | `/ARCHITECTURE.md` | 二層構造・実行時の動き・ビルド/配布の地図 |
| フォルダの意味・配置判断 | `.claude/docs/taxonomy.md` | agents/skills/rules/hooks/... の役割と命名 |
| 配布判断・設定優先順位 | `.claude/docs/config-policy.md` | 配布マトリクス・3 ファイル同期・落とし穴 |
| LLM 行動規範・承認フロー | `.claude/CLAUDE.md` | エージェントの振る舞いルール |
| 配布元開発者向け | `/CLAUDE.md`（gitignore・配布されない） | template 編集禁止・wheel 検証・リリース手順 |
| 設定キー詳細 | `.claude/docs/settings.json.md` | settings.json の各キー仕様 |
| プラットフォーム adapter | `.claude/docs/platform-adapters.md` | Codex / Cursor 生成物 |
| 並列実行セットアップ | `.claude/docs/parallel-agents-setup.md` | worktree.baseRef 等の個人設定 |

> 迷ったら本書（地図）から各正典へ辿る。新しい設計判断は `.claude/docs/decisions.md`（配布元のみ・D-NNN 採番）に記録する。
