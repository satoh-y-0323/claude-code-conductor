# Platform Adapters

C3 は **Claude Code** を canonical platform として設計されているが、
`c3 init --platform` で Codex / Cursor / OpenCode 向けの adapter を生成し、同じ `.claude/`
ツリーを共通の source of truth として利用できる。

本ドキュメントは:
- `c3 init --platform` の選択肢と生成物
- MCP server と `c3 ask` CLI fallback
- managed block の仕様と手動編集の取り扱い
- 動作差分のまとめ

を一箇所にまとめたリファレンス。実装は `src/c3/adapters.py` / `src/c3/mcp_server.py` /
`src/c3/cli_ask.py` / `src/c3/platforms.py` / `src/c3/cli_doctor.py` を参照。

---

## 1. `c3 init --platform` の選択肢

| 値 | 生成物 | 用途 |
|---|---|---|
| `claude` (デフォルト) | `.claude/` のみ | Claude Code ネイティブ |
| `codex` | `.claude/` + `AGENTS.md` + `.codex/` + `.agents/` | Codex CLI / 職場 Codex |
| `cursor` | `.claude/` + `.cursor/` | Cursor IDE |
| `opencode` | `.claude/` + `AGENTS.md` + `.opencode/` | OpenCode |
| `all` | 上記すべて | マルチプラットフォーム共存 |

`c3 init --platform claude` 以外でも `.claude/` は canonical source として残り、
adapter は派生ファイルを生成するのみ（`.claude/` の中身を変更しない）。

`c3 update --platform <p>` で adapter を再生成できる。`.claude/` 側の skill / agent
を編集したら adapter も再生成して整合を取る。

---

## 2. 生成される具体的ファイル

### `codex` 選択時

| パス | 内容 | 生成ロジック |
|---|---|---|
| `AGENTS.md` | プロジェクト直下。Codex に C3 workflow の存在と adapter を伝える | `_codex_agents_section` を managed block で挿入 |
| `.codex/config.toml` | Codex CLI の MCP 設定 | `[mcp_servers.c3]` セクションを managed block で挿入 |
| `.codex/agents/<name>.toml` | `.claude/agents/<name>.md` から生成した Codex subagent 定義 | `_codex_agent_toml` で TOML 化 |
| `.agents/skills/<name>/SKILL.md` | `.claude/skills/<name>/SKILL.md` を Codex 向けに変換コピー | `_convert_skill` で frontmatter 正規化 + adapter note 追加 |
| `.agents/skills/<name>/<その他>` | `.claude/skills/<name>/` 配下の非 `SKILL.md` ファイルをそのまま複製 | `_copy_file_if_changed` |

### `cursor` 選択時

| パス | 内容 | 生成ロジック |
|---|---|---|
| `.cursor/rules/c3-core.mdc` | Cursor の rule。`alwaysApply: true` で C3 workflow を常時参照 | `_cursor_core_rule` の静的テキスト |
| `.cursor/mcp.json` | Cursor の MCP サーバー設定。`mcpServers.c3` のみ管理対象 | `_write_cursor_mcp` で既存 JSON にマージ |

### `opencode` 選択時

| パス | 内容 | 生成ロジック |
|---|---|---|
| `AGENTS.md` | プロジェクト直下。OpenCode に C3 workflow の存在と `@c3-*` agent の使い方を伝える | `_opencode_agents_section` を managed block で挿入（Codex とは別マーカー） |
| `.opencode/agents/c3-<name>.md` | `.claude/agents/<name>.md` から生成した OpenCode agent 定義 | `_opencode_agent_md`。`interviewer`/`architect`/`planner` は `mode: all-purpose`、他は `mode: subagent` |
| `.opencode/agents/c3-skill-<name>.md` | `.claude/skills/<name>/SKILL.md` を OpenCode agent として変換 | `_skill_to_opencode_agent_md`。`mode: all-purpose` 固定 |

> OpenCode adapter は MCP 設定ファイルを生成しない。`AskUserQuestion` は OpenCode 上でユーザーに直接確認する方式（`AGENTS.md` の adapter 指示で `multiSelect: true` を維持）。

---

## 3. MCP server (`c3.mcp_server`)

stdio MCP サーバー。Codex / Cursor から `.codex/config.toml` / `.cursor/mcp.json`
経由で起動される。プロトコル: JSON-RPC 2.0 (`2025-11-25`)。

### 提供ツール

| ツール名 | 用途 | 入力 |
|---|---|---|
| `c3_ask_user_question` | `AskUserQuestion` 互換の単一/複数選択。MCP elicitation を使用 | `payload`: `AskUserQuestion` の JSON |
| `c3_list_skills` | `.claude/skills/` 配下のスキル一覧を返す | なし |
| `c3_read_skill` | `.claude/skills/<name>/SKILL.md` を読み込む | `name`: skill 名 |

### セキュリティ

- `c3_read_skill` は `.claude/skills/` 外への symlink を `resolved.parents` チェックで拒否
- パス区切りに `.` / `..` を含む name は弾く
- skill ファイル名は `SKILL.md` で固定（任意ファイル読み出しはできない）

### 起動コマンド

```bash
python -m c3.mcp_server
```

`C3_PROJECT_ROOT` 環境変数でプロジェクトルートを指定可能（adapter 生成時に自動設定）。
ソースインストール時は `PYTHONPATH` も adapter が `<repo>/src` に設定する。

生成される `.codex/config.toml` / `.cursor/mcp.json` の `command` は `python` ではなく、
adapter 生成時点の `sys.executable`（絶対パス）を書き込む。理由は「7. 既知の制限」を参照。

---

## 4. `c3 ask` CLI fallback

MCP elicitation が使えない環境（CI / 非対応 host）向けの fallback。

```bash
# ファイルから AskUserQuestion JSON を読み、対話で回答
c3 ask --file question.json

# JSON を直接渡す
c3 ask --json '{"questions":[...]}'

# 非対話実行（CI 用）
c3 ask --file question.json --response "Plan,Review"
```

回答は JSON で stdout に出力されるため、シェルパイプで次のステップに渡せる。

---

## 5. managed block

adapter は **管理範囲を明示するマーカー** で囲んだブロックのみを書き換え、
ユーザーが追記した他の部分には触らない。

### Markdown (`AGENTS.md`)

```
<!-- BEGIN C3 CODEX ADAPTER -->
... (adapter が管理する内容)
<!-- END C3 CODEX ADAPTER -->
```

### TOML (`.codex/config.toml`)

```
# BEGIN C3 CODEX ADAPTER
[mcp_servers.c3]
...
# END C3 CODEX ADAPTER
```

### JSON (`.cursor/mcp.json`)

managed block は使わず、`mcpServers.c3` キーだけを上書き。他のサーバー定義は保持。

### 手動編集の取り扱い

- **managed block の外は自由に編集 OK**: adapter は触らない
- **managed block の中は再生成で上書き**: `c3 update --platform codex` で `.claude/` 側の変更が反映される
- **managed block を削除した場合**: 次回 adapter 生成時にファイル末尾に追加される
- **managed block を残したまま中身を書き換えた場合**: `c3 update --platform codex` で上書きされる
- **`[mcp_servers.c3]` を managed block の外で定義した場合**: 競合検出して `ValueError` を投げる（`adapters.py:_write_codex_config`）

---

## 6. 動作差分まとめ

| 機能 | Claude Code | Codex | Cursor | OpenCode |
|---|---|---|---|---|
| `AskUserQuestion` | ネイティブツール | MCP `c3_ask_user_question` / fallback `c3 ask` | 同左 | `AGENTS.md` の指示でユーザーに直接確認（`multiSelect` 維持） |
| `Agent` ツール | ネイティブ subagent | `.codex/agents/<name>.toml` 経由の subagent | runtime に subagent 機構があれば使用、無ければ同一 agent 内でフェーズ実行 | `.opencode/agents/c3-<name>.md` を `@mention` で起動 |
| `Skill` ツール / `/<skill>` | ネイティブ | `.agents/skills/<name>/SKILL.md` を読む | `.claude/skills/<name>/SKILL.md` を rule から指示 | `.opencode/agents/c3-skill-<name>.md`（本文に `.claude/skills/<name>/SKILL.md` を埋め込み） |
| `isolation: worktree` | サポート | 一部 Codex runtime で対応、不可時は同一 worktree 実行 | 反映されない | 反映されない |
| `permissionMode` | サポート | 概念なし（無視） | 概念なし（無視） | 概念なし（無視） |
| `tools` 制限 | サポート | Codex subagent のツール制限に部分対応 | rule テキスト内で補完 | 全 agent に `bash/read/edit/write/websearch` を付与 |
| `hooks` (lifecycle) | サポート | 非対応（無視） | 非対応（無視） | 非対応（無視） |
| `memory` (`MEMORY.md` 注入) | サポート | `.claude/agent-memory/` を共通参照 | 同左 | 同左 |
| パターン昇格 (`/promote-pattern`) | ネイティブ | `c3 init --platform codex` 後は `.agents/skills/promote-pattern/SKILL.md` を読んで実行 | rule から `.claude/skills/promote-pattern/SKILL.md` を指示 | `.opencode/agents/c3-skill-promote-pattern.md` を `@mention` |
| レポート (`.claude/reports/`) | 共通 | 共通 | 共通 | 共通 |
| state (`.claude/state/`) | 共通 | 共通 | 共通 | 共通 |

レポート・state・memory のファイル名と書き込み先は全プラットフォーム共通。これにより
Claude Code / Codex / Cursor / OpenCode が同じプロジェクトを行き来しても workflow 状態が保たれる。

---

## 7. 既知の制限

- **`AskUserQuestion` の `multiSelect: true`**: MCP elicitation の仕様上、ホストが multi-select UI に対応していない場合は単一選択に degrade される。`c3 ask --file` の fallback は multi-select に対応
- **Cursor の subagent**: 2026-05 時点で Cursor は dedicated subagent 機構が限定的。adapter は「同一 agent でフェーズ実行・レポート契約を維持」を方針とする
- **Codex の `isolation: worktree`**: Codex 側の subagent runtime に依存。worktree 非対応の場合は無視される
- **OpenCode の `AskUserQuestion`**: OpenCode adapter は MCP 設定を生成しないため、`c3_ask_user_question` ツールは使わず `AGENTS.md` の指示に従ってユーザーへ直接確認する。`multiSelect: true` は「複数選択の質問」として維持するよう指示済み
- **OpenCode の `tools` 制限**: 生成される agent には一律で `bash/read/edit/write/websearch` を付与する。Claude 側 frontmatter の細かな `tools` 制限は反映されない（C3 の `code-reviewer` / `security-reviewer` は Claude 側でもレポート出力のため `write` を持つ）
- **改行コード**: adapter 生成ファイルは LF 改行で書き出す（`newline="\n"`）。`.gitattributes` で `eol=lf` を固定済み
- **Codex の trust 要件**: プロジェクトが Codex に trust されていない場合、`.codex/config.toml` は**エラーなしで無視**される（Codex 公式仕様）。MCP サーバーが読み込まれず `AskUserQuestion` が動かない場合は、まず対象プロジェクトを Codex 側で trust 済みか確認する（Codex CLI: 初回起動時のプロンプトで trust するか、設定で許可済みディレクトリに追加する）
- **MCP `command` の絶対パス化**: adapter が書き込む `command` は `python` ではなく `sys.executable`（adapter 生成時の絶対パス）を使う。理由:
  - macOS 12.3 以降、無印 `python` は標準に存在しない（`python3` のみ。Homebrew も同様）。`command: "python"` のままだと Codex が MCP spawn 時に program not found となり、「起動時にエラーで読み込まれない」症状になる
  - `python` が PATH 上にあっても、pipx / venv などの隔離環境では `No module named c3` で起動失敗することがある
  - GUI 起動の Codex（Desktop / IDE 拡張）はシェルの PATH を継承しないことがある（最小 PATH）
  - 代案の「POSIX では `python3` にする」は不採用: 無印 python 不在の問題しか救えず、pipx 隔離や GUI PATH 最小の問題には無力なため
  - `c3 doctor --platform codex|cursor` は生成済み config から `command` を読み取り、実行可能か・`<command> -c "import c3"` が成功するかを自己診断する
- **adapter 生成物はマシン固有**: `command` に埋め込む `sys.executable` は生成したマシン・環境（venv / pipx / システム Python）に依存する絶対パスであるため、adapter 生成物（`.codex/config.toml` / `.cursor/mcp.json` など）はチームで共有せず、各マシンで `c3 init --platform <p>` または `c3 update --platform <p>` を実行して再生成する運用とする
