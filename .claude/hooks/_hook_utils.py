#!/usr/bin/env python3
"""Shared utilities for .claude/hooks/ scripts (配布対象).

複数 hook で共有するヘルパー関数を集約する。各 hook はスタンドアロン実行されるため、
このファイルへのアクセスは `sys.path.insert(0, dirname(__file__))` で hooks/ を
PYTHONPATH に追加してから `from _hook_utils import ...` する経路を取る。

## Exports

- ``write_debug_log(log_path, line)`` — ``C3_HOOK_DEBUG=1`` のときのみログを追記する
  fail-safe な書き込みヘルパー。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

DEBUG_ENV = "C3_HOOK_DEBUG"

# 制御文字を除去するための正規表現。debug ログは端末には直接表示されないが、
# ファイルが汚染されると後段で `cat` などで確認した際にエスケープが解釈される
# 可能性があるため除去する。
#
# 除去範囲:
#   - C0 制御文字 (\x00-\x1f) — NUL/BEL/BS/HT/LF/VT/FF/CR/ESC など。ANSI エスケープ
#     シーケンスの ESC (\x1b) もここに含まれる
#   - DEL (\x7f) — 古い端末で破壊的削除制御に使われる
#   - C1 制御文字 (\x80-\x9f) — Latin-1 拡張領域の制御文字。一部の端末・ロケールで
#     エスケープシーケンスのプリフィクス（例: CSI = \x9b）として解釈される
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def write_debug_log(log_path: Path, line: str) -> None:
    """``C3_HOOK_DEBUG=1`` のとき、ログファイルに ``ISO8601 line`` を 1 行追記する。

    - 環境変数未設定なら即 return（コスト 0）。
    - ファイル作成・書き込みに失敗しても本体動作を止めない（``OSError`` を握りつぶす）。
    - 各 hook 固有のフォーマットはこの関数の呼び出し側で組み立て、``line`` 引数として渡す。
    - ``log_path`` は ``Path`` 前提。各 hook 側で ``__file__`` ベースの絶対パスに統一すること。
    - ``line`` に含まれる C0/C1 制御文字（ANSI ESC を含む）と DEL は除去してから書き込む。
      呼び出し側 hook の入力に制御文字が混入してもログファイルが汚染されないようにする。
    """
    if os.environ.get(DEBUG_ENV) != "1":
        return
    try:
        import datetime as _dt
        log_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = _dt.datetime.now().isoformat(timespec="seconds")
        sanitized = _CONTROL_CHARS_RE.sub("", str(line))
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{timestamp} {sanitized}\n")
    except OSError:
        pass
