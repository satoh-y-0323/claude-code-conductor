#!/usr/bin/env python3
"""Utility: ensure full sandbox config is present in settings.json."""

import json
import os
import sys

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

    # git worktree 内では実行しない（.git がファイルの場合は worktree）
    git_path = os.path.join(cwd, '.git')
    if os.path.exists(git_path) and os.path.isfile(git_path):
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

    with open(settings_path, 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
        f.write('\n')

    print('[enable-sandbox] sandbox を有効化しました。Claude Code 再起動後に反映されます。')


if __name__ == '__main__':
    main()
