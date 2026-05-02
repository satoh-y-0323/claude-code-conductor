"""Tests for src/c3/_template/.claude/hooks/pre_tool.py — TDD Red phase.

These tests verify that the template version of pre_tool.py has the same
correct (post-fix) behavior as the main .claude/hooks/pre_tool.py.

Since the template file does not yet exist, all tests are expected to FAIL
(FileNotFoundError / non-zero exit due to missing script).

Fixes verified:
  [Sec High-1] rm -rf: only flags immediately after 'rm' are collected,
               preventing false positives from preceding commands.
  [Sec High-2] cd bypass: subshell $(), backtick, eval, and newline
               separators are detected in addition to ;, &, |.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# Path to the template hook script under test
HOOK_SCRIPT = (
    Path(__file__).parent.parent
    / "src"
    / "c3"
    / "_template"
    / ".claude"
    / "hooks"
    / "pre_tool.py"
)


def _run_hook(command: str) -> subprocess.CompletedProcess:
    """Run the template pre_tool.py hook with the given Bash command string."""
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
# Prerequisite: template file must exist
# ---------------------------------------------------------------------------


def test_template_hook_file_exists():
    """The template pre_tool.py must exist at the expected path.

    This test fails (Red) when the template file has not yet been created.
    """
    assert HOOK_SCRIPT.exists(), (
        f"Template hook script not found: {HOOK_SCRIPT}\n"
        "The template file must be created by syncing from .claude/hooks/pre_tool.py"
    )


# ---------------------------------------------------------------------------
# [Sec High-1] rm -rf detection
# ---------------------------------------------------------------------------


class TestTemplateRmRfDetection:
    """Tests for rm -rf detection logic in the template hook."""

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

    def test_ls_rf_then_rm_somefile_is_NOT_blocked(self):
        """'ls -rf && rm somefile' must NOT be blocked.

        The '-rf' flags belong to 'ls', not 'rm'. The fixed implementation
        only collects flags that appear immediately after 'rm'.
        """
        result = _run_hook("ls -rf && rm somefile")
        assert _is_allowed(result), (
            "[Sec High-1] 'ls -rf && rm somefile' was incorrectly blocked.\n"
            "The '-rf' flags belong to 'ls', not 'rm'.\n"
            f"exit={result.returncode}, stderr={result.stderr}"
        )

    def test_echo_rf_pipe_rm_somefile_is_NOT_blocked(self):
        """\"echo '-rf' | rm somefile\" must NOT be blocked.

        The '-rf' comes from echo's argument, not rm's flags.
        """
        result = _run_hook("echo '-rf' | rm somefile")
        assert _is_allowed(result), (
            "[Sec High-1] \"echo '-rf' | rm somefile\" was incorrectly blocked.\n"
            "The '-rf' comes from echo's argument, not rm's flags.\n"
            f"exit={result.returncode}, stderr={result.stderr}"
        )

    def test_flag_before_rm_keyword_is_NOT_blocked(self):
        """'-rf' appearing before 'rm' must not trigger a block."""
        result = _run_hook("mytool -rf data && rm output.txt")
        assert _is_allowed(result), (
            "[Sec High-1] '-rf' before rm was incorrectly attributed to rm.\n"
            f"exit={result.returncode}, stderr={result.stderr}"
        )


# ---------------------------------------------------------------------------
# [Sec High-2] cd bypass detection
# ---------------------------------------------------------------------------


class TestTemplateCdBypassDetection:
    """Tests for cd bypass detection logic in the template hook."""

    def test_plain_cd_is_blocked(self):
        """Plain 'cd /home' at the start of a command must be blocked."""
        result = _run_hook("cd /home")
        assert _is_blocked(result), (
            f"Expected 'cd /home' to be blocked, got exit={result.returncode}"
        )

    def test_cd_after_semicolon_is_blocked(self):
        """'echo test; cd /tmp' must be blocked."""
        result = _run_hook("echo test; cd /tmp")
        assert _is_blocked(result), (
            f"Expected 'echo test; cd /tmp' to be blocked, got exit={result.returncode}"
        )

    def test_cd_after_pipe_is_blocked(self):
        """'echo test | cd /tmp' must be blocked (pipe separator)."""
        result = _run_hook("echo test | cd /tmp")
        assert _is_blocked(result), (
            f"Expected 'echo test | cd /tmp' to be blocked, got exit={result.returncode}"
        )

    def test_cd_after_ampersand_is_blocked(self):
        """'true && cd /tmp' must be blocked."""
        result = _run_hook("true && cd /tmp")
        assert _is_blocked(result), (
            f"Expected 'true && cd /tmp' to be blocked, got exit={result.returncode}"
        )

    def test_no_cd_command_is_allowed(self):
        """'python -m pytest' (no cd) must be allowed."""
        result = _run_hook("python -m pytest")
        assert _is_allowed(result), (
            f"Expected 'python -m pytest' to be allowed, got exit={result.returncode}"
        )

    def test_word_containing_cd_is_allowed(self):
        """'docker' contains 'cd' substring but must not be blocked."""
        result = _run_hook("docker ps")
        assert _is_allowed(result), (
            f"Expected 'docker ps' to be allowed, got exit={result.returncode}"
        )

    def test_subshell_cd_is_blocked(self):
        """'$(cd /tmp)' via subshell must be blocked.

        The fixed implementation includes '$(' as a bypass separator.
        """
        result = _run_hook("$(cd /tmp)")
        assert _is_blocked(result), (
            "[Sec High-2] '$(cd /tmp)' subshell bypass was not detected.\n"
            f"exit={result.returncode}, stderr={result.stderr}"
        )

    def test_backtick_subshell_cd_is_blocked(self):
        """'`cd /tmp`' via backtick subshell must be blocked.

        The fixed implementation includes backtick '`' as a bypass separator.
        """
        result = _run_hook("`cd /tmp`")
        assert _is_blocked(result), (
            "[Sec High-2] '`cd /tmp`' backtick bypass was not detected.\n"
            f"exit={result.returncode}, stderr={result.stderr}"
        )

    def test_eval_cd_is_blocked(self):
        """\"eval 'cd /tmp'\" must be blocked.

        The fixed implementation detects eval-based cd invocations.
        """
        result = _run_hook("eval 'cd /tmp'")
        assert _is_blocked(result), (
            "[Sec High-2] \"eval 'cd /tmp'\" bypass was not detected.\n"
            f"exit={result.returncode}, stderr={result.stderr}"
        )

    def test_newline_separated_cd_is_blocked(self):
        """Multiline command with cd on second line must be blocked."""
        result = _run_hook("echo hello\ncd /tmp")
        assert _is_blocked(result), (
            "[Sec High-2] Newline-separated 'cd /tmp' was not detected.\n"
            f"exit={result.returncode}, stderr={result.stderr}"
        )
