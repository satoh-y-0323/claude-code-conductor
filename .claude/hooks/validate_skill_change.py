#!/usr/bin/env python3
"""PostToolUse hook: remind to test when skills/ files are modified."""

import json
import sys
import os

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    if payload.get('tool_name') not in ('Write', 'Edit'):
        sys.exit(0)

    file_path = payload.get('tool_input', {}).get('file_path', '')
    normalized = file_path.replace('\\', '/')

    if '/.claude/skills/' not in normalized:
        sys.exit(0)

    skill_name = os.path.basename(file_path)
    print(f'[C3] .claude/skills/{skill_name} を変更しました。実際のエージェント動作で確認してください。')
    sys.exit(0)


if __name__ == '__main__':
    main()
