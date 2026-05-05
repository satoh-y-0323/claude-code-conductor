"""
Tests for pre_compact.py basic runtime behavior.

These tests verify that:
  1. Normal execution outputs valid JSON containing the 'additionalContext' key.
  2. Worktree detection exits 0 with no stdout output.

Implementation notes:
  - pre_compact.py is invoked via subprocess so that sys.exit() and stdout/stdin
    behavior are tested end-to-end without mocking.
  - The session file is written to the real .claude/memory/sessions/ directory
    (path is derived from __file__ inside pre_compact.py, not from cwd).
    This is acceptable for integration tests.
  - cwd is set to tmp_path to control the is_worktree() check, which inspects
    os.getcwd()/.git to determine whether we are inside a git worktree.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parent.parent
PRE_COMPACT_PY = WORKTREE_ROOT / ".claude" / "hooks" / "pre_compact.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_valid_input() -> bytes:
    """Return a minimal valid JSON payload for pre_compact.py stdin."""
    return json.dumps({"trigger": "manual", "context_items_before": 10}).encode()


def _run_pre_compact(cwd: Path, stdin: bytes) -> subprocess.CompletedProcess:
    """Run pre_compact.py as a subprocess with the given cwd and stdin bytes."""
    return subprocess.run(
        [sys.executable, str(PRE_COMPACT_PY)],
        input=stdin,
        capture_output=True,
        cwd=str(cwd),
    )


# ---------------------------------------------------------------------------
# Test Case 1: Normal execution — stdout is valid JSON with additionalContext
# ---------------------------------------------------------------------------

class TestPreCompactNormalExecution:
    """pre_compact.py in a non-worktree directory must output valid JSON."""

    def test_stdout_is_valid_json(self, tmp_path: Path) -> None:
        """stdout must be parseable as JSON when run outside a worktree."""
        result = _run_pre_compact(tmp_path, _make_valid_input())
        assert result.returncode == 0, (
            f"pre_compact.py exited with code {result.returncode}.\n"
            f"stderr: {result.stderr.decode(errors='replace')}"
        )
        stdout = result.stdout.strip()
        assert stdout, (
            "stdout must not be empty during normal (non-worktree) execution"
        )
        try:
            json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"stdout is not valid JSON: {exc}\nstdout was: {stdout!r}"
            ) from exc

    def test_stdout_json_has_additional_context_key(self, tmp_path: Path) -> None:
        """stdout JSON must contain hookSpecificOutput.additionalContext."""
        result = _run_pre_compact(tmp_path, _make_valid_input())
        assert result.returncode == 0, (
            f"pre_compact.py exited with code {result.returncode}.\n"
            f"stderr: {result.stderr.decode(errors='replace')}"
        )
        stdout = result.stdout.strip()
        assert stdout, (
            "stdout must not be empty during normal (non-worktree) execution"
        )
        output = json.loads(stdout)
        assert "hookSpecificOutput" in output, (
            f"stdout JSON missing 'hookSpecificOutput' key. "
            f"Got keys: {list(output.keys())}"
        )
        hook_output = output["hookSpecificOutput"]
        assert "additionalContext" in hook_output, (
            f"hookSpecificOutput missing 'additionalContext' key. "
            f"Got keys: {list(hook_output.keys())}"
        )
        assert hook_output["additionalContext"], (
            "'additionalContext' value must not be empty"
        )


# ---------------------------------------------------------------------------
# Test Case 2: Worktree detection — exits 0 with no stdout output
# ---------------------------------------------------------------------------

class TestPreCompactWorktreeDetection:
    """pre_compact.py must exit 0 silently when .git is a file (worktree)."""

    def test_exits_zero_in_worktree(self, tmp_path: Path) -> None:
        """When .git is a file (git worktree), exit code must be 0."""
        git_file = tmp_path / ".git"
        git_file.write_text(
            "gitdir: ../../.git/worktrees/some-worktree\n",
            encoding="utf-8",
        )

        result = _run_pre_compact(tmp_path, _make_valid_input())
        assert result.returncode == 0, (
            f"Expected exit code 0 in worktree, got {result.returncode}.\n"
            f"stderr: {result.stderr.decode(errors='replace')}"
        )

    def test_no_stdout_in_worktree(self, tmp_path: Path) -> None:
        """When .git is a file (git worktree), stdout must be empty."""
        git_file = tmp_path / ".git"
        git_file.write_text(
            "gitdir: ../../.git/worktrees/some-worktree\n",
            encoding="utf-8",
        )

        result = _run_pre_compact(tmp_path, _make_valid_input())
        assert result.stdout.strip() == b"", (
            f"Expected no stdout in worktree, got: {result.stdout!r}"
        )
