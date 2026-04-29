# Parallel Orchestra YAML フロントマター仕様

Parallel Orchestra が読み込む YAML フロントマターのフォーマット定義。
C3 では `plan-report` ファイルのフロントマターとして planner が出力する。

---

## フィールド一覧

| フィールド | 必須 | 型 | 備考 |
|---|---|---|---|
| `po_plan_version` | ✅ | string | フォーマットバージョン（現在: `"0.1"`） |
| `name` | ✅ | string | プランの表示名 |
| `cwd` | ✅ | string | YAML フロントマターが書かれたファイルのディレクトリからプロジェクトルートへの相対パス |
| `tasks` | ✅ | array | タスク定義の配列（1件以上） |
| `tasks[].id` | ✅ | string | タスクの一意識別子。英数字・ハイフン・アンダースコアのみ |
| `tasks[].agent` | ✅ | string | 使用するエージェント名 |
| `tasks[].read_only` | ✅ | boolean | `true`: 読み取り専用（worktree なし）／`false`: 書き込みあり（worktree 作成） |
| `tasks[].prompt` | ✅ | string | エージェントへの指示内容 |
| `tasks[].depends_on` | 任意 | string[] | 先行タスクの ID リスト。DAG スケジューリングに使用 |
| `tasks[].writes` | 任意 | string[] | このタスクが書き込むファイルパス。タスク間の衝突検出ヒントに使用 |
| `tasks[].max_retries` | 任意 | integer (≥0) | プロセス失敗時の最大リトライ回数。`defaults.max_retries` を上書き |
| `tasks[].concurrency_group` | 任意 | string | 同時実行数を制限するグループ名。`concurrency_limits` に対応するエントリが必要 |
| `defaults` | 任意 | object | 全タスク共通のデフォルト値 |
| `defaults.max_retries` | 任意 | integer (≥0) | タスクレベルの `max_retries` が未指定の場合に使用 |
| `concurrency_limits` | 任意 | object | グループ名をキー、最大同時実行数を値とするマップ |
| `on_complete` | 任意 | object | 全タスク成功時の Webhook 設定 |
| `on_complete.webhook_url` | — | string | HTTP/HTTPS URL |
| `on_failure` | 任意 | object | 1件以上のタスク失敗時の Webhook 設定 |
| `on_failure.webhook_url` | — | string | HTTP/HTTPS URL |

---

## Parallel Orchestra が内部で管理するもの（フロントマターに書かない）

| 項目 | 理由 |
|---|---|
| `timeout_sec` | Parallel Orchestra がデフォルト値を内部保持 |
| `idle_timeout_sec` | worktree + Agent ツール構成では誤検知になるため不採用 |
| `retry_delay_sec` / `retry_backoff_factor` | Parallel Orchestra が固定値を内部保持 |
| タスクの CWD（起動時） | `read_only: false` → worktree ルート、`read_only: true` → プロジェクトルートを PO が自動設定 |
| `PO_WORKTREE_GUARD=1` | `read_only: false` タスク起動時に PO が自動セット |

---

## フォーマット例

```yaml
---
po_plan_version: "0.1"
name: "ユーザー認証機能の並列実装"
cwd: "../.."

tasks:
  - id: tdd-auth-login
    agent: tdd-develop
    read_only: false
    prompt: |
      ログイン機能を TDD で実装してください。
      plan-report: .claude/reports/plan-report-20260429-120000.md
    writes:
      - src/auth/login.py
      - tests/test_login.py

  - id: tdd-auth-logout
    agent: tdd-develop
    read_only: false
    prompt: |
      ログアウト機能を TDD で実装してください。
      plan-report: .claude/reports/plan-report-20260429-120000.md
    writes:
      - src/auth/logout.py
      - tests/test_logout.py

  - id: review-auth
    agent: code-reviewer
    read_only: true
    prompt: "認証モジュール全体のコードレビューを行ってください。"
    depends_on: [tdd-auth-login, tdd-auth-logout]
    concurrency_group: api-calls

defaults:
  max_retries: 1

concurrency_limits:
  api-calls: 2

on_failure:
  webhook_url: "https://example.com/notify"
---
```

---

## C3 における配置場所

| ファイル | パス |
|---|---|
| plan-report（フロントマターを含む） | `.claude/reports/plan-report-YYYYMMDD-HHMMSS.md` |
| `cwd` の値（C3 標準） | `"../.."` |

---

## 注意事項

- `claude -p` の起動 CWD は `cwd` フィールドではなく worktree ルートパスを使用する
- `cwd` は Parallel Orchestra が git worktree を作成するための**プロジェクトルート特定**に使用する
- `read_only: false` タスクは必ず git リポジトリ内で実行すること（worktree が git 依存のため）
