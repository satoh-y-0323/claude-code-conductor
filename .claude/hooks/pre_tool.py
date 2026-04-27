#!/usr/bin/env python3
"""PreToolUse hook: block dangerous Bash commands."""

import json
import sys
import re

DANGEROUS_PATTERNS = [
    (
        r'\brm\s+\S*-\S*[rR]\S*[fF]|\brm\s+\S*-\S*[fF]\S*[rR]',
        'rm による再帰強制削除は禁止されています',
    ),
    (
        r'\brmdir\s+/[sS]\b',
        'rmdir /s による再帰削除は禁止されています',
    ),
    (
        r'\bmkfs\b',
        'mkfs によるファイルシステム上書きは禁止されています',
    ),
    (
        r'\bdd\b.+\bof=/dev/[a-z]',
        'dd によるデバイスへの直接書き込みは禁止されています',
    ),
    (
        r':\(\)\s*\{[^}]*\|[^}]*&',
        'フォークボムは禁止されています',
    ),
    (
        r'\bgit\s+push\b.*(?:--force|-f)\b',
        'git force push は禁止されています（必要な場合はユーザーに確認してください）',
    ),
]


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    if payload.get('tool_name') != 'Bash':
        sys.exit(0)

    command = payload.get('tool_input', {}).get('command', '')

    for pattern, reason in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            response = {'decision': 'block', 'reason': f'[C3 pre_tool] {reason}'}
            print(json.dumps(response, ensure_ascii=False))
            sys.exit(2)

    sys.exit(0)


if __name__ == '__main__':
    main()
