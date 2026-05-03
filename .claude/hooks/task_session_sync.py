#!/usr/bin/env python3
"""TaskCompleted hook: auto-check matching task in today's session file."""

import json
import os
import re
import sys
from datetime import date

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from session_utils import SESSIONS_DIR, is_worktree

UNCHECKED = re.compile(r'^- \[ \] ')


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    if is_worktree(os.getcwd()):
        sys.exit(0)

    task_subject = payload.get('task_subject', '').strip()
    if not task_subject:
        sys.exit(0)

    date_str = date.today().strftime('%Y%m%d')
    session_file = os.path.join(SESSIONS_DIR, f'{date_str}.tmp')
    if not os.path.exists(session_file):
        sys.exit(0)

    with open(session_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    matched_idx = None
    for i, line in enumerate(lines):
        if UNCHECKED.match(line) and task_subject in line:
            matched_idx = i
            break

    if matched_idx is None:
        print(f'[TaskCompleted] 対応行なし: {task_subject}', file=sys.stderr)
        sys.exit(0)

    lines[matched_idx] = UNCHECKED.sub('- [x] ', lines[matched_idx])
    with open(session_file, 'w', encoding='utf-8') as f:
        f.writelines(lines)

    print(f'[TaskCompleted] チェック済みに更新: {task_subject}', file=sys.stderr)


if __name__ == '__main__':
    main()
