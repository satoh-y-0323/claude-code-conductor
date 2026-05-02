"""Tests for .claude/hooks/clear_file_history.py

Acceptance criteria verified:
1. FileNotFoundError catch block has a skip-reason comment or a counter variable
2. shutil.rmtree call is preceded by os.path.islink check; symlinks use os.unlink
3. Hook exit code convention (0 = pass) is preserved
4. .claude/hooks/clear_file_history.py and
   src/c3/_template/.claude/hooks/clear_file_history.py have identical content
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "clear_file_history.py"
TEMPLATE_HOOK_PATH = (
    WORKTREE_ROOT / "src" / "c3" / "_template" / ".claude" / "hooks" / "clear_file_history.py"
)


def _load_hook_module(path: Path) -> types.ModuleType:
    """Dynamically load the hook script as a module without executing __main__."""
    spec = importlib.util.spec_from_file_location("clear_file_history", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def _parse_source(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Test 1: FileNotFoundError handler has skip-reason comment or counter
# ---------------------------------------------------------------------------


class TestFileNotFoundHandling:
    """[Code Low-6] FileNotFoundError should not be silently swallowed."""

    def test_skip_reason_comment_or_counter_present(self):
        """The except FileNotFoundError block must contain a comment explaining why
        it is skipped, OR reference a counter variable to track skipped entries.

        Currently the block is just `pass` with no comment or counter — this
        test is expected to FAIL until the fix is applied.
        """
        source = HOOK_PATH.read_text(encoding="utf-8")
        tree = _parse_source(HOOK_PATH)

        # Find all ExceptHandler nodes catching FileNotFoundError
        found_acceptable_handler = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            # Check if this handler catches FileNotFoundError
            exc = node.type
            if exc is None:
                continue
            exc_name = ""
            if isinstance(exc, ast.Name):
                exc_name = exc.id
            elif isinstance(exc, ast.Attribute):
                exc_name = exc.attr
            if exc_name != "FileNotFoundError":
                continue

            # Check: does the body have more than a bare `pass`?
            body = node.body
            has_non_pass = any(
                not isinstance(stmt, ast.Pass) for stmt in body
            )
            if has_non_pass:
                found_acceptable_handler = True
                break

            # Alternative: the source lines for this handler contain a comment
            handler_lineno = node.lineno
            handler_end_lineno = getattr(node, "end_lineno", handler_lineno + 5)
            lines = source.splitlines()
            handler_lines = lines[handler_lineno - 1 : handler_end_lineno]
            has_comment = any("#" in line for line in handler_lines)
            if has_comment:
                found_acceptable_handler = True
                break

        assert found_acceptable_handler, (
            "The except FileNotFoundError block contains only a bare `pass` with no "
            "explanatory comment and no counter variable. "
            "Add a comment (e.g. # already deleted by another process) or increment "
            "a skipped-entry counter."
        )


# ---------------------------------------------------------------------------
# Test 2: shutil.rmtree is guarded by os.path.islink check
# ---------------------------------------------------------------------------


class TestSymlinkSafety:
    """[Sec Low-3] shutil.rmtree must not be called on symlinks (TOCTOU risk).

    The fix: check os.path.islink(full_path) before calling rmtree; if True,
    call os.unlink instead.
    """

    def test_islink_check_before_rmtree(self):
        """The source code must contain an os.path.islink guard that routes
        symlinks to os.unlink rather than shutil.rmtree.

        Currently there is no islink check — this test is expected to FAIL
        until the fix is applied.
        """
        tree = _parse_source(HOOK_PATH)

        # Collect all Call nodes for shutil.rmtree
        rmtree_calls: list[ast.Call] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "rmtree"
                and isinstance(func.value, ast.Name)
                and func.value.id == "shutil"
            ):
                rmtree_calls.append(node)

        assert rmtree_calls, "shutil.rmtree call not found in source (unexpected)"

        # For each rmtree call, verify there is an enclosing If that tests islink
        def _has_islink_guard(rmtree_call: ast.Call, full_tree: ast.Module) -> bool:
            """Walk upward looking for an If node whose test includes islink."""
            rmtree_lineno = rmtree_call.lineno

            for node in ast.walk(full_tree):
                if not isinstance(node, ast.If):
                    continue
                # Determine line range of the If block
                if_end = getattr(node, "end_lineno", rmtree_lineno + 1)
                if not (node.lineno <= rmtree_lineno <= if_end):
                    continue
                # Check if the test expression involves islink
                test_src = ast.dump(node.test)
                if "islink" in test_src:
                    return True
            return False

        for rmtree_call in rmtree_calls:
            guarded = _has_islink_guard(rmtree_call, tree)
            assert guarded, (
                f"shutil.rmtree at line {rmtree_call.lineno} is not protected by an "
                "os.path.islink check. Symlinks should be removed with os.unlink to "
                "prevent TOCTOU attacks."
            )

    def test_symlink_uses_unlink_not_rmtree(self, tmp_path: Path):
        """When a symlink is present in FILE_HISTORY_DIR, main() must call
        os.unlink (not shutil.rmtree) to remove it.

        This test exercises runtime behaviour; it is expected to FAIL until
        the fix is applied because the current code calls rmtree on symlinks.
        """
        module = _load_hook_module(HOOK_PATH)

        # Build a fake FILE_HISTORY_DIR with one symlink entry
        fake_history = tmp_path / "file-history"
        fake_history.mkdir()
        target_dir = tmp_path / "real_dir"
        target_dir.mkdir()
        symlink = fake_history / "link_entry"
        try:
            symlink.symlink_to(target_dir)
        except OSError:
            pytest.skip("Cannot create symlinks on this platform (needs privilege)")

        with (
            patch.object(module.os.path, "isdir", wraps=os.path.isdir) as mock_isdir,
            patch("shutil.rmtree") as mock_rmtree,
            patch("os.unlink") as mock_unlink,
            patch.object(module, "FILE_HISTORY_DIR", str(fake_history)),
        ):
            module.main()

        symlink_path = str(symlink)
        # rmtree must NOT have been called with the symlink path
        for c in mock_rmtree.call_args_list:
            assert c.args[0] != symlink_path, (
                "shutil.rmtree was called on a symlink path. Use os.unlink instead."
            )
        # os.unlink MUST have been called with the symlink path
        mock_unlink.assert_any_call(symlink_path)


# ---------------------------------------------------------------------------
# Test 3: Exit code convention (main() must not raise / sys.exit non-zero)
# ---------------------------------------------------------------------------


class TestExitCodeConvention:
    """Hook exit code: main() must complete without raising and without calling
    sys.exit with a non-zero code.
    """

    def test_main_exits_zero_on_empty_dir(self, tmp_path: Path):
        """main() should not raise when FILE_HISTORY_DIR exists but is empty."""
        module = _load_hook_module(HOOK_PATH)
        fake_history = tmp_path / "file-history"
        fake_history.mkdir()

        with patch.object(module, "FILE_HISTORY_DIR", str(fake_history)):
            # Should not raise
            module.main()

    def test_main_exits_zero_when_dir_missing(self, tmp_path: Path):
        """main() should not raise when FILE_HISTORY_DIR does not exist."""
        module = _load_hook_module(HOOK_PATH)
        missing = tmp_path / "nonexistent" / "file-history"

        with patch.object(module, "FILE_HISTORY_DIR", str(missing)):
            module.main()

    def test_main_deletes_regular_file(self, tmp_path: Path):
        """main() must delete a regular file and print the count."""
        module = _load_hook_module(HOOK_PATH)
        fake_history = tmp_path / "file-history"
        fake_history.mkdir()
        (fake_history / "some_file.json").write_text("{}", encoding="utf-8")

        with patch.object(module, "FILE_HISTORY_DIR", str(fake_history)):
            module.main()

        assert not (fake_history / "some_file.json").exists()

    def test_main_deletes_subdirectory(self, tmp_path: Path):
        """main() must delete a subdirectory via rmtree (for non-symlink dirs)."""
        module = _load_hook_module(HOOK_PATH)
        fake_history = tmp_path / "file-history"
        fake_history.mkdir()
        sub = fake_history / "sub_dir"
        sub.mkdir()
        (sub / "child.txt").write_text("x", encoding="utf-8")

        with patch.object(module, "FILE_HISTORY_DIR", str(fake_history)):
            module.main()

        assert not sub.exists()

    def test_main_handles_file_not_found_gracefully(self, tmp_path: Path):
        """main() must not raise even when a race-condition FileNotFoundError occurs."""
        module = _load_hook_module(HOOK_PATH)
        fake_history = tmp_path / "file-history"
        fake_history.mkdir()
        (fake_history / "vanishing_file.json").write_text("{}", encoding="utf-8")

        original_unlink = os.unlink

        def _raising_unlink(path: str) -> None:
            raise FileNotFoundError(f"gone: {path}")

        with (
            patch.object(module, "FILE_HISTORY_DIR", str(fake_history)),
            patch.object(module.os, "unlink", side_effect=_raising_unlink),
        ):
            # Must not raise
            module.main()


# ---------------------------------------------------------------------------
# Test 4: Both hook files are identical
# ---------------------------------------------------------------------------


class TestFilesAreIdentical:
    """Both copies of the hook must have the same content."""

    def test_hook_files_are_identical(self):
        """The hook at .claude/hooks/ and the template copy at
        src/c3/_template/.claude/hooks/ must be identical.
        """
        assert HOOK_PATH.exists(), f"Hook not found: {HOOK_PATH}"
        assert TEMPLATE_HOOK_PATH.exists(), (
            f"Template hook not found: {TEMPLATE_HOOK_PATH}\n"
            "Both copies must be kept in sync."
        )
        content_main = HOOK_PATH.read_text(encoding="utf-8")
        content_template = TEMPLATE_HOOK_PATH.read_text(encoding="utf-8")
        assert content_main == content_template, (
            "The two copies of clear_file_history.py differ.\n"
            f"  main:     {HOOK_PATH}\n"
            f"  template: {TEMPLATE_HOOK_PATH}\n"
            "Ensure both files are updated together."
        )
