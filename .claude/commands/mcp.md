# /mcp コマンド

MCP サーバーの追加・一覧・削除を対話形式で行う。
全ての設定は `.claude/settings.json` のプロジェクトスコープに書き込む。

---

## Step 1: 操作を選択する

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "MCP サーバーに対して何をしますか？",
    "header": "操作選択",
    "options": [
      { "label": "追加する", "description": "新しい MCP サーバーを登録する" },
      { "label": "一覧を見る", "description": "登録済みの MCP サーバーを確認する" },
      { "label": "削除する", "description": "登録済みの MCP サーバーを削除する" }
    ]
  }]
}
```

---

## Step 2（一覧表示）

`.claude/settings.json` を Read して `mcpServers` の内容を整形して表示する。

- `mcpServers` が存在しない・空の場合: 「登録済みの MCP サーバーはありません」と表示して終了する。
- 存在する場合: サーバー名・接続方法・URL/コマンドを一覧形式で表示して終了する。

---

## Step 2（削除）

`.claude/settings.json` を Read して `mcpServers` の一覧を表示する。

登録がない場合: 「登録済みの MCP サーバーはありません」と表示して終了する。

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "削除するサーバー名を入力してください"
  }]
}
```

入力されたサーバー名のエントリを `settings.json` から Edit ツールで削除する。
削除後: 「{name} を削除しました」と表示して終了する。

---

## Step 2（追加）: 公開 or 社内を選ぶ

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "追加する MCP サーバーの種別を選んでください",
    "header": "サーバー種別",
    "options": [
      { "label": "公開サーバー", "description": "npm / GitHub 等で公開されているもの（例: @modelcontextprotocol/server-slack）" },
      { "label": "社内・カスタムサーバー", "description": "社内に建てられているもの・自作のもの" }
    ]
  }]
}
```

---

## [公開サーバー追加フロー]

### Step 3a: キーワードを入力させる

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "探したい MCP サーバーのキーワードや用途を入力してください（例: Slack, GitHub, Postgres）"
  }]
}
```

### Step 4a: WebSearch で候補を調査する

WebSearch で以下のクエリで検索する:
```
MCP server {キーワード} model context protocol site:github.com OR site:npmjs.com OR site:mcp.so
```

検索結果から候補を最大 4 件抽出する。各候補につき以下を収集する:
- サーバー名（わかりやすい表示名）
- npm パッケージ名またはリポジトリ URL
- 概要（1行）
- 必要な env vars（判明している範囲で）

### Step 5a: 候補を提示して選択させる

収集した候補をテキストで以下の形式で提示する:

```
候補:
1. {名前}
   パッケージ  : {npm package or repo}
   概要        : {概要}
   必要な設定  : {env vars / なし}
...
```

AskUserQuestion で選択させる（最大 4 択）。
「Other」が入力された場合: パッケージ名または GitHub URL を自由入力させる。

### Step 6a: サーバー識別名を確認する

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "settings.json に登録するサーバー識別名を入力してください（例: slack, github-mcp）。英数字・ハイフン・アンダースコアのみ使用可。"
  }]
}
```

### Step 7a: 必要な env vars を収集する

必要な env vars が判明している場合、1 つずつ AskUserQuestion で確認する:

```json
{
  "questions": [{
    "question": "{ENV_VAR_NAME} の値を入力してください"
  }]
}
```

⚠️ **APIキー等の秘密情報は環境変数として設定することを推奨する。**
`settings.json` には `"KEY": "${ENV_VAR_NAME}"` 形式で記録し、実際の値は OS の環境変数またはシェルプロファイルで設定するよう案内する。

### Step 8a: インストールを案内する

npm パッケージの場合、以下のメッセージを表示する:

```
実行前に以下のコマンドを実行してください（npx を使う場合は不要）:
  npm install -g {package-name}
または、毎回自動取得する場合（推奨）:
  command: "npx"
  args: ["-y", "{package-name}"]
```

→ 共通の承認フローへ進む。

---

## [社内・カスタムサーバー追加フロー]

### Step 3b: transport 種別を選ぶ

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "サーバーの接続方式（transport）を選んでください",
    "header": "transport",
    "options": [
      { "label": "stdio", "description": "コマンドとして起動するサーバー（node / python / バイナリ等）" },
      { "label": "SSE", "description": "Server-Sent Events で接続（http://... の URL）" },
      { "label": "HTTP", "description": "Streamable HTTP で接続（http://... の URL）" }
    ]
  }]
}
```

### Step 4b: 接続情報を収集する

**stdio の場合:**

AskUserQuestion でコマンドを確認する:
```json
{
  "questions": [{
    "question": "実行コマンドを入力してください（例: node, python, /usr/local/bin/mcp-server）"
  }]
}
```

続けて引数を確認する:
```json
{
  "questions": [{
    "question": "引数をスペース区切りで入力してください。ない場合は空のまま送信してください（例: /path/to/server.js --port 3000）"
  }]
}
```

**SSE / HTTP の場合:**

AskUserQuestion で URL を確認する:
```json
{
  "questions": [{
    "question": "接続先の URL を入力してください（例: http://internal-server:3000/sse）"
  }]
}
```

### Step 5b: サーバー識別名を確認する

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "settings.json に登録するサーバー識別名を入力してください（例: internal-db, company-tools）。英数字・ハイフン・アンダースコアのみ使用可。"
  }]
}
```

### Step 6b: 認証・追加設定を確認する

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "認証ヘッダーや環境変数を設定しますか？",
    "header": "認証設定",
    "options": [
      { "label": "設定する", "description": "Bearer トークンや API キーなどを設定する" },
      { "label": "スキップ", "description": "認証なし・後で手動設定する" }
    ]
  }]
}
```

「設定する」の場合:

- **stdio**: env vars を 1 つずつ収集する。値は `"${ENV_VAR_NAME}"` 形式で記録することを推奨する。
- **SSE / HTTP**: headers の key-value ペアを収集する（例: `Authorization: Bearer ${TOKEN}`）。

→ 共通の承認フローへ進む。

---

## [共通] 承認・settings.json 更新

### 承認ステップ

収集した設定内容を以下の形式でテキスト提示する:

```
登録内容:
  識別名    : {name}
  種別      : {公開 / 社内}
  transport : {stdio / SSE / HTTP}
  コマンド  : {command + args  ※stdio のみ}
  URL       : {url             ※SSE / HTTP のみ}
  env / headers : {設定内容 または "なし"}
```

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "登録内容を確認してください。どうしますか？",
    "header": "確認",
    "options": [
      { "label": "登録する", "description": ".claude/settings.json の mcpServers に追記する" },
      { "label": "やり直す", "description": "Step 2（追加）に戻る" }
    ]
  }]
}
```

### settings.json への書き込み

`.claude/settings.json` を Read する。

`mcpServers` キーが存在しない場合は追加する。

**stdio の場合（env vars あり）:**
```json
"server-name": {
  "command": "{command}",
  "args": ["{arg1}", "{arg2}"],
  "env": {
    "KEY": "${ENV_VAR_NAME}"
  }
}
```

**stdio の場合（env vars なし）:**
```json
"server-name": {
  "command": "{command}",
  "args": ["{arg1}", "{arg2}"]
}
```

**SSE の場合:**
```json
"server-name": {
  "type": "sse",
  "url": "{url}",
  "headers": {
    "Authorization": "Bearer ${TOKEN}"
  }
}
```

**HTTP の場合:**
```json
"server-name": {
  "type": "http",
  "url": "{url}",
  "headers": {}
}
```

Edit ツールで `settings.json` を更新する。
更新後: 「{name} を登録しました。Claude Code を再起動すると MCP サーバーが有効になります。」と表示して終了する。
