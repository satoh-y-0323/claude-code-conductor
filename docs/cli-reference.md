# CLI リファレンス

`pip install claude-code-conductor` で同時にインストールされるターミナルコマンド `c3` のリファレンス。

## グローバル

```bash
c3 --version    # バージョン表示
c3 --help       # サブコマンド一覧
```

## `c3 init`

利用先プロジェクトに `.claude/` を展開する。

```bash
c3 init [--force]
```

| オプション | 内容 |
|---|---|
| `--force` | 既存ファイルを上書きする（通常はスキップ） |

## `c3 update`

`.claude/` をパッケージ最新版へ更新する。個人ファイル（`reports/`, `memory/sessions/` 等）はスキップ。

```bash
c3 update [--dry-run]
```

| オプション | 内容 |
|---|---|
| `--dry-run` | 変更内容のプレビューのみ（実際には更新しない） |

## `c3 list-agents` / `c3 list-skills`

設置済みエージェント・スキルを一覧表示する。

```bash
c3 list-agents
c3 list-skills
```

## `c3 doctor`

環境診断を実行する。

```bash
c3 doctor
```

確認項目:
- `.claude/` ディレクトリの存在
- `settings.json` の有効性
- `claude` バイナリのパス
- `parallel-orchestra` のインポート可否

## `c3 po` — Parallel Orchestra

PO（Parallel Orchestra）の各種コマンド。詳細は [スキル一覧 / 並列実行](skills.md#parallel-orchestra) 参照。

```bash
c3 po dry-run <plan-report>           # マニフェスト妥当性検証
c3 po waves   <plan-report>           # wave 分解結果を JSON 出力
c3 po run     <plan-report>           # 全 wave を並列実行
c3 po run-wave <plan-report> --wave-index N
                                       # 指定 wave のみ実行
```

| オプション | 対象 | 内容 |
|---|---|---|
| `--wave-index N` | run-wave | 実行する wave のインデックス（0 始まり） |

## `c3 status` — PO ダッシュボード

F-003 で追加。`.claude/state/c3.db` の `po_status` テーブルから PO 並列実行の状況を表示する。

```bash
c3 status                     # 最新 session の active worktree
c3 status --all               # 直近 5 session を横断表示
c3 status --watch             # リアルタイム再描画（30 秒間隔）
c3 status --json              # 機械可読 JSON 出力
c3 status --state failed --verbose
                              # 失敗 worktree の error_message 全文表示
```

| オプション | 内容 |
|---|---|
| `--session SESSION_ID` | session_id でフィルタ |
| `--state {starting,running,completed,failed,waiting}` | state でフィルタ |
| `--worktree GLOB` | worktree_id を glob でフィルタ（例: `po/*-task-*`） |
| `--watch` | 自動再描画モード |
| `--interval N` | --watch の間隔（秒、デフォルト 30） |
| `--stale-threshold N` | heartbeat N 秒超で stale 判定（デフォルト 90） |
| `--limit N` | 表示件数上限（デフォルト 50） |
| `--json` | JSON 出力 |
| `--verbose` | error_message を全文表示 |

## `c3 tier stats` — Tier ルーティング統計

F-005 の効果計測用 CLI。`.claude/state/c3.db` の `tier_bandit` / `tier_recent_outcomes` を可視化。

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

- `/init-session` / `/setup` / `/start` / `/develop` / `/code-review` / `/promote-pattern` / `/doc` / `/mcp-config` / `/extract-lib`
- 詳細は [スキル一覧](skills.md) を参照

## 次に読むページ

- [はじめに](getting-started.md) — インストールから初回セッション
- [スキル一覧](skills.md) — Claude Code 側のスラッシュコマンド
