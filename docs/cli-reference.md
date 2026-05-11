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
