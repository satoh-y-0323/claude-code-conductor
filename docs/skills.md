# スキル一覧

C3 のスラッシュコマンドはすべてスキル（`.claude/skills/{name}/SKILL.md`）として実装されています。Claude Code 2026 の Skills 標準に準拠しており、YAML フロントマターで設定を持ちます。

## 開発ワークフロー系

| スキル | 役割 |
|---|---|
| `/init-session` | セッション初期化・前回状態の復元・残タスクと git log の整合性チェック |
| `/setup` | コーディング規約の設定（`coding-standards.md` / `project-conventions.md` を生成） |
| `/start` | 開発ワークフローの入口。開始地点（標準ワークフロー / 実装 / デバッグ / レビュー）を選んで対応する dev-workflow フェーズへ遷移 |
| `/develop` | TDD フェーズ（D）から直接開始 |
| `/review-phase` | レビューフェーズ（E）から直接開始（code-reviewer + security-reviewer） |
| `/promote-pattern` | 蓄積されたパターンを `rules/promoted/` または `skills/promoted-*/` に昇格 |
| `/pattern-status` | `patterns.json` の現状（信用度・昇格候補・期限残・既昇格）を表形式で可視化する読み取り専用コマンド。`/promote-pattern` の前段に使う |

## ユーティリティ系

| スキル | 役割 |
|---|---|
| `/doc` | ドキュメントをヒアリングして生成（mermaid 図・README・API 仕様書など） |
| `/mcp-config` | MCP サーバーの追加・一覧・削除（プロジェクトスコープ） |
| `/extract-lib` | 複数プロジェクトのコードを横断解析し、共通処理をライブラリとして設計・生成 |
| `/recall` | 過去のセッション・レポート・パターンから類似情報を意味検索で取得（v2.10.0+ / numpy ベクトル検索 + 多言語 embedding） |
| `/brainstorm` | 仕事・設計の相談を、資料（PDF/画像）を読み込んだ上で気軽に発散・壁打ち。視点・選択肢・論点を増やす方向で結論を急がない（grill＝詰めるとは逆）。Excel は PDF に書き出して渡す（v2.29.0+） |
| `/codex-review` | Codex CLI に `.codex/agents/` の定義を読み込ませ、code-reviewer / security-reviewer ペルソナでレビューを実行。単一ファイルモードとワークフローモード（git diff 全体の並走レビュー）があり、レポート契約（`[CR-XX-NNN]` / `[SR-XX-NNN]`）は C3 と共通。Codex adapter（`c3 init --platform codex`）セットアップ済み環境のみ有効 |

## 内部参照スキル（`/start` などから自動呼び出し）

| スキル | 役割 |
|---|---|
| `dev-workflow` | 5 フェーズのワークフロー本体 |
| `parallel-agents` | plan-report の wave 単位で親 Claude の Agent ツール並列起動 + isolation:worktree |
| `report-timestamp` | レポートファイル名のタイムスタンプ取得 |

## `/start` の開始地点

`/start` 実行時、以下の 4 つから開始地点を選択します（v2.8.0 で `task_type` 選択を廃止し直接フェーズを選ぶ方式に簡素化）。

| 開始地点 | 遷移先 |
|---|---|
| **標準ワークフロー** | ヒアリング / 設計 / 計画 のいずれか（新機能・リファクタ・改善など） |
| **実装から** | 既存 plan-report を使って実装フェーズ（D）へ |
| **デバッグ調査から** | systematic-debugger → 実装（developer + tester Green）→ レビュー |
| **レビューから** | 既存コードを code-reviewer + security-reviewer でレビュー（指摘あれば計画フェーズへ戻る） |

## 並列実行 (parallel-agents skill)

計画フェーズで生成した `plan-report` を YAML フロントマター付きマニフェストとして読み込み、独立タスクを親 Claude の Agent ツール並列起動 + 公式 `isolation: "worktree"` で並列実行できます。

### 使い方

1. `/start` で要件→設計→計画フェーズを完走させる（planner が plan-report の先頭に YAML フロントマターを自動付与）
2. `/develop` を起動 → **D-0** で plan-report のフロントマターを自動検出し parallel-agents skill へ切替
3. C3 が `c3 plan validate` でマニフェスト妥当性を検証、wave ごとにユーザー承認を取りながら Agent ツールで並列実行

v2.0.0 以降、外部の parallel-orchestra プロセスは不要で、Claude Code 標準機能のみで並列実行が完結します。

## エージェント一覧

各スキルが起動するエージェントは以下の通り。

| エージェント | model | 主な出力 | 起動方式 |
|---|---|---|---|
| interviewer | sonnet | requirements-report | 親 Claude がペルソナ採用 |
| architect | opus | architecture-report | 親 Claude がペルソナ採用 |
| planner | opus | plan-report | 親 Claude がペルソナ採用 |
| design-critic | opus | design-review-report | Agent ツールで起動（フェーズ C 承認後の任意監査） |
| developer | sonnet | 実装コード | Agent ツールで起動 |
| tester | sonnet | テスト・test-report | Agent ツールで起動 |
| code-reviewer | sonnet | code-review-report | Agent ツールで起動 |
| security-reviewer | sonnet | security-review-report | Agent ツールで起動 |
| doc-writer | opus | ドキュメント各種 | Agent ツールで起動 |
| systematic-debugger | sonnet | debug-analysis-report | Agent ツールで起動（行き詰まり時） |
| project-setup | opus | `rules/` 規約ファイル | Agent ツールで起動（`/setup` 時） |
| wt_developer / wt_tester / wt_systematic-debugger | sonnet | task_id ベースのレポート | parallel-agents skill が isolation:worktree 付きで起動 |

インタラクティブな対話が必要なエージェント（interviewer・architect・planner）は親 Claude がペルソナを採用して動作します。実装・検証系エージェントはサブエージェントとして起動されます。

model 列は既定値です。developer / wt_developer は tier-routing（タスク複雑度に応じた Haiku/Sonnet/Opus の自動選択）の hook が起動時に model を自動適用するため、実際の使用 model は複雑度と学習データに応じて変わります。

## SKILL.md の場所

各スキルの実体は GitHub リポジトリの以下を参照してください。

- [skills/init-session/SKILL.md](https://github.com/satoh-y-0323/claude-code-conductor/blob/main/.claude/skills/init-session/SKILL.md)
- [skills/setup/SKILL.md](https://github.com/satoh-y-0323/claude-code-conductor/blob/main/.claude/skills/setup/SKILL.md)
- [skills/start/SKILL.md](https://github.com/satoh-y-0323/claude-code-conductor/blob/main/.claude/skills/start/SKILL.md)
- [skills/dev-workflow/SKILL.md](https://github.com/satoh-y-0323/claude-code-conductor/blob/main/.claude/skills/dev-workflow/SKILL.md)
- [skills/review-phase/SKILL.md](https://github.com/satoh-y-0323/claude-code-conductor/blob/main/.claude/skills/review-phase/SKILL.md)
- [skills/develop/SKILL.md](https://github.com/satoh-y-0323/claude-code-conductor/blob/main/.claude/skills/develop/SKILL.md)
- [skills/promote-pattern/SKILL.md](https://github.com/satoh-y-0323/claude-code-conductor/blob/main/.claude/skills/promote-pattern/SKILL.md)
- [skills/brainstorm/SKILL.md](https://github.com/satoh-y-0323/claude-code-conductor/blob/main/.claude/skills/brainstorm/SKILL.md)
- [skills/pattern-status/SKILL.md](https://github.com/satoh-y-0323/claude-code-conductor/blob/main/.claude/skills/pattern-status/SKILL.md)
- [skills/codex-review/SKILL.md](https://github.com/satoh-y-0323/claude-code-conductor/blob/main/.claude/skills/codex-review/SKILL.md)

## 次に読むページ

- [CLI リファレンス](cli-reference.md) — ターミナルから使う `c3` コマンド
- [はじめに](getting-started.md) — インストールと初回セッション
