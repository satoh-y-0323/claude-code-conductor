#!/usr/bin/env python3
"""PreToolUse hook: worktree boundary guardrail.

C3_WORKTREE_GUARD=1 が設定されている場合のみ動作する。
Write / Edit ツールの対象パスが CWD（worktree ルート）外であればブロックする。
"""

import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')


def main():
    if os.environ.get('PO_WORKTREE_GUARD') != '1':
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

    cwd = os.path.realpath(os.getcwd())
    resolved = os.path.realpath(
        file_path if os.path.isabs(file_path) else os.path.join(cwd, file_path)
    )

    if resolved != cwd and not resolved.startswith(cwd + os.sep):
        print(
            f'[WorktreeGuard BLOCK] worktree 外へのファイル操作をブロックしました。\n'
            f'  対象パス: {file_path}\n'
            f'  解決パス: {resolved}\n'
            f'  許可範囲: {cwd}',
            file=sys.stderr
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == '__main__':
    main()
