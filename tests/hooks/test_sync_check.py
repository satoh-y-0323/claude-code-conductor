"""Tests for .dev/hooks/_sync_check.py

PostToolUse hook（配布元専用）の挙動を検証する。

テストケース:
 警告動作:
  1. .gitignore を Write → stderr に _excludes.py と hatch_build.py の同期警告
  2. src/c3/_excludes.py を Edit → stderr に .gitignore と hatch_build.py の同期警告
  3. hatch_build.py を Write → stderr に .gitignore と _excludes.py の同期警告

 通過動作（警告なし）:
  4. 関係ないファイル → stderr 空
  5. tool_name が Read など → stderr 空
  6. file_path が空 / payload に無い → stderr 空
  7. 不正な JSON → crash しない

 ブロックしない:
  8. いかなる入力でも exit 0

`.dev/` は gitignore 対象だが、テストファイル自体は配布される。利用者環境に
`.dev/hooks/_sync_check.py` が無い場合は skip する。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".dev" / "hooks" / "_sync_check.py"

pytestmark = pytest.mark.skipif(
    not HOOK_PATH.is_file(),
    reason=".dev/hooks/_sync_check.py is distributor-only (gitignored)",
)


def _run_hook(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(WORKTREE_ROOT),
    )


def _payload(tool_name: str, file_path: str) -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
    }


class TestWarn:
    """SYNC_GROUP の 3 ファイル変更時に他 2 ファイルの同期警告が出る。"""

    def test_warn_on_gitignore_change(self) -> None:
        result = _run_hook(_payload("Write", ".gitignore"))
        assert result.returncode == 0
        assert "[SyncCheck WARN]" in result.stderr
        assert "src/c3/_excludes.py" in result.stderr
        assert "hatch_build.py" in result.stderr
        # 自分自身は出力されないこと
        assert ".gitignore を変更しました" in result.stderr

    def test_warn_on_excludes_change(self) -> None:
        result = _run_hook(_payload("Edit", "src/c3/_excludes.py"))
        assert result.returncode == 0
        assert "[SyncCheck WARN]" in result.stderr
        assert ".gitignore" in result.stderr
        assert "hatch_build.py" in result.stderr

    def test_warn_on_hatch_build_change(self) -> None:
        result = _run_hook(_payload("Write", "hatch_build.py"))
        assert result.returncode == 0
        assert "[SyncCheck WARN]" in result.stderr
        assert ".gitignore" in result.stderr
        assert "src/c3/_excludes.py" in result.stderr

    def test_warn_with_absolute_path(self) -> None:
        abs_target = str(WORKTREE_ROOT / "hatch_build.py")
        result = _run_hook(_payload("Edit", abs_target))
        assert result.returncode == 0
        assert "[SyncCheck WARN]" in result.stderr


class TestNoWarn:
    """対象外なら警告なし。"""

    def test_no_warn_on_unrelated_file(self) -> None:
        result = _run_hook(_payload("Write", "src/c3/cli.py"))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_no_warn_on_read_tool(self) -> None:
        result = _run_hook(_payload("Read", ".gitignore"))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_no_warn_on_empty_file_path(self) -> None:
        result = _run_hook(_payload("Write", ""))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_no_warn_on_payload_without_file_path(self) -> None:
        result = _run_hook({"tool_name": "Write", "tool_input": {}})
        assert result.returncode == 0
        assert result.stderr == ""

    def test_invalid_json_does_not_crash(self) -> None:
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input="not json",
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0


class TestNeverBlocks:
    """いかなる入力でも exit 0（ブロックしない）。"""

    @pytest.mark.parametrize(
        "payload",
        [
            {"tool_name": "Write", "tool_input": {"file_path": ".gitignore"}},
            {"tool_name": "Edit", "tool_input": {"file_path": "src/c3/_excludes.py"}},
            {"tool_name": "Read", "tool_input": {"file_path": ".gitignore"}},
            {"tool_name": "Write", "tool_input": {}},
            {},
        ],
    )
    def test_exit_zero(self, payload: dict) -> None:
        result = _run_hook(payload)
        assert result.returncode == 0
