#!/usr/bin/env python3
"""PreToolUse hook: worktree boundary guardrail.

CWD が `.claude/worktrees/` 配下の場合のみ自動的に有効化される。
parallel-agents skill が isolation:"worktree" 付きで起動する subagent の
CWD は worktree root になるため、本 hook が自動的に防護を ON にする。
Write / Edit ツールの対象パスが CWD（worktree ルート）外であればブロックする。

main セッション（CWD が project root）では何もしないため既存挙動を破壊しない。
"""

import json
import os
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

# CWD が `.claude/worktrees/` を含む場合のみ有効化するためのマーカー
_WORKTREES_MARKER = os.sep + ".claude" + os.sep + "worktrees" + os.sep


def _sanitize(s: str) -> str:
    """ターミナルインジェクション対策: 制御文字（ANSI エスケープ含む）を除去する。"""
    return re.sub(r'[\x00-\x1f\x7f]', '', s)


def main():
    cwd = os.path.realpath(os.getcwd())
    # CWD が `.claude/worktrees/` 配下でなければスルー（main セッション等）
    if _WORKTREES_MARKER not in cwd + os.sep:
        sys.exit(0)

    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = payload.get('tool_name', '')
    if tool_name not in ('Write', 'Edit'):
        sys.exit(0)

    file_path = payload.get('tool_input', {}).get('file_path', '')
    if not file_path:
        sys.exit(0)

    resolved = os.path.realpath(
        file_path if os.path.isabs(file_path) else os.path.join(cwd, file_path)
    )

    if resolved != cwd and not resolved.startswith(cwd + os.sep):
        print(
            f'[WorktreeGuard BLOCK] worktree 外へのファイル操作をブロックしました。\n'
            f'  対象パス: {_sanitize(file_path)}\n'
            f'  解決パス: {_sanitize(resolved)}\n'
            f'  許可範囲: {_sanitize(cwd)}',
            file=sys.stderr
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == '__main__':
    main()
