"""Tests for .dev/hooks/_planner_check.py

PostToolUse hook（配布元専用）の挙動を検証する。
plan-report-*.md の YAML frontmatter を機械検査し、planner の記述ミスを早期検出する。

検査ルール:
  R2: code-reviewer / security-reviewer の writes ファイル名に task_id を含み・タイムスタンプを含まないか
  R3: writes に src/c3/_template/ パスが 1 つでも含まれていたらブロック（exit 2）
  R4: 同一 writes パスを複数 task が宣言し、depends_on で順序付けされていない場合に警告

廃止ルール:
  R1 (tdd-develop writes 完備): v2.1.0 で `tdd-develop` agent 廃止に伴い削除。

テストケース:
  1. R2 正常: code-reviewer の writes がタスクID含み・タイムスタンプなし → 警告なし
  2. R2 違反 (2 サブケース):
     - code-reviewer writes にタイムスタンプ入り → [PlannerCheck WARN]
     - security-reviewer writes にタイムスタンプ入り → [PlannerCheck WARN]
  3. R3 違反: writes に _template/ パスを含む → exit 2 + [PlannerCheck BLOCK]
  4. R4 違反: 同一 writes パスを 2 task が宣言・depends_on なし → [PlannerCheck WARN]
     R4 正常: depends_on で順序付けあり → 警告なし
  5. 対象外動作:
     - plan-report 以外のファイルへの Write → exit 0・stderr 空
     - tool_name が Read → exit 0・stderr 空
     - file_path 空 → exit 0・stderr 空
     - payload に file_path なし → exit 0・stderr 空
     - 不正 JSON → exit 0 (crash しない)
     - frontmatter なしの plan-report → exit 0・stderr 空
     - YAML 構文エラーの plan-report → exit 0・stderr 空

`.dev/` は gitignore 対象だが、テストファイル自体は配布される。利用者環境に
`.dev/hooks/_planner_check.py` が無い場合は skip する。
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
    """plan-report への Write/Edit を模擬する payload を生成する。"""
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
    }


def _write_plan_report(tmp_path: Path, name: str, frontmatter_body: str) -> Path:
    """tmp_path に plan-report-{name}.md を書き出し、そのパスを返す。"""
    content = f"---\n{textwrap.dedent(frontmatter_body).strip()}\n---\n\n# plan-report\n"
    report_path = tmp_path / f"plan-report-{name}.md"
    report_path.write_text(content, encoding="utf-8")
    return report_path


# ---------------------------------------------------------------------------
# Group 1: R2 正常 — code-reviewer/security-reviewer の writes がタスクID付き・タイムスタンプなし
# ---------------------------------------------------------------------------

class TestR2Pass:
    """code-reviewer/security-reviewer の writes がタスク ID 含み・タイムスタンプなしの場合。"""

    def test_code_reviewer_with_task_id_no_timestamp(self, tmp_path: Path) -> None:
        """task_id を含むファイル名でタイムスタンプが入っていない場合は警告なし。"""
        report_path = _write_plan_report(
            tmp_path,
            "reviewer-correct",
            """
            po_plan_version: "0.1"
            name: "reviewer with task id"
            tasks:
              - id: review1
                agent: code-reviewer
                writes:
                  - .claude/reports/code-review-report-review1.md
                prompt: "review"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert "[PlannerCheck WARN]" not in result.stderr

    def test_security_reviewer_with_task_id_no_timestamp(self, tmp_path: Path) -> None:
        """security-reviewer も task_id 含み・タイムスタンプなしなら警告なし。"""
        report_path = _write_plan_report(
            tmp_path,
            "sec-reviewer-correct",
            """
            po_plan_version: "0.1"
            name: "security reviewer correct"
            tasks:
              - id: sec_review
                agent: security-reviewer
                writes:
                  - .claude/reports/security-review-report-sec_review.md
                prompt: "security review"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert "[PlannerCheck WARN]" not in result.stderr

    def test_task_id_with_8_digits_no_false_positive(self, tmp_path: Path) -> None:
        """task_id に偶然 8 桁の数字が含まれてもタイムスタンプ誤検知しない。

        例: code-review-report-task87654321.md は YYYYMMDD 風 8 桁を含むが、
        前後に数字以外の境界がないため誤検知してはならない。
        """
        report_path = _write_plan_report(
            tmp_path,
            "task-id-numeric",
            """
            po_plan_version: "0.1"
            name: "task id with 8 digits"
            tasks:
              - id: task87654321
                agent: code-reviewer
                writes:
                  - .claude/reports/code-review-report-task87654321.md
                prompt: "review"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert "[PlannerCheck WARN]" not in result.stderr

    def test_reviewer_without_writes_field_no_warn(self, tmp_path: Path) -> None:
        """writes フィールドが省略されている code-reviewer は R2 チェック対象なし。"""
        report_path = _write_plan_report(
            tmp_path,
            "reviewer-no-writes",
            """
            po_plan_version: "0.1"
            name: "reviewer without writes"
            tasks:
              - id: review1
                agent: code-reviewer
                read_only: true
                prompt: "review only"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert "[PlannerCheck WARN]" not in result.stderr

    def test_task_id_prefix_8digits_no_false_positive(self, tmp_path: Path) -> None:
        """task_id が 8 桁数字で始まる場合（例: 87654321abc）でも誤検知しない。

        ファイル名: code-review-report-87654321abc.md
        8 桁数字の直後が英字なので _TIMESTAMP_RE にはマッチしない。
        """
        report_path = _write_plan_report(
            tmp_path,
            "task-id-8digit-prefix",
            """
            po_plan_version: "0.1"
            name: "task id starting with 8 digits"
            tasks:
              - id: 87654321abc
                agent: code-reviewer
                writes:
                  - .claude/reports/code-review-report-87654321abc.md
                prompt: "review"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert "[PlannerCheck WARN]" not in result.stderr

    def test_task_id_suffix_8digits_no_false_positive(self, tmp_path: Path) -> None:
        """task_id が英字の後に 8 桁数字で終わる場合（例: review87654321）でも誤検知しない。

        ファイル名: code-review-report-review87654321.md
        8 桁数字の直前が英字なので _TIMESTAMP_RE にはマッチしない。
        """
        report_path = _write_plan_report(
            tmp_path,
            "task-id-8digit-suffix",
            """
            po_plan_version: "0.1"
            name: "task id ending with 8 digits"
            tasks:
              - id: review87654321
                agent: code-reviewer
                writes:
                  - .claude/reports/code-review-report-review87654321.md
                prompt: "review"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert "[PlannerCheck WARN]" not in result.stderr

    def test_14digit_number_no_false_positive(self, tmp_path: Path) -> None:
        """14 桁の連続数字（YYYYMMDDHHMMSS）はハイフンなしのため誤検知しない。

        _TIMESTAMP_RE は YYYYMMDD-HHMMSS（ハイフン区切り）のみマッチする。
        14 桁の連続数字は最初の 8 桁が境界なしの数字（後ろが数字）でマッチしない。
        """
        report_path = _write_plan_report(
            tmp_path,
            "task-id-14digits",
            """
            po_plan_version: "0.1"
            name: "task id with 14 consecutive digits"
            tasks:
              - id: t1
                agent: code-reviewer
                writes:
                  - .claude/reports/code-review-report-20260510021200.md
                prompt: "review"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert "[PlannerCheck WARN]" not in result.stderr

    def test_8digit_standalone_in_filename_triggers_warn(self, tmp_path: Path) -> None:
        """8 桁数字がハイフンで区切られてファイル名中に現れる場合はタイムスタンプとして検知する。

        ファイル名: code-review-report-20260510.md（ハイフン区切りで孤立した 8 桁数字）
        _TIMESTAMP_RE の境界条件: 前後が非英数字（ハイフン・`.`）なのでマッチする。
        """
        report_path = _write_plan_report(
            tmp_path,
            "standalone-8digit",
            """
            po_plan_version: "0.1"
            name: "8 digit standalone timestamp in filename"
            tasks:
              - id: t1
                agent: code-reviewer
                writes:
                  - .claude/reports/code-review-report-20260510.md
                prompt: "review"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert "[PlannerCheck WARN]" in result.stderr


# ---------------------------------------------------------------------------
# Group 2: R2 違反 — タイムスタンプ入りファイル名を検出する
# ---------------------------------------------------------------------------

class TestR2Violation:
    """code-reviewer/security-reviewer の writes にタイムスタンプ入りパスがある場合。"""

    def test_code_reviewer_with_date_timestamp(self, tmp_path: Path) -> None:
        """YYYYMMDD 形式のタイムスタンプが入ったファイル名は違反。"""
        report_path = _write_plan_report(
            tmp_path,
            "reviewer-date-ts",
            """
            po_plan_version: "0.1"
            name: "reviewer with date timestamp"
            tasks:
              - id: review1
                agent: code-reviewer
                writes:
                  - .claude/reports/code-review-report-20260510.md
                prompt: "review"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert "[PlannerCheck WARN]" in result.stderr

    def test_code_reviewer_with_datetime_timestamp(self, tmp_path: Path) -> None:
        """YYYYMMDD-HHMMSS 形式のタイムスタンプが入ったファイル名は違反。"""
        report_path = _write_plan_report(
            tmp_path,
            "reviewer-datetime-ts",
            """
            po_plan_version: "0.1"
            name: "reviewer with datetime timestamp"
            tasks:
              - id: review1
                agent: code-reviewer
                writes:
                  - .claude/reports/code-review-report-20260510-021200.md
                prompt: "review"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert "[PlannerCheck WARN]" in result.stderr

    def test_security_reviewer_with_timestamp(self, tmp_path: Path) -> None:
        """security-reviewer の writes にタイムスタンプ入りパスがある場合も違反。"""
        report_path = _write_plan_report(
            tmp_path,
            "sec-reviewer-ts",
            """
            po_plan_version: "0.1"
            name: "security reviewer with timestamp"
            tasks:
              - id: sec_review
                agent: security-reviewer
                writes:
                  - .claude/reports/security-review-report-20260510-021200.md
                prompt: "security review"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert "[PlannerCheck WARN]" in result.stderr


# ---------------------------------------------------------------------------
# Group 3: R3 違反 — _template/ パスを含む writes はブロック（exit 2）
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Group 4: R4 正常・違反 — 同一 writes パスを複数 task が宣言する場合
# ---------------------------------------------------------------------------

class TestR4:
    """同一 writes パスを複数 task が宣言する際の depends_on による順序付け検証。"""

    def test_duplicate_writes_without_dependency_warns(self, tmp_path: Path) -> None:
        """同一パスを 2 task が宣言し、depends_on で順序付けされていない → WARN。"""
        report_path = _write_plan_report(
            tmp_path,
            "duplicate-no-dep",
            """
            po_plan_version: "0.1"
            name: "duplicate writes no dependency"
            tasks:
              - id: t1
                agent: developer
                writes:
                  - src/foo.py
                prompt: "first"
              - id: t2
                agent: developer
                writes:
                  - src/foo.py
                prompt: "second without dependency"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert "[PlannerCheck WARN]" in result.stderr

    def test_duplicate_writes_with_correct_dependency_no_warn(self, tmp_path: Path) -> None:
        """後発 task が先発 task を depends_on で参照していれば警告なし。"""
        report_path = _write_plan_report(
            tmp_path,
            "duplicate-with-dep",
            """
            po_plan_version: "0.1"
            name: "duplicate writes with dependency"
            tasks:
              - id: t1
                agent: tester
                writes:
                  - tests/test_foo.py
                  - .claude/reports/test-report-t1.md
                prompt: "Red phase"
              - id: t2
                agent: tester
                depends_on:
                  - t1
                writes:
                  - tests/test_foo.py
                  - .claude/reports/test-report-t1.md
                prompt: "refactor"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert "[PlannerCheck WARN]" not in result.stderr

    def test_three_tasks_with_partial_dependency_warns(self, tmp_path: Path) -> None:
        """3 task で同一パスを宣言し、t3 が t1 に依存しているが t2 には依存していない場合。
        t2 と t3 の間に順序がないため警告が発生する。"""
        report_path = _write_plan_report(
            tmp_path,
            "partial-dep",
            """
            po_plan_version: "0.1"
            name: "three tasks partial dependency"
            tasks:
              - id: t1
                agent: developer
                writes:
                  - src/bar.py
                prompt: "first"
              - id: t2
                agent: developer
                writes:
                  - src/bar.py
                prompt: "second"
              - id: t3
                agent: developer
                depends_on:
                  - t1
                writes:
                  - src/bar.py
                prompt: "third depends on t1 only"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert "[PlannerCheck WARN]" in result.stderr


# ---------------------------------------------------------------------------
# Group 5: 対象外動作 — 検査をスキップして exit 0・stderr 空になるケース
# ---------------------------------------------------------------------------

class TestOutOfScope:
    """hook が検査をスキップするケースを確認する。"""

    def test_non_plan_report_write_is_ignored(self, tmp_path: Path) -> None:
        """plan-report でないファイルへの Write は何もしない。"""
        other_report = tmp_path / "code-review-report-test.md"
        other_report.write_text("# code review\n", encoding="utf-8")
        result = _run_hook(_payload("Write", str(other_report)))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_test_report_write_is_ignored(self, tmp_path: Path) -> None:
        """test-report-*.md は plan-report でないので無視される。"""
        report = tmp_path / "test-report-20260510-021200.md"
        report.write_text("# test report\n", encoding="utf-8")
        result = _run_hook(_payload("Write", str(report)))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_read_tool_is_ignored(self, tmp_path: Path) -> None:
        """tool_name が Read の場合は何もしない。"""
        report_path = _write_plan_report(
            tmp_path,
            "some-plan",
            """
            po_plan_version: "0.1"
            name: "test"
            tasks: []
            """,
        )
        result = _run_hook(_payload("Read", str(report_path)))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_empty_file_path_is_ignored(self) -> None:
        """file_path が空文字列の場合は何もしない。"""
        result = _run_hook(_payload("Write", ""))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_missing_file_path_key_is_ignored(self) -> None:
        """payload に file_path キーがない場合は何もしない。"""
        result = _run_hook({"tool_name": "Write", "tool_input": {}})
        assert result.returncode == 0
        assert result.stderr == ""

    def test_invalid_json_does_not_crash(self) -> None:
        """不正な JSON 入力で crash しない（exit 0）。"""
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input="this is not valid json {{{",
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(WORKTREE_ROOT),
        )
        assert result.returncode == 0

    def test_plan_report_without_frontmatter_is_ignored(self, tmp_path: Path) -> None:
        """frontmatter がない plan-report ファイルは silent exit 0。"""
        report_path = tmp_path / "plan-report-no-frontmatter.md"
        report_path.write_text(
            "# plan-report: no frontmatter\n\nsome content here\n",
            encoding="utf-8",
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_plan_report_with_invalid_yaml_is_ignored(self, tmp_path: Path) -> None:
        """YAML 構文エラーの plan-report は silent exit 0。"""
        report_path = tmp_path / "plan-report-bad-yaml.md"
        report_path.write_text(
            "---\n: invalid: yaml: content [{{{\n---\n# body\n",
            encoding="utf-8",
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_plan_report_file_not_found_is_ignored(self, tmp_path: Path) -> None:
        """参照先の plan-report ファイルが存在しない場合も silent exit 0。"""
        nonexistent = tmp_path / "plan-report-nonexistent.md"
        result = _run_hook(_payload("Write", str(nonexistent)))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_plan_report_with_non_list_tasks_is_ignored(self, tmp_path: Path) -> None:
        """tasks が list でない異常構造の frontmatter は silent exit 0。"""
        report_path = tmp_path / "plan-report-bad-structure.md"
        report_path.write_text(
            "---\npo_plan_version: '0.1'\ntasks: not_a_list\n---\n# body\n",
            encoding="utf-8",
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert result.stderr == ""
