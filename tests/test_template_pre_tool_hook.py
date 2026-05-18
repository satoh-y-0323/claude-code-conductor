"""Tests for src/c3/_template/.claude/hooks/pre_tool.py — Green 回帰防止テスト。

`hatch_build.py` のビルドフックでテンプレートファイルが `.claude/hooks/pre_tool.py` から
自動再生成される。本テスト群はテンプレート版がメイン版と同等の振る舞いを保ち続けるかを
検証する Green 回帰防止テスト。

Fixes verified:
  [Sec High-1] rm -rf: only flags immediately after 'rm' are collected,
               preventing false positives from preceding commands.
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

    `hatch_build.py` のビルドフックにより `.claude/hooks/pre_tool.py` から自動生成される。
    本テストは生成漏れの退行を防ぐ Green 回帰防止テスト。
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

