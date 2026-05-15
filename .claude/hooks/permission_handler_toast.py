#!/usr/bin/env python3
"""permission_handler_toast.py: ボタン付き Windows トースト通知を表示する detached worker.

permission_handler.py が PermissionRequest 時に detached subprocess として起動する。
ユーザーが「自動承認に追加」ボタンをクリックしたら permission_rules.json の
auto_allow 配列にパターンを atomic append する。

windows-toasts のインストール:
  pip install windows-toasts

windows-toasts が見つからない場合は何もせず exit する（既存通知が代替で出ている前提）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time

try:
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass


_TIMEOUT_SEC = 60


def append_to_auto_allow(rules_path: str, pattern: str) -> bool:
    """permission_rules.json の auto_allow 配列に pattern を atomic に追加する。

    既に存在する場合は何もせず False を返す。追加に成功したら True。
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


def show_toast(message: str, pattern: str, rules_path: str) -> None:
    """windows-toasts でボタン付き通知を表示し、コールバックでパターン追加を行う。"""
    try:
        from windows_toasts import (  # type: ignore
            InteractableWindowsToaster,
            Toast,
            ToastActivatedEventArgs,
            ToastButton,
        )
    except ImportError:
        # windows-toasts 未インストール: 何もせず終了（permission_handler.py 側は
        # この subprocess の出力に依存していないので silent fail で OK）
        print(
            '[permission_handler_toast] windows-toasts が見つかりません。'
            '`pip install windows-toasts` でインストールしてください。',
            file=sys.stderr,
        )
        return

    done = threading.Event()

    def on_activated(event: 'ToastActivatedEventArgs') -> None:
        args = getattr(event, 'arguments', '') or ''
        if 'action=add_auto_allow' in args:
            added = append_to_auto_allow(rules_path, pattern)
            if added:
                _show_followup_toast(f'✓ 自動承認パターンに追加しました: {pattern}')
        done.set()

    def on_dismissed(_event) -> None:
        done.set()

    def on_failed(_event) -> None:
        done.set()

    toaster = InteractableWindowsToaster('Claude Code')
    toast = Toast()
    toast.text_fields = ['⚠ 承認が必要', message]
    toast.actions = [
        ToastButton(
            content=f'自動承認に追加: {pattern}',
            arguments='action=add_auto_allow',
        )
    ]
    toast.on_activated = on_activated
    toast.on_dismissed = on_dismissed
    toast.on_failed = on_failed

    try:
        toaster.show_toast(toast)
    except Exception as e:
        print(f'[permission_handler_toast] toast 表示失敗: {e}', file=sys.stderr)
        return

    # ボタンクリック or タイムアウトまで待機
    done.wait(timeout=_TIMEOUT_SEC)


def _show_followup_toast(message: str) -> None:
    """パターン追加完了後の確認通知を非インタラクティブ toast で出す。"""
    try:
        from windows_toasts import Toast, WindowsToaster  # type: ignore
    except ImportError:
        return
    try:
        toaster = WindowsToaster('Claude Code')
        toast = Toast()
        toast.text_fields = [message]
        toaster.show_toast(toast)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description='Interactive toast for permission auto-allow.')
    parser.add_argument('--message', required=True, help='通知本文')
    parser.add_argument('--pattern', required=True, help='auto_allow に追加するパターン')
    parser.add_argument(
        '--rules-file', required=True, help='permission_rules.json の絶対パス'
    )
    args = parser.parse_args()

    show_toast(args.message, args.pattern, args.rules_file)
    return 0


if __name__ == '__main__':
    sys.exit(main())
