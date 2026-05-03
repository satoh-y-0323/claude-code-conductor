#!/usr/bin/env python3
"""PreToolUse hook: guard dangerous Bash commands."""

import json
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    if payload.get('tool_name') != 'Bash':
        sys.exit(0)

    cmd = payload.get('tool_input', {}).get('command', '')
    if not isinstance(cmd, str):
        sys.exit(0)

    # git force push: 警告（ブロックしない）
    if re.search(r'git\s+push\s+(--force|--force-with-lease|-f)\b', cmd):
        print('[PreToolUse WARNING] git force push を検出しました。実行前にユーザーに確認を取ってください。',
              file=sys.stderr)

    # DROP TABLE / DROP DATABASE / TRUNCATE: 警告（ブロックしない）
    if re.search(r'DROP\s+TABLE|DROP\s+DATABASE|TRUNCATE', cmd, re.IGNORECASE):
        print('[PreToolUse WARNING] 破壊的な DB 操作を検出しました。本番環境での実行でないことを確認してください。',
              file=sys.stderr)

    # rm -rf 系: ブロック
    # rm の直後のフラグのみを収集することで、前のコマンドのフラグを誤検出しない
    if re.search(r'\brm\b', cmd):
        rm_flags_match = re.findall(r'\brm\b((?:\s+-[a-zA-Z]+)*)', cmd)
        flags_str = ''.join(rm_flags_match)
        has_r = bool(re.search(r'-[a-zA-Z]*r', flags_str)) or '--recursive' in cmd
        has_f = bool(re.search(r'-[a-zA-Z]*f', flags_str)) or '--force' in cmd
        has_long_recursive = '--recursive' in cmd
        has_long_force = '--force' in cmd
        if (has_r and has_f) or (has_long_recursive and has_long_force):
            print(f'[PreToolUse BLOCK] 危険なコマンドをブロックしました: {cmd}', file=sys.stderr)
            sys.exit(2)

    sys.exit(0)


if __name__ == '__main__':
    main()
