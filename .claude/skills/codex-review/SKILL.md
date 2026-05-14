---
name: codex-review
description: |
  Codex CLI に .codex/agents/ のエージェント定義を読み込ませ、
  code-reviewer または security-reviewer のペルソナとしてコードレビューを実行するスキル。
  C3 Codex アダプター（.codex/ ディレクトリと AGENTS.md）がセットアップ済みの場合のみ有効。
  通常の C3 code-reviewer / security-reviewer と同じレポート契約（[CR-XX-NNN] / [SR-XX-NNN]）を維持する。

  【単一ファイルモード】特定ファイルを Codex でレビューする:
    args: "code-reviewer src/path/to/file.py"
    args: "security-reviewer src/path/to/file.py"

  【ワークフローモード】git diff の変更全体を Codex でレビューする（通常ワークフローとの並走用）:
    args: "workflow code-reviewer"
    args: "workflow security-reviewer"

  呼び出しトリガー:
    - 「Codex でレビューして」「Codex に code-reviewer をやらせて」
    - 「codex-review」「/codex-review」
    - 「Codex でセキュリティレビュー」
    - 「Codex も並列でレビューさせて」「ワークフローで Codex レビュー」
---

# codex-review

`.codex/agents/{reviewer_type}.toml` のエージェント定義を `codex exec` のプロンプトに埋め込み、
Codex 自身が code-reviewer / security-reviewer ペルソナとしてレビューを実行する。

2つのモードがある:
- **単一ファイルモード**: 指定ファイルを直接レビュー
- **ワークフローモード**: `git diff HEAD` の変更差分を対象にレビュー（通常ワークフローの Claude レビューと並走させる想定）

---

## 前提確認

Glob で `.codex/agents/code-reviewer.toml` を確認する。

存在しない場合は以下を表示してスキルを終了する:
```
[codex-review] Codex アダプターがセットアップされていません。
先に `c3 init --platform codex` を実行してください。
```

---

## Step 1: モードとレビュー設定を確認する

args を解析する:
- `"workflow code-reviewer"` → ワークフローモード + code-reviewer
- `"workflow security-reviewer"` → ワークフローモード + security-reviewer
- `"code-reviewer src/path/file.py"` → 単一ファイルモード + code-reviewer
- `"security-reviewer src/path/file.py"` → 単一ファイルモード + security-reviewer

args が不十分な場合、AskUserQuestion でレビュー種別とモードを確認する:

```json
{
  "questions": [
    {
      "question": "実行するレビューの種類を選択してください",
      "header": "レビュー種別",
      "multiSelect": false,
      "options": [
        { "label": "code-reviewer", "description": "品質・保守性・パフォーマンスをレビュー" },
        { "label": "security-reviewer", "description": "OWASP Top 10 基準でセキュリティ脆弱性をレビュー" }
      ]
    },
    {
      "question": "レビュー対象を選択してください",
      "header": "対象",
      "multiSelect": false,
      "options": [
        { "label": "ワークフロー（git diff）", "description": "現在の変更差分全体をレビュー。通常ワークフローと並走させる場合はこちら" },
        { "label": "単一ファイル", "description": "特定のファイルを指定してレビュー" }
      ]
    }
  ]
}
```

単一ファイルモードでファイルパスが未指定の場合:

```json
{
  "questions": [{
    "question": "レビュー対象のファイルパスを入力してください（「その他」から入力）",
    "header": "対象ファイル",
    "multiSelect": false,
    "options": [
      { "label": "その他（自由入力）", "description": "例: src/c3/cli_ask.py" }
    ]
  }]
}
```

---

## Step 2: レビュー対象のコンテンツを取得する

### ワークフローモードの場合

Bash で以下を実行する:

```bash
git diff HEAD --stat
```

変更ファイルがない場合は `git diff HEAD~1 --stat` を試す。
それも空の場合は「変更差分が見つかりません。コミット済みの変更を対象にするには `git diff HEAD~1` が必要です」と表示して終了する。

続けて差分本体を取得する:

```bash
git diff HEAD
```

差分が長い場合（目安 200 行超）は先頭 200 行に制限する:

```bash
git diff HEAD | head -200
```

取得した内容を `{review_target}` として保持する。対象説明文は「git diff HEAD の変更差分」とする。

### 単一ファイルモードの場合

指定パスを Read してファイル内容を `{review_target}` として保持する。
対象説明文はファイルパスとする。

---

## Step 3: エージェント定義を Read する

`.codex/agents/{reviewer_type}.toml` を Read して `{agent_toml}` として保持する。

---

## Step 4: タイムスタンプを取得する

Skill ツールで `report-timestamp` を呼び出して `{timestamp}` を取得する。

レポートファイル名:
- code-reviewer: `code-review-report-{timestamp}.md`
- security-reviewer: `security-review-report-{timestamp}.md`

---

## Step 5: codex exec を実行する

Bash で以下を実行する（`--sandbox workspace-write`）。

```bash
codex exec "以下の定義に従ってエージェントとして動作してください。

=== エージェント定義（.codex/agents/{reviewer_type}.toml）===
{agent_toml}
=== エージェント定義ここまで ===

上記の定義に従い、以下のコードをレビューしてください。
ファイルシステムへのアクセスが必要な場合（チェックリストの参照など）は Read ツールを使用してください。

対象: {対象説明文}

{review_target}" --sandbox workspace-write 2>&1
```

出力を `{codex_output}` として保持する。

---

## Step 6: レポートを Write する

`.claude/reports/{report_filename}` に Write する:

```markdown
# {reviewer_type} Report (Codex)

**対象:** {対象説明文}
**実行エンジン:** Codex CLI (gpt-5.5) / ペルソナ: {reviewer_type}
**実行日時:** {timestamp}

---

{codex_output}
```

---

## Step 7: 結果を表示してフォローアップを確認する

レポートの内容を表示し、保存先パスを伝える。

AskUserQuestion で確認する:

```json
{
  "questions": [{
    "question": "Codex レビュー結果を確認してください。次のアクションを選択してください。",
    "header": "次のアクション",
    "multiSelect": false,
    "options": [
      { "label": "確認完了", "description": "レポートを確認した" },
      { "label": "別ファイルも続けてレビューする", "description": "Step 1 から再実行する" },
      { "label": "C3 レビューフローへ引き継ぐ", "description": "code-review スキルへ引き継いでフェーズ E を実行する" }
    ]
  }]
}
```

「C3 レビューフローへ引き継ぐ」が選択された場合は Skill ツールで `code-review` を呼び出す。
