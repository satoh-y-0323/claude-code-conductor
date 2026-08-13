"""Shared terminal helpers for c3 CLI subcommands.

Used by cli_doctor.py, cli_tier.py, cli_ask.py, question.py, and future
cli_*.py to keep terminal-facing logic in one place:

- ``supports_color`` / ``strip_ansi`` / ``sanitize_terminal_text``: ANSI
  color support detection, escape-sequence stripping, and sanitization of
  untrusted text before printing.
- ``stdin_is_interactive_console``: console-attachment detection for stdin
  (whether an interactive console is really attached, as opposed to a NUL /
  pipe / file redirection).
"""

from __future__ import annotations

import os
import re
import sys


# CSI sequences with final byte 'm' (color / SGR). Other escape sequences
# (e.g. \033c reset, OSC \033]0;...\007 title) are not handled by _strip_ansi
# and would skew column-width calculations if they appeared in cell text.
_CSI_M_RE = re.compile(r"\033\[[0-9;]*m")

# Control characters disallowed when printing untrusted text to the terminal.
# Allow newline (\n), tab (\t), carriage return (\r) but strip any other
# C0 control or escape character (\x1b) so that ANSI/title-injection cannot
# happen via DB-stored values such as ``current_step`` / ``error_message``.
# Also strip the Unicode line/paragraph separators U+2028 / U+2029 so they
# match the init-session SR L-2 sanitization spec (removes U+2028 / U+2029).
# NEL (U+0085) is intentionally NOT included per that spec.
_DISALLOWED_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f" + chr(0x2028) + chr(0x2029) + "]")


def supports_color() -> bool:
    """Return True if stdout supports ANSI color sequences.

    Honors the NO_COLOR environment variable (https://no-color.org/) and
    requires stdout to be a TTY.
    """
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    return True


def strip_ansi(s: str) -> str:
    """Remove ANSI CSI 'm' (SGR / color) sequences for visible-width calc.

    Note: handles only ``\\033[...m`` style sequences. Other escape sequences
    such as cursor movement (``\\033[H``), screen clear (``\\033[2J``), or
    OSC title-set are intentionally not stripped here because the cells we
    measure should never contain them.
    """
    return _CSI_M_RE.sub("", s)


def sanitize_terminal_text(s: str) -> str:
    """Strip control / escape characters from untrusted text before printing.

    Used for DB-sourced strings (``current_step`` / ``error_message``) so
    they cannot inject ANSI escape sequences (title, cursor, screen clear)
    into the terminal. Newlines / tabs / carriage returns are preserved.
    The Unicode line/paragraph separators U+2028 / U+2029 are also stripped
    (init-session SR L-2 sanitization spec); NEL (U+0085) is out of scope.
    """
    if not s:
        return s
    return _DISALLOWED_CONTROL_RE.sub("", s)


def stdin_is_interactive_console() -> bool:
    """Return True only when stdin is attached to a real interactive console.

    Windows の CRT ``_isatty()``（``sys.stdin.isatty()`` の内部実装）は対象ハンドルが
    ``FILE_TYPE_CHAR`` かどうかしか見ておらず、NUL デバイス（``subprocess.DEVNULL`` /
    ``NUL`` リダイレクト）もコンソールと同じ ``FILE_TYPE_CHAR`` に分類されるため
    誤って True を返す（実測: debug-analysis-20260814-011032.md）。
    Windows では ``GetConsoleMode``（実コンソールハンドルに対してのみ成功する
    Win32 API）で実際のコンソール接続を確認し、NUL・パイプ・ファイルいずれの
    リダイレクトも False として扱う。POSIX は ``isatty()`` が ``/dev/null`` に
    対して標準どおり正しく False を返すためそのまま使う。
    """
    if os.name == "nt":
        return _windows_console_attached()
    return sys.stdin.isatty()


def _windows_console_attached() -> bool:
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        handle = msvcrt.get_osfhandle(0)
        kernel32 = ctypes.windll.kernel32
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        mode = wintypes.DWORD()
        return bool(kernel32.GetConsoleMode(wintypes.HANDLE(handle), ctypes.byref(mode)))
    except Exception:
        # 判定不能ならすべて非対話扱いに倒す (fail-closed)。answer_questions() 経由の
        # 呼び出し位置も含め、想定外の例外型で対話ゲートを突破させない。
        return False
