"""Tests for .claude/hooks/pre_tool.py — TDD Red phase.

These tests verify the *expected* (post-fix) behavior of the hook.
Several tests are expected to FAIL against the current (unfixed) implementation,
demonstrating the security gaps documented in [Sec High-1] and [Sec High-2].
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# Path to the hook script under test
HOOK_SCRIPT = (
    Path(__file__).parent.parent / ".claude" / "hooks" / "pre_tool.py"
)


def _run_hook(command: str) -> subprocess.CompletedProcess:
    """Run the pre_tool.py hook with the given Bash command string.

    Returns the CompletedProcess with returncode, stdout, stderr.
    """
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": command}}
    )
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _is_blocked(result: subprocess.CompletedProcess) -> bool:
    """Return True if the hook blocked the command (exit code 2)."""
    return result.returncode == 2


def _is_allowed(result: subprocess.CompletedProcess) -> bool:
    """Return True if the hook allowed the command (exit code 0)."""
    return result.returncode == 0


# ---------------------------------------------------------------------------
# [Sec High-1] rm -rf detection — correct behavior tests
# ---------------------------------------------------------------------------


class TestRmRfDetection:
    """Tests for rm -rf detection logic."""

    def test_rm_rf_combined_flag_is_blocked(self):
        """rm -rf /path must be blocked."""
        result = _run_hook("rm -rf /some/path")
        assert _is_blocked(result), (
            f"Expected rm -rf to be blocked, got exit={result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    def test_rm_fr_combined_flag_is_blocked(self):
        """rm -fr /path (reversed flags) must be blocked."""
        result = _run_hook("rm -fr /some/path")
        assert _is_blocked(result), (
            f"Expected rm -fr to be blocked, got exit={result.returncode}"
        )

    def test_rm_r_f_separate_flags_is_blocked(self):
        """rm -r -f /path (separate flags) must be blocked."""
        result = _run_hook("rm -r -f /some/path")
        assert _is_blocked(result), (
            f"Expected rm -r -f to be blocked, got exit={result.returncode}"
        )

    def test_rm_recursive_force_long_opts_is_blocked(self):
        """rm --recursive --force /path must be blocked."""
        result = _run_hook("rm --recursive --force /some/path")
        assert _is_blocked(result), (
            f"Expected rm --recursive --force to be blocked, got exit={result.returncode}"
        )

    def test_rm_single_file_is_allowed(self):
        """Plain rm somefile (no -r/-f) must be allowed."""
        result = _run_hook("rm somefile.txt")
        assert _is_allowed(result), (
            f"Expected plain rm to be allowed, got exit={result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    # --- [Sec High-1] BUG TESTS (expected to FAIL on current implementation) ---

    def test_ls_rf_then_rm_somefile_is_NOT_blocked(self):
        """BUG [Sec High-1]: 'ls -rf && rm somefile' must NOT be blocked.

        Current bug: short_flags is collected from the entire command string,
        so '-rf' in 'ls -rf' is incorrectly attributed to 'rm', causing a
        false positive block.

        This test FAILS on the unfixed implementation.
        """
        result = _run_hook("ls -rf && rm somefile")
        assert _is_allowed(result), (
            "[Sec High-1 BUG] 'ls -rf && rm somefile' was incorrectly blocked.\n"
            "The '-rf' flags belong to 'ls', not 'rm'.\n"
            f"exit={result.returncode}, stderr={result.stderr}"
        )

    def test_echo_rf_pipe_rm_somefile_is_NOT_blocked(self):
        """BUG [Sec High-1]: \"echo '-rf' | rm somefile\" must NOT be blocked.

        Current bug: '-rf' from the echo argument is collected into short_flags,
        causing a false positive block on rm.

        This test FAILS on the unfixed implementation.
        """
        result = _run_hook("echo '-rf' | rm somefile")
        assert _is_allowed(result), (
            "[Sec High-1 BUG] \"echo '-rf' | rm somefile\" was incorrectly blocked.\n"
            "The '-rf' comes from echo's argument, not rm's flags.\n"
            f"exit={result.returncode}, stderr={result.stderr}"
        )

    def test_flag_before_rm_keyword_is_NOT_blocked(self):
        """BUG [Sec High-1]: '-rf' appearing before 'rm' in the command must not trigger a block.

        Example: a script passing '-rf' as argument to another tool, then running rm on a file.
        """
        result = _run_hook("mytool -rf data && rm output.txt")
        assert _is_allowed(result), (
            "[Sec High-1 BUG] '-rf' before rm was incorrectly attributed to rm.\n"
            f"exit={result.returncode}, stderr={result.stderr}"
        )


# ---------------------------------------------------------------------------
# git force push detection — warning only (not blocked)
# ---------------------------------------------------------------------------


class TestGitForcePushDetection:
    """Tests for git force push detection logic (warning emitted, command not blocked)."""

    def test_git_push_force_is_allowed_with_warning(self):
        """git push --force must be allowed (exit 0) but emit a warning to stderr."""
        result = _run_hook("git push --force")
        assert _is_allowed(result), (
            f"Expected git push --force to be allowed, got exit={result.returncode}\n"
            f"stderr: {result.stderr}"
        )
        assert result.stderr.strip() != "", (
            "Expected a warning on stderr for git push --force, but got none"
        )

    def test_git_push_f_is_allowed_with_warning(self):
        """git push -f must be allowed (exit 0) but emit a warning to stderr."""
        result = _run_hook("git push -f")
        assert _is_allowed(result), (
            f"Expected git push -f to be allowed, got exit={result.returncode}\n"
            f"stderr: {result.stderr}"
        )
        assert result.stderr.strip() != "", (
            "Expected a warning on stderr for git push -f, but got none"
        )

    def test_git_push_force_with_lease_is_allowed_with_warning(self):
        """git push --force-with-lease must be allowed (exit 0) but emit a warning to stderr."""
        result = _run_hook("git push --force-with-lease")
        assert _is_allowed(result), (
            f"Expected git push --force-with-lease to be allowed, "
            f"got exit={result.returncode}\n"
            f"stderr: {result.stderr}"
        )
        assert result.stderr.strip() != "", (
            "Expected a warning on stderr for git push --force-with-lease, but got none"
        )

    def test_git_push_without_options_is_allowed_without_warning(self):
        """Plain git push must be allowed (exit 0) with no stderr output."""
        result = _run_hook("git push")
        assert _is_allowed(result), (
            f"Expected plain git push to be allowed, got exit={result.returncode}\n"
            f"stderr: {result.stderr}"
        )
        assert result.stderr.strip() == "", (
            f"Expected no warning for plain git push, but got: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# Destructive DB operations — warning only (not blocked)
# ---------------------------------------------------------------------------


class TestDbDestructiveDetection:
    """Tests for DROP TABLE / DROP DATABASE / TRUNCATE detection (warning emitted, command not blocked)."""

    def test_drop_table_is_allowed_with_warning(self):
        """DROP TABLE must be allowed (exit 0) but emit a warning to stderr."""
        result = _run_hook("DROP TABLE users;")
        assert _is_allowed(result), (
            f"Expected DROP TABLE to be allowed, got exit={result.returncode}\n"
            f"stderr: {result.stderr}"
        )
        assert result.stderr.strip() != "", (
            "Expected a warning on stderr for DROP TABLE, but got none"
        )

    def test_drop_database_is_allowed_with_warning(self):
        """DROP DATABASE must be allowed (exit 0) but emit a warning to stderr."""
        result = _run_hook("DROP DATABASE mydb;")
        assert _is_allowed(result), (
            f"Expected DROP DATABASE to be allowed, got exit={result.returncode}\n"
            f"stderr: {result.stderr}"
        )
        assert result.stderr.strip() != "", (
            "Expected a warning on stderr for DROP DATABASE, but got none"
        )

    def test_truncate_is_allowed_with_warning(self):
        """TRUNCATE must be allowed (exit 0) but emit a warning to stderr."""
        result = _run_hook("TRUNCATE orders;")
        assert _is_allowed(result), (
            f"Expected TRUNCATE to be allowed, got exit={result.returncode}\n"
            f"stderr: {result.stderr}"
        )
        assert result.stderr.strip() != "", (
            "Expected a warning on stderr for TRUNCATE, but got none"
        )

    def test_create_table_is_allowed_without_warning(self):
        """CREATE TABLE must be allowed (exit 0) with no stderr output."""
        result = _run_hook("CREATE TABLE foo (id int);")
        assert _is_allowed(result), (
            f"Expected CREATE TABLE to be allowed, got exit={result.returncode}\n"
            f"stderr: {result.stderr}"
        )
        assert result.stderr.strip() == "", (
            f"Expected no warning for CREATE TABLE, but got: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# Non-Bash tool inputs — pass through silently
# ---------------------------------------------------------------------------


class TestNonBashTool:
    """Tests that non-Bash tool inputs are passed through with no output and exit 0."""

    def test_write_tool_is_allowed_without_any_output(self):
        """Non-Bash tool (Write) must exit 0 with no stdout or stderr output,
        even when tool_input contains a dangerous-looking command string.
        """
        payload = json.dumps(
            {"tool_name": "Write", "tool_input": {"command": "rm -rf /"}}
        )
        result = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, (
            f"Expected exit 0 for non-Bash tool, got exit={result.returncode}\n"
            f"stderr: {result.stderr}"
        )
        assert result.stderr.strip() == "", (
            f"Expected no stderr for non-Bash tool, but got: {result.stderr}"
        )
        assert result.stdout.strip() == "", (
            f"Expected no stdout for non-Bash tool, but got: {result.stdout}"
        )


