#!/usr/bin/env python3
"""PreCompact hook: append checkpoint marker and inject save instruction."""

import json
import os
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

from session_utils import SESSION_JSON_MARKER, append_checkpoint, is_worktree, SESSIONS_DIR


SAVE_INSTRUCTION = (
    "コンテキスト圧縮が間もなく発生します。詳細な文脈が失われる前に、"
    "今日のセッションファイル（.claude/memory/sessions/YYYYMMDD.tmp）を以下のとおり「更新」してください（無制限の追記はしないこと）。\n"
    "1. 「現在地:」行を現在のフェーズ名に更新する（例: 「現在地: フェーズD 実装中」「現在地: Wave 2 実装中」）。\n"
    "2. 「## 残タスク」をチェックリストとして更新する（完了タスクは - [x] 化し、不要になった行は整理する）。\n"
    "CLAUDE.md の Compact Instructions（KEEP/DISCARD）に従い、雑談・解決済みエラーログ・冗長なコード断片は書かないこと。\n"
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
    context_items_before = payload.get('context_items_before')
    context_items_before_str = 'N/A' if context_items_before is None else str(context_items_before)

    now = datetime.now(timezone.utc)
    date_str = now.strftime('%Y%m%d')
    session_file = os.path.join(SESSIONS_DIR, f'{date_str}.tmp')

    summary = (
        f"- trigger: {trigger}\n"
        f"- context_items_before: {context_items_before_str}\n"
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
