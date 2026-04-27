#!/usr/bin/env python3
"""PreCompact hook: mark compact event in today's session file."""

import json
import sys
import os
import re
from datetime import date, datetime

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
SESSIONS_DIR = os.path.join(_CLAUDE_DIR, 'memory', 'sessions')


def main():
    try:
        json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        pass

    today_str = date.today().strftime('%Y%m%d')
    session_path = os.path.join(SESSIONS_DIR, f'{today_str}.tmp')

    if not os.path.exists(session_path):
        sys.exit(0)

    with open(session_path, 'r', encoding='utf-8') as f:
        content = f.read()

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    compact_line = f'- コンパクト発生: {now}\n'

    # Insert compact marker after the "記録時刻" line
    updated = re.sub(
        r'(- 記録時刻: [^\n]*\n)',
        rf'\1{compact_line}',
        content,
        count=1,
    )

    if updated != content:
        with open(session_path, 'w', encoding='utf-8') as f:
            f.write(updated)

    sys.exit(0)


if __name__ == '__main__':
    main()
