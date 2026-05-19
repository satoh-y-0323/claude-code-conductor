# CLI リファレンス

`pip install claude-code-conductor` で同時にインストールされるターミナルコマンド `c3` のリファレンス。

## グローバル

```bash
c3 --version    # バージョン表示
c3 --help       # サブコマンド一覧
```

## `c3 init`

利用先プロジェクトに `.claude/` を展開する。Codex/Cursor adapter を指定した場合も、`.claude/` は C3 の canonical source として展開される。

```bash
c3 init [--force] [--platform claude|codex|cursor|all]
```

| オプション | 内容 |
|---|---|
| `--force` | 既存ファイルを上書きする（通常はスキップ） |
| `--platform` | 生成対象。既定は `claude`。`codex` は `AGENTS.md` / `.agents/skills/` / `.codex/`、`cursor` は `.cursor/` を追加 |

## `c3 update`

`.claude/` と adapter 生成物をパッケージ最新版へ更新する。個人ファイル（`reports/`, `memory/sessions/` 等）はスキップ。

```bash
c3 update [--dry-run] [--platform claude|codex|cursor|all]
```

| オプション | 内容 |
|---|---|
| `--dry-run` | 変更内容のプレビューのみ（実際には更新しない） |
| `--platform` | 更新対象。既定は `claude` |

## `c3 list-agents` / `c3 list-skills`

設置済みエージェント・スキルを一覧表示する。

```bash
c3 list-agents
c3 list-skills
```

## `c3 doctor`

環境診断を実行する。

```bash
c3 doctor [--platform claude|codex|cursor|all]
```

確認項目:
- `.claude/` ディレクトリの存在
- `settings.json` の有効性
- `claude` バイナリのパス
- Codex adapter: `AGENTS.md`, `.agents/skills/`, `.codex/config.toml`, `.codex/agents/`
- Cursor adapter: `.cursor/rules/c3-core.mdc`, `.cursor/mcp.json`

## `c3 ask`

Claude Code の `AskUserQuestion` 互換 schema を、Codex/Cursor adapter やターミナル fallback から利用する。

```bash
c3 ask --file question.json
c3 ask --json '{"questions":[...]}' --response 1,3
```

| オプション | 内容 |
|---|---|
| `--file` / `--json` | `AskUserQuestion` と同じ `{ "questions": [...] }` 形式 |
| `--response` | 非対話実行用。ラベルまたは 1 始まりの番号を指定。複数質問は `;` で区切る |
| `--pretty` | JSON 出力を整形 |

## `c3 plan` — plan-report 検証 / wave 分解

YAML フロントマター付き `plan-report-*.md` の検証と wave 分解を行う。`parallel-agents` skill が内部で利用する純粋ユーティリティ。

```bash
c3 plan validate <plan-report>        # YAML フロントマターと agent 存在確認
c3 plan waves    <plan-report>        # wave 分解結果を JSON 出力
```

| サブコマンド | exit code | 内容 |
|---|---|---|
| `validate` | 0 / 2 | 0=妥当、2=不正（フロントマター・agent ファイル不在・循環依存等） |
| `waves` | 0 | 標準出力に wave ごとのタスク配列を JSON で出力 |

> v1.14.0 までの `c3 po dry-run` / `c3 po waves` は `c3 plan validate` / `c3 plan waves` で置き換えられた。v2.0.0 で `c3 po` サブコマンド全体を削除。

## `c3 recall` — 意味検索 (v2.10.0+)

過去のセッション・エージェント学習データ・レポートアーカイブ・パターンを HNSW + 多言語 embedding で意味検索する。fastembed + `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`（384 次元、約 220MB、Apache-2.0）を使用。

```bash
c3 recall search "<query>" [--top N] [--source SOURCE] [--min-score F] [--json]
c3 recall "<query>"                 # search の省略形
c3 recall rebuild [--force]         # HNSW インデックスを再構築
c3 recall stats [--json]            # チャンク数・モデル名・最終 rebuild 日時を表示
```

| サブコマンド | 主なオプション | 内容 |
|---|---|---|
| `search` | `--top` (既定 5) / `--source` (sessions/agent-memory/reports/patterns/all) / `--min-score` (既定 0.3) / `--json` | 類似チャンク上位 N 件を返却 |
| `rebuild` | `--force` / `--source` | 全ソースを再 embedding し HNSW インデックスを atomic write |
| `stats` | `--json` | チャンク数・ソース別内訳・モデル名・index ファイルサイズ |

初回 `c3 recall rebuild` 時に fastembed がモデル（~220MB）を `~/.cache/fastembed/` にダウンロードする。オフライン環境では `FASTEMBED_CACHE_PATH` を社内ミラーに向ける。

検索時、元データソースの mtime が index ファイルより新しい場合は stderr に `[recall] WARN: index is older ...` を出力。`UserPromptSubmit` hook が起動された場合は親 Claude に AskUserQuestion で rebuild 確認を促す指示が注入される。

`/recall` Skill を Claude Code から呼び出すと同等の検索を LLM 自律で実行できる。

## `c3 tier stats` — Tier ルーティング統計

tier-routing の効果計測用 CLI。`.claude/state/c3.db` の `tier_bandit` / `tier_recent_outcomes` を可視化。

```bash
c3 tier stats                 # 累積 + 直近 outcome を表形式表示
c3 tier stats --json          # JSON 出力
c3 tier stats --recent N      # 直近 outcome の表示件数（デフォルト 10）
```

表示内容:

- **学習データ収集状況**（X / 30 試行 + uniform/thompson モード）
- **Tier 別累積**（complexity × tier × alpha / beta / trials / 期待成功率）
- **直近 outcome 履歴**（時系列降順、success/failure ラベル）

学習データは dev-workflow フェーズ E（最終承認時）の `record_tier_outcome.py` でのみ記録されます。直接指示作業ではデータが溜まりません（設計通り）。

## CLI で扱われない項目

以下は Claude Code 内（スラッシュコマンド）で扱う領域:

- `/init-session` / `/setup` / `/start` / `/develop` / `/code-review` / `/promote-pattern` / `/doc` / `/mcp-config` / `/extract-lib` / `/recall`
- 詳細は [スキル一覧](skills.md) を参照

Codex では `.agents/skills/` に生成された `$start` などの skills と `.codex/agents/` の custom agents を使う。Cursor では `.cursor/rules/c3-core.mdc` が `.claude/skills/` と `.claude/agents/` を参照する。

## 次に読むページ

- [はじめに](getting-started.md) — インストールから初回セッション
- [スキル一覧](skills.md) — Claude Code 側のスラッシュコマンド
