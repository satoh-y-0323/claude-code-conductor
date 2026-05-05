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


