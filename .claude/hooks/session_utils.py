#!/usr/bin/env python3
"""Shared utilities for session management hooks (stop.py, pre_compact.py)."""

import os

SESSION_JSON_MARKER = 'C3:SESSION:JSON'


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
        f"<!-- {SESSION_JSON_MARKER}\n"
        f"{{\n"
        f'  "session": "{date_str}",\n'
        f'  "patterns": [],\n'
        f'  "successes": [],\n'
        f'  "failures": [],\n'
        f'  "todos": []\n'
        f"}}\n"
        f"-->\n"
    )
