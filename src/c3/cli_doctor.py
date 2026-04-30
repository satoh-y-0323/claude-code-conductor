"""``c3 doctor`` - diagnose the C3 installation health."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from c3.paths import claude_root_for
from c3.po.detect import detect_po

_OK = "OK"
_WARN = "WARN"
_ERR = "ERR"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "doctor",
        help="Run diagnostics on the local C3 setup",
    )
    parser.add_argument(
        "--check",
        choices=["all", "po-only"],
        default="all",
        help="Limit checks to a subset (default: all)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only failures and warnings",
    )
    parser.set_defaults(handler=handle)


def handle(args: argparse.Namespace) -> int:
    color = _supports_color()
    findings: list[tuple[str, str, str]] = []  # (status, label, detail)

    if args.check == "po-only":
        findings.append(_check_po())
    else:
        findings.append(_check_claude_dir())
        findings.append(_check_settings_json())
        findings.append(_check_claude_binary())
        findings.append(_check_po())

    exit_code = 0
    for status, label, detail in findings:
        if args.quiet and status == _OK:
            continue
        print(_format(status, label, detail, color=color))
        if status == _ERR:
            exit_code = 1

    return exit_code


def _check_claude_dir() -> tuple[str, str, str]:
    root = claude_root_for(Path.cwd())
    if root is None:
        return (
            _WARN,
            ".claude/ directory",
            "not found from cwd; run `c3 init` to scaffold one",
        )
    return _OK, ".claude/ directory", str(root / ".claude")


def _check_settings_json() -> tuple[str, str, str]:
    root = claude_root_for(Path.cwd())
    if root is None:
        return _WARN, "settings.json", "skipped (.claude/ not found)"
    settings = root / ".claude" / "settings.json"
    if not settings.is_file():
        return _WARN, "settings.json", f"missing at {settings}"
    try:
        json.loads(settings.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _ERR, "settings.json", f"invalid JSON: {exc}"
    return _OK, "settings.json", str(settings)


def _check_claude_binary() -> tuple[str, str, str]:
    path = shutil.which("claude")
    if path is None:
        return (
            _WARN,
            "claude binary",
            "not on PATH; install Claude Code CLI before running parallel-orchestra",
        )
    return _OK, "claude binary", path


def _check_po() -> tuple[str, str, str]:
    available, version, cli_path = detect_po()
    if available:
        ver = version or "unknown version"
        return _OK, "parallel-orchestra", f"{ver} at {cli_path}"
    return (
        _WARN,
        "parallel-orchestra",
        (
            "not installed (optional). 並列実行を使うには "
            "`pip install parallel-orchestra` を実行してください。"
            "詳細: https://pypi.org/project/parallel-orchestra/"
        ),
    )


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    return True


def _format(status: str, label: str, detail: str, *, color: bool) -> str:
    icon = {
        _OK: "[OK]",
        _WARN: "[WARN]",
        _ERR: "[ERR]",
    }[status]
    if color:
        ansi = {
            _OK: "\033[32m",
            _WARN: "\033[33m",
            _ERR: "\033[31m",
        }[status]
        icon = f"{ansi}{icon}\033[0m"
    return f"  {icon} {label}: {detail}"
