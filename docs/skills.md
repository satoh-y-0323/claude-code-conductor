# スキル一覧

C3 のスラッシュコマンドはすべてスキル（`.claude/skills/{name}/SKILL.md`）として実装されています。Claude Code 2026 の Skills 標準に準拠しており、YAML フロントマターで設定を持ちます。

## 開発ワークフロー系

| スキル | 役割 |
|---|---|
| `/init-session` | セッション初期化・前回状態の復元・残タスクと git log の整合性チェック |
| `/setup` | コーディング規約の設定（`coding-standards.md` / `project-conventions.md` を生成） |
| `/start` | 開発ワークフローの入口。タスク種別を選び、最適なエージェント編成で開始 |
| `/develop` | TDD フェーズ（D）から直接開始 |
| `/code-review` | レビューフェーズ（E）から直接開始（code-reviewer + security-reviewer） |
| `/promote-pattern` | 蓄積されたパターンを `rules/promoted/` または `skills/promoted-*/` に昇格 |

## ユーティリティ系

| スキル | 役割 |
|---|---|
| `/doc` | ドキュメントをヒアリングして生成（mermaid 図・README・API 仕様書など） |
| `/mcp-config` | MCP サーバーの追加・一覧・削除（プロジェクトスコープ） |
| `/extract-lib` | 複数プロジェクトのコードを横断解析し、共通処理をライブラリとして設計・生成 |

## 内部参照スキル（`/start` などから自動呼び出し）

| スキル | 役割 |
|---|---|
| `task-routing` | タスク種別から推奨エージェント編成を決定 |
| `dev-workflow` | 5 フェーズのワークフロー本体 |
| `wave-execution` | PO（Parallel Orchestra）の wave 単位実行 |
| `worktree-tdd-workflow` | ヘッドレス TDD サイクル（PO 並列タスク内で使う） |
| `report-timestamp` | レポートファイル名のタイムスタンプ取得 |

## タスク種別と推奨エージェント編成

`/start` 実行時、以下の 5 種別から選択します。

| 種別 | 推奨フロー |
|---|---|
| **feature** | 5 フェーズフル（A→B→C→D→E） |
| **bug-fix** | systematic-debugger → developer → tester → code-reviewer + security-reviewer 並列 |
| **refactor** | 計画 → 実装 → テスト → レビュー（既存テストでカバー可能なら C→D-2→E） |
| **security-audit** | code-reviewer + security-reviewer 並列レビュー → planner → TDD → 最終レビュー |
| **docs** | doc-writer 単独 |

## 並列実行 (Parallel Orchestra)

計画フェーズで生成した `plan-report` を YAML フロントマター付きマニフェストとして parallel-orchestra (PO) に渡し、独立タスクを git worktree で並列実行できます。

### 使い方

1. `/start` で要件→設計→計画フェーズを完走させる（planner が plan-report の先頭に PO 用 YAML フロントマターを自動付与）
2. `/develop` を起動 → **D-0** で plan-report のフロントマターを自動検出し PO 並列モードへ切替
3. C3 が `c3 po dry-run` でマニフェスト妥当性を検証、wave ごとにユーザー承認を取りながら `c3 po run-wave` で実行

PO は C3 に同梱されているため、`pip install claude-code-conductor` だけで利用可能です。

## エージェント一覧

各スキルが起動するエージェントは以下の通り。

| エージェント | model | 主な出力 | 起動方式 |
|---|---|---|---|
| interviewer | sonnet | requirements-report | 親 Claude がペルソナ採用 |
| architect | opus | architecture-report | 親 Claude がペルソナ採用 |
| planner | opus | plan-report | 親 Claude がペルソナ採用 |
| developer | sonnet | 実装コード | Agent ツールで起動 |
| tester | sonnet | テスト・test-report | Agent ツールで起動 |
| tdd-develop | sonnet | -（PO 並列時のみ） | Agent ツールで起動 |
| code-reviewer | sonnet | code-review-report | Agent ツールで起動 |
| security-reviewer | sonnet | security-review-report | Agent ツールで起動 |
| doc-writer | sonnet | ドキュメント各種 | Agent ツールで起動 |
| systematic-debugger | sonnet | debug-analysis-report | Agent ツールで起動（行き詰まり時） |

インタラクティブな対話が必要なエージェント（interviewer・architect・planner）は親 Claude がペルソナを採用して動作します。実装・検証系エージェントはサブエージェントとして起動されます。

## SKILL.md の場所

各スキルの実体は GitHub リポジトリの以下を参照してください。

- [skills/init-session/SKILL.md](https://github.com/satoh-y-0323/claude-code-conductor/blob/main/.claude/skills/init-session/SKILL.md)
- [skills/start/SKILL.md](https://github.com/satoh-y-0323/claude-code-conductor/blob/main/.claude/skills/start/SKILL.md)
- [skills/dev-workflow/SKILL.md](https://github.com/satoh-y-0323/claude-code-conductor/blob/main/.claude/skills/dev-workflow/SKILL.md)
- [skills/code-review/SKILL.md](https://github.com/satoh-y-0323/claude-code-conductor/blob/main/.claude/skills/code-review/SKILL.md)
- [skills/develop/SKILL.md](https://github.com/satoh-y-0323/claude-code-conductor/blob/main/.claude/skills/develop/SKILL.md)
- [skills/promote-pattern/SKILL.md](https://github.com/satoh-y-0323/claude-code-conductor/blob/main/.claude/skills/promote-pattern/SKILL.md)

## 次に読むページ

- [CLI リファレンス](cli-reference.md) — ターミナルから使う `c3` コマンド
- [はじめに](getting-started.md) — インストールと初回セッション
