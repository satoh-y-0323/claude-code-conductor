"""
Additional tests for .claude/hooks/session_utils.py.

Covers:
  - is_worktree(cwd)
  - create_session_template(date_str)
  - append_checkpoint(session_file, label, summary)

[New Red-phase tests]
  - append_checkpoint_validates_label: label に制御文字や --> が含まれる場合にサニタイズされること
  - ensure_session_logic_is_not_duplicated_in_stop (Round 5 Medium-2):
    stop.py の ensure_session_file が空ファイル再初期化ロジックをインラインで重複実装していないこと

[Round 6 Red-phase tests]
  - test_ensure_session_initialized_has_single_process_comment (Round 6 Medium-2):
    ensure_session_initialized 関数のソースに「単一プロセス」「single process」「TOCTOU」コメントがあること
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parent.parent
SESSION_UTILS_PY = WORKTREE_ROOT / ".claude" / "hooks" / "session_utils.py"
STOP_PY = WORKTREE_ROOT / ".claude" / "hooks" / "stop.py"


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


# ---------------------------------------------------------------------------
# [New Red-phase] TestAppendCheckpointValidatesLabel
# ---------------------------------------------------------------------------

class TestAppendCheckpointValidatesLabel:
    """[New] append_checkpoint must sanitize label before writing to session file.

    Current implementation:
        block = (
            f"\\n"
            f"## [Checkpoint: {label} - {ts}]\\n"  # label is inserted verbatim
            f"{body}\\n"
        )

    Expected after fix:
        - Control characters (\\x00-\\x1f except \\n/\\t) must be removed or replaced
        - '-->' must be sanitized (e.g. replaced with '-- >') to avoid breaking the
          <!-- C3:SESSION:JSON ... --> block

    実装側でサニタイズ済み。本テストは将来の改修で素通しに退行しないかを守る Green 回帰防止テスト群。
    """

    def test_append_checkpoint_sanitizes_control_chars_in_label(
        self, tmp_path: Path
    ) -> None:
        """[sec-Low] label に含まれる制御文字がサニタイズされてセッションファイルに書き込まれること。

        制御文字（\\x00-\\x1f、\\n と \\t を除く）はターミナルインジェクションや
        パース破壊の原因になる。

        実装側でサニタイズ済み。本テストは将来 ANSI/NULL/BEL を素通しさせる退行を防ぐ Green 回帰防止テスト。
        """
        su = _load_session_utils()
        session_file = tmp_path / "2026-05-05.tmp"
        session_file.write_text("existing content\n", encoding="utf-8")

        # label に制御文字（ESC, NULL, BEL）を埋め込む
        label_with_control_chars = "wave-complete\x1b[31mINJECTED\x1b[0m\x00\x07"
        su.append_checkpoint(str(session_file), label_with_control_chars, "summary")

        content = session_file.read_text(encoding="utf-8")

        # ANSI エスケープシーケンス ESC (\x1b) が含まれていてはいけない
        assert "\x1b" not in content, (
            "[sec-Low] append_checkpoint must strip ANSI escape sequences from label. "
            "Found '\\x1b' in session file after writing label with control chars.\n"
            f"Label was: {label_with_control_chars!r}\n"
            f"Content preview: {content[:300]!r}"
        )

        # NULL 文字が含まれていてはいけない
        assert "\x00" not in content, (
            "[sec-Low] append_checkpoint must strip NULL bytes from label. "
            f"Found '\\x00' in session file.\nLabel was: {label_with_control_chars!r}"
        )

    def test_append_checkpoint_sanitizes_comment_closer_in_label(
        self, tmp_path: Path
    ) -> None:
        """[sec-Low] label に含まれる '-->' がサニタイズされてセッションファイルに書き込まれること。

        セッションファイルの <!-- C3:SESSION:JSON ... --> ブロックを破壊しないよう、
        '-->' は '-- >' 等に変換されるべき。

        実装側で `-->` → `-- >` への置換済み。本テストは将来素通しに退行しないかを守る Green 回帰防止テスト。
        """
        su = _load_session_utils()
        session_file = tmp_path / "2026-05-05-comment.tmp"

        # まず C3:SESSION:JSON ブロックを含むセッションファイルを作成
        initial_content = (
            "SESSION: 2026-05-05\n"
            "## 事実ログ\n"
            "- 記録時刻: 2026-05-05 00:00:00\n"
            "\n"
            "<!-- C3:SESSION:JSON\n"
            '{\n'
            '  "session": "20260505",\n'
            '  "patterns": []\n'
            "}\n"
            "-->\n"
        )
        session_file.write_text(initial_content, encoding="utf-8")

        # label に --> を埋め込む（コメントブロックを早期クローズしようとする）
        label_with_closer = "wave --> complete"
        su.append_checkpoint(str(session_file), label_with_closer, "summary text")

        content = session_file.read_text(encoding="utf-8")

        # チェックポイントブロックヘッダ行に --> が含まれていてはいけない
        for line in content.splitlines():
            if "## [Checkpoint:" in line:
                assert "-->" not in line, (
                    "[sec-Low] append_checkpoint must sanitize '-->' in label. "
                    "Found '-->' in checkpoint heading line, which could break "
                    "the C3:SESSION:JSON comment block.\n"
                    f"Line: {line!r}"
                )
                break
        else:
            pytest.fail("Checkpoint heading line not found in session file")


# ---------------------------------------------------------------------------
# [New Red-phase Round 5] TestEnsureSessionLogicNotDuplicated (Medium-2)
# ---------------------------------------------------------------------------


class TestEnsureSessionLogicNotDuplicated:
    """[Red Round 5] stop.py の ensure_session_file と session_utils.py の
    append_checkpoint に「空ファイル再初期化」ロジックが重複していること（DRY違反）を検出する。

    Current situation (DRY violation):
        stop.py::ensure_session_file (FileExistsError branch) contains:
            if os.path.getsize(path) == 0:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(create_session_template(date_str))

        session_utils.py::append_checkpoint (FileExistsError branch) contains:
            if os.path.getsize(session_file) == 0:
                with open(session_file, 'w', encoding='utf-8') as f:
                    f.write(create_session_template(date_str))

    The "empty file re-initialization" logic is duplicated in both functions.
    If this logic needs to change (e.g. new template structure), it must be
    updated in two places.

    Expected after fix:
        Extract the empty-file check + re-init logic into a shared helper in
        session_utils.py (e.g. ensure_session_initialized(path, date_str)),
        then have BOTH stop.py::ensure_session_file AND session_utils.py::append_checkpoint
        call that helper instead of re-implementing inline.

    Verification:
        If the fix is applied, stop.py::ensure_session_file should NO LONGER contain
        a direct call to os.path.getsize() (since the logic is delegated to session_utils).
        We check that os.path.getsize() is NOT called inside ensure_session_file.

    Status: 実装側で session_utils 側のヘルパー (`ensure_session_initialized`) に委譲済み。
    本テストは stop.py 側で再びインライン重複に退行しないかを AST で守る Green 回帰防止テスト。
    """

    def test_ensure_session_logic_is_not_duplicated_in_stop(self):
        """[Medium-2] stop.py::ensure_session_file が空ファイル再初期化ロジックを
        インラインで直接実装していないことを AST で確認する。

        具体的には: os.path.getsize() が ensure_session_file 内で直接呼ばれていないこと。
        ロジックが session_utils のヘルパーに委譲されていれば、
        ensure_session_file は os.path.getsize を直接呼ぶ必要がない。

        実装側でヘルパー (`session_utils.ensure_session_initialized`) に委譲済み。
        本テストは stop.py で再びインライン重複に退行しないかを AST で守る Green 回帰防止テスト。
        """
        stop_source = STOP_PY.read_text(encoding="utf-8")
        stop_tree = ast.parse(stop_source)

        # 1. ensure_session_file 関数ノードを探す
        fn_node = None
        for node in ast.walk(stop_tree):
            if isinstance(node, ast.FunctionDef) and node.name == "ensure_session_file":
                fn_node = node
                break

        assert fn_node is not None, "ensure_session_file function not found in stop.py"

        # 2. ensure_session_file 内で os.path.getsize が呼ばれているか確認する
        #    呼ばれている場合 = 空ファイル判定ロジックがインラインで重複実装されている (DRY違反)
        #    呼ばれていない場合 = ヘルパーへ委譲済み (DRY解消)
        has_getsize_call = False
        for node in ast.walk(fn_node):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # os.path.getsize(...)
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "getsize"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "path"
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "os"
            ):
                has_getsize_call = True
                break

        assert not has_getsize_call, (
            "[Medium-2] DRY violation: stop.py::ensure_session_file contains a direct call to\n"
            "os.path.getsize(), duplicating the 'empty file re-initialization' logic from\n"
            "session_utils.py::append_checkpoint.\n"
            "\n"
            "Current stop.py::ensure_session_file (FileExistsError branch):\n"
            "    if os.path.getsize(path) == 0:   # <- duplicated logic\n"
            "        with open(path, 'w', encoding='utf-8') as f:\n"
            "            f.write(create_session_template(date_str))\n"
            "\n"
            "session_utils.py::append_checkpoint has the SAME pattern:\n"
            "    if os.path.getsize(session_file) == 0:  # <- same logic\n"
            "        with open(session_file, 'w', encoding='utf-8') as f:\n"
            "            f.write(create_session_template(date_str))\n"
            "\n"
            "Expected fix:\n"
            "  1. Add a shared helper to session_utils.py:\n"
            "     def ensure_session_initialized(path: str, date_str: str) -> None:\n"
            "         if os.path.getsize(path) == 0:\n"
            "             with open(path, 'w', encoding='utf-8') as f:\n"
            "                 f.write(create_session_template(date_str))\n"
            "  2. Call this helper from both:\n"
            "     - stop.py::ensure_session_file (FileExistsError branch)\n"
            "     - session_utils.py::append_checkpoint (FileExistsError branch)\n"
            "\n"
            "After the fix, os.path.getsize() should NOT appear inside ensure_session_file.\n"
            "AST check: os.path.getsize() call found inside ensure_session_file — DRY violation."
        )


# ---------------------------------------------------------------------------
# [Round 6 Red-phase] TestEnsureSessionInitializedHasSingleProcessComment (Medium-2)
# ---------------------------------------------------------------------------


class TestEnsureSessionInitializedHasSingleProcessComment:
    """[Red Round 6] ensure_session_initialized() must have a comment documenting
    the TOCTOU risk and single-process assumption.

    Current implementation:
        def ensure_session_initialized(path: str, date_str: str) -> None:
            \"\"\"空のセッションファイルをテンプレートで再初期化する共有ヘルパー。

            FileExistsError ブランチで使用: ファイルが空の場合のみテンプレートを書き直す。
            stop.py::ensure_session_file と append_checkpoint の両方から呼ばれる。
            \"\"\"
            if os.path.getsize(path) == 0:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(create_session_template(date_str))

    Problem (TOCTOU risk):
        There is a race condition between os.path.getsize(path) and open(path, 'w'):
        - Thread/process A calls getsize() → returns 0 (file is empty)
        - Thread/process B writes content to the file
        - Thread/process A opens the file with 'w' → overwrites B's content

    The current docstring does NOT mention:
        - "単一プロセス" (single process)
        - "single process"
        - "TOCTOU"

    Expected after fix:
        Add a comment (inline comment or docstring) inside ensure_session_initialized
        that includes at least one of the keywords:
        - "単一プロセス"
        - "single process"
        - "TOCTOU"

    本テストは Green 回帰防止テスト（実装側修正済み）。修正前は none of those keywords
    appear in the function source.
    """

    def test_ensure_session_initialized_has_single_process_comment(self):
        """[Round 6 Medium-2] ensure_session_initialized() must document the TOCTOU risk
        with a comment containing '単一プロセス', 'single process', or 'TOCTOU'.

        Verification: inspect.getsource() on the ensure_session_initialized function
        to check for keyword presence in the source text (covers both docstrings and
        inline comments).

        本テストは Green 回帰防止テスト（実装側修正済み）。修正前は the current docstring
        does not mention any of the required keywords.
        """
        su = _load_session_utils()

        assert hasattr(su, "ensure_session_initialized"), (
            "ensure_session_initialized function not found in session_utils.py"
        )

        fn = su.ensure_session_initialized
        fn_source = inspect.getsource(fn)

        keywords = ["単一プロセス", "single process", "TOCTOU"]
        has_keyword = any(kw in fn_source for kw in keywords)

        assert has_keyword, (
            "[Round 6 Medium-2] ensure_session_initialized() must document the TOCTOU risk.\n"
            "\n"
            "The function has a race condition (TOCTOU) between:\n"
            "    os.path.getsize(path)  # check: is the file empty?\n"
            "    open(path, 'w', ...)   # write: overwrite the file\n"
            "\n"
            "Between these two operations, another process could write to the file,\n"
            "and open(path, 'w') would silently overwrite that content.\n"
            "\n"
            "Required fix: Add a comment in the function (docstring or inline) that\n"
            "contains at least one of these keywords:\n"
            "  - '単一プロセス'\n"
            "  - 'single process'\n"
            "  - 'TOCTOU'\n"
            "\n"
            "Example:\n"
            "    # TOCTOU: getsize と open('w') の間にウィンドウがあるが、\n"
            "    # このフックは単一プロセス前提のため許容する。\n"
            "\n"
            f"Current function source (first 500 chars):\n{fn_source[:500]!r}\n"
            "\n"
            f"Keywords searched: {keywords}\n"
            "None of the keywords found in the function source."
        )
