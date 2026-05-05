"""
Additional tests for .claude/hooks/session_utils.py.

Covers:
  - is_worktree(cwd)
  - create_session_template(date_str)
  - append_checkpoint(session_file, label, summary)
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parent.parent
SESSION_UTILS_PY = WORKTREE_ROOT / ".claude" / "hooks" / "session_utils.py"


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

def _load_session_utils() -> types.ModuleType:
    """Load session_utils.py as a module via importlib."""
    spec = importlib.util.spec_from_file_location("_session_utils_additional", SESSION_UTILS_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fixture: load session_utils module once per test session
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def session_utils() -> types.ModuleType:
    return _load_session_utils()


# ---------------------------------------------------------------------------
# TestIsWorktree
# ---------------------------------------------------------------------------

class TestIsWorktree:
    """Tests for is_worktree(cwd)."""

    def test_git_file_returns_true(self, session_utils: types.ModuleType, tmp_path: Path) -> None:
        """.git がファイルのディレクトリ → True"""
        git_path = tmp_path / ".git"
        git_path.write_text("gitdir: ../main/.git", encoding="utf-8")
        assert session_utils.is_worktree(str(tmp_path)) is True

    def test_git_directory_returns_false(self, session_utils: types.ModuleType, tmp_path: Path) -> None:
        """.git がディレクトリのディレクトリ → False"""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        assert session_utils.is_worktree(str(tmp_path)) is False

    def test_no_git_returns_false(self, session_utils: types.ModuleType, tmp_path: Path) -> None:
        """.git が存在しないディレクトリ → False"""
        assert session_utils.is_worktree(str(tmp_path)) is False


# ---------------------------------------------------------------------------
# TestCreateSessionTemplate
# ---------------------------------------------------------------------------

class TestCreateSessionTemplate:
    """Tests for create_session_template(date_str)."""

    def test_contains_session_json_marker(self, session_utils: types.ModuleType) -> None:
        """SESSION_JSON_MARKER が含まれる"""
        result = session_utils.create_session_template("2026-01-01")
        assert session_utils.SESSION_JSON_MARKER in result

    def test_contains_date_str(self, session_utils: types.ModuleType) -> None:
        """渡した date_str が含まれる"""
        date = "2026-05-05"
        result = session_utils.create_session_template(date)
        assert date in result

    def test_contains_remaining_tasks_heading(self, session_utils: types.ModuleType) -> None:
        """## 残タスク などの見出しが含まれる"""
        result = session_utils.create_session_template("2026-01-01")
        assert "## 残タスク" in result


# ---------------------------------------------------------------------------
# TestAppendCheckpoint
# ---------------------------------------------------------------------------

class TestAppendCheckpoint:
    """Tests for append_checkpoint(session_file, label, summary)."""

    def test_appends_checkpoint_block_when_file_exists(
        self, session_utils: types.ModuleType, tmp_path: Path
    ) -> None:
        """セッションファイルが存在する場合 → CHECKPOINT ブロックが末尾に追記される"""
        session_file = tmp_path / "2026-05-05.tmp"
        session_file.write_text("existing content\n", encoding="utf-8")

        session_utils.append_checkpoint(str(session_file), "test-label", "test summary")

        content = session_file.read_text(encoding="utf-8")
        assert "## [Checkpoint: test-label" in content
        assert "test summary" in content
        # CHECKPOINT ブロックが "existing content" より後に現れること
        assert content.rindex("## [Checkpoint: test-label") > content.index("existing content")

    def test_no_crash_when_file_does_not_exist(
        self, session_utils: types.ModuleType, tmp_path: Path
    ) -> None:
        """セッションファイルが存在しない場合 → 例外を送出せずに完了する"""
        session_file = tmp_path / "new-subdir" / "2026-05-05.tmp"
        # ファイルも親ディレクトリも存在しない状態で呼び出しても例外が起きないこと
        session_utils.append_checkpoint(str(session_file), "test-label", "test summary")
