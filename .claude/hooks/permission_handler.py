#!/usr/bin/env python3
"""
permission_handler.py: PermissionRequest hook.
権限確認ダイアログが出るタイミングで通知を表示し、
permission_rules.json のパターンにマッチすれば自動承認する。
"""

import json
import os
import platform
import re
import subprocess
import sys

try:
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
RULES_PATH = os.path.join(_CLAUDE_DIR, 'permission_rules.json')

DEFAULT_RULES: dict = {'auto_allow': [], 'notify_on_auto': True}


def notify(message: str) -> None:
    system = platform.system()
    try:
        if system == 'Darwin':
            safe = message.replace('\\', '\\\\').replace('"', '\\"')
            subprocess.run(
                ['osascript', '-e', f'display notification "{safe}" with title "Claude Code"'],
                capture_output=True, timeout=5
            )
        elif system == 'Linux':
            subprocess.run(
                ['notify-send', 'Claude Code', message],
                capture_output=True, timeout=5
            )
        elif system == 'Windows':
            safe = re.sub(r'[`$(){}\r\n]', '', message)
            safe = safe.replace('"', '`"')
            ps = (
                'Add-Type -AssemblyName System.Windows.Forms; '
                '$n = New-Object System.Windows.Forms.NotifyIcon; '
                '$n.Icon = [System.Drawing.SystemIcons]::Information; '
                '$n.Visible = $true; '
                f'$n.ShowBalloonTip(4000, "Claude Code", "{safe}", '
                '[System.Windows.Forms.ToolTipIcon]::Info); '
                'Start-Sleep -Milliseconds 4500; '
                '$n.Dispose()'
            )
            subprocess.Popen(
                ['powershell', '-WindowStyle', 'Hidden', '-Command', ps],
                creationflags=0x08000000  # CREATE_NO_WINDOW
            )
    except Exception as e:
        print(f'[permission_handler] 通知エラー: {e}', file=sys.stderr)


def load_rules() -> dict:
    if not os.path.exists(RULES_PATH):
        return DEFAULT_RULES
    try:
        with open(RULES_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f'[permission_handler] permission_rules.json の読み込みエラー: {e}', file=sys.stderr)
        return DEFAULT_RULES


def _glob_to_regex(pattern: str) -> str:
    """glob パターン（* と **）を正規表現に変換する。"""
    # ** を一時プレースホルダーに退避してから * と ** を別々に処理する
    parts = pattern.split('**')
    escaped = [re.escape(p).replace(r'\*', '[^/]*') for p in parts]
    return '.*'.join(escaped)


def matches_pattern(tool_name: str, tool_input: dict, pattern: str) -> bool:
    """
    "Bash(git *)" / "Write(.claude/**)" 形式のパターンとマッチするか判定する。
    ToolName のみ（引数なし）も許容する。
    """
    m = re.match(r'^(\w+)(?:\((.+)\))?$', pattern.strip())
    if not m:
        return False

    p_tool, p_arg = m.group(1), m.group(2)
    if tool_name != p_tool:
        return False
    if not p_arg:
        return True

    # ツール別に照合対象を決定
    if tool_name == 'Bash':
        subject = tool_input.get('command', '')
    elif tool_name in ('Write', 'Edit', 'Read', 'Glob'):
        subject = tool_input.get('file_path', tool_input.get('pattern', ''))
    elif tool_name == 'WebFetch':
        url = tool_input.get('url', '')
        if p_arg.startswith('domain:'):
            domain = p_arg[len('domain:'):]
            return domain in url
        subject = url
    else:
        subject = str(tool_input)

    regex = _glob_to_regex(p_arg)
    return bool(re.fullmatch(regex, subject))


def describe_tool(tool_name: str, tool_input: dict) -> str:
    if tool_name == 'Bash':
        cmd = tool_input.get('command', '')
        return f"{tool_name}({cmd[:60]}{'...' if len(cmd) > 60 else ''})"
    if tool_name in ('Write', 'Edit', 'Read'):
        return f"{tool_name}({tool_input.get('file_path', '')})"
    if tool_name == 'WebFetch':
        return f"{tool_name}({tool_input.get('url', '')})"
    return f"{tool_name}({str(tool_input)[:60]})"


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = payload.get('tool_name', '')
    tool_input = payload.get('tool_input', {})
    rules = load_rules()
    description = describe_tool(tool_name, tool_input)

    for pattern in rules.get('auto_allow', []):
        if matches_pattern(tool_name, tool_input, pattern):
            if rules.get('notify_on_auto', True):
                notify(f'✓ 自動承認: {description}')
            print(json.dumps({
                'hookSpecificOutput': {
                    'hookEventName': 'PermissionRequest',
                    'decision': {'behavior': 'allow'}
                }
            }))
            return

    # マッチなし → ダイアログが出る前に通知
    notify(f'⚠ 承認が必要: {description}')


if __name__ == '__main__':
    main()
