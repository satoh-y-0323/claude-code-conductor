# C3 Taxonomy — フォルダ構成と意味の定義

Claude Code Conductor（C3）における各ファイルタイプの意味を定義する。
**ここで定義した意味が、全ファイルの配置判断の基準になる。**

---

## フォルダ一覧

### `agents/`
カスタムサブエージェントの定義ファイルを置く。YAML フロントマターで設定し、その後の Markdown 本文がサブエージェントのシステムプロンプトになる。

**スコープと配置場所:**

| 場所 | スコープ | 優先度 |
|---|---|---|
| `.claude/agents/` | このプロジェクトのみ | 高（プロジェクト固有） |
| `~/.claude/agents/` | 全プロジェクト共通 | 低（個人用） |

同名のエージェントが複数のスコープに存在する場合、プロジェクトレベルが優先される。

**YAML フロントマター（有効なキー）:**

| フィールド | 必須 | 説明 |
|---|---|---|
| `name` | はい | 小文字とハイフンを使った一意の識別子 |
| `description` | はい | Claude がこのサブエージェントに委譲するかどうかの判断に使う説明 |
| `tools` | いいえ | 使用できるツールの許可リスト。省略時は全ツールを親から継承 |
| `disallowedTools` | いいえ | 拒否するツールのリスト。継承リストから除外 |
| `model` | いいえ | 使用モデル: `sonnet` / `opus` / `haiku` / 完全モデルID / `inherit`。省略時は `inherit` |
| `permissionMode` | いいえ | 権限モード（下表参照）。プラグインサブエージェントでは無視される |
| `maxTurns` | いいえ | 停止するまでの最大 agentic ターン数 |
| `skills` | いいえ | スタートアップ時にコンテキストへプリロードするスキル名リスト |
| `mcpServers` | いいえ | このサブエージェント専用の MCP サーバー設定 |
| `hooks` | いいえ | このサブエージェントにスコープされたライフサイクルフック |
| `memory` | いいえ | 永続メモリスコープ: `user` / `project` / `local` |
| `background` | いいえ | `true` で常にバックグラウンド実行。デフォルト: `false` |
| `effort` | いいえ | 努力レベル: `low` / `medium` / `high` / `xhigh` / `max` |
| `isolation` | いいえ | `worktree` で一時的な git worktree 内で実行（変更なしなら自動削除） |
| `color` | いいえ | UI 表示色: `red` / `blue` / `green` / `yellow` / `purple` / `orange` / `pink` / `cyan` |
| `initialPrompt` | いいえ | `--agent` でメインセッションとして実行する場合の最初のユーザーターン |

**`permissionMode` の値:**

| 値 | 動作 |
|---|---|
| `default` | 標準の権限チェック（プロンプトあり） |
| `acceptEdits` | ファイル編集と一般的なファイルシステム操作を自動承認 |
| `auto` | バックグラウンド分類器がコマンドを確認 |
| `dontAsk` | 権限プロンプトを自動拒否（明示的に許可済みのツールは動作） |
| `bypassPermissions` | 全権限チェックをスキップ（注意して使用） |
| `plan` | プランモード（読み取り専用） |

> 親が `bypassPermissions` または `acceptEdits` の場合、子はそれを継承しオーバーライドできない。

**`memory` スコープの保存場所:**

| スコープ | 場所 | コミット対象 |
|---|---|---|
| `user` | `~/.claude/agent-memory/<agent-name>/` | × (個人) |
| `project` | `.claude/agent-memory/<agent-name>/` | × (`.gitignore` で除外) |
| `local` | `.claude/agent-memory-local/<agent-name>/` | × (個人) |

メモリが有効な場合、`MEMORY.md` の最初の 200 行（最大 25KB）がシステムプロンプトに自動注入される。

**運用ルール:**

- **配布除外**: `.claude/agent-memory/` は `.gitignore` / `_excludes.py` / `hatch_build.py` の 3 ファイルで除外済み。wheel にも含まれない（プロジェクト固有・個人作業）
- **書き込みタイミング**: agent が `memory:` frontmatter を持つ場合、Claude Code がエージェント停止時に自動更新する
- **手動編集**: `MEMORY.md` は index ファイル（150 文字/行 × 200 行が注入対象）。個別の memory ファイルは別ファイルとして同ディレクトリ配置
- **chat / Codex / Cursor 共通**: `.claude/agent-memory/` は canonical source として全プラットフォームで参照される
- **削除タイミング**: 不要になった agent-memory はディレクトリごと削除して問題ない（次回起動時に空から再構築）

**`tools` での Agent 生成制限:**

`Agent(worker, researcher)` のように書くと、指定したサブエージェントのみ生成できる許可リストになる。括弧なしの `Agent` は制限なし。`Agent` を省略するとサブエージェント生成不可。

**Codex / Cursor での扱い:**

上記のフロントマターキーは **Claude Code 公式仕様**。`c3 init --platform codex|cursor` で生成される adapter 経由で動かす場合、以下のように読み替え・無視される:

| キー | Codex | Cursor |
|---|---|---|
| `name` / `description` | TOML `name` / `description` に反映 | `.cursor/rules/c3-core.mdc` の参照対象 |
| `tools` / `disallowedTools` | Codex の subagent ツール制限に読み替え（一部キー差あり） | 反映されない（rule 内テキストで補完） |
| `model` | Codex 側のモデル選択に従う（adapter は値を保持しない） | Cursor 側のモデル選択に従う |
| `permissionMode` | 概念が存在しないため無視 | 同左 |
| `isolation: worktree` | Codex の subagent runtime に worktree 機構があれば適用、なければ無視 | 反映されない |
| `mcpServers` | 別途 `.codex/config.toml` で MCP 設定 | `.cursor/mcp.json` で MCP 設定 |
| `hooks` | Claude Code lifecycle hook のため無視 | 同左 |
| `memory` | `.claude/agent-memory/` を共通参照（パスは Claude Code と同じ） | 同左 |
| `background` / `effort` / `maxTurns` | Codex 側の対応機能に従う | 反映されない |

adapter 生成物の詳細仕様は [`platform-adapters.md`](platform-adapters.md) を参照。

---

### `rules/`
エージェントに注入される背景知識・制約を置く。

- 作業原則（「1タスク = 1コミット」等）
- セキュリティ制約
- プロジェクト固有の制約

> **手順ではなく知識・制約**。「こうしろ」ではなく「これを知っておけ」。
> 手順を書きたくなったら `skills/` に置くこと。

**YAML フロントマター（有効なキー）:**

```yaml
---
paths:
  - "src/**/*.py"
  - "tests/**/*.py"
---
```

- `paths`: 指定したグロブパターンにマッチするファイルを扱う時だけルールが適用される。省略時は常に適用。

**サブフォルダ:**

- `promoted/` — `/promote-pattern` スキルが昇格させたルールを配置する。`index.md` が `CLAUDE.md` から `@` インクルードされ、常時注入される。

---

### `skills/`
複数エージェントをまたぐオーケストレーション手順、およびユーザー向けユーティリティ手順を置く。Claude Code のスラッシュコマンドとしても機能する。

**2種類のスキル:**

| 種別 | 説明 | 例 |
|---|---|---|
| **オーケストレーション skill** | 複数エージェントをまたぐフェーズ構成・受け渡し・TDD サイクル等 | `dev-workflow` / `develop` / `parallel-agents` |
| **ユーティリティ skill** | エージェント起動を伴わないユーザー向け対話型ツール。`disable-model-invocation: true` が目印 | `mcp-config` / `pattern-status` / `recall` |

> **Skill = オーケストレーション手順 または ユーザー向けユーティリティ**（どちらも skills/ に置く）。
> 単一エージェントの作業手順は `agents/` に書く。参照知識は `rules/` に書く。

**ディレクトリ構造:**

```
skills/
  <skill-name>/
    SKILL.md           # メイン指示（必須）
    reference.md       # 詳細なリファレンス資料 — 必要な時だけ Claude が読む
    template.md        # Claude が埋めるテンプレート
    examples/
      sample.md        # 期待する出力の例
    scripts/
      helper.py        # SKILL.md から Bash で呼ぶ CLI ヘルパースクリプト
```

- `SKILL.md` は必須。他のファイルは任意のサポートファイル。
- `SKILL.md` はサポートファイルへの参照のみ書き、詳細はそちらに分離する（500行以下推奨）。
- サポートファイルはスキル実行のたびに自動ロードされない。`SKILL.md` で参照された時にのみ読まれる。
- `scripts/` 配下にはこの skill 専用の CLI ヘルパー（例: `review_hint_inject.py` / `record_review_decision.py` のようにスキル本体から Bash 経由で呼ぶスクリプト）を置く。複数 skill が共有する場合は別途集約場所を検討する。

**SKILL.md の YAML フロントマター（有効なキー）:**

| フィールド | 説明 |
|---|---|
| `name` | スキルの表示名。省略時はディレクトリ名を使用 |
| `description` | スキルの説明。Claude が自動呼び出しするかどうかの判断に使う（推奨） |
| `when_to_use` | Claude がスキルを呼び出すべき追加コンテキスト（`description` に追記される） |
| `argument-hint` | オートコンプリート中に表示される引数ヒント（例: `[issue-number]`） |
| `arguments` | `$name` 置換用の名前付き位置引数（スペース区切り文字列または YAML リスト） |
| `disable-model-invocation` | `true` にすると Claude による自動呼び出しを禁止。`/name` 手動呼び出し専用になる |
| `user-invocable` | `false` にすると `/` メニューから非表示。Claude のみ呼び出せるバックグラウンド知識用 |
| `allowed-tools` | このスキルがアクティブな時に承認なしで使えるツールのリスト |
| `model` | このスキルがアクティブな時のモデル上書き |
| `effort` | このスキルがアクティブな時の努力レベル（`low` / `medium` / `high` / `xhigh` / `max`） |
| `context` | `fork` を設定するとサブエージェントで分離実行 |
| `agent` | `context: fork` 時に使用するサブエージェントタイプ |
| `hooks` | このスキルのライフサイクルにスコープされたフック |
| `paths` | スキルを自動有効化するパスの Glob パターン（`rules/` の `paths` と同じ形式） |
| `shell` | `!`command`` の実行に使うシェル（`bash` または `powershell`） |

**文字列置換（SKILL.md 内で使用可能）:**

| 変数 | 説明 |
|---|---|
| `$ARGUMENTS` | スキル呼び出し時に渡された全引数 |
| `$ARGUMENTS[N]` / `$N` | N番目の引数（0始まり） |
| `$name` | `arguments` フロントマターで宣言した名前付き引数 |
| `${CLAUDE_SESSION_ID}` | 現在のセッション ID |
| `${CLAUDE_EFFORT}` | 現在の努力レベル |
| `${CLAUDE_SKILL_DIR}` | このスキルの `SKILL.md` があるディレクトリの絶対パス |

**動的コンテキスト注入:**

`` !`command` `` 構文でスキルコンテンツが Claude に送信される前にシェルコマンドを実行し、出力をインライン展開できる。

```yaml
---
description: Summarize uncommitted changes
---

## Current diff
!`git diff HEAD`

Summarize the changes above.
```

- スキルは `Skill` ツールの `skill` パラメータや `/skill-name` スラッシュコマンドで呼び出す。
- `promoted-YYYYMMDD-{id}/` のようなサブフォルダに昇格スキルを置くと Claude Code が自動検出する。

---

### `hooks/`
Claude Code のライフサイクルイベントに登録される自動実行スクリプト、およびそのヘルパーモジュールを置く。

**2種類のファイル:**

| 種別 | 説明 | 例 |
|---|---|---|
| **イベントフック** | Claude Code のライフサイクルイベントに登録し自動実行。`settings.json` の `hooks` セクションに登録必須 | `session_start.py` / `pre_tool.py` / `post_tool.py` |
| **Hook ワーカー** | イベントフックから `importlib` 等で内部呼び出しされるヘルパーモジュール。単独でイベント登録しない | `stop.py` / `consolidate_memory.py` / `session_utils.py` |
| **リソースファイル** | フックスクリプトが参照する DDL・設定ファイルなど。Python スクリプトではなく hooks/ に同居するが、イベント登録も内部呼び出しもしない | `schema.sql` |

対応イベント: `SessionStart` / `PreToolUse` / `PostToolUse` / `Stop` / `PreCompact` / `PermissionRequest` / `UserPromptSubmit` 等

> Python スクリプト（`.py`）で実装する。イベントフックの登録は `settings.json` の `hooks` セクションで行う。
> スキルが Bash 経由で呼ぶ CLI ヘルパーは `hooks/` ではなく **`skills/<name>/scripts/`** に置く（taxonomy 上 hook ではないため）。

---

### `docs/`
人間が読むリファレンスドキュメントを置く。

- フレームワークの設計判断記録
- タクソノミー定義（このファイル）
- 操作手順書・セットアップガイド

> **エージェントは読まなくてよい**。人間向け専用。
> エージェントが参照する必要のある情報は `rules/` または `agents/` に置くこと。

---

### `memory/`
セッションをまたいだ記憶を置く。

- セッションファイル（`sessions/*.tmp`）— Stop フックがスケルトンを自動生成し、タイムスタンプ・最終応答を追記する。本文（`## 残タスク` `## うまくいったアプローチ` `## 試みたが失敗したアプローチ`）は LLM がスキル（dev-workflow / init-session 等）の指示に従って更新する
- パターンDB（`patterns.json`）— Stop フックが信頼スコアと昇格候補フラグを管理。LLM は session.tmp 末尾の JSON ブロックに観測を記録するのみ

> ユーザーは原則として手動編集しない。Hook が骨格を、LLM が内容を更新する責務分担になっている。

---

### `reports/`
エージェントが出力するレポートを置く。

- `requirements-report-YYYYMMDD-HHMMSS.md` — ヒアリング結果
- `architecture-report-YYYYMMDD-HHMMSS.md` — 設計結果
- `plan-report-YYYYMMDD-HHMMSS.md` — 実装計画
- `code-review-report-YYYYMMDD-HHMMSS.md` — コードレビュー結果
- `security-review-report-YYYYMMDD-HHMMSS.md` — セキュリティレビュー結果
- `test-report-YYYYMMDD-HHMMSS.md` — テスト結果
- `archive/` — 完了したサイクルのレポートをアーカイブするサブフォルダ

> タイムスタンプはレポートファイル名に使用する。生成時は `report-timestamp` スキルで取得すること。

---

### `tmp/`（暗黙の作業領域）

エージェントが一時的に使うスクラッチスペース。

- エージェントが Write ツールで中間ファイルを置く
- hooks（Stop イベント）がセッション終了時にクリーンアップする

> `.gitignore` 対象。セッションをまたいで保持しない。

---

### `output-styles/`（Claude Code ネイティブ機能）

Claude のシステムプロンプトを丸ごと差し替えるスタイル定義を置く。CLAUDE.md や rules/ とは異なり、**コーディング向けデフォルトシステムプロンプトそのものを置換**できる。

**配置場所:**
- `~/.claude/output-styles/` — 全プロジェクト共通（ユーザーレベル）
- `.claude/output-styles/` — このプロジェクトのみ

**ファイル形式（Markdown + YAML フロントマター）:**

```markdown
---
name: My Style
description: /config ピッカーに表示される説明
keep-coding-instructions: false
---

You are an interactive CLI tool that helps users with...
（ここがシステムプロンプトに追加される内容）
```

**フロントマターキー:**

| キー | 説明 | デフォルト |
|---|---|---|
| `name` | スタイルの表示名。省略時はファイル名を使用 | ファイル名 |
| `description` | `/config` ピッカーに表示される説明 | なし |
| `keep-coding-instructions` | `true` にするとコーディング向け指示を残す | `false` |

**適用方法:**
- `/config` → Output style メニューから選択
- `settings.local.json` に `"outputStyle": "スタイル名"` を記述

**組み込みスタイル:** `Default` / `Explanatory`（教育的インサイト付き）/ `Learning`（協調学習モード）

> CLAUDE.md はユーザーメッセージとして追記。`--append-system-prompt` はシステムプロンプトに追記。output-styles はシステムプロンプト本体を置換。用途が根本的に異なる。
> 変更は次のセッション開始時に有効になる（プロンプトキャッシュの安定性のため）。

---

### `plugins/`（Claude Code ネイティブ機能）

skills / agents / hooks / MCP サーバー等をひとまとめにしてチームや外部に配布できる拡張パッケージ。`.claude-plugin/plugin.json` マニフェストを持つディレクトリがプラグイン単位。

**ディレクトリ構造:**

```
my-plugin/
├── .claude-plugin/
│   └── plugin.json       # マニフェスト（name, description, version, author）
├── skills/               # スキル定義（/plugin-name:skill-name で呼び出す）
│   └── skill-name/
│       └── SKILL.md
├── commands/             # スキルのレガシー形式（新規は skills/ を使う）
├── agents/               # カスタムエージェント定義
├── hooks/
│   └── hooks.json        # フック定義（settings.json の hooks と同じ形式）
├── .mcp.json             # MCP サーバー設定
├── .lsp.json             # LSP サーバー設定（コードインテリジェンス）
├── monitors/
│   └── monitors.json     # バックグラウンドモニター設定
├── bin/                  # Bash ツールの PATH に追加される実行可能ファイル
└── settings.json         # プラグイン有効時に適用されるデフォルト設定
```

> `skills/` / `agents/` / `hooks/` 等は `.claude-plugin/` 内ではなく**プラグインルート直下**に置く。

**plugin.json の主なキー:**

| キー | 説明 |
|---|---|
| `name` | プラグイン識別子。スキル名前空間になる（例: `/my-plugin:hello`） |
| `description` | プラグインマネージャーに表示される説明 |
| `version` | 省略時は git コミット SHA を使用。更新通知の判定に使われる |
| `author` | 属性情報 |

**テスト方法:**
```bash
claude --plugin-dir ./my-plugin          # ローカルディレクトリ
claude --plugin-url https://example.com/plugin.zip  # ZIP アーカイブ
```

**プラグイン内でのリロード:** `/reload-plugins` で再起動なしに反映。

**スタンドアロン（`.claude/`）との違い:**

| | スタンドアロン | プラグイン |
|---|---|---|
| スキル名 | `/hello` | `/plugin-name:hello` |
| 共有 | 手動コピー | `/plugin install` |
| 適用範囲 | そのプロジェクトのみ | インストール先全体 |

---

## 配置判断チートシート

```
書きたい内容は...
  │
  ├─ 手順・ワークフロー？
  │   ├─ 複数エージェントをまたぐオーケストレーション → skills/（orchestration skill）
  │   ├─ ユーザー向け対話型ユーティリティ（エージェント起動なし）→ skills/（utility skill）
  │   └─ 単一エージェントの作業手順・ペルソナ → agents/
  │
  ├─ 知識・制約？
  │   ├─ 常時適用 → rules/
  │   └─ 特定パスにのみ適用 → rules/（paths フロントマター）
  │
  ├─ 自動実行スクリプト？
  │   ├─ Claude Code イベントに登録するフック → hooks/（settings.json に登録）
  │   └─ フックから内部呼び出しされるヘルパー → hooks/（co-location 許容）
  │
  ├─ スキルから Bash で呼ぶ CLI ヘルパー？
  │   └─ skills/<name>/scripts/（その skill 専用 CLI スクリプト）
  │
  ├─ レポート出力？
  │   └─ reports/
  │
  └─ 人間向けドキュメント？
      └─ docs/
```
