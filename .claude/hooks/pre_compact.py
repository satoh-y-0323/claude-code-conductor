#!/usr/bin/env python3
"""PreCompact hook: append checkpoint marker to today's session file."""

import json
import os
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')


def is_worktree(cwd: str) -> bool:
    git_path = os.path.join(cwd, '.git')
    return os.path.exists(git_path) and os.path.isfile(git_path)


def create_session_template(date_str: str) -> str:
    return (
        f"SESSION: {date_str}\n"
        f"AGENT: \n"
        f"DURATION: \n"
        f"\n"
        f"## うまくいったアプローチ\n"
        f"\n"
        f"## 試みたが失敗したアプローチ\n"
        f"\n"
        f"## 残タスク\n"
        f"\n"
        f"## 事実ログ（自動生成 / stop.py）\n"
        f"- 記録時刻: \n"
        f"\n"
        f"<!-- C3:SESSION:JSON\n"
        f"{{\n"
        f'  "session": "{date_str}",\n'
        f'  "patterns": [],\n'
        f'  "successes": [],\n'
        f'  "failures": [],\n'
        f'  "todos": []\n'
        f"}}\n"
        f"-->\n"
    )


def main():
    try:
        json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        pass

    cwd = os.getcwd()

    if is_worktree(cwd):
        sys.exit(0)

    session_dir = os.path.join(cwd, '.claude', 'memory', 'sessions')
    os.makedirs(session_dir, exist_ok=True)

    now = datetime.now(timezone.utc)
    date_str = now.strftime('%Y%m%d')
    session_file = os.path.join(session_dir, f'{date_str}.tmp')

    if not os.path.exists(session_file):
        with open(session_file, 'w', encoding='utf-8') as f:
            f.write(create_session_template(date_str))

    ts = now.isoformat()
    checkpoint = (
        f'\n'
        f'## [PreCompact checkpoint: {ts}]\n'
        f'コンテキストウィンドウ圧縮が発生しました。\n'
        f'このポイント以前の詳細な文脈は失われています。\n'
    )

    with open(session_file, 'a', encoding='utf-8') as f:
        f.write(checkpoint)

    print(f'[PreCompact] セッション状態を {session_file} に保存しました', file=sys.stderr)


if __name__ == '__main__':
    main()
