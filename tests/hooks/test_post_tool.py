"""Tests for .claude/hooks/post_tool.py

PostToolUse hook（F-007）の挙動を検証する。

テストケース:
 各パターン検出:
  1. .py に print( → 警告
  2. .py に TODO コメント → 警告
  3. .js に console.log → 警告
  4. .ts に FIXME → 警告
  5. .py に複数ヒット → 全件警告

 対象外・スキップ:
  6. .md ファイル → スキップ（対象外拡張子）
  7. .py に NUL バイト含むバイナリ → スキップ
  8. 256KB 超ファイル → 先頭部分のみスキャン

 既存挙動:
  9. tool_name が Write/Edit 以外 → 何もしない
 10. file_path が無い payload → 何もしない
 11. 不正な JSON → crash しない
 12. 検出があっても exit 0（非ブロッキング）

 言語制限:
 13. .js ファイルの print( は警告しない（print は .py のみ）
 14. .py ファイルの console.log は警告しない（console.log は js/ts 系のみ）
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "post_tool.py"


def _run_hook(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _payload(tool_name: str, file_path: str) -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
    }


# ---------------------------------------------------------------------------
# 各パターン検出
# ---------------------------------------------------------------------------


class TestPatternDetection:

    def test_py_print_is_warned(self, tmp_path: Path) -> None:
        """.py に print( があれば警告される。"""
        target = tmp_path / "sample.py"
        target.write_text("def foo():\n    print('debug')\n", encoding="utf-8")

        result = _run_hook(_payload("Write", str(target)))
        assert result.returncode == 0  # 非ブロッキング
        assert "[C3 quality]" in result.stderr
        assert "print" in result.stderr
        assert "sample.py" in result.stderr

    def test_py_todo_is_warned(self, tmp_path: Path) -> None:
        """.py の TODO コメントは警告される。"""
        target = tmp_path / "sample.py"
        target.write_text("# TODO: implement this later\nx = 1\n", encoding="utf-8")

        result = _run_hook(_payload("Edit", str(target)))
        assert result.returncode == 0
        assert "TODO" in result.stderr

    def test_js_console_log_is_warned(self, tmp_path: Path) -> None:
        """.js の console.log は警告される。"""
        target = tmp_path / "sample.js"
        target.write_text("function foo() {\n  console.log('debug');\n}\n", encoding="utf-8")

        result = _run_hook(_payload("Write", str(target)))
        assert result.returncode == 0
        assert "console.log" in result.stderr

    def test_ts_fixme_is_warned(self, tmp_path: Path) -> None:
        """.ts の FIXME は警告される。"""
        target = tmp_path / "sample.ts"
        target.write_text("// FIXME: 後で直す\nconst x: number = 1;\n", encoding="utf-8")

        result = _run_hook(_payload("Edit", str(target)))
        assert result.returncode == 0
        assert "FIXME" in result.stderr

    def test_multiple_findings_in_one_file(self, tmp_path: Path) -> None:
        """1 ファイル内の複数ヒットを全件警告する。"""
        target = tmp_path / "sample.py"
        target.write_text(
            "# TODO: foo\n"
            "print('debug')\n"
            "# FIXME: bar\n",
            encoding="utf-8",
        )

        result = _run_hook(_payload("Write", str(target)))
        assert result.returncode == 0
        # 3 つのパターンが検出される
        assert "TODO" in result.stderr
        assert "print" in result.stderr
        assert "FIXME" in result.stderr


# ---------------------------------------------------------------------------
# 対象外・スキップ
# ---------------------------------------------------------------------------


class TestSkipConditions:

    def test_md_file_is_skipped(self, tmp_path: Path) -> None:
        """.md ファイルは対象外でスキップされる（パターンが含まれていても警告なし）。"""
        target = tmp_path / "doc.md"
        target.write_text("# TODO: improve docs\n", encoding="utf-8")

        result = _run_hook(_payload("Write", str(target)))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_binary_file_is_skipped(self, tmp_path: Path) -> None:
        """先頭に NUL バイトを含むファイルはバイナリとしてスキップ。"""
        target = tmp_path / "binary.py"
        # NUL バイトを含むデータ。テキスト解釈すれば TODO もあるがスキップされるべき
        target.write_bytes(b"\x00\x01\x02 some content with TODO marker\n")

        result = _run_hook(_payload("Write", str(target)))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_large_file_is_truncated_to_head(self, tmp_path: Path) -> None:
        """256KB 超ファイルは先頭のみスキャンされ、末尾の TODO は検出されない。"""
        target = tmp_path / "big.py"
        # 先頭にダミーコンテンツを 260KB 入れ、末尾に TODO を置く
        head_padding = "x = 1\n" * (260 * 1024 // 6)  # ~260KB
        content = head_padding + "# TODO: at the very end\n"
        target.write_text(content, encoding="utf-8")

        result = _run_hook(_payload("Write", str(target)))
        assert result.returncode == 0
        # 末尾 TODO は読まれていないので警告されない
        assert "TODO" not in result.stderr

    def test_large_file_head_todo_is_warned(self, tmp_path: Path) -> None:
        """256KB 超ファイルでも先頭にある TODO は警告される（先頭は読まれる）。"""
        target = tmp_path / "big.py"
        content = "# TODO: at the head\n" + ("x = 1\n" * (260 * 1024 // 6))
        target.write_text(content, encoding="utf-8")

        result = _run_hook(_payload("Write", str(target)))
        assert result.returncode == 0
        assert "TODO" in result.stderr

    def test_nonexistent_file_is_skipped(self, tmp_path: Path) -> None:
        """ファイルが存在しなければ何もしない（crash しない）。"""
        target = tmp_path / "ghost.py"

        result = _run_hook(_payload("Write", str(target)))
        assert result.returncode == 0
        assert result.stderr == ""


# ---------------------------------------------------------------------------
# 既存挙動・エラー耐性
# ---------------------------------------------------------------------------


class TestExistingBehavior:

    def test_non_write_edit_tool_is_skipped(self, tmp_path: Path) -> None:
        """tool_name が Write/Edit 以外なら何もしない。"""
        target = tmp_path / "sample.py"
        target.write_text("print('debug')\n", encoding="utf-8")

        result = _run_hook({
            "tool_name": "Read",
            "tool_input": {"file_path": str(target)},
        })
        assert result.returncode == 0
        assert result.stderr == ""

    def test_payload_without_file_path(self) -> None:
        """file_path が無い payload では何もしない。"""
        result = _run_hook({
            "tool_name": "Write",
            "tool_input": {},
        })
        assert result.returncode == 0
        assert result.stderr == ""

    def test_invalid_json_does_not_crash(self) -> None:
        """不正な JSON でも crash しない（exit 0）。"""
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input="this is not json",
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# 言語制限
# ---------------------------------------------------------------------------


class TestLanguageRestrictions:
    """console.log は js/ts 系のみ、print( は .py のみ という制限を確認する。"""

    def test_js_print_is_not_warned(self, tmp_path: Path) -> None:
        """.js の print( は警告されない（print は .py 限定）。"""
        target = tmp_path / "sample.js"
        target.write_text("function print(x) { return x; }\nprint('hello');\n", encoding="utf-8")

        result = _run_hook(_payload("Write", str(target)))
        assert result.returncode == 0
        assert "print" not in result.stderr

    def test_py_console_log_is_not_warned(self, tmp_path: Path) -> None:
        """.py の console.log( は警告されない（console.log は js/ts 系限定）。"""
        target = tmp_path / "sample.py"
        target.write_text("class console:\n    @staticmethod\n    def log(x): pass\nconsole.log('x')\n", encoding="utf-8")

        result = _run_hook(_payload("Write", str(target)))
        assert result.returncode == 0
        assert "console.log" not in result.stderr
