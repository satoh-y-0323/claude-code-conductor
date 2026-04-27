#!/usr/bin/env python3
"""Utility: clean up tmp files left over from previous sessions."""

import os
import glob

def main():
    claude_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tmp_dir = os.path.join(claude_dir, 'tmp')

    if not os.path.isdir(tmp_dir):
        print('[clear-file-history] 削除対象なし。')
        return

    files = [f for f in glob.glob(os.path.join(tmp_dir, '*')) if os.path.isfile(f)]
    for f in files:
        os.remove(f)

    print(f'[clear-file-history] {len(files)} 件削除しました。')

if __name__ == '__main__':
    main()
