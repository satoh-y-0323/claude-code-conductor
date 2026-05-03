#!/usr/bin/env python3
"""PreCompact hook: append checkpoint marker to today's session file."""

import json
import os
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
SESSIONS_DIR = os.path.join(_CLAUDE_DIR, 'memory', 'sessions')

from session_utils import is_worktree, create_session_template


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        payload = {}

    cwd = os.getcwd()
    if is_worktree(cwd):
        sys.exit(0)

    trigger = payload.get('trigger', 'unknown')
    context_items_before = payload.get('context_items_before', 0)

    os.makedirs(SESSIONS_DIR, exist_ok=True)

    now = datetime.now(timezone.utc)
    date_str = now.strftime('%Y%m%d')
    session_file = os.path.join(SESSIONS_DIR, f'{date_str}.tmp')

    try:
        with open(session_file, 'x', encoding='utf-8') as f:
            f.write(create_session_template(date_str))
    except FileExistsError:
        pass  # already created by stop.py or another process

    ts = now.isoformat()
    checkpoint = (
        f'\n'
        f'## [PreCompact checkpoint: {trigger} - {ts}]\n'
        f'コンテキスト圧縮 ({trigger}) が発生しました。圧縮前: {context_items_before} アイテム。\n'
        f'このポイント以前の詳細な文脈は失われています。\n'
    )

    with open(session_file, 'a', encoding='utf-8') as f:
        f.write(checkpoint)

    print(f'[PreCompact] セッション状態を {session_file} に保存しました', file=sys.stderr)


if __name__ == '__main__':
    main()
