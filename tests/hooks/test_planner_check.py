"""Tests for .claude/hooks/planner_check.py (配布対象 PostToolUse hook)

plan-report-*.md の YAML frontmatter を機械検査する配布対象 hook の挙動を検証する。
利用先環境でも動作する汎用ルール（R2/R4/R6）を扱う。

検査ルール:
  R2: code-reviewer / security-reviewer の writes ファイル名にタイムスタンプを含まないか
  R4: 同一 writes パスを複数 task が宣言し、depends_on で順序付けされていない場合に警告
  R6: タスク総数 >= 3 かつ reviewer 系タスクが 0 件の場合に WARN（レビュー全削除検出）

C3 開発リポジトリ固有の R3（src/c3/_template/ ブロック）は
`.dev/hooks/_planner_check.py` に分離。テストは `test_planner_check_dev.py` 参照。

廃止ルール:
  R1 (tdd-develop writes 完備): v2.1.0 で `tdd-develop` agent 廃止に伴い削除。
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "planner_check.py"

pytestmark = pytest.mark.skipif(
    not HOOK_PATH.is_file(),
    reason=".claude/hooks/planner_check.py not found",
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
# Note: R2 テストは reviewer タスクを 1 件含むため R6 (reviewer 0 件検出) は発火しない
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

    def test_r2_violation_on_edit_payload(self, tmp_path: Path) -> None:
        """Edit ペイロードでも R2 違反は検出される。

        hook 本体は Write/Edit 両方を処理するため、Edit でも代表的な違反ケースで
        WARN が発火することを確認する（R6 Edit テストと対称な代表ケース）。
        """
        report_path = _write_plan_report(
            tmp_path,
            "r2-edit-variant",
            """
            po_plan_version: "0.1"
            name: "r2 edit variant"
            tasks:
              - id: review1
                agent: code-reviewer
                writes:
                  - .claude/reports/code-review-report-20260510.md
                prompt: "review"
            """,
        )
        result = _run_hook(_payload("Edit", str(report_path)))
        assert result.returncode == 0
        assert "[PlannerCheck WARN]" in result.stderr
        assert "R2" in result.stderr


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

    def test_r4_violation_on_edit_payload(self, tmp_path: Path) -> None:
        """Edit ペイロードでも R4 違反は検出される。

        hook 本体は Write/Edit 両方を処理するため、Edit でも代表的な違反ケースで
        WARN が発火することを確認する。
        """
        report_path = _write_plan_report(
            tmp_path,
            "r4-edit-variant",
            """
            po_plan_version: "0.1"
            name: "r4 edit variant"
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
        result = _run_hook(_payload("Edit", str(report_path)))
        assert result.returncode == 0
        assert "[PlannerCheck WARN]" in result.stderr
        assert "R4" in result.stderr


# ---------------------------------------------------------------------------
# Group R6: レビュータスク全削除検出（タスク総数 >= 3 かつ reviewer 0 件で WARN）
# ---------------------------------------------------------------------------

class TestR6ReviewerAbsence:
    """plan-report のレビュータスクが完全消失している場合の WARN 検証。"""

    def test_three_tasks_with_no_reviewer_warns(self, tmp_path: Path) -> None:
        """タスク総数 3 件で reviewer 0 件なら WARN。"""
        report_path = _write_plan_report(
            tmp_path,
            "no-reviewer-3tasks",
            """
            po_plan_version: "0.1"
            name: "three tasks no reviewer"
            tasks:
              - id: t1
                agent: tester
                writes: [tests/test_foo.py, .claude/reports/test-report-t1.md]
                prompt: "Red"
              - id: t2
                agent: developer
                depends_on: [t1]
                writes: [src/foo.py]
                prompt: "Green"
              - id: t3
                agent: tester
                depends_on: [t2]
                writes: [.claude/reports/test-report-t3.md]
                prompt: "Confirm"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert "[PlannerCheck WARN]" in result.stderr
        assert "R6" in result.stderr

    def test_three_tasks_with_no_reviewer_edit_triggers_warn(self, tmp_path: Path) -> None:
        """Edit ペイロードでも、タスク総数 3 件で reviewer 0 件なら WARN が発火する。

        tool_name を "Write" から "Edit" に変えた場合も R6 検査が同様に機能することを確認する。
        """
        report_path = _write_plan_report(
            tmp_path,
            "no-reviewer-3tasks-edit",
            """
            po_plan_version: "0.1"
            name: "three tasks no reviewer edit variant"
            tasks:
              - id: t1
                agent: tester
                writes: [tests/test_foo.py, .claude/reports/test-report-t1.md]
                prompt: "Red"
              - id: t2
                agent: developer
                depends_on: [t1]
                writes: [src/foo.py]
                prompt: "Green"
              - id: t3
                agent: tester
                depends_on: [t2]
                writes: [.claude/reports/test-report-t3.md]
                prompt: "Confirm"
            """,
        )
        result = _run_hook(_payload("Edit", str(report_path)))
        assert result.returncode == 0
        assert "[PlannerCheck WARN]" in result.stderr
        assert "R6" in result.stderr

    def test_two_tasks_with_no_reviewer_below_threshold_no_warn(self, tmp_path: Path) -> None:
        """タスク総数 2 件（閾値未満）で reviewer 0 件でも WARN なし。

        小規模な単発タスク（ドキュメント修正など）の合理的な省略を巻き込まないため。
        """
        report_path = _write_plan_report(
            tmp_path,
            "no-reviewer-2tasks",
            """
            po_plan_version: "0.1"
            name: "two tasks no reviewer below threshold"
            tasks:
              - id: t1
                agent: tester
                writes: [tests/test_foo.py, .claude/reports/test-report-t1.md]
                prompt: "Red"
              - id: t2
                agent: developer
                depends_on: [t1]
                writes: [src/foo.py]
                prompt: "Green"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert "R6" not in result.stderr

    def test_code_reviewer_present_no_warn(self, tmp_path: Path) -> None:
        """code-reviewer タスクが 1 件以上含まれていれば WARN なし。"""
        report_path = _write_plan_report(
            tmp_path,
            "with-code-reviewer",
            """
            po_plan_version: "0.1"
            name: "with code reviewer"
            tasks:
              - id: t1
                agent: tester
                writes: [tests/test_foo.py, .claude/reports/test-report-t1.md]
                prompt: "Red"
              - id: t2
                agent: developer
                depends_on: [t1]
                writes: [src/foo.py]
                prompt: "Green"
              - id: t3
                agent: tester
                depends_on: [t2]
                writes: [.claude/reports/test-report-t3.md]
                prompt: "Confirm"
              - id: review1
                agent: code-reviewer
                depends_on: [t3]
                read_only: true
                prompt: "review"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert "R6" not in result.stderr

    def test_security_reviewer_present_no_warn(self, tmp_path: Path) -> None:
        """security-reviewer タスクのみでも reviewer 系として WARN を抑制する。"""
        report_path = _write_plan_report(
            tmp_path,
            "with-security-reviewer",
            """
            po_plan_version: "0.1"
            name: "with security reviewer only"
            tasks:
              - id: t1
                agent: tester
                writes: [tests/test_foo.py, .claude/reports/test-report-t1.md]
                prompt: "Red"
              - id: t2
                agent: developer
                depends_on: [t1]
                writes: [src/foo.py]
                prompt: "Green"
              - id: sec_review
                agent: security-reviewer
                depends_on: [t2]
                read_only: true
                prompt: "security review"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert "R6" not in result.stderr


# ---------------------------------------------------------------------------
# Group JSON 出力 — LLM コンテキスト注入用の hookSpecificOutput.additionalContext
# ---------------------------------------------------------------------------

class TestJsonAdditionalContext:
    """stderr に加えて stdout に JSON 出力で LLM コンテキストに WARN を注入する。

    Claude Code 公式仕様（PostToolUse exit 0 + stdout JSON）に従い、
    hookSpecificOutput.additionalContext を返すと LLM が system reminder として
    受け取る。stderr は人間向け、stdout JSON は LLM 向けの二重出力。
    """

    def test_r6_violation_emits_json_additional_context(self, tmp_path: Path) -> None:
        """R6 違反時に stdout に hookSpecificOutput.additionalContext を含む JSON が出力される。"""
        report_path = _write_plan_report(
            tmp_path,
            "r6-json-test",
            """
            po_plan_version: "0.1"
            name: "r6 json output test"
            tasks:
              - id: t1
                agent: tester
                writes: [tests/test_x.py, .claude/reports/test-report-t1.md]
                prompt: "Red"
              - id: t2
                agent: developer
                depends_on: [t1]
                writes: [src/x.py]
                prompt: "Green"
              - id: t3
                agent: tester
                depends_on: [t2]
                writes: [.claude/reports/test-report-t3.md]
                prompt: "Confirm"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        # stdout は JSON
        parsed = json.loads(result.stdout)
        assert "hookSpecificOutput" in parsed
        assert parsed["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        assert "R6" in ctx
        assert "[PlannerCheck WARN]" in ctx

    def test_r2_violation_emits_json_additional_context(self, tmp_path: Path) -> None:
        """R2 違反でも JSON additionalContext が出る。"""
        report_path = _write_plan_report(
            tmp_path,
            "r2-json-test",
            """
            po_plan_version: "0.1"
            name: "r2 json output test"
            tasks:
              - id: review1
                agent: code-reviewer
                writes: [.claude/reports/code-review-report-20260510.md]
                prompt: "review"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        parsed = json.loads(result.stdout)
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        assert "R2" in ctx

    def test_no_violation_no_json_output(self, tmp_path: Path) -> None:
        """違反なしなら stdout は空（JSON も出さない）。"""
        report_path = _write_plan_report(
            tmp_path,
            "no-violation",
            """
            po_plan_version: "0.1"
            name: "no violation"
            tasks:
              - id: t1
                agent: tester
                writes: [tests/test_x.py, .claude/reports/test-report-t1.md]
                prompt: "Red"
              - id: t2
                agent: developer
                depends_on: [t1]
                writes: [src/x.py]
                prompt: "Green"
              - id: review1
                agent: code-reviewer
                depends_on: [t2]
                read_only: true
                prompt: "review"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""


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


# ---------------------------------------------------------------------------
# Group Security: 守備深化（path traversal / sanitize / size limit / debug path）
# ---------------------------------------------------------------------------

class TestSecurityHardening:
    """セキュリティ・防御的コーディングの検証（SR-V-001/SR-V-002/SR-NEW 対応）。"""

    def test_path_traversal_in_file_path_is_ignored(self, tmp_path: Path) -> None:
        """L-4 [SR-V-002]: file_path に `..` を含むパストラバーサルは検査対象外とする。

        `_is_plan_report` で basename だけを見て通過させてしまうと、後段の
        `open(file_path)` が任意のパスにアクセスする経路が成立する。
        防御として `..` セグメントを含むパスは silent exit 0 で拒否する。
        """
        result = _run_hook(_payload("Write", "../../plan-report-malicious.md"))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_path_traversal_with_subdir_is_ignored(self, tmp_path: Path) -> None:
        """`subdir/../../plan-report-foo.md` のような中間 `..` も拒否する。"""
        result = _run_hook(
            _payload("Write", "subdir/../../plan-report-malicious.md")
        )
        assert result.returncode == 0
        assert result.stderr == ""

    def test_u2028_in_task_id_is_sanitized_from_output(self, tmp_path: Path) -> None:
        """L-5 [SR-V-001]: task id に U+2028/U+2029 を含むと WARN 出力から除去される。

        `_sanitize` が U+2028 (Line Separator) / U+2029 (Paragraph Separator) を
        除去する。これらの文字が一部の JS/JSON パーサで行区切りとして扱われ
        JSON 解析エラーになる問題を防ぐ。
        """
        # task id に U+2028 ( ) を埋め込んだ R4 違反シナリオ
        report_path = _write_plan_report(
            tmp_path,
            "u2028-task-id",
            """
            po_plan_version: "0.1"
            name: "u2028 sanitize test"
            tasks:
              - id: "t1 malicious"
                agent: developer
                writes:
                  - src/foo.py
                prompt: "first"
              - id: t2
                agent: developer
                writes:
                  - src/foo.py
                prompt: "second"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        # stderr / stdout のいずれにも U+2028 が含まれないこと
        assert " " not in result.stderr, (
            "stderr に U+2028 (Line Separator) が残っている"
        )
        assert " " not in result.stdout, (
            "stdout に U+2028 (Line Separator) が残っている"
        )

    def test_u2029_in_task_id_is_sanitized_from_output(self, tmp_path: Path) -> None:
        """U+2029 (Paragraph Separator) も同様に除去される。"""
        report_path = _write_plan_report(
            tmp_path,
            "u2029-task-id",
            """
            po_plan_version: "0.1"
            name: "u2029 sanitize test"
            tasks:
              - id: "t1 bad"
                agent: developer
                writes:
                  - src/bar.py
                prompt: "first"
              - id: t2
                agent: developer
                writes:
                  - src/bar.py
                prompt: "second"
            """,
        )
        result = _run_hook(_payload("Write", str(report_path)))
        assert result.returncode == 0
        assert " " not in result.stderr
        assert " " not in result.stdout

    def test_stdin_read_has_size_limit(self) -> None:
        """L-1 [SR-V-001]: sys.stdin.read() がサイズ制限引数を持つ。

        DoS 対策として stdin から読み取る最大バイト数を制限する。
        実装は `sys.stdin.read(<MAX_BYTES>)` を想定。
        """
        import re
        source = HOOK_PATH.read_text(encoding="utf-8")
        # sys.stdin.read() に少なくとも 1 つの引数（整数式）があることを確認
        assert re.search(
            r"sys\.stdin\.read\(\s*[^\s)][^)]*\)", source
        ), "sys.stdin.read() にサイズ制限引数がない（無制限読み取り）"

    def test_file_read_has_size_limit(self) -> None:
        """L-3 [SR-V-001]: plan-report ファイルの読み取りにサイズ制限がある。

        実装は `fh.read(<MAX_BYTES>)`（リテラル数値または定数名）を想定。
        """
        import re
        source = HOOK_PATH.read_text(encoding="utf-8")
        # fh.read() 単独（無制限）は不可。fh.read(<引数>) を 1 件以上見つけられること。
        # 引数はリテラル数値・定数名・式のいずれでもよい。
        assert re.search(
            r"fh\.read\(\s*[^\s)][^)]*\)", source
        ), "plan-report ファイル読み取りにサイズ制限引数がない（無制限読み取り）"

    def test_debug_log_path_is_absolute(self) -> None:
        """L-6 [SR-NEW]: DEBUG_LOG_PATH が cwd 依存の相対パスでなく絶対パスである。

        実装は `Path(__file__).resolve().parents[N]` ベースを想定。
        """
        source = HOOK_PATH.read_text(encoding="utf-8")
        # __file__ ベースで絶対パスに変換する記法が含まれること
        assert "__file__" in source, (
            "hook ファイルが __file__ を参照していない（絶対パス変換に必要）"
        )
        # 相対パス文字列定数として DEBUG_LOG_PATH を持たないこと
        # 具体的には ".claude/tmp/..." の文字列リテラルが直接代入されていないこと
        import re
        assert not re.search(
            r'DEBUG_LOG_PATH\s*=\s*["\']\.claude/tmp/', source
        ), "DEBUG_LOG_PATH が cwd 相対の文字列リテラルのまま"
