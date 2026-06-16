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
c3 init [--force] [--platform claude|codex|cursor|opencode|all]
```

| オプション | 内容 |
|---|---|
| `--force` | 既存ファイルを上書きする（通常はスキップ） |
| `--platform` | 生成対象。既定は `claude`。`codex` は `AGENTS.md` / `.agents/skills/` / `.codex/`、`cursor` は `.cursor/`、`opencode` は `AGENTS.md` / `.opencode/agents/` を追加 |

## `c3 update`

`.claude/` と adapter 生成物をパッケージ最新版へ更新する。個人ファイル（`reports/`, `memory/sessions/` 等）はスキップ。

```bash
c3 update [--dry-run] [--platform claude|codex|cursor|opencode|all]
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
c3 doctor [--platform claude|codex|cursor|opencode|all]
```

確認項目:
- `.claude/` ディレクトリの存在
- `settings.json` の有効性
- `claude` バイナリのパス
- Codex adapter: `AGENTS.md`, `.agents/skills/`, `.codex/config.toml`, `.codex/agents/`
- Cursor adapter: `.cursor/rules/c3-core.mdc`, `.cursor/mcp.json`
- OpenCode adapter: `AGENTS.md`, `.opencode/agents/`

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

過去のセッション・エージェント学習データ・レポートアーカイブ・パターンを numpy ベクトル検索 + 多言語 embedding で意味検索する。fastembed + `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`（384 次元、約 220MB、Apache-2.0）を使用。

```bash
c3 recall search "<query>" [--top N] [--source SOURCE] [--min-score F] [--json]
c3 recall "<query>"                 # search の省略形
c3 recall rebuild [--force]         # numpy ベクトル検索インデックスを再構築
c3 recall stats [--json]            # チャンク数・モデル名・最終 rebuild 日時を表示
```

| サブコマンド | 主なオプション | 内容 |
|---|---|---|
| `search` | `--top` (既定 5) / `--source` (sessions/agent-memory/reports/patterns/all) / `--min-score` (既定 0.3) / `--json` | 類似チャンク上位 N 件を返却 |
| `rebuild` | `--force` / `--source` | 全ソースを再 embedding し numpy ベクトル検索インデックスを atomic write |
| `stats` | `--json` | チャンク数・ソース別内訳・モデル名・index ファイルサイズ |

初回 `c3 recall rebuild` 時に fastembed がモデル（~220MB）を `~/.cache/fastembed/` にダウンロードする。オフライン環境では `FASTEMBED_CACHE_PATH` を社内ミラーに向ける。

検索時、元データソースの mtime が index ファイルより新しい場合は stderr に `[recall] WARN: index is older ...` を出力。`UserPromptSubmit` hook が起動された場合は親 Claude に AskUserQuestion で rebuild 確認を促す指示が注入される。

`/recall` Skill を Claude Code から呼び出すと同等の検索を LLM 自律で実行できる。

## `c3 tier stats` — Tier ルーティング統計

tier-routing の効果計測用 CLI。`.claude/state/c3.db` の `tier_bandit` / `tier_recent_outcomes` / `agent_cost_runs` を可視化。

```bash
c3 tier stats                 # 累積 + 直近 outcome + コストを表形式表示
c3 tier stats --json          # JSON 出力
c3 tier stats --recent N      # 直近 outcome の表示件数（デフォルト 10）
```

表示内容:

- **学習データ収集状況**（X / 30 試行 + uniform/thompson モード）
- **Tier 別累積**（complexity × tier × alpha / beta / trials / 期待成功率 / 累積コスト `total_cost_usd` / `cost_samples`（v2.25.0〜））
- **直近 outcome 履歴**（時系列降順、success/failure ラベル）
- **Agent 別コスト集計**（agent_cost_runs・agent_type 別の runs / USD / トークン内訳。v2.21.0〜）
- **Tier 別コストレート**（complexity × tier の USD/MTok レート。model 一致集計。v2.24.0〜）
- **routing パラメータ**（現在有効な λ / ε / escalation threshold を環境変数名つきで表示。v2.27.0〜）

学習データは dev-workflow フェーズ E（最終承認時）の `record_tier_outcome.py` でのみ記録されます。直接指示作業ではデータが溜まりません（設計通り）。コストデータは session 終了時に `session_stop.py` のセッションログ ingester（v2.21.0〜）が自動集計し、`tier_bandit` への materialize は v2.25.0〜（`sync_tier_bandit_cost`）。

### Tier ルーティングのチューニング（環境変数）

tier-routing の挙動は以下の環境変数で調整できます。**すべて未設定の場合は安定動作する既定値**で動き、設定は任意です（不正値は警告を出して既定値にフォールバック）。

| 環境変数 | 既定 | 範囲 | 役割 |
|---|---|---|---|
| `C3_TIER_COST_LAMBDA` | 未設定（cost-aware tie-break のみ） | `0 ≤ λ ≤ 5`（v2.27.0〜・v2.26.0 は `≤ 1`） | **cost-weighted Thompson の重み係数（v2.26.0〜）**。`λ>0` で全 tier の `score = 成功率サンプル − λ × 正規化コスト` を比較し、成功率とコストをトレードオフして選択。`λ=0` 明示でコスト無視（純 Thompson）。`λ>1` でコストを成功率より強く効かせられる（v2.27.0 で上限を 5 に拡張）。**未設定時は v2.25.0 と同じ「成功率が拮抗した群でのみ低コストを選ぶ」挙動**。 |
| `C3_TIER_EPSILON` | `0.05` | `0 < x ≤ 1` | tie-break の拮抗判定閾値（v2.25.0〜）。最大サンプルからこの差以内の Tier を「拮抗」とみなす。 |
| `C3_ESCALATION_THRESHOLD` | `0.5` | `0 < x ≤ 1` | failure-rate がこの値以上で 1 段上位 Tier へ昇格する閾値（v2.26.0〜）。 |

`λ` を大きくするほど安価な Tier が選ばれやすくなり（成功率を犠牲にしうる）、小さいほど成功率優先になります。cost-weighted 発動時は `tier_selection.json` と親 Claude 注入コンテキストに `cost_weighted` / `cost_lambda` が記録されます。

## CLI で扱われない項目

以下は Claude Code 内（スラッシュコマンド）で扱う領域:

- `/init-session` / `/setup` / `/start` / `/develop` / `/review-phase` / `/promote-pattern` / `/doc` / `/mcp-config` / `/extract-lib` / `/recall`
- 詳細は [スキル一覧](skills.md) を参照

Codex では `.agents/skills/` に生成された `$start` などの skills と `.codex/agents/` の custom agents を使う。Cursor では `.cursor/rules/c3-core.mdc` が `.claude/skills/` と `.claude/agents/` を参照する。OpenCode では `.opencode/agents/` の `@c3-*`（agent）と `@c3-skill-*`（skill）を `@mention` で起動する。

## 次に読むページ

- [はじめに](getting-started.md) — インストールから初回セッション
- [スキル一覧](skills.md) — Claude Code 側のスラッシュコマンド
