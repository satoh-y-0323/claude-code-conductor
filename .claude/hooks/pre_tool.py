#!/usr/bin/env python3
"""PreToolUse hook: guard dangerous Bash commands."""

import json
import os
import re
import shlex
import sys

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')


def _is_rm_rf(tokens: list[str], rm_idx: int) -> bool:
    """tokens[rm_idx] が rm であるとき、再帰強制削除フラグ（-rf 相当）が続くか検査する。

    rm の直後のフラグトークンのみを見る。フラグ以外のトークン（ファイル名等）が
    出現した時点で検査を終了し、前のコマンドのフラグを誤検出しない。
    """
    has_r = False
    has_f = False
    for tok in tokens[rm_idx + 1:]:
        stripped = tok.strip("'\"")
        if stripped == '--recursive':
            has_r = True
        elif stripped == '--force':
            has_f = True
        elif stripped.startswith('-') and not stripped.startswith('--'):
            # 短形式フラグ: -r/-R/-f など
            flag_chars = stripped[1:]
            if 'r' in flag_chars or 'R' in flag_chars:
                has_r = True
            if 'f' in flag_chars:
                has_f = True
        elif not stripped.startswith('-'):
            # フラグ以外のトークン（ファイル名等）が来たらフラグ収集終了
            break
    return has_r and has_f


# 秘密情報パターン (secret-scan)
# 検出値そのものは警告に含めない（二次漏洩防止）。パターン名のみを返す。
# 初期パターンは「= で続く値」を必須とすることで、シェルコメント
# （# password reset 等）の誤爆を回避する。
_SECRET_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ('password', re.compile(r'password=\S+', re.IGNORECASE)),
    ('api_key', re.compile(r'api[_-]?key=\S+', re.IGNORECASE)),
    ('bearer', re.compile(r'Bearer\s+[\w\-\.]+', re.IGNORECASE)),
    ('token', re.compile(r'\btoken=\S+', re.IGNORECASE)),
    ('secret', re.compile(r'\bsecret=\S+', re.IGNORECASE)),
    ('aws_secret', re.compile(r'aws_secret_access_key=\S+', re.IGNORECASE)),
    ('private_key', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
]


def _contains_secret(cmd: str) -> tuple[bool, str | None]:
    """秘密情報パターンを検出する。ヒット時は (True, パターン名) を返す。

    パターン名のみを警告文用に返し、検出値そのものは返さない（二次漏洩防止）。
    """
    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(cmd):
            return True, name
    return False, None


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
    # \bTRUNCATE\b でワードバウンダリを使って PRETRUNCATE 等の誤検出を防ぐ
    if re.search(r'DROP\s+TABLE|DROP\s+DATABASE|\bTRUNCATE\b', cmd, re.IGNORECASE):
        print('[PreToolUse WARNING] 破壊的な DB 操作を検出しました。本番環境での実行でないことを確認してください。',
              file=sys.stderr)

    # rm -rf 系: ブロック
    # shlex.split() でトークン分割し、各 rm トークンのフラグを _is_rm_rf() で検査する。
    # これにより "ls -rf && rm somefile" のような前コマンドのフラグを誤検出しない。
    try:
        tokens = shlex.split(cmd, posix=False)
    except ValueError:
        # shlex が解析できない場合はスキップ
        tokens = []

    for idx, tok in enumerate(tokens):
        if os.path.basename(tok.strip("'\"")) == 'rm':
            if _is_rm_rf(tokens, idx):
                cmd_preview = cmd[:200] + ('...' if len(cmd) > 200 else '')
                print(f'[PreToolUse BLOCK] 危険なコマンドをブロックしました: {cmd_preview}', file=sys.stderr)
                sys.exit(2)

    # 秘密情報の代入を検出: ブロック (secret-scan)
    # bypass: C3_SKIP_SECRET_CHECK=1 で検出をスキップ（誤爆時の逃げ道）
    if os.environ.get('C3_SKIP_SECRET_CHECK') != '1':
        is_secret, pattern_name = _contains_secret(cmd)
        if is_secret:
            print(f'[PreToolUse BLOCK] 秘密情報の代入を検出しました（パターン: {pattern_name}）。',
                  file=sys.stderr)
            print('検出値そのものは表示しません。誤検出の場合は C3_SKIP_SECRET_CHECK=1 を設定してください。',
                  file=sys.stderr)
            sys.exit(2)

    sys.exit(0)


if __name__ == '__main__':
    main()
