# はじめに

C3 を初めて使う方向けのガイド。インストールから最初のセッション完了まで進めます。

## 前提条件

- Claude Code で使う場合: [Claude Code](https://claude.ai/code) がインストール済みでログインしていること
- Codex で使う場合: Codex CLI または IDE extension が利用できること
- Cursor で使う場合: Cursor editor または `cursor-agent` が利用できること
- Python **3.10 以上** がインストール済みであること
- Git が使えること

## インストール

### 推奨: PyPI から

```bash
pip install claude-code-conductor
```

これで `c3` CLI と `.claude/` テンプレートが入ります。v2.0.0 以降は Claude Code の Agent ツール並列起動と `isolation: "worktree"` を使うため、別途のプロセスは不要です。

### 代替: git clone から

```bash
git clone https://github.com/satoh-y-0323/claude-code-conductor.git
```

クローンしたリポジトリの `.claude/` をプロジェクトにコピーするか、`pip install -e .` で開発モードで利用できます。

## プロジェクトに `.claude/` を展開

既存プロジェクトのルートで:

```bash
cd /path/to/your-project
c3 init
```

パッケージ同梱の `.claude/` テンプレートがカレントディレクトリへ展開されます。プロジェクトの `src/` 等の既存コードには一切触れません。

| 変更される場所 | 内容 |
|---|---|
| `.claude/` ディレクトリ（追加） | C3 のフレームワーク一式 |
| `.gitignore`（追記推奨） | `.claude/reports/`・`.claude/memory/sessions/` 等の個人作業ファイルを除外 |

後日 C3 を更新したくなったら:

```bash
pip install --upgrade claude-code-conductor
c3 update
```

`c3 update` はパッケージ最新版へ差分のみ反映します（`reports/` や `memory/sessions/` 等の個人ファイルは保持されます）。

## Codex / Cursor adapter を追加

Claude Code の使い方は従来通りです。Codex/Cursor でも同じ C3 状態を使いたい場合だけ、明示的に adapter を生成します。

```bash
c3 init --platform codex
c3 init --platform cursor
# 両方まとめて追加する場合
c3 init --platform all
```

生成後も `.claude/` が C3 の canonical source です。

| platform | 追加される主なファイル |
|---|---|
| `codex` | `AGENTS.md`, `.agents/skills/`, `.codex/config.toml`, `.codex/agents/` |
| `cursor` | `.cursor/rules/c3-core.mdc`, `.cursor/mcp.json` |

Codex/Cursor adapter は、C3 の `AskUserQuestion` JSON を MCP tool `c3_ask_user_question` に渡して単一選択・複数選択を維持します。Claude Code の `Agent` / `Skill` tool 前提は、Codex では `.codex/agents/` と `.agents/skills/`、Cursor では `.cursor/rules/c3-core.mdc` 経由で読み替えます。MCP elicitation が使えない環境では、fallback として `c3 ask --file question.json` を使えます。

## 初回セッション

プロジェクトを Claude Code で開き、以下のスラッシュコマンドを順に実行します。

### 1. `/init-session` — セッション初期化

セッション開始時に必ず実行します。前回の作業状態・残タスク・昇格候補パターンを確認できます。

### 2. `/setup` — プロジェクト規約を設定（初回のみ）

```
/setup
```

技術スタック・コーディング規約をヒアリングし、以下のファイルを自動生成します:

- `.claude/rules/coding-standards.md`
- `.claude/rules/project-conventions.md`

これらは以降の全セッションで自動的にエージェントへ注入されます。

### 3. `/start` — 開発開始

```
/start
```

開始地点（標準ワークフローの各フェーズ / 実装 / デバッグ調査 / レビュー）を選び、対応する dev-workflow フェーズに遷移します（v2.8.0 以降は `task_type` 概念を廃止し、フェーズを直接選ぶ方式に簡素化）。

## 5 フェーズの開発ワークフロー

`/start` で開発を始めると、以下の 5 フェーズで進みます。

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
フェーズ C へ戻る（内部遷移）
```

各フェーズの移行時にユーザーが**承認・否認・修正**を選択します。フェーズ D・E への遷移は承認後に自動で行われます。

## 終了時の挙動

セッション終了時、`stop.py` フックが自動的に以下を実行します:

- session ファイルの記録時刻を更新
- Claude の最終応答を事実ログに自動記録（次セッションで「前回何をしたか」が分かる）
- `patterns.json` の信用度を再計算
- LLM 要約をバックグラウンドで生成し、次セッションのコンテキストに注入する `llm_summary.md` を更新

## 次に読むページ

- [スキル一覧](skills.md) — 全スキルの一覧
- [CLI リファレンス](cli-reference.md) — `c3` CLI の詳細
