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

## フォーマット例（推奨パターン）

ファイル/モジュール境界で TDD タスクを並列化し、レビューを末尾に集約する形。3 つの dev タスクが PO で同時起動され、すべて完了してからレビューが走る。`writes` で衝突検出を効かせている。

```yaml
---
po_plan_version: "0.1"
name: "ユーザー認証機能の並列実装"
cwd: "../.."

tasks:
  # 1機能 = 1 TDD タスク。tdd-develop が内部で tester→developer→tester を
  # 回すので、外側で「先にテスト」「次に実装」と分けない。
  - id: tdd-auth-login
    agent: tdd-develop
    read_only: false
    prompt: |
      ログイン機能を TDD で実装してください。
      plan-report: .claude/reports/plan-report-20260429-120000.md
    writes:
      - src/auth/login.py
      - tests/test_login.py

  # 別ファイルなので login と並列実行可能。depends_on は付けない。
  - id: tdd-auth-logout
    agent: tdd-develop
    read_only: false
    prompt: |
      ログアウト機能を TDD で実装してください。
      plan-report: .claude/reports/plan-report-20260429-120000.md
    writes:
      - src/auth/logout.py
      - tests/test_logout.py

  # パスワードリセットも独立。3 つの dev タスクが並列で動く。
  - id: tdd-auth-reset
    agent: tdd-develop
    read_only: false
    prompt: |
      パスワードリセット機能を TDD で実装してください。
      plan-report: .claude/reports/plan-report-20260429-120000.md
    writes:
      - src/auth/reset.py
      - tests/test_reset.py

  # レビューは read_only:true、全 dev タスクに depends_on を付けて末尾集約。
  - id: review-auth
    agent: code-reviewer
    read_only: true
    prompt: "認証モジュール全体のコードレビューを行ってください。"
    depends_on: [tdd-auth-login, tdd-auth-logout, tdd-auth-reset]

defaults:
  max_retries: 1
---
```

実行イメージ:

```
時間→
[tdd-auth-login    ]
[tdd-auth-logout   ]   ← 3 つが同時に起動（max_workers=3 想定）
[tdd-auth-reset    ]
                      [review-auth]   ← 全完了後に走る
```

---

## 並列・直列交互パターン

中間レビューや段階的な動作確認を挟みたい場合の plan-report 構造。stage 内では並列、stage 間は順序を強制する形を取る。C3 の wave-execution でも各 stage 完了後にユーザー承認が入るので、ヒューマン・イン・ザ・ループ前提の中規模実装で有効。

### 用途

- 段階的にユーザー確認・中間レビューを挟みたい
- 実装ステージごとに動作確認が必要（例: 認証 → 認可 → 監査ログ、と段階的に積み上げる）
- 後続の dev タスクが前段のレビュー指摘を受けて方針調整する可能性がある

### 構造

```
Stage 1: dev_a, dev_b, dev_c (並列)
  └─ Stage 2: review_or_sync (依存: dev_a / dev_b / dev_c)  ← 中間レビュー / 集約
      └─ Stage 3: dev_d, dev_e (並列、依存: review_or_sync)
          └─ Stage 4: review_or_sync_2 (依存: dev_d / dev_e)
              └─ ...
```

特徴:

- 各 stage 内は **2 件以上の独立タスク**で並列度を維持する
- stage を区切る `review_or_sync` タスクは `code-reviewer`（read_only: true）か、軽い同期目的のサマライズタスク
- 中間レビュータスクが次 stage の dev タスクに `depends_on` で繋がる → **stage 間は完全に直列**

### 動作実績

17 tasks / 7 stages 構成（3〜4 並列の dev stage と 1 件の review stage が交互）で動作確認済み。各 stage が wave 1 つに対応し、wave-execution.md の per-wave 承認フローに綺麗に乗る。

### 採用条件（planner 側のルール）

- ユーザーが明示的に要求している（自動でこの形にしない）
- 各 stage 内の並列度は **2 以上**を維持する（直列に潰さない）
- 全体のチェーン長 ≦ タスク数 / 2 を依然として守る

詳細は `.claude/agents/planner.md` の「直列・並列交互パターンの取り扱い」を参照。

---

## アンチパターン（避けるべき書き方）

### A. 全部直列にしてしまう

```yaml
tasks:
  - id: tdd-login
    agent: tdd-develop
    # ...
  - id: tdd-logout
    agent: tdd-develop
    depends_on: [tdd-login]   # ❌ login と logout は別ファイル。依存ない
    # ...
  - id: tdd-reset
    agent: tdd-develop
    depends_on: [tdd-logout]  # ❌ 「前のタスクが終わってから」レベルの依存
    # ...
```

→ `depends_on` チェーンの最大長 = タスク数 - 1。並列度 1 になり PO を使う意味なし。

### B. TDD を「テスト → 実装」に分割する

```yaml
tasks:
  - id: write-login-tests
    agent: tester
    # ...
  - id: implement-login
    agent: developer
    depends_on: [write-login-tests]   # ❌ tdd-develop が内部でやる仕事を外で分割
```

→ tdd-develop が tester→developer→tester ループを内部で回すので、外で分けると Red-Green-Refactor サイクルが壊れる。**1 機能 = 1 TDD タスク**でまとめる。

### C. writes が空・同じファイルが重複

```yaml
tasks:
  - id: tdd-login
    writes: []                # ❌ PO の衝突検出が効かない
  - id: tdd-auth-helpers
    writes:
      - src/auth/login.py     # ❌ tdd-login と同じファイル → 競合
```

→ 必ず書き込みファイルを `writes` に列挙する。重複したらタスクをまとめるか `concurrency_group` で同時実行を制限する。

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
