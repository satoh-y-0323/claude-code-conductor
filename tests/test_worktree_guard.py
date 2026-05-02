"""Tests for .claude/hooks/worktree_guard.py

Tests are organised around four scenarios:
1. PO_WORKTREE_GUARD not set  → exits 0 (guard disabled)
2. PO_WORKTREE_GUARD not set  → stderr must be EMPTY (no noisy print)  [RED – production code still prints]
3. PO_WORKTREE_GUARD=1        → Write inside worktree passes (exit 0)
4. PO_WORKTREE_GUARD=1        → Write outside worktree is blocked (exit 2)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Absolute path to the hook under test
_HOOK = Path(__file__).parent.parent / ".claude" / "hooks" / "worktree_guard.py"

# Absolute path to the template copy of the hook
_TEMPLATE_HOOK = (
    Path(__file__).parent.parent
    / "src"
    / "c3"
    / "_template"
    / ".claude"
    / "hooks"
    / "worktree_guard.py"
)


def _run_guard(
    payload: dict,
    *,
    env_guard: str | None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """Run worktree_guard.py as a subprocess, feeding *payload* via stdin."""
    env = {}
    if env_guard is not None:
        env["PO_WORKTREE_GUARD"] = env_guard

    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=cwd,
    )


# ---------------------------------------------------------------------------
# 1. PO_WORKTREE_GUARD unset → exits 0
# ---------------------------------------------------------------------------

def test_guard_disabled_exits_zero_when_env_not_set(tmp_path: Path):
    """When PO_WORKTREE_GUARD is absent the hook must exit 0 without blocking."""
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "/some/outside/path/file.txt"},
    }
    result = _run_guard(payload, env_guard=None, cwd=str(tmp_path))
    assert result.returncode == 0, (
        f"Expected exit 0 when guard is disabled, got {result.returncode}"
    )


# ---------------------------------------------------------------------------
# 2. PO_WORKTREE_GUARD unset → stderr must be EMPTY (no noisy print)  [RED]
# ---------------------------------------------------------------------------

def test_guard_disabled_silent_when_env_not_set(tmp_path: Path):
    """When PO_WORKTREE_GUARD is absent the hook must exit 0 with NO stderr output.

    This is the Red phase: current code still prints to stderr, so this test
    should FAIL until the production code removes the print statement.
    """
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "/some/outside/path/file.txt"},
    }
    result = _run_guard(payload, env_guard=None, cwd=str(tmp_path))
    assert result.returncode == 0, (
        f"Expected exit 0 when guard is disabled, got {result.returncode}"
    )
    assert not result.stderr.strip(), (
        "Expected NO stderr output when PO_WORKTREE_GUARD is not set, "
        f"but got: {result.stderr!r}. "
        "The print statement must be removed from the production code."
    )


def test_template_guard_disabled_silent_when_env_not_set(tmp_path: Path):
    """Same test but for the template copy of worktree_guard.py.

    This test is skipped if the template file does not yet exist. Once
    src/c3/_template/.claude/hooks/worktree_guard.py is present it must
    also exit 0 silently when PO_WORKTREE_GUARD is not set.
    """
    import pytest

    if not _TEMPLATE_HOOK.exists():
        pytest.skip(f"Template hook not found at {_TEMPLATE_HOOK}; skipping.")

    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "/some/outside/path/file.txt"},
    }
    env = {}  # no PO_WORKTREE_GUARD
    result = subprocess.run(
        [sys.executable, str(_TEMPLATE_HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0
    assert not result.stderr.strip(), (
        f"Template hook should produce NO stderr when guard disabled, got: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# 3. PO_WORKTREE_GUARD=1 → Write inside worktree passes
# ---------------------------------------------------------------------------

def test_write_inside_worktree_is_allowed(tmp_path: Path):
    """A Write whose file_path resolves inside the CWD must exit 0."""
    target = tmp_path / "subdir" / "file.txt"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target)},
    }
    result = _run_guard(payload, env_guard="1", cwd=str(tmp_path))
    assert result.returncode == 0, (
        f"Write inside worktree should be allowed (exit 0), got {result.returncode}.\n"
        f"stderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# 4. PO_WORKTREE_GUARD=1 → Write outside worktree is blocked
# ---------------------------------------------------------------------------

def test_write_outside_worktree_is_blocked(tmp_path: Path):
    """A Write whose file_path resolves outside the CWD must exit 2."""
    # Use the parent of tmp_path as an outside target; it is guaranteed to be
    # outside tmp_path (which is the simulated worktree root / CWD).
    outside = tmp_path.parent / "outside_file.txt"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(outside)},
    }
    result = _run_guard(payload, env_guard="1", cwd=str(tmp_path))
    assert result.returncode == 2, (
        f"Write outside worktree should be blocked (exit 2), got {result.returncode}.\n"
        f"stderr: {result.stderr}"
    )
    assert result.stderr.strip(), (
        "Blocked operation must also emit a message to stderr."
    )
