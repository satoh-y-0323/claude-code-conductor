"""Tests for .dev/hooks/_planner_check.py (C3 開発元専用 PostToolUse hook)

C3 リポジトリ固有のルール R3（`src/c3/_template/` への writes 禁止）のみを検査する
開発元専用 hook の挙動を検証する。`.dev/` は gitignore 対象のため、利用者環境では
この hook ファイル自体が存在せず skip される。

検査ルール:
  R3: writes に src/c3/_template/ パスが 1 つでも含まれていたらブロック（exit 2）

汎用ルール（R2/R4/R6）は配布対象 `.claude/hooks/planner_check.py` に分離。
テストは `test_planner_check.py` 参照。
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".dev" / "hooks" / "_planner_check.py"

pytestmark = pytest.mark.skipif(
    not HOOK_PATH.is_file(),
    reason=".dev/hooks/_planner_check.py is distributor-only (gitignored)",
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


def _write_plan_report(tmp_path: Path, name: str, frontmatter_body: str) -> Path:
    content = f"---\n{textwrap.dedent(frontmatter_body).strip()}\n---\n\n# plan-report\n"
    report_path = tmp_path / f"plan-report-{name}.md"
    report_path.write_text(content, encoding="utf-8")
    return report_path


class TestR3Block:
    """writes に src/c3/_template/ パスが含まれる場合は exit 2 でブロックする。"""

    def test_block_template_path_in_writes(self, tmp_path: Path) -> None:
        """developer task の writes に _template/ パスがある → exit 2 + BLOCK。"""
        report_path = _write_plan_report(
            tmp_path,
            "template-in-writes",
            """
            po_plan_version: "0.1"
            name: "template path in writes"
            tasks:
              - id: t1
                agent: developer
                writes:
                  - src/c3/_template/.claude/hooks/foo.py
                prompt: "implement"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 2
        assert "[PlannerCheck BLOCK]" in result.stderr

    def test_block_template_path_in_tester_task(self, tmp_path: Path) -> None:
        """tester task の writes でも _template/ があれば exit 2。"""
        report_path = _write_plan_report(
            tmp_path,
            "tester-template",
            """
            po_plan_version: "0.1"
            name: "tester with template path"
            tasks:
              - id: t1
                agent: tester
                writes:
                  - tests/test_foo.py
                  - .claude/reports/test-report-t1.md
                  - src/c3/_template/.claude/settings.json
                prompt: "Red phase"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 2
        assert "[PlannerCheck BLOCK]" in result.stderr

    def test_block_template_subdirectory_path(self, tmp_path: Path) -> None:
        """_template/ 配下の深いパスも検出する。"""
        report_path = _write_plan_report(
            tmp_path,
            "template-deep",
            """
            po_plan_version: "0.1"
            name: "deep template path"
            tasks:
              - id: t1
                agent: developer
                writes:
                  - src/c3/_template/deeply/nested/path/file.py
                prompt: "implement"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 2
        assert "[PlannerCheck BLOCK]" in result.stderr

    def test_no_template_path_no_block(self, tmp_path: Path) -> None:
        """_template/ パスを含まない通常の plan-report は何も出力しない。"""
        report_path = _write_plan_report(
            tmp_path,
            "no-template",
            """
            po_plan_version: "0.1"
            name: "normal plan without template"
            tasks:
              - id: t1
                agent: developer
                writes: [src/c3/normal_file.py]
                prompt: "implement"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert "[PlannerCheck BLOCK]" not in result.stderr

    def test_r3_block_on_edit_payload(self, tmp_path: Path) -> None:
        """Edit ペイロードでも R3 違反は exit 2 でブロックされる。

        hook 本体は Write/Edit 両方を処理するため、Edit でも _template/ パス検出で
        BLOCK が発火することを確認する。
        """
        report_path = _write_plan_report(
            tmp_path,
            "r3-edit-variant",
            """
            po_plan_version: "0.1"
            name: "r3 edit variant"
            tasks:
              - id: t1
                agent: developer
                writes:
                  - src/c3/_template/.claude/hooks/foo.py
                prompt: "implement"
            """,
        )
        result = _run_hook(_payload("Edit", str(report_path)))
        assert result.returncode == 2
        assert "[PlannerCheck BLOCK]" in result.stderr


class TestOutOfScope:
    """R3 検査をスキップして exit 0 になるケース。"""

    def test_non_plan_report_is_ignored(self, tmp_path: Path) -> None:
        other = tmp_path / "test-report-foo.md"
        other.write_text("# test report\n", encoding="utf-8")
        result = _run_hook(_payload("Write", str(other)))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_invalid_json_does_not_crash(self) -> None:
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input="this is not valid json {{{",
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(WORKTREE_ROOT),
        )
        assert result.returncode == 0
