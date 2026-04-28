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

    # cd コマンド: CWD 固定バグを防ぐためブロック
    # Bash ツールで cd を実行すると以降の全コマンドの CWD が変わり、
    # フックが相対パスで .claude/hooks/ を参照できなくなる。
    if re.search(r'(?:^|[;&|])\s*cd(?:\s|$)', cmd):
        print(
            '[PreToolUse BLOCK] cd コマンドをブロックしました。\n'
            'Bash ツールで cd を実行すると CWD が変わり、以降のフックが失敗します。\n'
            'cd を使わず、プロジェクトルートからの相対パスで実行してください。\n'
            '例: python -m pytest test1/tests -v  （cd test1 && python -m pytest の代わり）',
            file=sys.stderr
        )
        sys.exit(2)

    # rm -rf 系: ブロック
    # 短フラグ形式（-rf / -fr / -r -f 等）とロングオプション形式（--recursive --force）に対応
    if re.search(r'\brm\b', cmd):
        short_flags = ''.join(re.findall(r'-[a-zA-Z]+', cmd))
        has_r = 'r' in short_flags or bool(re.search(r'\brm\b.*\s-[a-zA-Z]*r[a-zA-Z]*', cmd))
        has_f = 'f' in short_flags or bool(re.search(r'\brm\b.*\s-[a-zA-Z]*f[a-zA-Z]*', cmd))
        has_long_recursive = '--recursive' in cmd
        has_long_force = '--force' in cmd
        if (has_r and has_f) or (has_long_recursive and has_long_force):
            print(f'[PreToolUse BLOCK] 危険なコマンドをブロックしました: {cmd}', file=sys.stderr)
            sys.exit(2)

    sys.exit(0)


if __name__ == '__main__':
    main()
