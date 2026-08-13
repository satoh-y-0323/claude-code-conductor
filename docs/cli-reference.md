# CLI リファレンス

`pip install claude-code-conductor` で同時にインストールされるターミナルコマンド `c3` のリファレンス。

## グローバル

```bash
c3 --version    # バージョン表示
c3 --help       # サブコマンド一覧
```

サブコマンド未指定・存在しないサブコマンド名は、argparse が usage を stderr に表示して **exit 2** になる。同様に、各サブコマンドの必須引数の不足・型不正（例: 数値指定オプションへの非数値入力）・不正な選択値も、argparse 自身の引数検証によって usage を stderr に表示して **exit 2** になる（各サブコマンドの解説にある「カスタム検証によるエラーは exit 1」とは別系統であり、以降の exit code 記載は特に断りがない限り argparse 自身の引数検証エラーを含まない）。

## `c3 init`

利用先プロジェクトに `.claude/` を展開する。Codex/Cursor adapter を指定した場合も、`.claude/` は C3 の canonical source として展開される。

```bash
c3 init [--force] [--target DIR] [--platform claude|codex|cursor|opencode|all] [--git | --no-git]
```

| オプション | 内容 |
|---|---|
| `--force` | 既存ファイルを上書きする（通常はスキップ） |
| `--target` | 展開先ディレクトリ。既定はカレントディレクトリ |
| `--platform` | 生成対象。既定は `claude`。`codex` は `AGENTS.md` / `.agents/skills/` / `.codex/`、`cursor` は `.cursor/`、`opencode` は `AGENTS.md` / `.opencode/agents/` を追加 |
| `--git` | 展開先が git 管理外のとき、確認なしで `git init` を実行する（CI / 非対話環境の明示 opt-in） |
| `--no-git` | `git init` を行わない（worktree 並列実装の案内メッセージのみ出力） |

展開先が git 管理外でフラグ未指定の場合、対話ターミナル（TTY）でのみ `git init しますか? [Y/n]` の同意プロンプトを表示する（既定は Y）。非 TTY では `git init` せず案内メッセージのみ出力する。`git init` の成否は `c3 init` の exit code に影響しない。

## `c3 update`

`.claude/` と adapter 生成物をパッケージ最新版へ更新する。個人ファイル（`reports/`, `memory/sessions/` 等）はスキップ。

```bash
c3 update [--dry-run] [--target DIR] [--platform claude|codex|cursor|opencode|all] [--yes]
```

| オプション | 内容 |
|---|---|
| `--dry-run` | 変更内容のプレビューのみ（実際には更新しない） |
| `--target` | 更新先ディレクトリ。既定はカレントディレクトリ |
| `--platform` | 更新対象。既定は `claude` |
| `--yes` / `-y` | 確認プロンプト（旧ファイル削除・MAJOR バージョン更新の承認）をスキップする（CI / 自動化ワークフロー用）。`--dry-run` 併用時は効果なし |

ファイルコピーに加えて、以下の付随処理を行う:

1. **breaking changes の表示**: 前回更新時のバージョン（`.claude/state/c3_version.txt` checkpoint）と今回のパッケージバージョンの間に該当する `breaking-changes.txt` のエントリを表示する。checkpoint が無い初回は全件表示
2. **MAJOR バージョン承認**: MAJOR bump（例: 2.x → 3.x）に該当する breaking changes がある場合は `[y/N]` の承認プロンプトを表示し、拒否すると更新せず正常終了する
3. **旧配布ファイルの削除**: パッケージ同梱の `deletions.txt` に列挙された「過去のリリースで削除された配布ファイル」を利用先からも削除する（`[y/N]` 確認あり。`--yes` でスキップ）
4. **checkpoint 更新**: 上記が完了すると `.claude/state/c3_version.txt` を今回のバージョンへ更新する（`--dry-run`・ダウングレード・プロンプト拒否時は更新しない）

## `c3 list-agents` / `c3 list-skills`

設置済みエージェント・スキルを一覧表示する。

```bash
c3 list-agents
c3 list-skills
```

## `c3 doctor`

環境診断を実行する。

```bash
c3 doctor [--quiet] [--platform claude|codex|cursor|opencode|all]
```

| オプション | 内容 |
|---|---|
| `--quiet` | 失敗と警告のみ表示する（成功項目を省略） |
| `--platform` | 診断対象。既定は `claude` |

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

| exit code | 条件 |
|---|---|
| 0 | 正常終了（回答 JSON を stdout に出力） |
| 1 | 入力エラー（JSON 不正・ファイル読み込み失敗・入力中断）、または非対話 stdin で `--response` 未指定 |
| 130 | 対話プロンプト中の Ctrl-C（KeyboardInterrupt） |

**Windows での非対話判定**: Windows では `sys.stdin.isatty()` が NUL デバイスへのリダイレクト時にも `True` を誤って返す CRT の癖があるが、`c3 ask` は実コンソール接続を `GetConsoleMode`（Win32 API）で直接判定するため、この誤判定の影響を受けない。`--response` を指定せずに非対話 stdin（NUL / パイプ / ファイルいずれのリダイレクト）で実行した場合は、上記の exit 1（`--response is required in non-interactive mode`）で即座に終了し、ハングしない。

## `c3 run` — 配布スクリプトの共通起動口 (v2.51.0+)

`.claude/` の hooks・skills スクリプトを、無印 `python` の代わりに起動するクロス OS の呼び出し口。`c3` は pip が全 OS で PATH 保証する唯一のランチャーであり、`c3 run` 経由なら interpreter の探索や、マシン固有の絶対パスが git 共有される `settings.json` に混入する問題が発生しない。

```bash
c3 run <script.py> [args...]   # スクリプト実行（python parity）
c3 run -m <module> [args...]   # モジュール実行
c3 run -c <code> [args...]     # コード片実行
```

実行セマンティクスは `python` と同等:

- 残りのトークンはすべてスクリプトへそのまま転送される（`--` も透過）
- スクリプトの `SystemExit` はそのまま exit code になる（Claude Code hook の exit 0/2 語彙を保持）
- 未捕捉例外は traceback を stderr に出して **exit 1**（exit 2 は script が明示要求した場合のみ。クラッシュが hook の「ブロック」と誤認されない）

## `c3 plan` — plan-report 検証 / wave 分解

YAML フロントマター付き `plan-report-*.md` の検証と wave 分解を行う。`parallel-agents` skill が内部で利用する純粋ユーティリティ。

```bash
c3 plan validate <plan-report>        # YAML フロントマターと agent 存在確認
c3 plan waves    <plan-report>        # wave 分解結果を JSON 出力
```

| サブコマンド | exit code | 内容 |
|---|---|---|
| `validate` | 0 / 2 | 0=妥当、2=不正（フロントマター・agent ファイル不在・循環依存等） |
| `waves` | 0 / 2 | 0=標準出力に wave ごとのタスク配列を JSON で出力、2=plan-report 不在・内部 validate 失敗（`validate` と同一検査を先に実行）・wave 分解不能 |

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
| `search` | `--top` (既定 5) / `--source` (sessions/agent-memory/reports/patterns/all) / `--min-score` (既定 0.3) / `--json` / `--target` | 類似チャンク上位 N 件を返却 |
| `rebuild` | `--force` / `--source` / `--target` | 全ソースを再 embedding し numpy ベクトル検索インデックスを atomic write |
| `stats` | `--json` / `--target` | チャンク数・ソース別内訳・モデル名・index ファイルサイズ |

`--target` は 3 サブコマンド共通で、`.claude/` を含むプロジェクトルートを明示指定する（既定はカレントディレクトリから上方探索）。

初回 `c3 recall rebuild` 時に fastembed がモデル（~220MB）を `~/.cache/fastembed/` にダウンロードする。オフライン環境では `FASTEMBED_CACHE_PATH` を社内ミラーに向ける。

検索時、元データソースの mtime が index ファイルより新しい場合は stderr に `[recall] WARN: index is older ...` を出力。`UserPromptSubmit` hook が起動された場合は親 Claude に AskUserQuestion で rebuild 確認を促す指示が注入される。

`/recall` Skill を Claude Code から呼び出すと同等の検索を LLM 自律で実行できる。

## `c3 tier stats` — Tier ルーティング統計

tier-routing の効果計測用 CLI。`.claude/state/c3.db` の `agent_outcomes` / `agent_cost_runs` を可視化。

```bash
c3 tier stats                 # 累積 + 直近 outcome + コストを表形式表示
c3 tier stats --json          # JSON 出力
c3 tier stats --recent N      # 直近 outcome の表示件数（デフォルト 10）
c3 tier stats --role ROLE     # role で絞り込み（interviewer/architect/planner/developer/tester）
```

`--role` に上記以外の値を渡すと有効値一覧を stderr に表示して exit 1 になる。

表示内容:

- **学習データ収集状況**（X / 30 試行 + uniform/thompson モード）
- **Tier 別累積**（complexity × tier × alpha / beta / trials / 期待成功率）
- **直近 outcome 履歴**（時系列降順、success/failure ラベル）
- **Agent 別コスト集計**（agent_cost_runs・agent_type 別の runs / USD / トークン内訳。v2.21.0〜）
- **Tier 別コストレート**（complexity × tier の USD/MTok レート。model 一致集計。v2.24.0〜）
- **routing パラメータ**（現在有効な λ / ε / escalation threshold を環境変数名つきで表示。v2.27.0〜）

学習データは dev-workflow フェーズ E（最終承認時）の `record_agent_outcome.py` でのみ記録されます。直接指示作業ではデータが溜まりません（設計通り）。コストデータは session 終了時に `session_stop.py` のセッションログ ingester（v2.21.0〜）が自動集計します。

### Tier ルーティングのチューニング（環境変数）

tier-routing の挙動は以下の環境変数で調整できます。**すべて未設定の場合は安定動作する既定値**で動き、設定は任意です（不正値は警告を出して既定値にフォールバック）。

| 環境変数 | 既定 | 範囲 | 役割 |
|---|---|---|---|
| `C3_TIER_COST_LAMBDA` | 未設定（cost-aware tie-break のみ） | `0 ≤ λ ≤ 5`（v2.27.0〜・v2.26.0 は `≤ 1`） | **cost-weighted Thompson の重み係数（v2.26.0〜）**。`λ>0` で全 tier の `score = 成功率サンプル − λ × 正規化コスト` を比較し、成功率とコストをトレードオフして選択。`λ=0` 明示でコスト無視（純 Thompson）。`λ>1` でコストを成功率より強く効かせられる（v2.27.0 で上限を 5 に拡張）。**未設定時は v2.25.0 と同じ「成功率が拮抗した群でのみ低コストを選ぶ」挙動**。 |
| `C3_TIER_EPSILON` | `0.05` | `0 < x ≤ 1` | tie-break の拮抗判定閾値（v2.25.0〜）。最大サンプルからこの差以内の Tier を「拮抗」とみなす。 |
| `C3_ESCALATION_THRESHOLD` | `0.5` | `0 < x ≤ 1` | failure-rate がこの値以上で 1 段上位 Tier へ昇格する閾値（v2.26.0〜）。 |

`λ` を大きくするほど安価な Tier が選ばれやすくなり（成功率を犠牲にしうる）、小さいほど成功率優先になります。cost-weighted 発動時は `tier_selection.json` と親 Claude 注入コンテキストに `cost_weighted` / `cost_lambda` が記録されます。

## `c3 metrics` — P4 効果の総括メトリクス

レビュー指摘の判断記録（`review_decisions`）・差し戻し実績（`agent_outcomes`）・コスト記録（`agent_cost_runs`）を read-only で集計し、事前検出実績・差し戻しの傾向と帰属・手戻りコスト概況の 3 セクションで表示する。

```bash
c3 metrics                          # 3 セクション構成の人間向け出力
c3 metrics --json                   # 機械可読 JSON 出力
c3 metrics --since YYYY-MM-DD       # 全セクション共通の下限日付フィルタ
c3 metrics --months N               # 差し戻し傾向（月次）の表示バケット数上限（デフォルト 12・上限 120）
c3 metrics --examples N             # 事前検出実例の表示件数上限（デフォルト 5）
```

入力検証: `--months` は 1〜120（上限は DoS 耐性のための頭打ち）、`--examples` は 1 以上、`--since` は `YYYY-MM-DD` 書式。範囲外・書式不正はエラーメッセージを stderr に表示して **exit 1** になる。

表示内容:

- **[1] 事前検出実績**: `fixed` かつ Medium 以上の判断記録件数（headline）・reviewer × severity × decision マトリクス・直近の実例。severity 未記録（unknown）の fixed 件数も別掲する（headline は severity 記録済みの下限値）。
- **[2] 差し戻しの傾向と帰属**: 差し戻し件数の月次推移（暦月ゼロ埋め）・fix-cycle 近似分布・帰属 role 分布（レビュー差し戻し `[E-1/E-2/C-3]` / 開発内 `[D-3/D-5]` / その他 `other`）。
- **[3] 手戻りコスト概況**: 差し戻しありセッションの合計コスト（USD）と全体比（`overall_ratio` は常に 1.0 以下）。session 粒度の近似であることを注記に明記する。

`data_available` はセクション別（`prevented_detection` / `rework` / `rework_cost`）に判定され、判断記録が未蓄積の場合は `[1]` のみ「収集中（forward-only）」表示になる（`[2]`/`[3]` は独立に表示）。

**severity 供給経路の注記**: severity 語彙は `critical`/`high`/`medium`/`low` の 4 段階だが、`critical` を供給し得るのは **security-reviewer のみ**（code-reviewer / design-critic は `high`/`medium`/`low` の 3 段階までしか供給しない）。したがって headline の `critical` 内訳が 0 のままでも異常ではなく、security-reviewer が critical 指摘を記録した場合にのみ非 0 になる。

判断記録は dev-workflow フェーズ E（code-review/security-review）・design-critic レビューで `record_review_decision.py --severity ...` を呼ぶことで蓄積される（forward-only。導入前の指摘は遡及記録されない）。

## 環境変数（CLI 全体）

| 環境変数 | 役割 |
|---|---|
| `C3_DB_PATH` | `.claude/state/c3.db` のパスを上書きする（`c3 tier stats` / `c3 metrics` など DB を読む全コマンドが参照）。指定パスにファイルが無い場合は警告を出して通常探索（カレントディレクトリからの上方探索）へフォールバック |
| `NO_COLOR` | 設定すると ANSI 色付き出力を抑止する（[no-color.org](https://no-color.org/) 準拠）。stdout が TTY でない場合も色は付かない |

このほか、tier-routing のチューニング用環境変数は `c3 tier stats` の節（Tier ルーティングのチューニング）、recall のモデルキャッシュ（`FASTEMBED_CACHE_PATH`）は `c3 recall` の節を参照。

## CLI で扱われない項目

以下は Claude Code 内（スラッシュコマンド）で扱う領域:

- `/init-session` / `/setup` / `/start` / `/develop` / `/review-phase` / `/promote-pattern` / `/pattern-status` / `/doc` / `/mcp-config` / `/extract-lib` / `/recall` / `/brainstorm` / `/codex-review`
- 詳細は [スキル一覧](skills.md) を参照

Codex では `.agents/skills/` に生成された `$start` などの skills と `.codex/agents/` の custom agents を使う。Cursor では `.cursor/rules/c3-core.mdc` が `.claude/skills/` と `.claude/agents/` を参照する。OpenCode では `.opencode/agents/` の `@c3-*`（agent）と `@c3-skill-*`（skill）を `@mention` で起動する。

## 次に読むページ

- [はじめに](getting-started.md) — インストールから初回セッション
- [スキル一覧](skills.md) — Claude Code 側のスラッシュコマンド
