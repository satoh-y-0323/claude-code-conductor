---
name: summarize-memory
description: 直近セッションファイルを集約して `.claude/memory/llm_summary.md` を更新する要約を起動する。Stop hook の自動指示（exit 2 + stderr）受け時、または手動 /summarize-memory で使用する。
---

# Summarize Memory（オーケストレーション）

`summarize-memory` サブエージェントをバックグラウンドで起動して
`.claude/memory/llm_summary.md` を更新する。

---

## Step 1: 重複起動を防ぐ

`.claude/state/llm_summary_agent_requested.flag` を確認する:

- **ファイルなし** → Step 2 へ進む
- **内容が空**（エージェント実行中） → 重複起動を防ぐため終了する
- **内容が "DONE"** → 完了済み。強制再実行する場合は Step 2 へ進む

---

## Step 2: サブエージェントを起動する

Agent ツールで `summarize-memory` エージェントをバックグラウンド起動する:

```
Agent(
  subagent_type="summarize-memory",
  description="Summarize recent session memory (background)",
  run_in_background=True
)
```

ユーザーをブロックしないよう、起動後すぐに次の入力を受け付ける。
