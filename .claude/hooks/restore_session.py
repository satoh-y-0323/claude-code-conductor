#!/usr/bin/env python3
"""
restore_session.py: SessionStart(compact) hook.
コンテキスト圧縮後に現在のセッション状態を再注入する。
"""

import os
import sys

try:
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
SESSIONS_DIR = os.path.join(_CLAUDE_DIR, 'memory', 'sessions')


def _load_session_utils():
    """session_utils モジュールを動的にロードして返す（同階層）。"""
    import importlib.util

    util_path = os.path.join(_HOOKS_DIR, "session_utils.py")
    spec = importlib.util.spec_from_file_location("session_utils", util_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"session_utils が見つかりません: {util_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def find_latest_session() -> str | None:
    if not os.path.isdir(SESSIONS_DIR):
        return None
    files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith('.tmp')]
    if not files:
        return None
    return os.path.join(SESSIONS_DIR, max(files))


def main():
    path = find_latest_session()
    if not path or not os.path.exists(path):
        sys.exit(0)

    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    session_utils = _load_session_utils()
    extract_section = session_utils.extract_section

    date_str = os.path.basename(path).replace('.tmp', '')
    todos = extract_section(content, '残タスク')
    successes = extract_section(content, 'うまくいったアプローチ')
    failures = extract_section(content, '試みたが失敗したアプローチ')

    # 全セクションが空なら注入不要
    if not todos and not successes and not failures:
        sys.exit(0)

    lines = [f'[C3 セッション復元: {date_str} / 圧縮後リマインダー]']

    if todos:
        lines.append('\n## 残タスク')
        lines.append(todos)

    if successes:
        lines.append('\n## うまくいったアプローチ')
        lines.append(successes)

    if failures:
        lines.append('\n## 試みたが失敗したアプローチ')
        lines.append(failures)

    print('\n'.join(lines))


if __name__ == '__main__':
    main()
