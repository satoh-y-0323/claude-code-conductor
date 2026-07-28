#!/usr/bin/env python3
"""Shared utilities for .claude/hooks/ scripts (配布対象).

複数 hook で共有するヘルパー関数を集約する。各 hook はスタンドアロン実行されるため、
このファイルへのアクセスは `sys.path.insert(0, dirname(__file__))` で hooks/ を
PYTHONPATH に追加してから `from _hook_utils import ...` する経路を取る。

## Exports

- ``norm_component(s)`` — パス成分を正規化する（`.lower().rstrip('. ')`）。
  ディレクトリ比較・拡張子判定の際にケース非依存化・末尾ドット/スペース除去を行う。
- ``write_debug_log(log_path, line)`` — ``C3_HOOK_DEBUG=1`` のときのみログを追記する
  fail-safe な書き込みヘルパー。
- ``sanitize_for_terminal(text)`` — 端末出力（stderr / stdout JSON）へ載せる前に
  C0 + DEL + C1 制御文字を除去するヘルパー。
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


# NOTE: 同一ロジックが src/c3/cli_update.py::_validate_deletion_path の step 13 に
# 複製されている（import 経路がないため）。変更時は両方を揃えること。
def norm_component(s: str) -> str:
    """パス成分を正規化する（ケース非依存化・末尾ドット/スペース除去）。

    Windows・macOS の既定（ケース非依存 FS）では大文字・小文字の違い、
    末尾のドットやスペースは同一実体を示す。複数ディレクトリ・拡張子判定の際に
    `.lower().rstrip('. ')` で正規化してから比較する。
    """
    return str(s).lower().rstrip('. ')


def sanitize_for_terminal(text: str) -> str:
    """端末インジェクション対策: C0 制御文字 / DEL / C1 制御文字を除去する。

    - 除去範囲は ``_CONTROL_CHARS_RE``（C0 + DEL + C1）と共通。ANSI エスケープの
      ESC (``\\x1b``) と CSI (``\\x9b``) の双方をカバーする。
    - hook が stderr / stdout JSON（additionalContext）へ外部由来の文字列
      （ファイル名など）を載せる前に通す。
    """
    return _CONTROL_CHARS_RE.sub("", str(text))


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
