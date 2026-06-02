"""``c3 doctor`` - diagnose the C3 installation health."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from c3._terminal import supports_color as _supports_color
from c3.paths import claude_root_for
from c3.platforms import PLATFORM_CHOICES, expand_platforms

_OK = "OK"
_WARN = "WARN"
_ERR = "ERR"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "doctor",
        help="Run diagnostics on the local C3 setup",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only failures and warnings",
    )
    parser.add_argument(
        "--platform",
        choices=PLATFORM_CHOICES,
        default="claude",
        help="Target host diagnostics to run. Defaults to claude.",
    )
    parser.set_defaults(handler=handle)


def handle(args: argparse.Namespace) -> int:
    color = _supports_color()
    findings: list[tuple[str, str, str]] = []  # (status, label, detail)
    platforms = expand_platforms(args.platform)

    findings.append(_check_claude_dir())
    if "claude" in platforms:
        findings.append(_check_settings_json())
        findings.append(_check_claude_binary())
    if "codex" in platforms:
        findings.extend(_check_codex_adapter())
    if "cursor" in platforms:
        findings.extend(_check_cursor_adapter())
    if "opencode" in platforms:
        findings.extend(_check_opencode_adapter())

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
            "not on PATH; install Claude Code CLI before invoking c3 skills",
        )
    return _OK, "claude binary", path


def _check_codex_adapter() -> list[tuple[str, str, str]]:
    root = claude_root_for(Path.cwd())
    if root is None:
        return [(_WARN, "codex adapter", "skipped (.claude/ not found)")]
    findings = []
    agents = root / "AGENTS.md"
    skills = root / ".agents" / "skills"
    config = root / ".codex" / "config.toml"
    custom_agents = root / ".codex" / "agents"
    findings.append(_file_or_warn("AGENTS.md", agents, "run `c3 init --platform codex`"))
    findings.append(_dir_or_warn(".agents/skills", skills, "run `c3 init --platform codex`"))
    findings.append(_file_or_warn(".codex/config.toml", config, "run `c3 init --platform codex`"))
    findings.append(_dir_or_warn(".codex/agents", custom_agents, "run `c3 init --platform codex`"))
    codex = shutil.which("codex")
    if codex is None:
        findings.append((_WARN, "codex binary", "not on PATH; install Codex CLI to use the adapter"))
    else:
        findings.append((_OK, "codex binary", codex))
    return findings


def _check_opencode_adapter() -> list[tuple[str, str, str]]:
    root = claude_root_for(Path.cwd())
    if root is None:
        return [(_WARN, "opencode adapter", "skipped (.claude/ not found)")]
    findings = []
    agents_md = root / "AGENTS.md"
    opencode_agents = root / ".opencode" / "agents"
    findings.append(_file_or_warn("AGENTS.md", agents_md, "run `c3 init --platform opencode`"))
    findings.append(
        _dir_or_warn(".opencode/agents", opencode_agents, "run `c3 init --platform opencode`")
    )
    opencode = shutil.which("opencode")
    if opencode is None:
        findings.append((_WARN, "opencode binary", "not on PATH; install OpenCode CLI to use the adapter"))
    else:
        findings.append((_OK, "opencode binary", opencode))
    return findings


def _check_cursor_adapter() -> list[tuple[str, str, str]]:
    root = claude_root_for(Path.cwd())
    if root is None:
        return [(_WARN, "cursor adapter", "skipped (.claude/ not found)")]
    findings = []
    rule = root / ".cursor" / "rules" / "c3-core.mdc"
    mcp = root / ".cursor" / "mcp.json"
    findings.append(_file_or_warn(".cursor/rules/c3-core.mdc", rule, "run `c3 init --platform cursor`"))
    findings.append(_file_or_warn(".cursor/mcp.json", mcp, "run `c3 init --platform cursor`"))
    cursor = shutil.which("cursor-agent") or shutil.which("cursor")
    if cursor is None:
        findings.append((_WARN, "cursor binary", "not on PATH; install Cursor CLI/editor to use the adapter"))
    else:
        findings.append((_OK, "cursor binary", cursor))
    return findings


def _file_or_warn(label: str, path: Path, hint: str) -> tuple[str, str, str]:
    if path.is_file():
        return _OK, label, str(path)
    return _WARN, label, f"missing at {path}; {hint}"


def _dir_or_warn(label: str, path: Path, hint: str) -> tuple[str, str, str]:
    if path.is_dir():
        return _OK, label, str(path)
    return _WARN, label, f"missing at {path}; {hint}"


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
