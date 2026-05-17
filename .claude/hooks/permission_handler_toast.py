#!/usr/bin/env python3
"""permission_handler_toast.py: ボタン付き Windows トースト通知を表示する同期ワーカー。

permission_handler.py が PermissionRequest 時に subprocess.run で同期起動する。
ユーザーが「追加して許可」または「今回だけ許可」をクリックすると
exit code _APPROVED_EXIT_CODE(10) で終了し、呼び出し元が decision:allow を出力する。
「追加して許可」は permission_rules.json の auto_allow 配列にパターンを atomic append する。

windows-toasts のインストール:
  pip install windows-toasts

windows-toasts が見つからない場合は _UNAVAILABLE_EXIT_CODE(2) で exit する。
呼び出し元（permission_handler.py）がこの code を検出してバルーン通知にフォールバックする。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import tempfile
import threading

try:
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass


_TIMEOUT_SEC = 60
_AUTO_ALLOW_MAX_SIZE = 100
# permission_handler.py の _TOAST_*_EXIT_CODE 定数と一致させること（変更時は両ファイルを同期する）
_APPROVED_EXIT_CODE = 10   # ユーザーが許可ボタンをクリック
_UNAVAILABLE_EXIT_CODE = 2  # windows-toasts 未インストール


def append_to_auto_allow(rules_path: str, pattern: str) -> bool:
    """permission_rules.json の auto_allow 配列に pattern を atomic に追加する。

    既に存在する場合は何もせず False を返す。追加に成功したら True。
    上限（_AUTO_ALLOW_MAX_SIZE）に達している場合は stderr に警告を出力して False を返す。
    書き込み失敗（OSError 等）は False を返す。
    """
    rules: dict
    if os.path.isfile(rules_path):
        try:
            with open(rules_path, 'r', encoding='utf-8') as f:
                rules = json.load(f)
        except (json.JSONDecodeError, OSError):
            rules = {}
    else:
        rules = {}

    if not isinstance(rules, dict):
        rules = {}
    auto_allow = rules.get('auto_allow')
    if not isinstance(auto_allow, list):
        auto_allow = []
    if pattern in auto_allow:
        return False
    if len(auto_allow) >= _AUTO_ALLOW_MAX_SIZE:
        print(
            f'[permission_handler_toast] auto_allow が上限 ({_AUTO_ALLOW_MAX_SIZE} 件) に達しています。'
            ' パターンを追加できません。不要なパターンを permission_rules.json から削除してください。',
            file=sys.stderr,
        )
        return False
    auto_allow.append(pattern)
    rules['auto_allow'] = auto_allow

    # atomic write: tempfile + os.replace
    dir_name = os.path.dirname(rules_path) or '.'
    try:
        os.makedirs(dir_name, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix='.permission_rules.', suffix='.tmp', dir=dir_name
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(rules, f, ensure_ascii=False, indent=2)
                f.write('\n')
            os.replace(tmp_path, rules_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return True
    except OSError as e:
        print(f'[permission_handler_toast] 書き込み失敗: {e}', file=sys.stderr)
        return False


def show_toast(message: str, pattern: str | None, rules_path: str) -> bool:
    """windows-toasts でボタン付き通知を同期表示する。

    ユーザーがいずれかの許可ボタンをクリックした場合に True を返す（現在のリクエストを承認）。
    ImportError 時は _UNAVAILABLE_EXIT_CODE で sys.exit する（呼び出し元がフォールバック処理）。
    """
    try:
        from windows_toasts import (  # type: ignore
            InteractableWindowsToaster,
            Toast,
            ToastActivatedEventArgs,
            ToastButton,
        )
    except ImportError:
        print(
            '[permission_handler_toast] windows-toasts が見つかりません。'
            '`pip install windows-toasts` でインストールしてください。',
            file=sys.stderr,
        )
        sys.exit(_UNAVAILABLE_EXIT_CODE)

    approved = threading.Event()
    done = threading.Event()

    def on_activated(event: 'ToastActivatedEventArgs') -> None:
        args = getattr(event, 'arguments', '') or ''
        if args == 'action=add_auto_allow':
            if pattern:
                added = append_to_auto_allow(rules_path, pattern)
                if added:
                    _show_followup_toast(f'✓ 自動承認パターンに追加しました: {pattern}')
            approved.set()
        elif args == 'action=allow_once':
            approved.set()
        done.set()

    def on_dismissed(_event) -> None:
        done.set()

    def on_failed(_event) -> None:
        done.set()

    toaster = InteractableWindowsToaster('Claude Code')
    toast = Toast()
    # windows-toasts は内部で XML テンプレートを生成するため、
    # '<' '&' 等を含むパスがそのまま渡るとパースエラーになる [SR-INJ-002]
    toast.text_fields = ['⚠ 承認が必要', html.escape(message)]
    toast.actions = []
    if pattern:
        toast.actions.append(ToastButton(
            content=f'追加して許可: {html.escape(str(pattern))}',
            arguments='action=add_auto_allow',
        ))
    toast.actions.append(ToastButton(
        content='今回だけ許可',
        arguments='action=allow_once',
    ))
    toast.on_activated = on_activated
    toast.on_dismissed = on_dismissed
    toast.on_failed = on_failed

    try:
        toaster.show_toast(toast)
    except Exception as e:
        print(f'[permission_handler_toast] toast 表示失敗: {e}', file=sys.stderr)
        return False

    done.wait(timeout=_TIMEOUT_SEC)
    return approved.is_set()


def _show_followup_toast(message: str) -> None:
    """パターン追加完了後の確認通知を非インタラクティブ toast で出す。

    message は内部で html.escape() を適用してから toast に渡す [SR-INJ-002]。
    """
    try:
        from windows_toasts import Toast, WindowsToaster  # type: ignore
    except ImportError:
        return
    try:
        toaster = WindowsToaster('Claude Code')
        toast = Toast()
        toast.text_fields = [html.escape(message)]
        toaster.show_toast(toast)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description='Interactive toast for permission handling.')
    parser.add_argument('--message', required=True, help='通知本文')
    parser.add_argument('--pattern', default=None, help='auto_allow に追加するパターン（省略可）')
    parser.add_argument(
        '--rules-file', required=True, help='permission_rules.json の絶対パス'
    )
    args = parser.parse_args()

    approved = show_toast(args.message, args.pattern, args.rules_file)
    return _APPROVED_EXIT_CODE if approved else 0


if __name__ == '__main__':
    sys.exit(main())
