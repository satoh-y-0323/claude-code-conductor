# /review コマンド

code-reviewer と security-reviewer を順番に Agent ツールで起動してレビューを行う。

## Step 1: code-reviewer エージェントの起動

Agent ツールで `code-reviewer` エージェントを起動する。
code-review-report をユーザーに報告して承認を求める。
否認された場合はフィードバックを確認して再起動する。

## Step 2: security-reviewer エージェントの起動

Agent ツールで `security-reviewer` エージェントを起動する。
security-review-report をユーザーに報告して承認を求める。
否認された場合はフィードバックを確認して再起動する。

## Step 3: 結果に応じた判断

**High / Critical の指摘がある場合:**
`/start` の計画フェーズ（plan）から再開するよう案内する。
→ planner が両レポートを読み込んで plan-report を更新する。

**Low のみ / 指摘なし:**
完了。コミットを提案する。
