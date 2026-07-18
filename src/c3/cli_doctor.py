"""``c3 doctor`` - diagnose the C3 installation health."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

from c3._terminal import supports_color as _supports_color
from c3.paths import claude_root_for
from c3.platforms import PLATFORM_CHOICES, expand_platforms

_OK = "OK"
_WARN = "WARN"
_ERR = "ERR"

_MCP_STARTUP_TIMEOUT_SEC = 10
_LAUNCHER_PROBE_TIMEOUT_SEC = 10


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
        findings.extend(_check_hook_launchers())
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
        # User-authored JSON syntax error: actionable, must surface as ERR so
        # `c3 doctor` exits non-zero (regression fix for Round 2, where this
        # was collateral-downgraded to WARN by the SR-V-001 resilience patch).
        return _ERR, "settings.json", f"invalid JSON: {exc}"
    except (OSError, RecursionError, MemoryError, UnicodeDecodeError) as exc:
        # Resource / environment failures (deep nesting -> RecursionError,
        # gigantic file -> MemoryError, IO error -> OSError, non-UTF-8 bytes ->
        # UnicodeDecodeError): degrade gracefully to WARN, do not crash doctor.
        return _WARN, "settings.json", f"skipped (could not parse settings.json: {exc})"
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


def _check_hook_launchers() -> list[tuple[str, str, str]]:
    """Verify that every hook / statusLine launcher token in settings.json can
    actually be launched in the current shell PATH (architecture §2-4).

    Judgement (``shutil.which``) and the ``--version`` probe run once per UNIQUE
    launcher token, not once per settings.json entry.
    """
    findings: list[tuple[str, str, str]] = []

    # Scope-limitation disclaimer: this diagnoses the current shell's PATH, not
    # the harness's hook-spawn context (architecture §2-4).
    findings.append(
        (
            _OK,
            "hook launcher scope",
            "本検証は doctor を実行している現在のシェル PATH に対するもので、"
            "ハーネスの hook 起動文脈の完全な再現ではありません",
        )
    )

    root = claude_root_for(Path.cwd())
    if root is None:
        findings.append((_WARN, "hook launchers", "skipped (.claude/ not found)"))
        return findings

    settings = root / ".claude" / "settings.json"
    if not settings.is_file():
        findings.append(
            (_WARN, "hook launchers", f"skipped (settings.json missing at {settings})")
        )
        return findings

    try:
        payload = json.loads(settings.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # Match _check_settings_json(): a genuine JSON syntax error is an
        # actionable user mistake and must surface as ERR ("align to the
        # stronger side" to resolve the prior asymmetry between the two checks).
        findings.append(
            (_ERR, "hook launchers", f"invalid JSON: {exc}")
        )
        return findings
    except (OSError, RecursionError, MemoryError, UnicodeDecodeError) as exc:
        # Resource / environment failures degrade gracefully to WARN.
        findings.append(
            (_WARN, "hook launchers", f"skipped (could not parse settings.json: {exc})")
        )
        return findings

    tokens = _extract_launcher_tokens(payload)
    if not tokens:
        findings.append(
            (_WARN, "hook launchers", "no hook / statusLine launchers found in settings.json")
        )
        return findings

    for token in tokens:  # already deduped + order-preserving
        findings.append(_judge_launcher_token(token))
    return findings


def _extract_launcher_tokens(payload: object) -> list[str]:
    """Collect the unique launcher tokens (first whitespace token of each
    ``command``) across every hooks entry and the statusLine, preserving order.
    """
    tokens: list[str] = []
    seen: set[str] = set()

    def add(command: object) -> None:
        if not isinstance(command, str):
            return
        parts = command.split()
        if not parts:
            return
        token = parts[0]
        if token not in seen:
            seen.add(token)
            tokens.append(token)

    if not isinstance(payload, dict):
        return tokens

    hooks = payload.get("hooks")
    if isinstance(hooks, dict):
        for event_groups in hooks.values():
            if not isinstance(event_groups, list):
                continue
            for group in event_groups:
                if not isinstance(group, dict):
                    continue
                entries = group.get("hooks")
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if isinstance(entry, dict):
                        add(entry.get("command"))

    status_line = payload.get("statusLine")
    if isinstance(status_line, dict):
        add(status_line.get("command"))

    return tokens


def _judge_launcher_token(token: str) -> tuple[str, str, str]:
    """Judge a single unique launcher token (which resolution + optional probe).

    3 branches per architecture §2-4: c3 / python(legacy) / catch-all unknown.
    """
    label = f"hook launcher ({token})"
    resolved = shutil.which(token)

    if token == "c3":
        if resolved is None:
            return (
                _ERR,
                label,
                "c3 が PATH で解決できません。pip インストール環境の PATH を確認"
                "してください（venv 未 activate 等）",
            )
        if not _probe_version_ok(resolved):
            return (
                _ERR,
                label,
                f"{resolved} は PATH で解決できましたが、`--version` プローブに失敗しました。"
                "この環境では全 hook が起動していません（c3 が実際には起動できない状態）。"
                "`pip install --force-reinstall claude-code-conductor` 等で c3 を"
                "再インストールしてください（壊れた c3 自身に依存する `c3 update` は"
                "この状況では使えません）",
            )
        return _OK, label, f"{resolved}（`--version` プローブ成功）"

    if token == "python":
        if resolved is None:
            return (
                _ERR,
                label,
                "この環境では全 hook が起動していません（python が PATH で解決できません）。"
                "`c3 update` で修復してください",
            )
        return (
            _WARN,
            label,
            f"旧形式の python 起動子です（{resolved}）。`c3 update` で c3 run 形式へ"
            "更新することを推奨します",
        )

    # catch-all: any other unknown launcher token (architecture §2-4).
    if resolved is None:
        return (
            _ERR,
            label,
            f"{token} が PATH で解決できず、該当 hook が起動しません。"
            "`c3 update` で settings.json を再生成してください",
        )
    return (
        _OK,
        label,
        f"カスタム起動子として通知: {token} -> {resolved}",
    )


def _probe_version_ok(resolved: str) -> bool:
    """Run ``<resolved> --version`` and report whether it exited cleanly."""
    try:
        result = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_LAUNCHER_PROBE_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


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
    if config.is_file():
        command = _extract_codex_mcp_command(config.read_text(encoding="utf-8"))
        findings.append(_check_mcp_command_startup("codex MCP startup", command, "codex"))
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
    if mcp.is_file():
        command = _extract_cursor_mcp_command(mcp.read_text(encoding="utf-8"))
        findings.append(_check_mcp_command_startup("cursor MCP startup", command, "cursor"))
    cursor = shutil.which("cursor-agent") or shutil.which("cursor")
    if cursor is None:
        findings.append((_WARN, "cursor binary", "not on PATH; install Cursor CLI/editor to use the adapter"))
    else:
        findings.append((_OK, "cursor binary", cursor))
    return findings


def _extract_codex_mcp_command(config_text: str) -> str | None:
    """Read the ``command`` value from the ``[mcp_servers.c3]`` table.

    Uses a targeted regex rather than a full TOML parser: ``tomllib`` is only
    available from Python 3.11 while C3 supports 3.10+, and the generated
    config is always a single-line ``key = "value"`` pair under the section.
    """
    match = re.search(
        r'(?ms)^\[mcp_servers\.c3\]\s*$.*?^command\s*=\s*"((?:[^"\\]|\\.)*)"',
        config_text,
    )
    if match is None:
        return None
    return match.group(1).replace('\\"', '"').replace("\\\\", "\\")


def _extract_cursor_mcp_command(mcp_json_text: str) -> str | None:
    try:
        payload = json.loads(mcp_json_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        return None
    c3 = servers.get("c3")
    if not isinstance(c3, dict):
        return None
    command = c3.get("command")
    return command if isinstance(command, str) else None


def _check_mcp_command_startup(
    label: str, command: str | None, platform: str
) -> tuple[str, str, str]:
    """Verify that the generated MCP ``command`` can actually launch and import c3."""
    hint = f"run `c3 update --platform {platform}` to regenerate"
    if not command:
        return _WARN, label, f"command not found in generated config; {hint}"

    resolvable = (Path(command).is_absolute() and Path(command).is_file()) or (
        shutil.which(command) is not None
    )
    if not resolvable:
        return _WARN, label, f"command not executable: {command}; {hint}"

    try:
        result = subprocess.run(
            [command, "-c", "import c3"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_MCP_STARTUP_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _WARN, label, f"failed to launch {command}: {exc}; {hint}"

    if result.returncode != 0:
        detail = next(
            (line for line in reversed(result.stderr.strip().splitlines()) if line.strip()),
            "import c3 failed",
        )
        return _WARN, label, f'`{command} -c "import c3"` failed: {detail}; {hint}'

    return _OK, label, f"{command} -c \"import c3\" succeeded"


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
