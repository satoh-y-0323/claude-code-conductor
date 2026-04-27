#!/usr/bin/env python3
"""Utility: clear ~/.claude/file-history/ directory."""

import os
import shutil
import sys

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

FILE_HISTORY_DIR = os.path.join(os.path.expanduser('~'), '.claude', 'file-history')


def main():
    if not os.path.isdir(FILE_HISTORY_DIR):
        print('[clear-file-history] file-history フォルダが存在しません。スキップします。')
        return

    entries = os.listdir(FILE_HISTORY_DIR)
    deleted = 0

    for name in entries:
        full_path = os.path.join(FILE_HISTORY_DIR, name)
        try:
            if os.path.isdir(full_path):
                shutil.rmtree(full_path)
            else:
                os.unlink(full_path)
            deleted += 1
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f'[clear-file-history] 削除に失敗: {name} ({e})')

    print(f'[clear-file-history] {deleted} 件削除しました。')


if __name__ == '__main__':
    main()
