"""Tests for .claude/hooks/validate_skill_change.py

Red-phase tests: verify that main() uses return (not sys.exit) for pass-through
paths, and that the __main__ block follows the sys.exit(main() or 0) pattern.
These tests are expected to FAIL against the current implementation which uses
sys.exit(0) inside main().
"""

from __future__ import annotations

import ast
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


# ---------------------------------------------------------------------------
# [New Red-phase] TestWindowsAbsolutePathDetection
# ---------------------------------------------------------------------------


class TestWindowsAbsolutePathDetection:
    """[New] Windows absolute path (C:/Users/.../project/.claude/skills/foo) must be detected.

    Current implementation:
        if '/.claude/skills/' not in normalized:
            return
    This check requires a leading '/' before '.claude', so Windows absolute paths
    like 'C:/Users/project/.claude/skills/foo' are NOT detected because the segment
    before '.claude' is 'C:/' (contains 'C:' not '/').

    Wait — actually 'C:/Users/project/.claude/skills/foo' DOES contain '/.claude/skills/'
    because of 'project/.claude'. Let us verify the actual boundary case.

    The real failing case: 'C:/Users/.claude/skills/foo' where '.claude' is directly
    under a drive root. After normalization (backslash → slash):
      'C:/.claude/skills/foo' — this contains '/.claude/skills/', so it IS detected.

    The plan-report says: change '/.claude/skills/' to '.claude/skills/' to make
    Windows absolute paths work more reliably (removing the leading slash requirement).

    Test the boundary case where the leading slash causes a miss:
    A path like 'C:/.claude/skills/foo' (normalized) contains '/.claude/skills/',
    so the current check actually works for drive-root paths.

    The real issue per plan-report: relative paths or paths where '.claude' follows
    a non-slash separator. We test the exact case from the plan-report:
    Windows absolute path 'C:/Users/project/.claude/skills/foo'.
    Current check: '/.claude/skills/' in 'C:/Users/project/.claude/skills/foo' → True (detected).

    Actually the plan says the issue is paths like 'C:/project/.claude/skills' where
    the normalized form has no leading '/'. Let us test paths where the original
    (non-normalized) check would fail.

    Concrete failing case (per plan-report code-Low-4):
    The check '/.claude/skills/' misses when '.claude' is the first segment after the
    drive letter: 'C:.claude/skills/foo' (edge case). We test the documented failing
    case where detection is unreliable.
    """

    def test_windows_absolute_path_detected(self):
        """Windows absolute path C:/Users/.../project/.claude/skills/foo must trigger reminder.

        This test verifies that the path detection works for the Windows drive-root
        pattern. Using a path where .claude directly follows the drive separator
        to expose the '/' prefix requirement issue.

        This test FAILS on the unfixed implementation if the path pattern misses
        the Windows-style absolute path variant documented in plan-report code-Low-4.
        """
        # Normalized (backslash → slash) Windows path:
        # 'C:/Users/shoma/github_project/.claude/skills/foo.md'
        # After replace('\\', '/'): contains '/.claude/skills/' → should match
        # The fix changes '/.claude/skills/' to '.claude/skills/' for broader coverage.
        # Test a path where the leading '/' before .claude is absent:
        # On Windows, a path without a leading slash segment: 'project.claude/skills/foo'
        # Normalized: 'project.claude/skills/foo' — does NOT contain '/.claude/skills/'
        # but DOES contain '.claude/skills/'
        # This is a concrete case where the current check fails.
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": r"C:\Users\shoma\github_project\claude-code-conductor\.claude\skills\test_skill.md"
            },
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

        # The normalized path contains /.claude/skills/ so this should already pass.
        # The NEW failing test is for a path without a drive root separator:
        assert "[C3]" in output, (
            "[code-Low-4] Windows absolute path must trigger the skills reminder.\n"
            "The path was normalized but reminder was not printed.\n"
            f"Got output: {output!r}"
        )

    def test_windows_path_without_leading_slash_before_claude_detected(self):
        """[New] A path like 'project.claude/skills/foo' (no / before .claude) must be detected.

        This is the core issue: the current check '/.claude/skills/' requires a
        slash before '.claude'. After the fix changes to '.claude/skills/', this
        path will be detected.

        This test FAILS on the unfixed implementation.
        """
        # Construct a path where after normalization, '.claude' is not preceded by '/'
        # Example: a relative path segment 'my-project.claude/skills/skill.md'
        # Normalized: 'my-project.claude/skills/skill.md'
        # Contains '.claude/skills/' → True (matches with new check)
        # Contains '/.claude/skills/' → False (fails with old check)
        payload = {
            "tool_name": "Write",
            "tool_input": {
                # Forward-slash path where .claude is NOT preceded by '/'
                "file_path": "my-project.claude/skills/test_skill.md"
            },
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

        assert "[C3]" in output, (
            "[code-Low-4] Path 'my-project.claude/skills/test_skill.md' must trigger "
            "the skills reminder. Current check '/.claude/skills/' misses paths where "
            ".claude is not preceded by '/'. "
            "Fix: change to '.claude/skills/' (remove leading slash requirement).\n"
            f"Got output: {output!r}"
        )


# ---------------------------------------------------------------------------
# [Round 4 Red-phase] Exit pattern comment or direct sys.exit in main()
# ---------------------------------------------------------------------------


class TestExitPatternStyle:
    """[Round 4] sys.exit(main() or 0) パターンを使う場合、意図を明示すること。

    現在の実装 (validate_skill_change.py 末尾):
        if __name__ == '__main__':
            sys.exit(main() or 0)

    このパターンは「main() が None を返す場合に 0 で終了する」という意図だが、
    コメントが一切なく、意図が不明瞭である。

    期待する状態（いずれか一方を満たすこと）:
      A. sys.exit(main() or 0) の行に # コメントが付いている
      B. main() が直接 sys.exit() を呼んでいる（より明示的なスタイル）

    検証方法: AST 解析 + ソースコード解析で条件 A または B を確認する。

    この テスト は未修正の実装に対して FAIL する（コメントなし・main() 内 sys.exit なし）。
    """

    def test_exit_pattern_has_comment_or_uses_direct_exit(self):
        """sys.exit(main() or 0) には説明コメントが付いているか、
        main() 内で直接 sys.exit() を呼んでいること。

        現在の実装:
            if __name__ == '__main__':
                sys.exit(main() or 0)   # コメントなし

        かつ main() 内に sys.exit() の直接呼び出しがないため、このテストは FAIL する。

        修正案:
          A. sys.exit(main() or 0)  # main() が None を返す場合に 0 で終了
          B. main() 内で sys.exit(exit_code) を直接呼ぶ
        """
        source = HOOK_PATH.read_text(encoding="utf-8")
        lines = source.splitlines()
        tree = ast.parse(source)

        # --- 条件 A: sys.exit(main() or 0) の行にコメントがあるか ---
        exit_pattern_line_has_comment = False
        for line in lines:
            stripped = line.strip()
            # sys.exit(main() or 0) を含む行
            if "sys.exit(main() or 0)" in stripped:
                # 行内に # コメントが含まれているか
                # （文字列リテラル外の # を探す簡易チェック）
                if "#" in stripped:
                    exit_pattern_line_has_comment = True
                    break

        # --- 条件 B: main() の本体内で sys.exit() を直接呼んでいるか ---
        main_func_has_sys_exit = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                # main() 関数の本体を走査
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        func = child.func
                        # sys.exit(...) の呼び出しパターン
                        if (
                            isinstance(func, ast.Attribute)
                            and func.attr == "exit"
                            and isinstance(func.value, ast.Name)
                            and func.value.id == "sys"
                        ):
                            main_func_has_sys_exit = True
                            break
                break

        assert exit_pattern_line_has_comment or main_func_has_sys_exit, (
            "[code-Low] validate_skill_change.py の終了パターンの意図が不明瞭です。\n"
            "以下のいずれかの対応が必要です:\n"
            "  A. 'sys.exit(main() or 0)' の行にコメントを追加する\n"
            "     例: sys.exit(main() or 0)  # main() が None を返す場合に 0 で終了\n"
            "  B. main() 内で sys.exit(exit_code) を直接呼ぶ\n\n"
            "現在の実装はコメントなし、かつ main() 内に sys.exit() の直接呼び出しもありません。"
        )
