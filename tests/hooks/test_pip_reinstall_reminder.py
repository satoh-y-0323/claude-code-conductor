"""Tests for .dev/hooks/_pip_reinstall_reminder.py

PostToolUse hook（配布元専用）の挙動を検証する。

テストケース:
 警告動作:
  1. src/c3/__init__.py を Write → stderr に再インストール案内
  2. pyproject.toml を Edit → stderr に再インストール案内
  3. 絶対パスでも検出

 通過動作（警告なし）:
  4. 関係ないファイル → stderr 空
  5. tool_name が Read など → stderr 空
  6. file_path が空 / payload に無い → stderr 空
  7. 不正な JSON → crash しない

 ブロックしない:
  8. いかなる入力でも exit 0
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".dev" / "hooks" / "_pip_reinstall_reminder.py"

pytestmark = pytest.mark.skipif(
    not HOOK_PATH.is_file(),
    reason=".dev/hooks/_pip_reinstall_reminder.py is distributor-only (gitignored)",
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
    """REINSTALL_TRIGGERS に該当するファイル変更時に警告を出す。"""

    def test_warn_on_c3_init_change(self) -> None:
        result = _run_hook(_payload("Write", "src/c3/__init__.py"))
        assert result.returncode == 0
        assert "[PipReinstallReminder]" in result.stderr
        assert "pip install -e ." in result.stderr

    def test_warn_on_pyproject_change(self) -> None:
        result = _run_hook(_payload("Edit", "pyproject.toml"))
        assert result.returncode == 0
        assert "[PipReinstallReminder]" in result.stderr

    def test_warn_with_absolute_path(self) -> None:
        abs_target = str(WORKTREE_ROOT / "src" / "c3" / "__init__.py")
        result = _run_hook(_payload("Write", abs_target))
        assert result.returncode == 0
        assert "[PipReinstallReminder]" in result.stderr


class TestNoWarn:
    """対象外なら警告なし。"""

    def test_no_warn_on_unrelated_file(self) -> None:
        result = _run_hook(_payload("Write", "src/c3/cli.py"))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_no_warn_on_other_init(self) -> None:
        """parallel_orchestra/__init__.py 等は対象外（dynamic 取得のため）。"""
        result = _run_hook(_payload("Edit", "src/parallel_orchestra/__init__.py"))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_no_warn_on_read_tool(self) -> None:
        result = _run_hook(_payload("Read", "src/c3/__init__.py"))
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
    """いかなる入力でも exit 0。"""

    @pytest.mark.parametrize(
        "payload",
        [
            {"tool_name": "Write", "tool_input": {"file_path": "src/c3/__init__.py"}},
            {"tool_name": "Edit", "tool_input": {"file_path": "pyproject.toml"}},
            {"tool_name": "Read", "tool_input": {"file_path": "pyproject.toml"}},
            {"tool_name": "Write", "tool_input": {}},
            {},
        ],
    )
    def test_exit_zero(self, payload: dict) -> None:
        result = _run_hook(payload)
        assert result.returncode == 0
