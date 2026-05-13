# settings.json リファレンス

Claude Code の設定ファイル。プロジェクトルートの `.claude/settings.json` に配置する。
ユーザーローカルの上書きは `.claude/settings.local.json`（`.gitignore` 推奨）。

> 設定の優先順位: `settings.local.json` > `settings.json` > グローバル設定（`~/.claude/settings.json`）
> ただし `hooks` セクションは `settings.local.json` が `settings.json` を**完全に上書き**する（マージしない）。

---

## `permissions`

ツール呼び出しの許可・拒否ルールを定義する。

```json
{
  "permissions": {
    "allow": [
      "Bash(git status*)",
      "Read(**)",
      "Write(.claude/reports/**)"
    ],
    "deny": [
      "Read(.env)",
      "Read(**/.env)"
    ]
  }
}
```

### パターン書式

| 書式 | 説明 |
|---|---|
| `ToolName` | 引数なしでツールを許可 |
| `ToolName(pattern)` | 引数が `pattern` にマッチする場合のみ許可 |
| `ToolName(domain:example.com)` | WebFetch の場合はドメイン指定も使える |
| `**` | パス境界を越えるワイルドカード |
| `*` | パス境界内のワイルドカード（`/` を越えない） |

> `promoted-*` のように境界以外に `*` を置くとバリデーションエラーになる。

### 利用できるツール名

`Bash` / `Read` / `Write` / `Edit` / `Glob` / `Grep` / `WebFetch` / `WebSearch` / `Agent` / `Skill` / `TodoWrite` / `NotebookEdit` など。

---

## `sandbox`

Claude Code のサンドボックス（プロセス分離）を設定する。

```json
{
  "sandbox": {
    "enabled": true,
    "autoAllowBashIfSandboxed": true,
    "allowUnsandboxedCommands": false,
    "excludedCommands": [],
    "network": {
      "allowUnixSockets": [],
      "allowAllUnixSockets": false,
      "allowLocalBinding": false,
      "allowedDomains": []
    },
    "enableWeakerNestedSandbox": true
  }
}
```

| キー | 型 | 説明 |
|---|---|---|
| `enabled` | boolean | サンドボックスを有効にする |
| `autoAllowBashIfSandboxed` | boolean | サンドボックス有効時に Bash を自動許可する |
| `allowUnsandboxedCommands` | boolean | サンドボックス外でのコマンド実行を許可する |
| `excludedCommands` | string[] | サンドボックスから除外するコマンドのリスト |
| `network.allowUnixSockets` | string[] | 許可する Unix ソケットのパス一覧 |
| `network.allowAllUnixSockets` | boolean | 全 Unix ソケットを許可する |
| `network.allowLocalBinding` | boolean | ローカルポートへのバインドを許可する |
| `network.allowedDomains` | string[] | 許可するネットワークドメイン一覧 |
| `enableWeakerNestedSandbox` | boolean | ネストされたサブエージェントに緩いサンドボックスを適用する |

---

## `hooks`

Claude Code のイベントに対してフックを登録する。マッチした全フックは並列実行され、同一コマンドは自動で重複排除される。

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python \"$CLAUDE_PROJECT_DIR/.claude/hooks/pre_tool.py\"",
            "if": "Bash(git *)",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

> `disableAllHooks: true` をトップレベルに設定すると全フックを無効化できる。

---

### イベント一覧

| イベント | タイミング | ブロック可否 |
|---|---|---|
| `SessionStart` | セッション開始・再開時 | 不可 |
| `Setup` | `--init-only` / `--init` / `--maintenance` 起動時 | 不可 |
| `UserPromptSubmit` | プロンプト送信時（Claude 処理前） | 可（exit 2） |
| `UserPromptExpansion` | スキル/コマンドの展開時（Claude 処理前） | 可（exit 2） |
| `PreToolUse` | ツール実行の直前 | 可（exit 2 または JSON） |
| `PermissionRequest` | 許可ダイアログが表示される時 | 可（JSON で allow/deny） |
| `PermissionDenied` | auto モードがツールを拒否した時 | — |
| `PostToolUse` | ツール実行の直後（成功） | 可（JSON `decision: "block"`） |
| `PostToolUseFailure` | ツール実行の直後（失敗） | — |
| `PostToolBatch` | 並列ツール呼び出しバッチ完了後 | 可（JSON `decision: "block"`） |
| `Stop` | Claude が応答を完了した時 | 可（JSON `decision: "block"`） |
| `StopFailure` | API エラーでターン終了時 | 不可（出力・終了コードは無視） |
| `SubagentStart` | サブエージェントが実行を開始した時 | 不可 |
| `SubagentStop` | サブエージェントが完了した時 | 不可 |
| `TaskCreated` | `TaskCreate` でタスク作成時 | — |
| `TaskCompleted` | タスクが完了マークされた時 | — |
| `TeammateIdle` | agent team の teammate が idle になる時 | — |
| `InstructionsLoaded` | CLAUDE.md / `rules/*.md` がロードされた時 | — |
| `ConfigChange` | 設定ファイルが変更された時 | 可（exit 2 または JSON） |
| `CwdChanged` | 作業ディレクトリが変更された時（`cd` 等） | — |
| `FileChanged` | 監視ファイルがディスク上で変更された時 | — |
| `WorktreeCreate` | worktree が作成される時（デフォルト git 動作を置換） | — |
| `WorktreeRemove` | worktree が削除される時 | — |
| `PreCompact` | コンテキスト圧縮の直前 | 不可 |
| `PostCompact` | コンテキスト圧縮完了後 | — |
| `Notification` | 通知が発生した時 | 不可 |
| `Elicitation` | MCP サーバーがユーザー入力を要求した時 | — |
| `ElicitationResult` | MCP 引き出し応答が返される前 | — |
| `SessionEnd` | セッション終了時 | — |

---

### イベント別 matcher の対応

| イベント | matcher がフィルタリングするもの | 値の例 |
|---|---|---|
| `PreToolUse` / `PostToolUse` / `PostToolUseFailure` / `PermissionRequest` / `PermissionDenied` | ツール名 | `Bash`, `Edit\|Write`, `mcp__github__.*` |
| `SessionStart` | 開始方法 | `startup`, `resume`, `clear`, `compact` |
| `Setup` | CLI フラグ | `init`, `maintenance` |
| `SessionEnd` | 終了理由 | `clear`, `resume`, `logout`, `prompt_input_exit`, `other` |
| `Notification` | 通知タイプ | `permission_prompt`, `idle_prompt`, `auth_success`, `elicitation_dialog` |
| `SubagentStart` / `SubagentStop` | エージェント型名 | `general-purpose`, `Explore`, カスタム名 |
| `PreCompact` / `PostCompact` | 圧縮のトリガー | `manual`, `auto` |
| `ConfigChange` | 設定ソース | `user_settings`, `project_settings`, `local_settings`, `policy_settings`, `skills` |
| `StopFailure` | エラータイプ | `rate_limit`, `authentication_failed`, `billing_error`, `server_error` |
| `InstructionsLoaded` | ロード理由 | `session_start`, `nested_traversal`, `path_glob_match`, `include`, `compact` |
| `FileChanged` | リテラルファイル名（パイプ区切り） | `.envrc\|.env` |
| `Elicitation` / `ElicitationResult` | MCP サーバー名 | 設定したサーバー名 |
| `UserPromptExpansion` | コマンド/スキル名 | スキル名 |
| `UserPromptSubmit`, `PostToolBatch`, `Stop`, `TeammateIdle`, `TaskCreated`, `TaskCompleted`, `WorktreeCreate`, `WorktreeRemove`, `CwdChanged` | 非対応 | 常に全発生で発火 |

---

### フックグループ構造

```json
{
  "matcher": "Edit|Write",
  "hooks": [
    {
      "type": "command",
      "command": "スクリプトパス",
      "if": "Edit(*.ts)",
      "timeout": 60
    }
  ]
}
```

| キー | 説明 |
|---|---|
| `matcher` | グループレベルのフィルタ。空文字列は全てにマッチ |
| `hooks[].type` | フックの種類（下記参照） |
| `hooks[].if` | フックレベルの追加フィルタ。許可ルール構文（`Bash(git *)`、`Edit(*.ts)` 等）。ツールイベントのみ有効 |
| `hooks[].timeout` | タイムアウト秒数。デフォルト 600秒（10分） |

---

### hook の type 一覧

#### `command`（標準）

シェルコマンドを実行する。最も一般的。

```json
{ "type": "command", "command": "python script.py" }
```

#### `http`

イベントデータを HTTP エンドポイントに POST する。

```json
{
  "type": "http",
  "url": "http://localhost:8080/hooks",
  "headers": { "Authorization": "Bearer $MY_TOKEN" },
  "allowedEnvVars": ["MY_TOKEN"]
}
```

- `headers` の値は `$VAR` 形式の環境変数補間をサポート（`allowedEnvVars` に列挙したもののみ）
- レスポンスボディで command フックと同じ JSON 形式で結果を返す

#### `prompt`（LLM 評価）

シングルターン LLM 呼び出しで判断を行う。デフォルトモデル: Haiku。

```json
{ "type": "prompt", "prompt": "Check if all tasks are complete...", "model": "sonnet" }
```

- `"ok": true` → 続行
- `"ok": false` + `"reason": "..."` → イベントに応じてブロックまたはフィードバック

#### `mcp_tool`

接続済み MCP サーバーのツールを呼び出す。

#### `agent`（実験的）

マルチターン・ツールアクセス付きの検証。デフォルトタイムアウト 60秒、最大 50 ターン。

```json
{ "type": "agent", "prompt": "Verify all tests pass...", "timeout": 120 }
```

---

### 終了コードによる動作

| 終了コード | 動作 |
|---|---|
| `0` | 続行。`UserPromptSubmit` / `UserPromptExpansion` / `SessionStart` は stdout を Claude のコンテキストに注入 |
| `2` | アクションをブロック。stderr の内容が Claude へのフィードバックになる。ブロック不可イベントでは stderr をユーザーに表示し続行 |
| その他 | 続行。トランスクリプトに `<hook name> hook error` 通知 + stderr 1行目を表示 |

> **注意**: exit 2 と JSON 出力を混在させないこと。Claude Code は exit 2 時に JSON を無視する。

---

### 構造化 JSON 出力（exit 0 + stdout）

より細かい制御が必要な場合、exit 0 で stdout に JSON を出力する。

#### `PreToolUse` の決定制御

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Use rg instead of grep"
  }
}
```

| `permissionDecision` | 動作 |
|---|---|
| `"allow"` | インタラクティブ許可プロンプトをスキップ（deny ルールより優先されない） |
| `"deny"` | ツール呼び出しをキャンセル。`permissionDecisionReason` が Claude へのフィードバック |
| `"ask"` | 通常通りユーザーに許可プロンプトを表示 |
| `"defer"` | `-p` 非インタラクティブモードでのみ有効。プロセスを終了しツール呼び出しを保留 |

#### `PostToolUse` / `Stop` / `PostToolBatch` のブロック

```json
{ "decision": "block", "reason": "Tests are failing" }
```

#### `PermissionRequest` の決定制御

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PermissionRequest",
    "decision": {
      "behavior": "allow",
      "updatedPermissions": [
        { "type": "setMode", "mode": "acceptEdits", "destination": "session" }
      ]
    }
  }
}
```

#### `UserPromptSubmit` へのコンテキスト注入

```json
{ "additionalContext": "現在のブランチ: feature/auth" }
```

#### `PermissionDenied` でリトライ許可

```json
{ "retry": true }
```

---

### 環境変数

フックスクリプト内で使える環境変数:

| 変数 | 内容 |
|---|---|
| `$CLAUDE_PROJECT_DIR` | プロジェクトのルートディレクトリの絶対パス |
| `$CLAUDE_ENV_FILE` | このファイルに書いた内容が各 Bash コマンド実行前にプリアンブルとして実行される（`direnv` 等との連携に使う） |

イベントデータ（ツール名・入力 JSON 等）は **stdin を通じて JSON として渡される**。`jq` 等で解析する。

```bash
INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
```

---

### hook の配置場所によるスコープ

| 場所 | スコープ | 共有 |
|---|---|---|
| `~/.claude/settings.json` | 全プロジェクト | なし（マシンローカル） |
| `.claude/settings.json` | このプロジェクトのみ | git 管理可能 |
| `.claude/settings.local.json` | このプロジェクトのみ | `.gitignore` 推奨 |
| skills / agents のフロントマター `hooks:` | スキル/エージェントがアクティブな間のみ | コンポーネントファイルで定義 |

---

## `statusLine`

Claude Code のステータスバーに表示するカスタム情報を定義する。

```json
{
  "statusLine": {
    "type": "command",
    "command": "python \"$CLAUDE_PROJECT_DIR/.claude/hooks/statusline.py\""
  }
}
```

| キー | 型 | 説明 |
|---|---|---|
| `type` | string | `"command"` のみ（現時点） |
| `command` | string | 実行するコマンド。stdout の1行目がステータスバーに表示される |

### C3 デフォルトの表示内容

C3 が提供する `.claude/hooks/statusline.py` は以下の形式で表示する:

```
[Claude Sonnet 4] 200K high | ctx used 8% | 5h lim 24% (1h 59m) | 7d lim 41% (2d 23h)
```

| 項目 | 内容 |
|---|---|
| `[モデル名]` | 現在のモデル表示名 |
| `200K` / `1M` | コンテキストウィンドウサイズ |
| `high` / `normal` / `low` | effort レベル |
| `ctx used N%` | コンテキスト使用率（色: 緑→黄→オレンジ→赤） |
| `5h lim N% (Xh Ym)` | 5時間レート制限の消費率とリセットまでの残り時間 |
| `7d lim N% (Xd Yh)` | 7日レート制限の消費率とリセットまでの残り時間 |

`rate_limits` は Claude.ai サブスクライバー（Pro/Max）がセッションの最初の API レスポンス後に取得できる。未取得の場合は該当項目を省略する。

---

## `model`

デフォルトで使用するモデルを上書きする。

```json
{
  "model": "claude-opus-4-7"
}
```

有効なモデル ID 例: `claude-sonnet-4-6` / `claude-opus-4-7` / `claude-haiku-4-5-20251001`

---

## `env`

Claude Code セッション全体で使う環境変数を設定する。

```json
{
  "env": {
    "MY_VAR": "value",
    "DEBUG": "1"
  }
}
```

---

## `outputStyle`

Claude のシステムプロンプト全体を差し替える出力スタイルを指定する。

```json
{
  "outputStyle": "Explanatory"
}
```

| 値 | 説明 |
|---|---|
| `"Default"` | ソフトウェアエンジニアリング向けデフォルト |
| `"Explanatory"` | 教育的インサイト付き（実装選択肢・パターン解説を追加） |
| `"Learning"` | 協調学習モード。一部コードを `TODO(human)` マーカーに置き換えてユーザーに実装を促す |
| カスタム名 | `.claude/output-styles/<name>.md` または `~/.claude/output-styles/<name>.md` のファイル名 |

- 変更は次のセッション開始時に有効になる
- `/config` → Output style からも変更できる（`settings.local.json` に保存される）
- CLAUDE.md・rules/・`--append-system-prompt` とは異なり、**システムプロンプト本体を置換**する

---

## `includeCoAuthoredBy`

git コミットの `Co-Authored-By` トレーラーに Claude の情報を含めるかを制御する。

```json
{
  "includeCoAuthoredBy": true
}
```

デフォルト: `true`

---

## `apiKeyHelper`

API キーを取得するカスタムコマンドを指定する。`ANTHROPIC_API_KEY` 環境変数の代わりに使う。

```json
{
  "apiKeyHelper": "cat ~/.secrets/anthropic_key"
}
```

---

## `cleanupPeriodDays`

ログファイルを自動削除するまでの日数。

```json
{
  "cleanupPeriodDays": 30
}
```

---

## `mcpServers`

MCP（Model Context Protocol）サーバーを登録する。登録されたサーバーのツールが Claude から利用できるようになる。

```json
{
  "mcpServers": {
    "my-server": {
      "type": "stdio",
      "command": "node",
      "args": ["path/to/server.js"],
      "env": {
        "API_KEY": "..."
      }
    }
  }
}
```

| キー | 型 | 説明 |
|---|---|---|
| `type` | string | `"stdio"` / `"sse"` / `"http"` |
| `command` | string | サーバーを起動するコマンド（stdio の場合） |
| `args` | string[] | コマンドの引数 |
| `env` | object | サーバープロセスに渡す環境変数 |
| `url` | string | サーバーの URL（sse / http の場合） |

---

## C3 での使い分け

| ファイル | 用途 |
|---|---|
| `.claude/settings.json` | プロジェクト共通設定。git 管理する |
| `.claude/settings.local.json` | 開発者個人の設定（追加 `allow` 等）。`.gitignore` に追加して git 管理しない |

> `hooks` を `settings.local.json` に書くと `settings.json` の `hooks` が**完全に無効化**される。フックは `settings.json` のみに書くこと。
