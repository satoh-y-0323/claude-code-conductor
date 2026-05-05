#!/usr/bin/env python3
"""PreCompact hook: append checkpoint marker and inject save instruction."""

import json
import os
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
SESSIONS_DIR = os.path.join(_CLAUDE_DIR, 'memory', 'sessions')

from session_utils import append_checkpoint, is_worktree


SAVE_INSTRUCTION = (
    "コンテキスト圧縮が間もなく発生します。圧縮で詳細な文脈が失われる前に、"
    "CLAUDE.md の Compact Instructions（KEEP/DISCARD ルール）に従って "
    "今日のセッションファイル（.claude/memory/sessions/YYYYMMDD.tmp）に "
    "現在の残タスク・直近の重要な判断・解決済みのハマりどころを書き出してください。"
    "雑談・解決済みエラーログ・冗長なコード断片は書かず、KEEP に該当する情報のみ。"
)


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

    now = datetime.now(timezone.utc)
    date_str = now.strftime('%Y%m%d')
    session_file = os.path.join(SESSIONS_DIR, f'{date_str}.tmp')

    summary = (
        f"- trigger: {trigger}\n"
        f"- context_items_before: {context_items_before}\n"
        f"- このポイント以前の詳細な文脈は圧縮により失われます。"
    )
    append_checkpoint(session_file, f'PreCompact: {trigger}', summary)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreCompact",
            "additionalContext": SAVE_INSTRUCTION,
        }
    }
    print(json.dumps(output, ensure_ascii=False))

    print(f'[PreCompact] セッション状態を {session_file} に保存しました', file=sys.stderr)


if __name__ == '__main__':
    main()
