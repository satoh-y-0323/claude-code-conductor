---
description: レポートファイル名に使用するタイムスタンプ (YYYYMMDD-HHMMSS) を取得する。requirements-report・architecture-report・plan-report・code-review-report・security-review-report・test-report などのレポートファイルを生成する際は、必ずこのスキルを使用してファイル名のタイムスタンプを決定すること。
user-invocable: false
---

# report-timestamp

レポートファイル名用のタイムスタンプを Python で取得する。

## 使い方

レポートファイル名が必要になったら、以下を実行してその出力をファイル名に使用する:

```bash
python "${CLAUDE_SKILL_DIR}/scripts/get_timestamp.py"
```

出力形式: `YYYYMMDD-HHMMSS`（例: `20260504-143022`）

## 注意

- PowerShell・bash の date コマンドは使わない（時刻部分が 000000 になる場合がある）
- python コマンドが使えない場合のみ `python3` にフォールバックする

## Bash を持たないエージェントの場合（親がタイムスタンプを渡す）

architect / planner / design-critic 等、tools に Bash を含まないエージェントは本スクリプトを実行できない。
その場合は**サブエージェントを起動する親 Claude が本 skill でタイムスタンプを取得し、
出力先レポートのファイル名を確定させた形で起動プロンプトに含める**こと（例: 「レポートは
`.claude/reports/architecture-report-20260714-213000.md` に Write。このファイル名を厳守」）。
エージェント側でのタイムスタンプの手採番（推測・連番）は禁止する。
（Bash を持たないエージェントに Bash を追加する対処は採らない: レポート role の最小権限を崩し、
allow 済み git コマンド経由で「非可逆操作は人間の関所」を迂回する経路が開くため）
