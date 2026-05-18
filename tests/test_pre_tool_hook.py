"""Tests for .claude/hooks/pre_tool.py — Green 回帰防止テスト。

実装側で [Sec High-1] / [Sec High-2] のセキュリティ修正は完了済み。
本ファイルは将来の改修で同等の脆弱性が退行しないかを守る回帰防止テスト群。
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

    # --- [Sec High-1] BUG 回帰防止テスト (実装側修正済み・Green 維持) ---

    def test_ls_rf_then_rm_somefile_is_NOT_blocked(self):
        """BUG [Sec High-1]: 'ls -rf && rm somefile' must NOT be blocked.

        Current bug: short_flags is collected from the entire command string,
        so '-rf' in 'ls -rf' is incorrectly attributed to 'rm', causing a
        false positive block.

        実装側修正済み。本テストは退行防止のための Green 回帰防止テスト。
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

        実装側修正済み。本テストは退行防止のための Green 回帰防止テスト。
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


# ---------------------------------------------------------------------------
# [Green 回帰防止テスト] — 実装側修正済み・退行を AST/振る舞いで守る
# ---------------------------------------------------------------------------


class TestBlockMessageTruncation:
    """[New] Block message must be truncated at 200 characters to prevent secret leakage."""

    def test_block_message_is_truncated_at_200_chars(self):
        """[sec-Medium] Block message for a dangerous command must be truncated to 200 chars.

        Current implementation outputs the full command without any length cap:
            f'[PreToolUse BLOCK] 危険なコマンドをブロックしました: {cmd}'
        This can leak long secrets embedded in the command string.

        実装側で修正済み（full command output）。本テストは Green 回帰防止テスト。
        """
        # Build a command that triggers rm -rf blocking and has a payload > 200 chars
        long_suffix = "A" * 300  # 300 extra characters
        cmd = f"rm -rf /some/path/{long_suffix}"
        result = _run_hook(cmd)
        assert _is_blocked(result), (
            f"Command should still be blocked (exit 2), got exit={result.returncode}"
        )
        # The stderr block message must NOT contain the full 300-char suffix verbatim
        # i.e. the command preview must be capped at 200 chars
        assert long_suffix not in result.stderr, (
            "[sec-Medium] Block message must truncate the command to 200 chars. "
            "The full long suffix was found verbatim in stderr, indicating no truncation.\n"
            f"stderr: {result.stderr[:300]!r}"
        )


class TestTruncateWordBoundary:
    """[New] TRUNCATE keyword detection must use word boundary to avoid false positives."""

    def test_truncate_table_triggers_warning(self):
        """TRUNCATE TABLE foo must produce a warning (sanity check — should still pass)."""
        result = _run_hook("TRUNCATE TABLE foo")
        assert _is_allowed(result), (
            f"TRUNCATE TABLE must be allowed, got exit={result.returncode}"
        )
        assert result.stderr.strip() != "", (
            "Expected a warning for 'TRUNCATE TABLE foo', but got no output"
        )

    def test_pretruncate_word_does_NOT_trigger_warning(self):
        """[sec-Low] 'PRETRUNCATE' must NOT trigger the DB destructive operation warning.

        Current implementation uses a plain 'TRUNCATE' substring pattern (no word boundary):
            re.search(r'DROP TABLE|DROP DATABASE|TRUNCATE', cmd, re.IGNORECASE)
        This matches the substring 'TRUNCATE' inside 'PRETRUNCATE orders', causing
        a false positive warning.

        After fix: pattern uses \\bTRUNCATE\\b so 'PRETRUNCATE' is not matched.

        実装側で修正済み（false positive warning emitted）。本テストは Green 回帰防止テスト。
        """
        result = _run_hook("PRETRUNCATE orders")
        assert _is_allowed(result), (
            f"Command with 'PRETRUNCATE' must be allowed, got exit={result.returncode}"
        )
        assert result.stderr.strip() == "", (
            "[sec-Low] 'PRETRUNCATE' must NOT trigger the TRUNCATE warning.\n"
            "Current implementation lacks word boundary (\\b) around TRUNCATE,\n"
            "causing 'PRETRUNCATE' to match the 'TRUNCATE' substring.\n"
            f"stderr: {result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# [Round 4 Green 回帰防止] Full-path rm detection
# ---------------------------------------------------------------------------


class TestFullPathRmDetection:
    """[Round 4] Full-path rm (e.g. /bin/rm, ./rm) must be blocked when -rf flags are given.

    Current implementation:
        re.fullmatch(r'rm', tok.strip("'\""))
    This pattern only matches the bare string 'rm'. A token like '/bin/rm' or './rm'
    does not satisfy fullmatch(r'rm', ...) so the block is bypassed.

    After fix: the check must match the basename of the rm token as well.

    実装側で os.path.basename ベースの検出に修正済み。本テスト群は退行防止のための Green 回帰防止テスト。
    """

    def test_full_path_rm_rf_is_blocked(self):
        """/bin/rm -rf /important must be blocked (sec-Medium).

        re.fullmatch(r'rm', '/bin/rm') is False, so the current implementation
        does NOT block this command.

        実装側修正済み。本テストは退行防止のための Green 回帰防止テスト。
        """
        result = _run_hook("/bin/rm -rf /important")
        assert _is_blocked(result), (
            "[sec-Medium] '/bin/rm -rf /important' must be blocked (exit 2). "
            "Current implementation uses re.fullmatch(r'rm', tok) which does not "
            "match '/bin/rm'. After fix, the basename of the rm token must also be "
            "checked so that full-path invocations are caught.\n"
            f"Got exit={result.returncode}, stderr={result.stderr!r}"
        )

    def test_relative_path_rm_rf_is_blocked(self):
        """./rm -rf /important must be blocked (sec-Medium).

        re.fullmatch(r'rm', './rm') is False, so the current implementation
        does NOT block this command.

        実装側修正済み。本テストは退行防止のための Green 回帰防止テスト。
        """
        result = _run_hook("./rm -rf /important")
        assert _is_blocked(result), (
            "[sec-Medium] './rm -rf /important' must be blocked (exit 2). "
            "Current implementation uses re.fullmatch(r'rm', tok) which does not "
            "match './rm'. After fix, path-prefixed rm invocations must also be detected.\n"
            f"Got exit={result.returncode}, stderr={result.stderr!r}"
        )

    def test_usr_bin_rm_rf_is_blocked(self):
        """/usr/bin/rm -rf /tmp/data must be blocked (sec-Medium).

        This covers an alternative installation path for rm.

        実装側修正済み。本テストは退行防止のための Green 回帰防止テスト。
        """
        result = _run_hook("/usr/bin/rm -rf /tmp/data")
        assert _is_blocked(result), (
            "[sec-Medium] '/usr/bin/rm -rf /tmp/data' must be blocked (exit 2). "
            "re.fullmatch(r'rm', '/usr/bin/rm') is False so the block is bypassed.\n"
            f"Got exit={result.returncode}, stderr={result.stderr!r}"
        )

    def test_bare_rm_rf_still_blocked(self):
        """Sanity check: bare 'rm -rf /path' (existing behavior) must still be blocked."""
        result = _run_hook("rm -rf /path")
        assert _is_blocked(result), (
            "Bare 'rm -rf /path' must still be blocked after the full-path fix.\n"
            f"Got exit={result.returncode}, stderr={result.stderr!r}"
        )
