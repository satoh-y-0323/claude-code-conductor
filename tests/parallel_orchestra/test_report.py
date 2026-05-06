"""Tests for parallel_orchestra.report module."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from parallel_orchestra._exceptions import ParallelOrchestraError
from parallel_orchestra.report import _md_escape, generate_report
from parallel_orchestra.runner import RunResult, TaskResult

_UTC = timezone.utc
_STARTED_AT = datetime(2026, 4, 26, 10, 0, 0, tzinfo=_UTC)
_FINISHED_AT = datetime(2026, 4, 26, 10, 1, 30, tzinfo=_UTC)


def _make_task_result(
    task_id: str = "task-a",
    agent: str = "general-purpose",
    returncode: int = 0,
    skipped: bool = False,
    resumed: bool = False,
    timed_out: bool = False,
    duration_sec: float = 10.0,
    retry_count: int = 0,
    failure_category: str = "none",
) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        agent=agent,
        returncode=returncode,
        stdout="",
        stderr="",
        timed_out=timed_out,
        timeout_reason=None,
        duration_sec=duration_sec,
        skipped=skipped,
        resumed=resumed,
        branch_name=None,
        retry_count=retry_count,
        failure_category=failure_category,
    )


def _make_run_result(*task_results: TaskResult) -> RunResult:
    return RunResult(results=tuple(task_results))


def _generate_json(tmp_path: Path, run_result: RunResult, filename: str = "report.json") -> dict:
    report_path = tmp_path / filename
    generate_report(
        run_result,
        report_path,
        manifest_name="test-manifest",
        started_at=_STARTED_AT,
        finished_at=_FINISHED_AT,
    )
    return json.loads(report_path.read_text(encoding="utf-8"))


def _generate_md(tmp_path: Path, run_result: RunResult, filename: str = "report.md") -> str:
    report_path = tmp_path / filename
    generate_report(
        run_result,
        report_path,
        manifest_name="test-manifest",
        started_at=_STARTED_AT,
        finished_at=_FINISHED_AT,
    )
    return report_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


def test_JSON_全タスク成功時のトップレベルフィールドが正しい(tmp_path: Path):
    run_result = _make_run_result(
        _make_task_result("task-a", returncode=0),
        _make_task_result("task-b", returncode=0),
    )
    data = _generate_json(tmp_path, run_result)

    assert data["manifest"] == "test-manifest"
    assert data["total"] == 2
    assert data["succeeded"] == 2
    assert data["failed"] == 0
    assert data["skipped"] == 0
    assert len(data["tasks"]) == 2


def test_JSON_タスクエントリのフィールドが正しい(tmp_path: Path):
    run_result = _make_run_result(
        _make_task_result("my-task", agent="reviewer", returncode=0, duration_sec=42.5)
    )
    data = _generate_json(tmp_path, run_result)

    task = data["tasks"][0]
    assert task["id"] == "my-task"
    assert task["agent"] == "reviewer"
    assert task["status"] == "succeeded"
    assert task["duration_sec"] == 42.5
    assert task["retry_count"] == 0
    assert task["failure_category"] == "none"


def test_JSON_失敗タスクのstatusがfailed(tmp_path: Path):
    run_result = _make_run_result(
        _make_task_result("fail-task", returncode=1, failure_category="transient")
    )
    data = _generate_json(tmp_path, run_result)

    assert data["failed"] == 1
    assert data["tasks"][0]["status"] == "failed"
    assert data["tasks"][0]["failure_category"] == "transient"


def test_JSON_スキップタスクのstatusがskipped(tmp_path: Path):
    run_result = _make_run_result(
        _make_task_result("skip-task", skipped=True, returncode=None)
    )
    data = _generate_json(tmp_path, run_result)

    assert data["skipped"] == 1
    assert data["tasks"][0]["status"] == "skipped"


def test_JSON_内部フィールドが除外される(tmp_path: Path):
    run_result = _make_run_result(_make_task_result())
    data = _generate_json(tmp_path, run_result)

    for key in data:
        assert not key.startswith("_"), f"Internal key {key!r} should not be in JSON"


def test_JSON_親ディレクトリが自動作成される(tmp_path: Path):
    run_result = _make_run_result(_make_task_result())
    report_path = tmp_path / "subdir" / "nested" / "report.json"
    generate_report(
        run_result,
        report_path,
        manifest_name="test",
        started_at=_STARTED_AT,
        finished_at=_FINISHED_AT,
    )
    assert report_path.exists()


# ---------------------------------------------------------------------------
# Markdown output
# ---------------------------------------------------------------------------


def test_Markdown_ヘッダーとテーブルが含まれる(tmp_path: Path):
    run_result = _make_run_result(_make_task_result("task-a", returncode=0))
    md = _generate_md(tmp_path, run_result)

    assert "# Run Summary: test-manifest" in md
    assert "| Task |" in md
    assert "task-a" in md


def test_Markdown_成功アイコンが含まれる(tmp_path: Path):
    run_result = _make_run_result(_make_task_result(returncode=0))
    md = _generate_md(tmp_path, run_result)
    assert "✓" in md


def test_Markdown_失敗アイコンが含まれる(tmp_path: Path):
    run_result = _make_run_result(_make_task_result(returncode=1))
    md = _generate_md(tmp_path, run_result)
    assert "✗" in md


def test_Markdown_スキップアイコンが含まれる(tmp_path: Path):
    run_result = _make_run_result(_make_task_result(skipped=True, returncode=None))
    md = _generate_md(tmp_path, run_result)
    assert "⊘" in md


def test_Markdown_resumedアイコンが含まれる(tmp_path: Path):
    run_result = _make_run_result(_make_task_result(resumed=True, returncode=0))
    md = _generate_md(tmp_path, run_result)
    assert "↩" in md


def test_Markdown_拡張子markdownも対応(tmp_path: Path):
    run_result = _make_run_result(_make_task_result())
    report_path = tmp_path / "report.markdown"
    generate_report(
        run_result,
        report_path,
        manifest_name="test",
        started_at=_STARTED_AT,
        finished_at=_FINISHED_AT,
    )
    assert report_path.exists()


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_未対応拡張子でParallelOrchestraErrorが送出される(tmp_path: Path):
    run_result = _make_run_result(_make_task_result())
    report_path = tmp_path / "report.txt"
    with pytest.raises(ParallelOrchestraError):
        generate_report(
            run_result,
            report_path,
            manifest_name="test",
        )


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks may need elevated rights on Windows")
def test_symlinkへの書き込みを拒否する(tmp_path: Path):
    run_result = _make_run_result(_make_task_result())
    real_path = tmp_path / "real_report.json"
    real_path.write_text("{}", encoding="utf-8")
    link_path = tmp_path / "link_report.json"
    link_path.symlink_to(real_path)

    with pytest.raises(ParallelOrchestraError):
        generate_report(
            run_result,
            link_path,
            manifest_name="test",
        )


# ---------------------------------------------------------------------------
# _md_escape
# ---------------------------------------------------------------------------


def test_md_escapeがパイプ文字をエスケープする():
    assert _md_escape("foo|bar") == r"foo\|bar"
    assert _md_escape("no pipe") == "no pipe"
