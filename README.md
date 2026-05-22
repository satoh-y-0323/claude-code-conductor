# Claude Code Conductor (C3)

[![PyPI version](https://img.shields.io/pypi/v/claude-code-conductor.svg)](https://pypi.org/project/claude-code-conductor/)
[![Python versions](https://img.shields.io/pypi/pyversions/claude-code-conductor.svg)](https://pypi.org/project/claude-code-conductor/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/satoh-y-0323/claude-code-conductor/blob/main/LICENSE)
[![Publish](https://github.com/satoh-y-0323/claude-code-conductor/actions/workflows/publish.yml/badge.svg)](https://github.com/satoh-y-0323/claude-code-conductor/actions/workflows/publish.yml)

複数エージェントのオーケストレーションを中心に据えた Claude Code フレームワーク。

📖 **公式ドキュメント**: [https://satoh-y-0323.github.io/claude-code-conductor/](https://satoh-y-0323.github.io/claude-code-conductor/)

---

## コンセプト

Claude Code Conductor（C3）は「親 Claude が複数の専門エージェントを指揮する」という設計思想で作られています。

```
ユーザー
    ↓ /start, /develop, /review-phase, /doc, /mcp-config, /extract-lib ...
親 Claude（オーケストレーター）
    ├─ interviewer       ← ヒアリング
    ├─ architect         ← 設計
    ├─ planner           ← タスク計画
    ├─ developer         ← 実装
    ├─ tester            ← テスト
    ├─ code-reviewer     ← コードレビュー
    ├─ security-reviewer ← セキュリティレビュー
    └─ doc-writer        ← ドキュメント生成
```

各エージェントは明確なスコープを持ち、担当外の作業は行いません。
フェーズ間の遷移・承認フロー・知識の蓄積はすべてフレームワークが管理します。

---

## なぜ CLAUDE.md だけでは足りないのか

Claude Code には標準で `CLAUDE.md` にプロジェクト指示を書く仕組みがあります。小規模・単発の作業ならそれで十分です。しかし業務開発でこう感じたことはないでしょうか。

**CLAUDE.md 一枚運用の限界:**

| 問題 | 何が起きるか |
|---|---|
| 指示が1ファイルに集中する | 長くなるほど Claude が全体を把握できなくなり、指示が無視されやすくなる |
| 「誰が何をするか」が分離されていない | ヒアリング・設計・実装・レビューを1つの Claude が兼任するため、コンテキストが汚染されて品質が下がる |
| ワークフローが定義されていない | 「いつ次のフェーズへ進むか」を Claude が自己判断する。承認なしに実装が始まることがある |
| セッションをまたいだ記憶がない | 前回うまくいったアプローチ・失敗した理由・プロジェクト固有のパターンが毎回リセットされる |

**C3 が解決すること:**

```
CLAUDE.md 一枚                     C3
─────────────────────────────────────────────────────
全指示が1ファイル              → 役割ごとにファイルを分離
Claude がすべてを兼任          → 専門エージェントが分業
フェーズ遷移は Claude が判断   → ユーザーが承認して進む
知識はセッションごとにリセット → patterns.json に蓄積・昇格
```

業務開発で「繰り返し使う」「チームで使う」「品質を担保する」を実現するには、この構造が必要です。

---

## ファイル構成と意味

C3 では「どのファイルをどこに置くべきか」の定義が設計の核心です。

```
.claude/
├── agents/      エージェント定義（役割・スコープ・連携先）
├── skills/      スキル定義（/xxx コマンドの実体・オーケストレーション手順）
├── rules/       エージェントに注入される背景知識・制約
├── hooks/       イベントドリブンで自動実行される Python スクリプト
├── docs/        人間向けリファレンス（エージェントは読まない）
└── memory/      セッション間の記憶（patterns.json・session ファイル）
```

各スキルは `skills/{name}/SKILL.md` の形式で配置されます（Claude Code 2026 skills 標準）。

### 配置の判断基準

| 書きたい内容 | 置き場所 |
|---|---|
| ユーザーが `/xxx` で呼び出す手順・フロー | `skills/` |
| 単一エージェントの役割・作業手順 | `agents/` |
| 知識・制約（「これを知っておけ」） | `rules/` |
| 自動実行スクリプト | `hooks/` |
| 人間向けドキュメント | `docs/` |

> **skills/ が C3 の核心**。Claude Code のスラッシュコマンドはすべてスキルとして実装されており、`skills/{name}/SKILL.md` に YAML フロントマター付きで定義されています。

---

## ワークフロー

開発は `skills/dev-workflow/SKILL.md` で定義された5フェーズで進みます。

```
フェーズ A: ヒアリング    requirements-report を生成
    ↓ 承認
フェーズ B: 設計          architecture-report を生成
    ↓ 承認
フェーズ C: 計画          plan-report を生成
    ↓ 承認（自動遷移）
フェーズ D: TDD           tester → developer → tester のサイクル
    ↓ 承認（自動遷移）
フェーズ E: レビュー      code-reviewer → security-reviewer
    ↓ 指摘あり
フェーズ C へ戻る（内部遷移・Step 0 なし）
```

各フェーズの移行時にユーザーが承認・否認・修正を選択します。
フェーズ D・E への遷移は承認後に自動で行われます。

---

## スキル一覧

C3 のスラッシュコマンドはすべてスキル（`skills/{name}/SKILL.md`）として実装されています。

### 開発ワークフロー

| スキル | 役割 |
|---|---|
| `/init-session` | セッション初期化・前回状態の復元 |
| `/setup` | コーディング規約の設定（coding-standards・project-conventions 生成） |
| `/start` | 開発ワークフローの入口（ヒアリング/設計/計画/実装 から選択） |
| `/develop` | TDD フェーズから直接開始 |
| `/review-phase` | レビューフェーズから直接開始（code-reviewer + security-reviewer） |
| `/promote-pattern` | 蓄積されたパターンを rules/ または skills/ に昇格 |

### ユーティリティ

| スキル | 役割 |
|---|---|
| `/doc` | ドキュメントをヒアリングして生成（mermaid 図・README・API 仕様書など） |
| `/mcp-config` | MCP サーバーの追加・一覧・削除（プロジェクトスコープ） |
| `/extract-lib` | 複数プロジェクトのコードを横断解析し、共通処理をライブラリとして設計・生成 |
| `/recall` | 過去のセッション・レポート・パターンから類似情報を意味検索（HNSW + 多言語 embedding） |

### ターミナルで使う `c3` CLI（PyPI インストール時）

| コマンド | 役割 |
|---|---|
| `c3 init` | 利用先プロジェクトに `.claude/` を展開する |
| `c3 init --platform codex|cursor|all` | `.claude/` を canonical source にしたまま Codex/Cursor adapter を追加 |
| `c3 update` | `.claude/` をパッケージ最新版へ更新する（個人ファイルはスキップ） |
| `c3 list-agents` / `list-skills` | 設置済みアセットを一覧表示 |
| `c3 doctor` | 環境診断（`.claude/`・settings.json・claude バイナリ・adapter 生成物） |
| `c3 ask` | Claude Code 以外で `AskUserQuestion` 互換の単一選択・複数選択を実行 |
| `c3 plan validate <plan-report>` | plan-report の YAML フロントマターと agent 存在を検証 |
| `c3 plan waves <plan-report>` | plan-report の wave 分解結果を JSON で出力 |
| `c3 recall search "<query>"` または `c3 recall "<query>"` | `.claude/memory/sessions/` 等から類似チャンクを意味検索 |
| `c3 recall rebuild [--force]` | HNSW インデックスを再構築（初回は fastembed が ~220MB のモデルを取得） |
| `c3 recall stats` | チャンク数・モデル名・最終 rebuild 日時を表示 |

### 基本的な使い方

```
/init-session          # セッション開始時に必ず実行
/setup                 # 初回のみ：プロジェクト規約を設定
/start                 # 開発開始
```

`/start` 実行後は、各フェーズの承認を進めるだけで最後まで自動的に流れます。

> **公式コマンドとの名前衝突について:** C3 のスキル名は Claude Code 公式コマンドと重複しないよう設計しています。`/review-phase`（≠ 公式 `/code-review` / `/review`）、`/mcp-config`（≠ 公式 `/mcp`）はこの方針に基づく命名です。公式コマンドが追加された場合は衝突回避のために本フレームワークの skill 名を変更することがあります（v2.15.1 で `/code-review` → `/review-phase` に変更しました）。

---

## エージェント一覧

| エージェント | model | 主な出力 | 起動方式 |
|---|---|---|---|
| interviewer | sonnet | requirements-report | 親 Claude がペルソナ採用 |
| architect | opus | architecture-report | 親 Claude がペルソナ採用 |
| planner | opus | plan-report | 親 Claude がペルソナ採用 |
| developer | sonnet | 実装コード | Agent ツールで起動 |
| tester | sonnet | テスト・test-report | Agent ツールで起動 |
| code-reviewer | sonnet | code-review-report | Agent ツールで起動 |
| security-reviewer | sonnet | security-review-report | Agent ツールで起動 |
| doc-writer | sonnet | ドキュメント各種 | Agent ツールで起動 |
| systematic-debugger | sonnet | debug-analysis-report | Agent ツールで起動（行き詰まり時）|

インタラクティブな対話が必要なエージェント（interviewer・architect・planner）は親 Claude がペルソナを採用して動作します。実装・検証系エージェントはサブエージェントとして起動されます。

---

## 既存プロジェクトへの導入

C3 の `.claude/` はプロジェクトコードに一切触れません。既存のコードベースにそのまま追加できます。

### 前提条件

- [Claude Code](https://claude.ai/code) がインストール済みでログインしていること
- Python 3.10 以上がインストール済みであること

### 手順（推奨: PyPI から）

**1. C3 をインストールする**

```bash
pip install claude-code-conductor
```

**2. プロジェクトに `.claude/` を展開する**

```bash
cd /path/to/your-project
c3 init
```

`c3 init` がパッケージに同梱された `.claude/` テンプレートをカレントディレクトリへコピーします。後日テンプレート側を更新したい場合は `c3 update` で差分のみ反映できます（`reports/` や `memory/sessions/` 等の個人ファイルは保持されます）。

Codex/Cursor でも同じ C3 workflow を使う場合は、`.claude/` を動かさず adapter を追加します。

```bash
c3 init --platform codex   # AGENTS.md / .agents/skills / .codex を生成
c3 init --platform cursor  # .cursor/rules / .cursor/mcp.json を生成
```

Codex/Cursor adapter は `.claude/skills` と `.claude/agents` を参照元にします。`AskUserQuestion` は MCP tool `c3_ask_user_question`、または fallback の `c3 ask` で単一選択・複数選択を維持します。Claude Code の `Agent` / `Skill` tool 前提は、Codex では `.codex/agents/` と `.agents/skills/`、Cursor では `.cursor/rules/c3-core.mdc` で読み替えます。

**3. プロジェクトを Claude Code で開き、初期設定を行う**

```
/init-session    # セッション初期化
/setup           # プロジェクトの技術スタック・規約を設定（初回のみ）
```

`/setup` を実行すると、使用言語・フレームワーク・命名規則などをヒアリングして `.claude/rules/` に規約ファイルが生成されます。これ以降は `/start` で開発を始められます。

### 手順（PyPI を使わない場合）

```bash
git clone https://github.com/satoh-y-0323/claude-code-conductor.git
cp -r claude-code-conductor/.claude /path/to/your-project/
```

Windows（PowerShell）:
```powershell
Copy-Item -Recurse claude-code-conductor\.claude your-project\
```

### 既存コードへの影響

| 変更される場所 | 内容 |
|---|---|
| `.claude/` ディレクトリ（追加） | C3 のフレームワーク一式 |
| `.gitignore`（追記推奨） | `reports/`・`memory/sessions/` 等の個人作業ファイルを除外 |

プロジェクトの `src/` や既存コードには一切触れません。

---

## カスタマイズ方法

### コーディング規約を追加する

```
/setup
```

を実行すると技術スタック・規約をヒアリングし、以下を自動生成します:
- `.claude/rules/coding-standards.md`
- `.claude/rules/project-conventions.md`

### プロジェクト固有の指示を追加する

| 内容 | 置き場所 |
|---|---|
| プロジェクトの概要・アーキテクチャ背景 | プロジェクトルートの `CLAUDE.md` |
| コーディング規約・命名規則 | `/setup` → `.claude/rules/` |
| C3 フレームワーク設定 | `.claude/CLAUDE.md`（変更しない） |

### エージェントを追加・カスタマイズする

`.claude/agents/` に新しいエージェント定義ファイルを追加します。
フォーマットは既存エージェントファイルの構成（Core Mandate / Key Scope / Workflow / Related Agents）に合わせてください。

---

## パターン昇格システム

開発中に発見した「うまくいったアプローチ」や「再発防止ルール」は session ファイルの `patterns` に記録されます。

- セッションをまたいで観測されるたびに **信用度（trust_score）** が上がります
- 登録から3日以上・信用度 0.8 以上で **昇格候補** になります
- `/promote-pattern` を実行すると `rules/promoted/` または `skills/promoted-YYYYMMDD-{id}/` にルールとして昇格します
- 昇格したルールは以降の全セッションで自動的にエージェントへ注入されます

昇格スキルは `skills/promoted-YYYYMMDD-{id}/SKILL.md` の形式で配置され、Claude Code が自動的に検出します。

---

## レポートの管理

各フェーズが生成するレポートは `.claude/reports/` に保存されます。

```
.claude/reports/
  requirements-report-YYYYMMDD-HHMMSS.md
  architecture-report-YYYYMMDD-HHMMSS.md
  plan-report-YYYYMMDD-HHMMSS.md
  code-review-report-YYYYMMDD-HHMMSS.md
  security-review-report-YYYYMMDD-HHMMSS.md
  archive/   ← /start 実行時に古いレポートをここに移動
```

`/start` の冒頭で既存レポートのアーカイブ確認が入ります（全移動・フェーズ選択・引き継ぎ）。

---

## 並列実行 (parallel-agents skill)

計画フェーズで生成した plan-report を YAML フロントマター付きマニフェストとして読み込み、独立タスクを親 Claude の Agent ツール並列起動 + 公式 `isolation: "worktree"` で並列実行できます。

> **アーキテクチャ移行履歴:** v1.x までは外部プロセス制御の **PO (Parallel Orchestra)** が並列実行を担っていましたが、v2.0.0 で完全撤去されました。現在は Claude Code 公式の subagent 並列起動機能に統一されており、追加プロセス・追加デーモン・追加スキーマは不要です。

要件は Python ≥ 3.10、PATH に `claude` バイナリ、PyYAML（C3 の依存として自動インストール）。

**使い方:**

1. `/start` で要件→設計→計画フェーズを完走させる（planner が plan-report の先頭に YAML フロントマターを自動付与します）
2. `/develop` を起動 → **D-0** で plan-report のフロントマターを自動検出し `parallel-agents` skill へ切り替わる
3. C3 が `c3 plan validate` でマニフェスト妥当性を検証、wave ごとにユーザー承認を取りながら Agent ツールで並列実行します

TDD を伴う機能実装は、planner が「test- → impl- → confirm-」の 3 タスクペアに分解し、各 wave 内で独立機能間で並列起動されます。並列実行時は worktree 専用の `wt_tester` / `wt_developer` / `wt_systematic-debugger` agent が使用され、レポートは `task_id` ベースのファイル名で出力されます。

---

## セッション管理

C3 はセッションをまたいで作業状態を記憶します。

- **毎回のセッション開始時:** `/init-session` を実行する
- **タスク完了のたびに:** session ファイルの残タスクを更新する（まとめて最後に書かない）
- **セッション終了時:** `stop.py` フックが自動的に以下を実行する
  - session ファイルの記録時刻を更新
  - Claude の最終応答（`last_assistant_message`）を事実ログに自動記録（次セッションで「前回何をしたか」が分かる）
  - パターン信用度を再計算

---

## 第三者ライブラリのライセンス

`c3 recall` 機能（v2.10.0〜）で利用する以下の依存は `LICENSES/` ディレクトリに出典を同梱しています:

| 依存 | ライセンス | 用途 |
|---|---|---|
| [chroma-hnswlib](https://github.com/chroma-core/hnswlib) | Apache-2.0 | HNSW 近傍検索インデックス |
| [fastembed](https://github.com/qdrant/fastembed) | Apache-2.0 | embedding 生成ランタイム（ONNX ベース） |
| [onnxruntime](https://github.com/microsoft/onnxruntime) | MIT | fastembed の推論バックエンド |
| [sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2) | Apache-2.0 | 多言語 embedding モデル（384 次元・約 220MB・約 50 言語対応） |

C3 本体は MIT のままです。業務アプリへの再配布時はこれらの LICENSE / NOTICE を同梱してください。

### `c3 recall` 自動コンテキスト注入（α 案）

v2.10.0 から `UserPromptSubmit` フック `.claude/hooks/recall_inject.py` が、毎プロンプトで `c3 recall search` を裏で実行し、類似情報の上位 3 件を親 Claude のコンテキストに追加します。前置きで「現タスクと無関係なら無視してください」と明示し、最終的な採否は LLM 側の判断に委ねる設計です（α 案）。

- 短い prompt / スラッシュコマンド / `@mention` / index 未構築の場合は silent no-op
- 環境変数 `C3_RECALL_HOOK_DISABLE=1` で完全停止可
- 注入対象は score 0.4 以上の上位 3 件のみ（CLI の既定 `--min-score 0.3` より厳格に絞る）
- インデックスが古い（ソース mtime > index mtime）と判定したら、注入テキストの冒頭に「AskUserQuestion で `今すぐ rebuild / 後で / 無視` を確認してください」ガイダンスを付加。親 Claude が読んで AskUserQuestion を発火し、ユーザー選択に応じて Bash で `c3 recall rebuild` を実行する

オフライン環境やプロキシ越しに利用する場合、初回 `c3 recall rebuild` 時のモデルダウンロードは `FASTEMBED_CACHE_PATH` 環境変数を社内ミラー / NAS パスに向けることで回避できます。

> **fastembed モデルダウンロードの整合性 (SR-L-3)**
> fastembed は HuggingFace Hub からモデルを取得する際に blob ID とサイズを検証しますが、SHA-256 チェックサムの独立検証は行いません。セキュリティ要件の高い環境では、`FASTEMBED_CACHE_PATH` を社内ミラーに向けたうえで、ダウンロード後に公式リポジトリ (https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2) 掲載の SHA-256 と手動で照合することを推奨します。

> **推移的依存 urllib3 の脆弱性 (SR-H-1)**
> `urllib3 <= 2.6.3` に既知脆弱性が報告されています。`fastembed → huggingface-hub → urllib3` 経由で間接的に利用されます。`pip install -U urllib3` で 2.7.0 以上にアップデートすることを推奨します。
