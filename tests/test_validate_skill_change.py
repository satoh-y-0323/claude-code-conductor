"""Tests for .claude/hooks/validate_skill_change.py

Red-phase tests: verify that main() uses return (not sys.exit) for pass-through
paths, and that the __main__ block follows the sys.exit(main() or 0) pattern.
These tests are expected to FAIL against the current implementation which uses
sys.exit(0) inside main().
"""

from __future__ import annotations

import importlib.util
import inspect
import io
import json
import sys
import textwrap
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HOOK_PATH = (
    Path(__file__).parent.parent
    / ".claude"
    / "hooks"
    / "validate_skill_change.py"
)


def _load_module():
    """Dynamically load the hook as a module without executing __main__."""
    spec = importlib.util.spec_from_file_location("validate_skill_change", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _call_main(payload: dict, *, mod=None) -> tuple[str, str]:
    """Call mod.main() with the given payload injected via stdin.

    Returns (stdout_text, stderr_text).
    Raises SystemExit if main() calls sys.exit().
    """
    if mod is None:
        mod = _load_module()
    stdin_data = json.dumps(payload)
    with (
        mock.patch("sys.stdin", io.StringIO(stdin_data)),
        mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        mock.patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
    ):
        mod.main()
        return mock_stdout.getvalue(), mock_stderr.getvalue()


# ---------------------------------------------------------------------------
# Red-phase tests: main() must NOT call sys.exit() for pass-through cases
# ---------------------------------------------------------------------------


class TestMainDoesNotCallSysExit:
    """main() should return normally (not raise SystemExit) for all code paths."""

    def test_invalid_json_does_not_raise_system_exit(self):
        """Invalid JSON input: main() must return, not call sys.exit(0)."""
        mod = _load_module()
        with mock.patch("sys.stdin", io.StringIO("not-json")):
            # If main() calls sys.exit(0), this raises SystemExit — test fails.
            try:
                mod.main()
            except SystemExit as exc:
                pytest.fail(
                    f"main() called sys.exit({exc.code}) for invalid JSON input; "
                    "expected a plain return instead."
                )

    def test_non_skill_tool_name_does_not_raise_system_exit(self):
        """Unsupported tool_name: main() must return, not call sys.exit(0)."""
        payload = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
        mod = _load_module()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            try:
                mod.main()
            except SystemExit as exc:
                pytest.fail(
                    f"main() called sys.exit({exc.code}) for non-skill tool; "
                    "expected a plain return instead."
                )

    def test_non_skills_file_path_does_not_raise_system_exit(self):
        """Write to a non-skills path: main() must return, not call sys.exit(0)."""
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/some/other/path/file.py"},
        }
        mod = _load_module()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            try:
                mod.main()
            except SystemExit as exc:
                pytest.fail(
                    f"main() called sys.exit({exc.code}) for non-skills path; "
                    "expected a plain return instead."
                )

    def test_skills_file_path_does_not_raise_system_exit(self):
        """Write to a .claude/skills/ path: main() must return, not call sys.exit(0)."""
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/project/.claude/skills/my_skill.md"},
        }
        mod = _load_module()
        with (
            mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            try:
                mod.main()
            except SystemExit as exc:
                pytest.fail(
                    f"main() called sys.exit({exc.code}) for skills path; "
                    "expected a plain return instead."
                )


# ---------------------------------------------------------------------------
# Red-phase test: __main__ block uses sys.exit(main() or 0) pattern
# ---------------------------------------------------------------------------


class TestMainBlockPattern:
    """The if __name__ == '__main__' block must use sys.exit(main() or 0)."""

    def test_dunder_main_block_calls_sys_exit_with_main_result(self):
        """Source must contain the sys.exit(main() or 0) pattern."""
        source = HOOK_PATH.read_text(encoding="utf-8")
        # Normalize whitespace for robust matching
        normalized = " ".join(source.split())
        pattern = "sys.exit(main() or 0)"
        assert pattern in normalized, (
            f"Expected '{pattern}' in the __main__ block, but it was not found.\n"
            "Current __main__ block likely still uses 'main()' without sys.exit wrapping."
        )

    def test_dunder_main_block_does_not_call_bare_main(self):
        """The __main__ block must NOT be a bare 'main()' call."""
        source = HOOK_PATH.read_text(encoding="utf-8")
        lines = source.splitlines()
        main_block_lines = [
            line for line in lines if "__name__" in line or (
                "main()" in line and "sys.exit" not in line and "def " not in line
            )
        ]
        bare_main_calls = [
            line for line in main_block_lines
            if line.strip() == "main()"
        ]
        assert not bare_main_calls, (
            "Found bare 'main()' call in __main__ block. "
            "Expected 'sys.exit(main() or 0)' pattern instead."
        )


# ---------------------------------------------------------------------------
# Behavioral tests: existing functionality must be preserved
# ---------------------------------------------------------------------------


class TestExistingBehavior:
    """Verify the hook's observable behavior is preserved after the refactor."""

    def test_skills_file_prints_reminder_message(self, capsys):
        """A Write to a .claude/skills/ file should print a reminder message."""
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/project/.claude/skills/my_skill.md"},
        }
        mod = _load_module()
        with (
            mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),
            mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            try:
                mod.main()
            except SystemExit:
                pass  # tolerated during Red phase
            output = mock_stdout.getvalue()
        assert "[C3]" in output, "Expected reminder message containing '[C3]' to be printed."
        assert "my_skill.md" in output, "Expected skill filename in reminder message."

    def test_non_skills_path_produces_no_output(self):
        """A Write to a non-skills path should produce no stdout output."""
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/project/src/main.py"},
        }
        mod = _load_module()
        with (
            mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),
            mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            try:
                mod.main()
            except SystemExit:
                pass  # tolerated during Red phase
            output = mock_stdout.getvalue()
        assert output == "", f"Expected no output for non-skills path, got: {output!r}"

    def test_windows_backslash_path_recognized(self):
        """Windows-style backslash paths to .claude\\skills\\ must trigger message."""
        payload = {
            "tool_name": "Edit",
            "tool_input": {"file_path": r"C:\project\.claude\skills\my_skill.md"},
        }
        mod = _load_module()
        with (
            mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),
            mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            try:
                mod.main()
            except SystemExit:
                pass  # tolerated during Red phase
            output = mock_stdout.getvalue()
        assert "[C3]" in output, (
            "Expected reminder message for Windows backslash path to skills/."
        )

    def test_edit_tool_also_triggers_reminder(self):
        """Edit tool on a .claude/skills/ file should also print the reminder."""
        payload = {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/project/.claude/skills/another.md"},
        }
        mod = _load_module()
        with (
            mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),
            mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            try:
                mod.main()
            except SystemExit:
                pass
            output = mock_stdout.getvalue()
        assert "[C3]" in output

    def test_bash_tool_produces_no_output(self):
        """Bash tool (not Write/Edit) should produce no output."""
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "ls .claude/skills/"},
        }
        mod = _load_module()
        with (
            mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),
            mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            try:
                mod.main()
            except SystemExit:
                pass
            output = mock_stdout.getvalue()
        assert output == ""

    def test_invalid_json_produces_no_output(self):
        """Invalid JSON should be silently ignored (no output)."""
        mod = _load_module()
        with (
            mock.patch("sys.stdin", io.StringIO("{{invalid json")),
            mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            try:
                mod.main()
            except SystemExit:
                pass
            output = mock_stdout.getvalue()
        assert output == ""
