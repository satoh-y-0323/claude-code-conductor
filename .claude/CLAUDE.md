# Claude Code Conductor (C3)

複数エージェントのオーケストレーションを中心に据えた Claude Code フレームワーク。

## Language

ユーザーとの応答は日本語で行うこと。コード・コマンド・ファイルパスは除く。
コミットメッセージ、チェンジログ、リリースページも日本語で記載すること。

## Communication Style

- 冒頭の褒め言葉・社交辞令は入れない
- 推測で話さない。事実・根拠のある内容のみ答える
- 不確かな場合は「確認が必要です」と明示する
- ルールを変更・省略する方が合理的だと判断した場合は、実行前にユーザーに確認する

## User Interaction Rules

- 長い出力・実装・設計を始める前に、1〜3行で計画を提示してユーザーの確認を取る
- 質問は1回に1つ。複数の質問を一度に投げない
- 選択肢を提示してユーザーが選びやすい形にする（AskUserQuestion を使う）
- 推測で進めない。不明点は確認してから動く
- 各エージェントの出力後は Approval Flow に従って承認を求める。承認なしに次フェーズへ進まない

## Approval Flow

エージェントの出力後は AskUserQuestion で以下の3択を提示する:
- 承認 → 次のフェーズへ進む
- 否認・修正を依頼する → フィードバックを追加入力させてエージェントを再起動する
- 否認・自分で修正する → ユーザーの合図を待ってから再開する

コンテキストに応じて選択肢を追加してよい。

## Compact Instructions

### KEEP（保持する）
- 設計判断・トレードオフの記録
- 確定した仕様・命名規則などの決定事項
- 修正に苦労したバグの原因と恒久的な対策
- 現在のタスクと次のステップ

### DISCARD（捨てる）
- 雑談・社交辞令
- 解決済みのエラーログ・デバッグ出力
- git 管理済みのコードの重複コピー
- 完了済みの古いタスク記録

## Directory Structure

`.claude/docs/` は人間向けリファレンスのためエージェントは読まなくてよい。
詳細は `.claude/docs/taxonomy.md` を参照。
設定の優先順位・配布判断は `.claude/docs/config-policy.md` を参照。

## Platform Compatibility

このファイルは Claude Code / Codex / Cursor から canonical source として読まれる。
他プラットフォームでの動作差分は以下の通り:

- **`AskUserQuestion`** への参照は、Codex/Cursor では MCP tool `c3_ask_user_question` に読み替える（MCP elicitation 非対応時の fallback は `c3 ask --file <json>`）
- **`Agent` ツール** の参照は、Codex では `.codex/agents/<name>.toml` 経由のサブエージェント、Cursor では現行 runtime のサブエージェント機構（無い場合は同一 agent 内でフェーズ実行しレポート契約のみ維持）に読み替える
- **`Skill` ツール / `/<skill>`** の参照は、Codex では `.agents/skills/<name>/SKILL.md`、Cursor では `.claude/skills/<name>/SKILL.md` を直接読み込む（スラッシュコマンド自動展開は Claude Code 専用機能）
- **`isolation: worktree`** / **`permissionMode`** / **`tools` 制限** など agent フロントマターの一部キーは Claude Code 仕様。adapter 側では読み替え不能なものは無視される

レポート（`.claude/reports/`）・state（`.claude/state/`）・memory（`.claude/agent-memory/`）のファイル名と書き込み先は全プラットフォーム共通。adapter 生成物の詳細は `c3 init --platform codex|cursor` で出力される `AGENTS.md` / `.cursor/rules/c3-core.mdc` を参照。

---

## C3 Managed
<!-- このセクションは C3 のコマンドが自動で更新する。手動で編集しないこと。 -->

@rules/promoted/index.md
