#!/usr/bin/env python3
"""Shared utilities for session management hooks (stop.py, pre_compact.py)."""

import os
from datetime import datetime, timezone

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
SESSIONS_DIR = os.path.join(_CLAUDE_DIR, 'memory', 'sessions')

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


def append_checkpoint(session_file: str, label: str, summary: str) -> None:
    """Append a checkpoint block to the session file.

    Used by wave-execution (milestone snapshots) and pre_compact.py
    (compaction markers). Checkpoint blocks are append-only — they record
    the state at a point in time and never overwrite earlier entries.

    Args:
        session_file: Absolute path to the session file (.tmp).
        label: Short identifier shown in the heading
            (e.g. "Wave 2 success", "PreCompact: manual").
        summary: Multi-line Markdown body describing the state.
    """
    os.makedirs(os.path.dirname(session_file), exist_ok=True)

    date_str = os.path.splitext(os.path.basename(session_file))[0]
    try:
        with open(session_file, 'x', encoding='utf-8') as f:
            f.write(create_session_template(date_str))
    except FileExistsError:
        if os.path.getsize(session_file) == 0:
            with open(session_file, 'w', encoding='utf-8') as f:
                f.write(create_session_template(date_str))

    ts = datetime.now(timezone.utc).isoformat()
    body = summary.strip()
    block = (
        f"\n"
        f"## [Checkpoint: {label} - {ts}]\n"
        f"{body}\n"
    )

    with open(session_file, 'a', encoding='utf-8') as f:
        f.write(block)
