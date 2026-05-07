"""Tests for code-review and security-review fixes (Red phase).

Each test is written to FAIL against the current implementation and PASS
after the planned fixes are applied.
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
from pathlib import Path

import pytest

import parallel_orchestra

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

_MINIMAL_MANIFEST_TMPL = """\
---
po_plan_version: "0.1"
name: {name}
cwd: "."
tasks:
{tasks}
---
"""


def _manifest_with_tasks(tasks_yaml: str, name: str = "test-plan") -> str:
    return _MINIMAL_MANIFEST_TMPL.format(name=name, tasks=tasks_yaml)


# ---------------------------------------------------------------------------
# Task 1 / C-1 — __version__ must match pyproject.toml
# ---------------------------------------------------------------------------


def test_version_matches_host_package():
    """__version__ must match the host package version (claude-code-conductor).

    PO is bundled inside claude-code-conductor; it inherits the host's version
    rather than maintaining a separate one.
    """
    init_path = _REPO_ROOT / "src" / "c3" / "__init__.py"
    content = init_path.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', content, re.MULTILINE)
    assert match, "Could not find __version__ in src/c3/__init__.py"
    expected = match.group(1)
    assert parallel_orchestra.__version__ == expected, (
        f"parallel_orchestra.__version__ is {parallel_orchestra.__version__!r} "
        f"but host claude-code-conductor declares {expected!r}"
    )


# ---------------------------------------------------------------------------
# Task 6 / C-5 — dashboard must be disabled when stderr is not a TTY
# ---------------------------------------------------------------------------


def test_dashboard_disabled_when_not_tty(tmp_path, monkeypatch):
    """When stderr is not a TTY, dashboard_enabled=None must disable the dashboard."""
    import parallel_orchestra.runner as runner_module
    from parallel_orchestra import load_manifest, run_manifest

    # Write a minimal manifest with one read-only task
    manifest_text = _manifest_with_tasks(
        "  - id: task1\n    agent: reviewer\n    read_only: true"
    )
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(manifest_text, encoding="utf-8")

    # Make stderr.isatty() return False
    fake_stderr = io.StringIO()
    fake_stderr.isatty = lambda: False  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    # Track whether _Dashboard.start() was called
    start_call_count = [0]
    original_start = runner_module._Dashboard.start

    def tracking_start(self: runner_module._Dashboard) -> None:
        start_call_count[0] += 1
        original_start(self)

    monkeypatch.setattr(runner_module._Dashboard, "start", tracking_start)

    # Provide a fake Popen so Claude subprocess is never actually launched
    class FakePopen:
        def __init__(self, *args, **kwargs):
            self.returncode = 0
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")
            self.pid = 9999

        def wait(self):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    manifest = load_manifest(manifest_path)
    run_manifest(manifest, dashboard_enabled=None, log_enabled=False)

    assert start_call_count[0] == 0, (
        f"_Dashboard.start() was called {start_call_count[0]} time(s); "
        "expected 0 because stderr is not a TTY"
    )


# ---------------------------------------------------------------------------
# Task 12 / S-7 — _sanitize_for_display must strip Unicode direction chars
# ---------------------------------------------------------------------------


def test_sanitize_strips_unicode_direction_chars():
    """_sanitize_for_display must remove Unicode bidirectional control chars."""
    from parallel_orchestra.runner import _sanitize_for_display

    # U+202E RIGHT-TO-LEFT OVERRIDE, U+200B ZERO WIDTH SPACE
    malicious = "‮​悪意あるテキスト"
    result = _sanitize_for_display(malicious)

    assert "‮" not in result, "U+202E (RIGHT-TO-LEFT OVERRIDE) was not removed"
    assert "​" not in result, "U+200B (ZERO WIDTH SPACE) was not removed"


# ---------------------------------------------------------------------------
# Task 14 / S-3 — _write_task_logs must mask ANTHROPIC_API_KEY
# ---------------------------------------------------------------------------


def test_log_masks_api_key(tmp_path, monkeypatch):
    """_write_task_logs must not write raw ANTHROPIC_API_KEY values to disk."""
    from parallel_orchestra.runner import LogConfig, _write_task_logs

    secret = "sk-test-secret-key-abcdef1234"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)

    log_config = LogConfig(base_dir=tmp_path / "logs")

    stdout_with_secret = f"Starting agent...\nAPI_KEY={secret}\nDone."
    _write_task_logs(
        "task1",
        stdout_with_secret,
        "",
        attempt=0,
        log_config=log_config,
    )

    stdout_log = (tmp_path / "logs" / "task1-stdout.log").read_text(encoding="utf-8")
    assert secret not in stdout_log, (
        f"ANTHROPIC_API_KEY value {secret!r} was found unmasked in the log file"
    )


# ---------------------------------------------------------------------------
# Task 16 / S-1 — read_only tasks use --dangerously-skip-permissions (not --read-only)
# read_only controls worktree creation only; it must never be passed to claude as a flag.
# ---------------------------------------------------------------------------


def test_readonly_task_uses_dangerously_skip_permissions(tmp_path, monkeypatch):
    """A task with read_only=true must use --dangerously-skip-permissions, not --read-only.

    read_only is an internal PO control field (worktree vs no-worktree).
    Claude Code CLI has no --read-only flag, so passing it would break execution.
    """
    from parallel_orchestra import load_manifest, run_manifest

    manifest_text = (
        "---\n"
        "po_plan_version: \"0.1\"\n"
        "name: readonly-flag-test\n"
        "cwd: \".\"\n"
        "tasks:\n"
        "  - id: readonly-task\n"
        "    agent: reviewer\n"
        "    read_only: true\n"
        "---\n"
    )
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(manifest_text, encoding="utf-8")

    captured_commands: list[list[str]] = []

    class FakePopen:
        def __init__(self, cmd, *args, **kwargs):
            captured_commands.append(list(cmd))
            self.returncode = 0
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")
            self.pid = 9999

        def wait(self):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    fake_stderr = io.StringIO()
    fake_stderr.isatty = lambda: False  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    manifest = load_manifest(manifest_path)
    run_manifest(manifest, dashboard_enabled=False, log_enabled=False)

    assert captured_commands, "No subprocess.Popen calls were recorded"

    task_cmd = captured_commands[0]
    assert "--dangerously-skip-permissions" in task_cmd, (
        f"--dangerously-skip-permissions was not found in read_only=true command: {task_cmd}"
    )
    assert "--read-only" not in task_cmd, (
        f"--read-only (non-existent Claude flag) was found in command: {task_cmd}"
    )


def test_write_task_gets_dangerously_skip_permissions(tmp_path, monkeypatch):
    """A task with read_only=false must use --dangerously-skip-permissions (regression-prevention).

    Tests _execute_task directly to avoid git-worktree setup complexity.
    """
    import parallel_orchestra.runner as runner_module
    from parallel_orchestra.manifest import Task

    write_task = Task(
        id="write-task",
        agent="coder",
        read_only=False,
        prompt="Do something",
        env={},
    )

    captured_commands: list[list[str]] = []

    class FakePopen:
        def __init__(self, cmd, *args, **kwargs):
            captured_commands.append(list(cmd))
            self.returncode = 0
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")
            self.pid = 9999

        def wait(self):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    # Stub out worktree setup so we don't need a real git repo
    monkeypatch.setattr(
        runner_module,
        "_setup_worktree",
        lambda git_root, task, claude_src_dir=None: (tmp_path, "branch-name"),
    )

    fake_stderr = io.StringIO()
    fake_stderr.isatty = lambda: False  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    runner_module._execute_task(
        write_task,
        "claude",
        git_root=tmp_path,
        effective_cwd=tmp_path,
    )

    assert captured_commands, "No subprocess.Popen calls were recorded"
    task_cmd = captured_commands[0]
    assert "--dangerously-skip-permissions" in task_cmd, (
        f"--dangerously-skip-permissions was not found in write task command: {task_cmd}"
    )
