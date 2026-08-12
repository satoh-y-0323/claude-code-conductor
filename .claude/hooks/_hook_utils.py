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
  C0 + DEL + C1 制御文字に加え、双方向制御・ゼロ幅・行区切り文字を除去するヘルパー。
- ``STRICT4_PREFIXES`` — タイムスタンプ形式のみ許容する prefix の tuple。
  ``timestamp_pattern()`` と組み合わせて使用（report_path_guard / report_contract_check）。
- ``timestamp_pattern(prefix)`` — ``{prefix}YYYYMMDD-HHMMSS.md`` のフルマッチ用
  正規表現（``re.compile`` オブジェクト）を返す。全角数字偽装防止のため ``re.ASCII``
  を指定。
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

# 端末表示用の除去集合。stop.py の `_DISPLAY_SANITIZE_RE` と同一範囲を持つ
# （CR-M-001 / SR-NEW-1: hook 側の表示サニタイズを最も広い集合へ揃える）。
# _CONTROL_CHARS_RE（C0 + DEL + C1）に加えて次を除去する:
#   - U+200B-U+200F — ゼロ幅スペース / ZWNJ / ZWJ / LRM / RLM（表示偽装）
#   - U+2028 / U+2029 — Line/Paragraph Separator（行区切り偽装・JSON パーサ破壊）
#   - U+202A-U+202E — 双方向埋め込み・上書き（RLO によるファイル名偽装）
#   - U+2066-U+2069 — 双方向 isolate
#   - U+FEFF — BOM / ZERO WIDTH NO-BREAK SPACE
# raw string は \uXXXX を解釈しないため、非 ASCII のコードポイントは chr() で連結する。
_TERMINAL_SANITIZE_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f"
    + chr(0x200b) + "-" + chr(0x200f)
    + chr(0x2028) + chr(0x2029)
    + chr(0x202a) + "-" + chr(0x202e)
    + chr(0x2066) + "-" + chr(0x2069)
    + chr(0xfeff)
    + r"]"
)

# strict-4: タイムスタンプ形式のみ許容する prefix の tuple。
# report_path_guard.py / report_contract_check.py が共有する。
STRICT4_PREFIXES = (
    'requirements-report-',
    'architecture-report-',
    'plan-report-',
    'design-review-report-',
)


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
    """端末インジェクション対策: 制御文字と表示偽装文字を除去する。

    - 除去範囲は ``_TERMINAL_SANITIZE_RE``（C0 + DEL + C1 + ゼロ幅 + 行区切り +
      双方向制御 + BOM）。ANSI エスケープの ESC (``\\x1b``) と CSI (``\\x9b``)、
      RLO によるファイル名偽装、改行による偽の警告行の注入をまとめてカバーする。
    - hook が stderr / stdout JSON（additionalContext）へ外部由来の文字列
      （ファイル名など）を載せる前に通す。
    """
    return _TERMINAL_SANITIZE_RE.sub("", str(text))


def timestamp_pattern(prefix: str) -> re.Pattern:
    """``{prefix}YYYYMMDD-HHMMSS.md`` のフルマッチ用パターンを返す。

    re.ASCII を指定してタイムスタンプを ASCII 数字に限定し、全角数字による偽装を防止する。
    """
    return re.compile(re.escape(prefix) + r'\d{8}-\d{6}\.md', re.ASCII)


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
