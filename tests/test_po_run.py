"""Tests for c3.po.run.run_manifest. The PO Python API is mocked."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from c3.po.run import RunResult, run_manifest
from parallel_orchestra import ManifestError, RunnerError


class _FakeTaskResult:
    """Stand-in for parallel_orchestra.TaskResult in tests."""

    def __init__(
        self,
        *,
        task_id: str = "t1",
        agent: str = "developer",
        returncode: int | None = 0,
        stderr: str = "",
        timed_out: bool = False,
        skipped: bool = False,
        resumed: bool = False,
    ) -> None:
        self.task_id = task_id
        self.agent = agent
        self.returncode = returncode
        self.stderr = stderr
        self.timed_out = timed_out
        self.skipped = skipped
        self.resumed = resumed

    @property
    def ok(self) -> bool:
        if self.resumed:
            return True
        return not self.skipped and self.returncode == 0 and not self.timed_out


class _FakeRunResult:
    def __init__(self, results: list[_FakeTaskResult]) -> None:
        self.results = results

    @property
    def overall_ok(self) -> bool:
        return all(r.ok for r in self.results)


def _write_manifest(tmp_path: Path) -> Path:
    manifest = tmp_path / "plan-report.md"
    manifest.write_text("---\npo_plan_version: '0.1'\n---\n", encoding="utf-8")
    return manifest


def test_run_manifest_success(tmp_path: Path):
    manifest = _write_manifest(tmp_path)
    fake = _FakeRunResult([_FakeTaskResult()])

    with patch("c3.po.run._po_run_manifest", return_value=fake) as po_run:
        result = run_manifest(manifest, max_workers=4)

    assert result == RunResult(
        exit_code=0, status="ok", report_path=None, stderr_tail=None
    )
    args, kwargs = po_run.call_args
    assert args[0] == manifest
    assert kwargs["max_workers"] == 4


def test_run_manifest_task_failure_captures_failure_tail(tmp_path: Path):
    manifest = _write_manifest(tmp_path)
    fake = _FakeRunResult(
        [
            _FakeTaskResult(
                task_id="t1",
                returncode=1,
                stderr="task A failed\nstack trace line 1\nstack trace line 2\n",
            )
        ]
    )

    with patch("c3.po.run._po_run_manifest", return_value=fake):
        result = run_manifest(manifest)

    assert result.exit_code == 1
    assert result.status == "task_failure"
    assert result.stderr_tail is not None
    assert "task A failed" in result.stderr_tail
    assert "task=t1" in result.stderr_tail


def test_run_manifest_manifest_invalid(tmp_path: Path):
    manifest = _write_manifest(tmp_path)

    with patch(
        "c3.po.run._po_run_manifest", side_effect=ManifestError("invalid manifest")
    ):
        result = run_manifest(manifest)

    assert result.exit_code == 2
    assert result.status == "manifest_invalid"
    assert result.stderr_tail is not None
    assert "invalid manifest" in result.stderr_tail


def test_run_manifest_runner_error(tmp_path: Path):
    manifest = _write_manifest(tmp_path)

    with patch("c3.po.run._po_run_manifest", side_effect=RunnerError("claude missing")):
        result = run_manifest(manifest)

    assert result.exit_code == 3
    assert result.status == "runner_error"
    assert result.stderr_tail is not None
    assert "claude missing" in result.stderr_tail


def test_run_manifest_dry_run_validates_only(tmp_path: Path):
    manifest = _write_manifest(tmp_path)

    with patch("c3.po.run.load_manifest") as loader, patch(
        "c3.po.run._po_run_manifest"
    ) as runner:
        result = run_manifest(manifest, dry_run=True)

    assert result.status == "ok"
    assert result.exit_code == 0
    loader.assert_called_once_with(manifest)
    runner.assert_not_called()


def test_run_manifest_dry_run_propagates_manifest_error(tmp_path: Path):
    manifest = _write_manifest(tmp_path)

    with patch(
        "c3.po.run.load_manifest", side_effect=ManifestError("bad frontmatter")
    ):
        result = run_manifest(manifest, dry_run=True)

    assert result.status == "manifest_invalid"
    assert result.exit_code == 2
    assert result.stderr_tail is not None
    assert "bad frontmatter" in result.stderr_tail


def test_run_manifest_kwargs_assembly(tmp_path: Path):
    manifest = _write_manifest(tmp_path)
    report = tmp_path / "report.json"
    fake = _FakeRunResult([_FakeTaskResult()])

    with patch("c3.po.run._po_run_manifest", return_value=fake) as po_run:
        run_manifest(
            manifest,
            max_workers=4,
            report=report,
            quiet=True,
            claude_exe="/opt/claude",
        )

    _, kwargs = po_run.call_args
    assert kwargs["max_workers"] == 4
    assert kwargs["report_path"] == report
    assert kwargs["dashboard_enabled"] is False
    assert kwargs["claude_executable"] == "/opt/claude"


def test_run_manifest_returns_report_path_on_success(tmp_path: Path):
    manifest = _write_manifest(tmp_path)
    report = tmp_path / "report.json"
    fake = _FakeRunResult([_FakeTaskResult()])

    with patch("c3.po.run._po_run_manifest", return_value=fake):
        result = run_manifest(manifest, report=report)

    assert result.report_path == report
