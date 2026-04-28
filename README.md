# Claude Code Conductor (C3)

複数エージェントのオーケストレーションを中心に据えた Claude Code フレームワーク。

---

## コンセプト

Claude Code Conductor（C3）は「親 Claude が複数の専門エージェントを指揮する」という設計思想で作られています。

```
ユーザー
    ↓ /start, /develop, /review, /doc, /mcp, /extract-lib ...
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
├── commands/    ユーザーが呼ぶエントリーポイント（/start 等）
├── skills/      複数エージェントをまたぐオーケストレーション手順
├── rules/       エージェントに注入される背景知識・制約
├── hooks/       イベントドリブンで自動実行される Python スクリプト
├── docs/        人間向けリファレンス（エージェントは読まない）
└── memory/      セッション間の記憶（patterns.json・session ファイル）
```

### 配置の判断基準

| 書きたい内容 | 置き場所 |
|---|---|
| 複数エージェントをまたぐ手順・フロー | `skills/` |
| 単一エージェントの役割・作業手順 | `agents/` |
| ユーザーが `/xxx` で呼び出す入口 | `commands/` |
| 知識・制約（「これを知っておけ」） | `rules/` |
| 自動実行スクリプト | `hooks/` |
| 人間向けドキュメント | `docs/` |

> **skills/ が C3 の核心**。「Skill = 複数エージェントをまたぐオーケストレーション手順」という定義がすべての配置判断の起点になります。

---

## ワークフロー

開発は `skills/dev-workflow.md` で定義された5フェーズで進みます。

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

## コマンド一覧

### 開発ワークフロー

| コマンド | 役割 |
|---|---|
| `/init-session` | セッション初期化・前回状態の復元 |
| `/setup` | コーディング規約の設定（coding-standards・project-conventions 生成） |
| `/start` | 開発ワークフローの入口（ヒアリング/設計/計画/実装 から選択） |
| `/develop` | TDD フェーズから直接開始 |
| `/review` | レビューフェーズから直接開始 |
| `/promote-pattern` | 蓄積されたパターンを rules/ または skills/ に昇格 |

### ユーティリティ

| コマンド | 役割 |
|---|---|
| `/doc` | ドキュメントをヒアリングして生成（mermaid 図・README・API 仕様書など） |
| `/mcp` | MCP サーバーの追加・一覧・削除（プロジェクトスコープ） |
| `/extract-lib` | 複数プロジェクトのコードを横断解析し、共通処理をライブラリとして設計・生成 |

### 基本的な使い方

```
/init-session          # セッション開始時に必ず実行
/setup                 # 初回のみ：プロジェクト規約を設定
/start                 # 開発開始
```

`/start` 実行後は、各フェーズの承認を進めるだけで最後まで自動的に流れます。

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

インタラクティブな対話が必要なエージェント（interviewer・architect・planner）は親 Claude がペルソナを採用して動作します。実装・検証系エージェントはサブエージェントとして起動されます。

---

## 既存プロジェクトへの導入

C3 の `.claude/` はプロジェクトコードに一切触れません。既存のコードベースにそのまま追加できます。

### 前提条件

- [Claude Code](https://claude.ai/code) がインストール済みでログインしていること
- Python 3.8 以上がインストール済みであること

### 手順

**1. C3 をダウンロードする**

```bash
git clone https://github.com/satoh-y-0323/claude-code-conductor.git
```

**2. `.claude/` をプロジェクトにコピーする**

```bash
cp -r claude-code-conductor/.claude /path/to/your-project/
```

Windows（PowerShell）の場合:
```powershell
Copy-Item -Recurse claude-code-conductor\.claude your-project\
```

**3. プロジェクトを Claude Code で開き、初期設定を行う**

```
/init-session    # セッション初期化
/setup           # プロジェクトの技術スタック・規約を設定（初回のみ）
```

`/setup` を実行すると、使用言語・フレームワーク・命名規則などをヒアリングして `.claude/rules/` に規約ファイルが生成されます。これ以降は `/start` で開発を始められます。

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
- `/promote-pattern` を実行すると `rules/promoted/` または `skills/promoted/` にルールとして昇格します
- 昇格したルールは以降の全セッションで自動的にエージェントへ注入されます

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

## セッション管理

C3 はセッションをまたいで作業状態を記憶します。

- **毎回のセッション開始時:** `/init-session` を実行する
- **タスク完了のたびに:** session ファイルが自動更新される（まとめて最後に書かない）
- **セッション終了時:** `stop.py` フックが自動的に session ファイルとパターン信用度を更新する
