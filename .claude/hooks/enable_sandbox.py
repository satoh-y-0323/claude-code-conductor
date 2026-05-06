#!/usr/bin/env python3
"""Utility: ensure full sandbox config is present in settings.json."""

import json
import os
import sys
import tempfile

from session_utils import is_worktree

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

FULL_SANDBOX_CONFIG = {
    "enabled": True,
    "autoAllowBashIfSandboxed": True,
    "allowUnsandboxedCommands": False,
    "excludedCommands": [],
    "network": {
        "allowUnixSockets": [],
        "allowAllUnixSockets": False,
        "allowLocalBinding": False,
        "allowedDomains": []
    },
    "enableWeakerNestedSandbox": True
}


def main():
    cwd = os.getcwd()

    # git worktree 内では実行しない（session_utils.is_worktree で判定）
    if is_worktree(cwd):
        print('[enable-sandbox] git worktree 内での実行のためスキップします。')
        return

    settings_path = os.path.join(cwd, '.claude', 'settings.json')
    if not os.path.exists(settings_path):
        print(f'[enable-sandbox] settings.json が見つかりません: {settings_path}')
        return

    with open(settings_path, 'r', encoding='utf-8') as f:
        try:
            settings = json.load(f)
        except json.JSONDecodeError as e:
            print(f'[enable-sandbox] settings.json の JSON 解析に失敗しました: {e}')
            return

    if settings.get('sandbox', {}).get('enabled') is True:
        print('[enable-sandbox] sandbox はすでに有効です。')
        return

    settings['sandbox'] = FULL_SANDBOX_CONFIG

    # アトミック書き込み: 一時ファイルに書き込んでから os.replace() で置換する
    tmp_path = None
    try:
        settings_dir = os.path.dirname(settings_path)
        fd, tmp_path = tempfile.mkstemp(dir=settings_dir, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as tmp_f:
                json.dump(settings, tmp_f, ensure_ascii=False, indent=2)
                tmp_f.write('\n')
        except Exception:
            os.close(fd)
            raise
        os.replace(tmp_path, settings_path)
        tmp_path = None  # os.replace が成功したので finally でのクリーンアップ不要
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    print('[enable-sandbox] sandbox を有効化しました。Claude Code 再起動後に反映されます。')


if __name__ == '__main__':
    main()
