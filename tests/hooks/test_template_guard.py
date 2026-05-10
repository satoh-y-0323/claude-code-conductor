"""Tests for .dev/hooks/_template_guard.py

PreToolUse hook（配布元専用）の挙動を検証する。

テストケース:
 ブロック動作:
  1. src/c3/_template/ への Write → exit 2 + stderr 警告
  2. src/c3/_template/ への Edit (絶対パス) → exit 2
  3. ディレクトリトラバーサル経由でも resolve 後 block

 通過動作:
  4. src/c3/cli.py など _template/ 外 → exit 0
  5. tool_name が Read など Write/Edit 以外 → exit 0
  6. file_path が空 / payload に無い → exit 0
  7. 不正な JSON → exit 0 (crash しない)

 bypass:
  8. C3_TEMPLATE_GUARD_DISABLE=1 設定下では _template/ 配下でも exit 0

`.dev/` は gitignore 対象だが、テストファイル自体は配布される。利用者環境で
このテストが落ちないよう、利用者環境に `.dev/hooks/_template_guard.py` が
無い場合は skip する。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".dev" / "hooks" / "_template_guard.py"

pytestmark = pytest.mark.skipif(
    not HOOK_PATH.is_file(),
    reason=".dev/hooks/_template_guard.py is distributor-only (gitignored)",
)


def _run_hook(payload: dict, *, env: dict | None = None) -> subprocess.CompletedProcess:
    run_env = os.environ.copy()
    # bypass 環境変数のテスト時のみ override したいので、デフォルトでは消す
    run_env.pop("C3_TEMPLATE_GUARD_DISABLE", None)
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(WORKTREE_ROOT),
        env=run_env,
    )


def _payload(tool_name: str, file_path: str) -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
    }


class TestBlock:
    """src/c3/_template/ 配下の Write / Edit を block する。"""

    def test_block_write_to_template_relative(self) -> None:
        result = _run_hook(_payload("Write", "src/c3/_template/foo.md"))
        assert result.returncode == 2
        assert "[TemplateGuard BLOCK]" in result.stderr

    def test_block_edit_with_absolute_template_path(self) -> None:
        abs_target = str(WORKTREE_ROOT / "src" / "c3" / "_template" / "subdir" / "x.py")
        result = _run_hook(_payload("Edit", abs_target))
        assert result.returncode == 2
        assert "[TemplateGuard BLOCK]" in result.stderr

    def test_block_realpath_traversal(self) -> None:
        """src/c3/../c3/_template/x も解決後に block される。"""
        result = _run_hook(_payload("Write", "src/c3/../c3/_template/x.md"))
        assert result.returncode == 2

    def test_block_template_root_itself(self) -> None:
        """_template/ ルート自身を file として書こうとしても block。"""
        result = _run_hook(_payload("Write", "src/c3/_template"))
        assert result.returncode == 2


class TestPass:
    """対象外パスは exit 0 で通過する。"""

    def test_allow_write_outside_template(self) -> None:
        result = _run_hook(_payload("Write", "src/c3/cli.py"))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_allow_edit_in_claude_dir(self) -> None:
        result = _run_hook(_payload("Edit", ".claude/hooks/post_tool.py"))
        assert result.returncode == 0

    def test_non_write_edit_tool_passes(self) -> None:
        result = _run_hook(_payload("Read", "src/c3/_template/foo.md"))
        assert result.returncode == 0

    def test_empty_file_path_passes(self) -> None:
        result = _run_hook(_payload("Write", ""))
        assert result.returncode == 0

    def test_payload_without_file_path(self) -> None:
        result = _run_hook({"tool_name": "Write", "tool_input": {}})
        assert result.returncode == 0

    def test_invalid_json_does_not_crash(self) -> None:
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input="this is not json",
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0


class TestBypass:
    """C3_TEMPLATE_GUARD_DISABLE=1 で全て exit 0 になる。"""

    def test_bypass_via_env_var(self) -> None:
        result = _run_hook(
            _payload("Write", "src/c3/_template/foo.md"),
            env={"C3_TEMPLATE_GUARD_DISABLE": "1"},
        )
        assert result.returncode == 0
        assert "[TemplateGuard BLOCK]" not in result.stderr

    def test_bypass_disabled_with_other_value_still_blocks(self) -> None:
        """値が '1' 以外なら無効化されず block する（誤設定の安全側挙動）。"""
        result = _run_hook(
            _payload("Write", "src/c3/_template/foo.md"),
            env={"C3_TEMPLATE_GUARD_DISABLE": "true"},
        )
        assert result.returncode == 2
