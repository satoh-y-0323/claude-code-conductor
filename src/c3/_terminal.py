"""Shared terminal helpers for c3 CLI subcommands.

Used by cli_doctor.py / cli_status.py / future cli_*.py to keep the
``_supports_color`` and ANSI-related logic in one place.
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
_DISALLOWED_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


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
    into the terminal. Newlines / tabs are preserved.
    """
    if not s:
        return s
    return _DISALLOWED_CONTROL_RE.sub("", s)
