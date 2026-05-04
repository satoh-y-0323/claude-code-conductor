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
