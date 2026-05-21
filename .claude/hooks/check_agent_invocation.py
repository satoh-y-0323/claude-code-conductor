#!/usr/bin/env python3
"""PreToolUse Agent hook: read_only タスクの worktree 違反を BLOCK する。

## 検査ルール

- **R5**: subagent_type が `code-reviewer` / `security-reviewer` のとき
  `isolation: "worktree"` は禁止。worktree 自動クリーンアップで
  `.claude/reports/*.md`（gitignored）が消失するため。

## fail-safe 設計

- `tool_input` のキー名（`subagent_type` / `isolation`）は Claude Code 公式仕様に
  ドキュメント化されていない（2026-05-21 時点）。キー名が想定と異なる場合は
  検出できず exit 0（許可）にフォールバックする。誤検知で全 Agent 呼び出しを
  ブロックすることはない。
- デバッグ用に `C3_HOOK_DEBUG=1` を設定すると `tool_input` を
  `.claude/tmp/agent_hook_debug.log` に追記する（キー名検証用）。

## 出力経路

- BLOCK 時は **stderr** に `[CheckAgentInvocation BLOCK]` を出力し ``exit 2`` する。
  PreToolUse の ``exit 2`` は Claude Code が動作をブロックする公式仕様。
  PreToolUse ``exit 2`` 時に stdout JSON で LLM コンテキストに注入する公式仕様は
  2026-05-21 時点で見当たらないため、本 hook は stderr のみで運用している
  （PostToolUse とは設計が異なる点に注意）。

## 過去パターン根拠

- 2026-05-21 のフルワークフロー動作確認で発生したルール違反 1
  （read_only: true タスクに isolation:worktree を指定し、reports が消失）への対策。
"""

import json
import os
import sys
from pathlib import Path

# 共通ヘルパー (_hook_utils.write_debug_log) を hooks/ 経由で import するため、
# このスクリプトのディレクトリを PYTHONPATH に追加する。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hook_utils import write_debug_log  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

REVIEWER_TYPES = frozenset({"code-reviewer", "security-reviewer"})

# プロジェクトルートと debug ログのパスはスクリプトファイルからの絶対パスに固定する。
# cwd 依存だと worktree コンテキストで `.claude/tmp/` が意図しない場所に作られる。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEBUG_LOG_PATH = PROJECT_ROOT / ".claude" / "tmp" / "agent_hook_debug.log"

# DoS 防御の読み取り上限（hook はローカル前提だが多層防御として明示する）。
_STDIN_READ_LIMIT_BYTES = 1 * 1024 * 1024  # 1 MiB


def _escape_for_log(value: str) -> str:
    """改行を含む値をログ・stderr に安全に表示するための簡易エスケープ。"""
    return value.replace("\r", "\\r").replace("\n", "\\n")


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read(_STDIN_READ_LIMIT_BYTES))
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    if payload.get("tool_name") != "Agent":
        sys.exit(0)

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        sys.exit(0)

    subagent_type = tool_input.get("subagent_type", "")
    isolation = tool_input.get("isolation", "")
    if not isinstance(subagent_type, str):
        subagent_type = ""
    if not isinstance(isolation, str):
        isolation = ""

    if subagent_type not in REVIEWER_TYPES:
        # 対象外（reviewer 系でない）は素通り。デバッグログも残さない（ノイズ削減）
        sys.exit(0)

    isolation_display = _escape_for_log(isolation) if isolation else "none"

    if isolation == "worktree":
        write_debug_log(
            DEBUG_LOG_PATH,
            f"{subagent_type or 'unknown'} isolation={isolation_display} BLOCK R5",
        )
        print(
            f"[CheckAgentInvocation BLOCK] R5: subagent_type={subagent_type!r} "
            f'(read_only) タスクには isolation: "worktree" を指定できません。'
            f" worktree 自動クリーンアップで .claude/reports/*.md が消失します。"
            f" isolation を省略して main リポジトリで直接実行してください。",
            file=sys.stderr,
        )
        sys.exit(2)

    write_debug_log(
        DEBUG_LOG_PATH,
        f"{subagent_type or 'unknown'} isolation={isolation_display} PASS",
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
