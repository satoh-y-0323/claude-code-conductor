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
from urllib.parse import urlparse

try:
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
_PROJECT_ROOT = os.path.dirname(_CLAUDE_DIR)
RULES_PATH = os.path.join(_CLAUDE_DIR, 'permission_rules.json')

DEFAULT_RULES: dict = {'auto_allow': [], 'notify_on_auto': True}
_CREATE_NO_WINDOW = 0x08000000
# p_arg 付きパターンに対してシェル制御文字を含むコマンドの自動承認を防ぐ
_SHELL_INJECTION_RE = re.compile(r';|&&|\|\||`|\$\(')
# permission_handler_toast.py の exit code と一致させること（変更時は両ファイルを同期する）
_TOAST_APPROVED_EXIT_CODE = 10    # ユーザーが許可ボタンをクリック
_TOAST_UNAVAILABLE_EXIT_CODE = 2  # windows-toasts 未インストール


def notify(message: str) -> None:
    system = platform.system()
    try:
        if system == 'Darwin':
            safe = message.replace('\n', ' ').replace('\r', ' ')
            safe = safe.replace('\\', '\\\\').replace('"', '\\"')
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
            import base64
            # メッセージを UTF-8 → Base64 に変換し、PowerShell スクリプト本文に
            # 生のユーザーデータを含めない。Base64 文字列は英数字と +/= のみで
            # PowerShell インジェクション ([SR-INJ-002]) が物理的に不可能。
            msg_b64 = base64.b64encode(message.encode('utf-8')).decode('ascii')
            ps_script = (
                'Add-Type -AssemblyName System.Windows.Forms; '
                '$n = New-Object System.Windows.Forms.NotifyIcon; '
                '$n.Icon = [System.Drawing.SystemIcons]::Information; '
                '$n.Visible = $true; '
                f'$msg = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String("{msg_b64}")); '
                '$n.ShowBalloonTip(4000, \'Claude Code\', $msg, '
                '[System.Windows.Forms.ToolTipIcon]::Info); '
                'Start-Sleep -Milliseconds 4500; '
                '$n.Dispose()'
            )
            encoded = base64.b64encode(ps_script.encode('utf-16-le')).decode('ascii')
            subprocess.Popen(
                ['powershell', '-WindowStyle', 'Hidden', '-EncodedCommand', encoded],
                creationflags=_CREATE_NO_WINDOW
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


def _match_file_path(raw: str, p_arg: str) -> bool:
    """Write/Edit/Read/Glob ツール用のパスマッチング。

    絶対パスと相対パス（プロジェクトルート基準）の両方で照合する。
    前提: ファイルパスは ASCII 文字のみを想定。
    lower() 統一後の文字列でスライス長を計算し、大文字小文字差異によるずれを防ぐ。
    """
    subject_abs = raw.replace(os.sep, '/')
    regex = _glob_to_regex(p_arg)
    if re.fullmatch(regex, subject_abs):
        return True
    # 絶対パスにマッチしない場合、プロジェクトルート基準の相対パスでも照合する。
    # settings.json の permissions.allow と同じ相対パス記法が permission_rules.json でも使える。
    project_prefix_lower = _PROJECT_ROOT.replace(os.sep, '/').rstrip('/').lower() + '/'
    subject_abs_lower = subject_abs.lower()
    if subject_abs_lower.startswith(project_prefix_lower):
        subject_rel = subject_abs[len(project_prefix_lower):]
        # ".." を含む相対パスはディレクトリトラバーサルのリスクがあるためスキップ
        if '..' in subject_rel.split('/'):
            return False
        return bool(re.fullmatch(regex, subject_rel))
    return False


def matches_pattern(tool_name: str, tool_input: dict, pattern: str) -> bool:
    """
    "Bash(git *)" / "Write(.claude/**)" 形式のパターンとマッチするか判定する。
    ToolName のみ（引数なし）も許容する。

    Write / Edit / Read / Glob は _match_file_path() で絶対・相対パスの両方を照合する。
    例: "Edit(.claude/**)" は "Edit(C:/project/.claude/**)" と等価に動作する。
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
        command = tool_input.get('command', '')
        if _SHELL_INJECTION_RE.search(command):
            return False
        subject = command
    elif tool_name in ('Write', 'Edit', 'Read', 'Glob'):
        raw = tool_input.get('file_path', tool_input.get('pattern', ''))
        return _match_file_path(raw, p_arg)
    elif tool_name == 'WebFetch':
        url = tool_input.get('url', '')
        if p_arg.startswith('domain:'):
            domain = p_arg[len('domain:'):]
            try:
                host = urlparse(url).hostname or ''
                return host == domain or host.endswith('.' + domain)
            except Exception:
                return False
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


def suggest_pattern(tool_name: str, tool_input: dict) -> str | None:
    """tool_name と tool_input から auto_allow 用のワイルドカードパターンを推定する。

    返り値の例:
      Bash + 'git status -s'           → 'Bash(git status*)'
      Bash + 'npm install'             → 'Bash(npm install*)'
      Bash + 'pwd'                     → 'Bash(pwd*)'
      Write + '.claude/reports/x.md'   → 'Write(.claude/reports/**)'
      WebFetch + 'https://github.com/' → 'WebFetch(domain:github.com)'
      WebSearch + 任意                  → 'WebSearch'
    返り値が None の場合は推定不能（呼び出し側はボタン表示をスキップする）。

    セキュリティ設計メモ:
      Bash コマンドに対して _SHELL_INJECTION_RE（; && || ` $( を検出）を適用し、
      シェル制御文字を含む場合は None を返してパターン推定を中断する。
      この同一フィルタは matches_pattern() 内でも再度適用されるため、
      仮に制御文字を含むパターンが permission_rules.json に混入しても
      自動承認されない二重防御になっている。
    """
    if not tool_name:
        return None

    if tool_name == 'Bash':
        cmd = tool_input.get('command', '').strip()
        if not cmd:
            return None
        # シェル制御文字を含むコマンドは安全にワイルドカード化できない
        if _SHELL_INJECTION_RE.search(cmd):
            return None
        tokens = cmd.split()
        if not tokens:
            return None
        if len(tokens) >= 2:
            head = f"{tokens[0]} {tokens[1]}"
        else:
            head = tokens[0]
        return f"Bash({head}*)"

    if tool_name in ('Write', 'Edit', 'Read'):
        path = tool_input.get('file_path', '')
        if not path:
            return None
        # 親ディレクトリを取り出し、posix 区切り（/）に正規化
        parent = os.path.dirname(path).replace(os.sep, '/')
        if not parent or parent in ('.', '/'):
            return f"{tool_name}(*)"
        return f"{tool_name}({parent}/**)"

    if tool_name == 'Glob':
        pat = tool_input.get('pattern', '')
        if not pat:
            return f"{tool_name}"
        return f"{tool_name}({pat})"

    if tool_name == 'WebFetch':
        url = tool_input.get('url', '')
        if not url:
            return None
        try:
            host = urlparse(url).hostname or ''
        except Exception:
            return None
        if not host:
            return None
        return f"WebFetch(domain:{host})"

    # その他のツールはツール名のみで auto_allow に登録
    return tool_name


def _is_pattern_already_in_auto_allow(pattern: str, rules: dict | None = None) -> bool:
    """指定パターンが既に auto_allow 配列に存在するかチェックする。"""
    if rules is None:
        rules = load_rules()
    return pattern in (rules.get('auto_allow') or [])


def notify_with_action(message: str, pattern: str | None) -> bool:
    """ボタン付きトースト通知を同期表示し、ユーザーが許可したか返す。

    True:  ユーザーが「許可」ボタンをクリック → 呼び出し元が decision:allow を出力する
    False: タイムアウト / 無視 / 非 Windows → Claude Code のダイアログに委ねる

    「追加して許可」ボタンは pattern が None / 既に auto_allow に存在する場合は省略し、
    「今回だけ許可」ボタンのみ表示する。
    """
    if platform.system() != 'Windows':
        notify(message)
        return False

    toast_script = os.path.join(_HOOKS_DIR, 'permission_handler_toast.py')
    if not os.path.isfile(toast_script):
        print(f'[permission_handler] toast スクリプトが見つかりません: {toast_script}', file=sys.stderr)
        notify(message)
        return False

    # pattern が既に auto_allow に存在する場合は「追加」ボタンを省略する
    add_pattern = pattern if (pattern and not _is_pattern_already_in_auto_allow(pattern)) else None
    cmd = [sys.executable, toast_script, '--message', message, '--rules-file', RULES_PATH]
    if add_pattern:
        cmd += ['--pattern', add_pattern]

    try:
        # timeout=70: toast 側の _TIMEOUT_SEC=60 より余裕を持たせ、
        # toast が内部タイムアウトで終了するのを確実に待つ。
        # この間 Claude Code は PermissionRequest の応答待ち状態になるが、
        # これはユーザーが「追加して許可」or「今回だけ許可」を選択するための意図的な待機であり
        # フリーズではない（選択後は即座に再開する）。
        result = subprocess.run(cmd, timeout=70, capture_output=True)
        if result.returncode == _TOAST_APPROVED_EXIT_CODE:
            return True
        if result.returncode == _TOAST_UNAVAILABLE_EXIT_CODE:
            # windows-toasts 未インストール → バルーン通知にフォールバック
            notify(message)
        return False
    except subprocess.TimeoutExpired:
        print('[permission_handler] toast タイムアウト', file=sys.stderr)
        return False
    except OSError as e:
        print(f'[permission_handler] toast 起動失敗: {e}', file=sys.stderr)
        notify(message)
        return False


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = payload.get('tool_name', '')
    tool_input = payload.get('tool_input', {})
    if not isinstance(tool_input, dict):
        tool_input = {}
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

    # マッチなし → toast でユーザーに確認する（許可されれば decision:allow を出力）
    pattern = suggest_pattern(tool_name, tool_input)
    approved = notify_with_action(f'⚠ 承認が必要: {description}', pattern)
    if approved:
        print(json.dumps({
            'hookSpecificOutput': {
                'hookEventName': 'PermissionRequest',
                'decision': {'behavior': 'allow'}
            }
        }))


if __name__ == '__main__':
    main()
