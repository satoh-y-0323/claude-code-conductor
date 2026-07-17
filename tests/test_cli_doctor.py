"""Tests for the ``c3 doctor`` CLI command (cli_doctor module).

Covers _check_opencode_adapter (CR L-05 / CR L-06) and the dispatch logic
in handle() for opencode, codex, and cursor platforms.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import c3.cli_doctor as cli_doctor
from c3.adapters import MANAGED_OPENCODE_BEGIN
from c3.cli_doctor import _OK, _WARN


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_args(platform: str = "claude", quiet: bool = False) -> argparse.Namespace:
    """Build a minimal Namespace that handle() accepts."""
    return argparse.Namespace(platform=platform, quiet=quiet)


# ---------------------------------------------------------------------------
# _check_opencode_adapter -- CR L-05: verifies the implemented function
# ---------------------------------------------------------------------------


def test_check_opencode_adapter_warns_when_missing(tmp_path: Path, monkeypatch):
    """When only .claude/ exists and AGENTS.md / .opencode/agents/ are absent,
    _check_opencode_adapter() must return WARN findings for both missing items.
    """
    (tmp_path / ".claude").mkdir()
    monkeypatch.chdir(tmp_path)

    findings = cli_doctor._check_opencode_adapter()

    # AGENTS.md absence should produce a WARN
    assert any(
        status == _WARN and "AGENTS.md" in label
        for status, label, _ in findings
    ), f"Expected WARN for AGENTS.md, got: {findings}"

    # .opencode/agents/ absence should produce a WARN
    assert any(
        status == _WARN and "opencode" in label.lower()
        for status, label, _ in findings
    ), f"Expected WARN for .opencode/agents/, got: {findings}"


def test_check_opencode_adapter_ok_when_present(tmp_path: Path, monkeypatch):
    """When AGENTS.md (containing MANAGED_OPENCODE_BEGIN) and .opencode/agents/
    both exist, _check_opencode_adapter() must return OK findings.
    """
    (tmp_path / ".claude").mkdir()
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text(
        f"{MANAGED_OPENCODE_BEGIN}\n# OpenCode agents\n<!-- END C3 OPENCODE ADAPTER -->\n",
        encoding="utf-8",
    )
    (tmp_path / ".opencode" / "agents").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    findings = cli_doctor._check_opencode_adapter()

    ok_findings = [f for f in findings if f[0] == _OK]
    warn_findings = [f for f in findings if f[0] == _WARN]

    # No WARN for AGENTS.md or .opencode/agents/ when both exist
    assert not any(
        "AGENTS.md" in label for _, label, _ in warn_findings
    ), f"Unexpected WARN for AGENTS.md: {findings}"
    assert not any(
        "opencode" in label.lower() and "agents" in label.lower()
        for _, label, _ in warn_findings
    ), f"Unexpected WARN for .opencode/agents/: {findings}"

    # At least one OK finding should be present
    assert ok_findings, f"Expected at least one OK finding, got: {findings}"


# ---------------------------------------------------------------------------
# dispatch -- verifies handle() routes opencode platform correctly
# ---------------------------------------------------------------------------


def test_doctor_dispatch_includes_opencode(tmp_path: Path, monkeypatch, capsys):
    """handle() with --platform opencode must dispatch _check_opencode_adapter,
    producing findings that include opencode-related labels.
    """
    (tmp_path / ".claude").mkdir()
    monkeypatch.chdir(tmp_path)

    args = _make_args(platform="opencode", quiet=False)

    exit_code = cli_doctor.handle(args)

    captured = capsys.readouterr()
    output = captured.out

    # The output must include at least one opencode-related label.
    # Expected labels: "AGENTS.md" or ".opencode/agents" (mirroring codex/cursor patterns).
    assert (
        "AGENTS.md" in output or ".opencode" in output
    ), f"Expected opencode-related labels in doctor output, got:\n{output!r}"


def test_doctor_dispatch_all_includes_opencode(tmp_path: Path, monkeypatch, capsys):
    """handle() with --platform all must also dispatch _check_opencode_adapter."""
    (tmp_path / ".claude").mkdir()
    monkeypatch.chdir(tmp_path)

    args = _make_args(platform="all", quiet=False)

    exit_code = cli_doctor.handle(args)

    captured = capsys.readouterr()
    output = captured.out

    # With --platform all, both codex/cursor AND opencode checks should appear.
    # Codex check produces "AGENTS.md" and ".codex/" labels.
    # Opencode check should add ".opencode" labels.
    assert ".opencode" in output, (
        f"Expected .opencode in --platform all output, got:\n{output!r}"
    )


# ---------------------------------------------------------------------------
# _check_opencode_adapter binary check -- CR L-06
# ---------------------------------------------------------------------------


def test_check_opencode_adapter_warns_binary_missing(tmp_path: Path, monkeypatch):
    """When shutil.which("opencode") returns None, _check_opencode_adapter()
    must include a WARN finding with label containing "opencode binary".

    CR L-06: mirrors the codex/cursor binary checks for symmetry.
    """
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".opencode" / "agents").mkdir(parents=True)
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text(
        f"{MANAGED_OPENCODE_BEGIN}\n# OpenCode agents\n<!-- END C3 OPENCODE ADAPTER -->\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_doctor.shutil, "which", lambda name: None)

    findings = cli_doctor._check_opencode_adapter()

    assert any(
        status == _WARN and "opencode binary" in label
        for status, label, _ in findings
    ), f"Expected WARN for 'opencode binary' when binary missing, got: {findings}"


def test_check_opencode_adapter_reports_binary_present(tmp_path: Path, monkeypatch):
    """When shutil.which("opencode") returns a path, _check_opencode_adapter()
    must include an OK finding with label "opencode binary" and the path in detail.

    CR L-06: mirrors the codex/cursor binary checks for symmetry.
    """
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".opencode" / "agents").mkdir(parents=True)
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text(
        f"{MANAGED_OPENCODE_BEGIN}\n# OpenCode agents\n<!-- END C3 OPENCODE ADAPTER -->\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    fake_path = "/usr/local/bin/opencode"
    monkeypatch.setattr(cli_doctor.shutil, "which", lambda name: fake_path if name == "opencode" else None)

    findings = cli_doctor._check_opencode_adapter()

    assert any(
        status == _OK and "opencode binary" in label and fake_path in detail
        for status, label, detail in findings
    ), f"Expected OK for 'opencode binary' with path, got: {findings}"


# ---------------------------------------------------------------------------
# MCP startup self-diagnostic — _check_mcp_command_startup
# ---------------------------------------------------------------------------


def test_check_mcp_command_startup_ok_when_import_succeeds(monkeypatch):
    """When the resolved command can `import c3`, the check must return OK."""
    monkeypatch.setattr(
        cli_doctor.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="", stderr=""),
    )

    status, label, detail = cli_doctor._check_mcp_command_startup(
        "codex MCP startup", sys.executable, "codex"
    )

    assert status == _OK
    assert "codex MCP startup" == label
    assert "import c3" in detail


def test_check_mcp_command_startup_warns_when_command_missing():
    """When the config has no `command` value at all, the check must WARN."""
    status, label, detail = cli_doctor._check_mcp_command_startup(
        "codex MCP startup", None, "codex"
    )

    assert status == _WARN
    assert "not found" in detail
    assert "c3 update --platform codex" in detail


def test_check_mcp_command_startup_warns_when_command_not_resolvable(monkeypatch):
    """When the command path/binary cannot be located at all, the check must WARN
    without attempting to spawn a subprocess."""
    monkeypatch.setattr(cli_doctor.shutil, "which", lambda name: None)
    called = []
    monkeypatch.setattr(
        cli_doctor.subprocess,
        "run",
        lambda *a, **k: called.append(a) or subprocess.CompletedProcess(a[0], 0),
    )

    status, label, detail = cli_doctor._check_mcp_command_startup(
        "cursor MCP startup", "/nonexistent/path/to/python", "cursor"
    )

    assert status == _WARN
    assert "not executable" in detail
    assert "c3 update --platform cursor" in detail
    assert called == []


def test_check_mcp_command_startup_warns_when_import_c3_fails(monkeypatch):
    """When the resolved command runs but `import c3` fails (e.g. an isolated
    venv/pipx environment without c3 installed), the check must WARN."""
    monkeypatch.setattr(
        cli_doctor.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 1, stdout="", stderr="ModuleNotFoundError: No module named 'c3'"
        ),
    )

    status, label, detail = cli_doctor._check_mcp_command_startup(
        "codex MCP startup", sys.executable, "codex"
    )

    assert status == _WARN
    assert "No module named" in detail
    assert "c3 update --platform codex" in detail


def test_check_mcp_command_startup_warns_when_launch_raises(monkeypatch):
    """When subprocess.run itself raises (e.g. the binary vanished between the
    resolvability check and the launch), the check must WARN, not raise."""
    def _boom(*a, **k):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(cli_doctor.subprocess, "run", _boom)

    status, label, detail = cli_doctor._check_mcp_command_startup(
        "codex MCP startup", sys.executable, "codex"
    )

    assert status == _WARN
    assert "failed to launch" in detail


# ---------------------------------------------------------------------------
# command extraction from generated adapter configs
# ---------------------------------------------------------------------------


def test_extract_codex_mcp_command_reads_absolute_windows_path():
    config_text = (
        "[mcp_servers.c3]\n"
        'command = "C:\\\\Python312\\\\python.exe"\n'
        'args = ["-m", "c3.mcp_server"]\n'
    )

    command = cli_doctor._extract_codex_mcp_command(config_text)

    assert command == r"C:\Python312\python.exe"


def test_extract_codex_mcp_command_none_when_section_missing():
    assert cli_doctor._extract_codex_mcp_command("[some_other_table]\nfoo = 1\n") is None


def test_extract_cursor_mcp_command_reads_absolute_path():
    mcp_json = '{"mcpServers": {"c3": {"command": "/usr/bin/python3", "args": []}}}'

    command = cli_doctor._extract_cursor_mcp_command(mcp_json)

    assert command == "/usr/bin/python3"


def test_extract_cursor_mcp_command_none_when_invalid_json():
    assert cli_doctor._extract_cursor_mcp_command("{not valid") is None


# ---------------------------------------------------------------------------
# integration — _check_codex_adapter / _check_cursor_adapter wire the
# self-diagnostic in using the real, currently-running interpreter
# ---------------------------------------------------------------------------


def test_check_codex_adapter_includes_ok_mcp_startup_with_real_interpreter(
    tmp_path: Path, monkeypatch
):
    """End-to-end: generate a real adapter config pointed at sys.executable
    (which does have c3 importable under the sandbox PYTHONPATH) and confirm
    the doctor wires the self-diagnostic in as OK."""
    (tmp_path / ".claude").mkdir()
    monkeypatch.chdir(tmp_path)
    from c3.adapters import _write_codex_config

    _write_codex_config(tmp_path, dry_run=False)

    findings = cli_doctor._check_codex_adapter()

    assert any(
        status == _OK and label == "codex MCP startup"
        for status, label, _ in findings
    ), f"Expected OK codex MCP startup finding, got: {findings}"


def test_check_cursor_adapter_includes_ok_mcp_startup_with_real_interpreter(
    tmp_path: Path, monkeypatch
):
    (tmp_path / ".claude").mkdir()
    monkeypatch.chdir(tmp_path)
    from c3.adapters import _write_cursor_mcp

    _write_cursor_mcp(tmp_path, dry_run=False)

    findings = cli_doctor._check_cursor_adapter()

    assert any(
        status == _OK and label == "cursor MCP startup"
        for status, label, _ in findings
    ), f"Expected OK cursor MCP startup finding, got: {findings}"
