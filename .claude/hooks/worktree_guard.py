#!/usr/bin/env python3
"""PreToolUse hook: worktree boundary guardrail.

PO_WORKTREE_GUARD=1 が設定されている場合のみ動作する。
worktree 内で実装タスクを実行するワークフロー（parallel-agents skill が
isolation:"worktree" 付きで起動する agent など）が事前にこの env を設定して有効化する。
Write / Edit ツールの対象パスが CWD（worktree ルート）外であればブロックする。
"""

import json
import os
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

# worktree パスの識別に使うコンポーネント名。
# `.claude/worktrees/agent-<id>/` という構造を前提とし、
# "worktrees" の直前のコンポーネントが ".claude" であることをパス分割で検査する。
# os.sep を末尾に補完する理由: `.claude/worktrees/agent-test/` のような
# パスを split(os.sep) すると末尾の空文字列が含まれるが、
# インデックス検索には影響しないため補完不要。
# ただし startswith(cwd + os.sep) による境界チェックでは os.sep が必須（例:
# `/foo/bar` が `/foo/baz` の prefix と誤判定されるのを防ぐ）。
_WORKTREES_PARENT = ".claude"
_WORKTREES_COMPONENT = "worktrees"


def _sanitize(s: str) -> str:
    """ターミナルインジェクション対策: 制御文字（ANSI エスケープ含む）を除去する。"""
    return re.sub(r'[\x00-\x1f\x7f]', '', s)


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

    # [SR-V-001] CWD がパスコンポーネント分割で ".claude/worktrees/..." の
    # 構造を持つことを検証する。
    # str.split(os.sep) でパス要素に分解し、"worktrees" の直前コンポーネントが
    # ".claude" であることを確認する。
    # これにより、".claude" 自体が symlink で別名解決される場合でも
    # os.path.realpath() 後のパスで正しく検証できる
    # （文字列部分一致 (_WORKTREES_MARKER in cwd) よりも誤検知が少ない）。
    parts = cwd.split(os.sep)
    try:
        wt_idx = parts.index(_WORKTREES_COMPONENT)
        if wt_idx == 0 or parts[wt_idx - 1] != _WORKTREES_PARENT:
            sys.exit(0)
    except ValueError:
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
