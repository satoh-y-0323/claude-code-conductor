"""Tests for c3.po.run.run_manifest. The PO subprocess is mocked."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

from c3.po.run import RunResult, run_manifest


class _FakeProc:
    def __init__(self, returncode: int, stderr_text: str = ""):
        self.returncode = returncode
        self.stderr = io.StringIO(stderr_text)

    def wait(self) -> int:
        return self.returncode


def _make_popen(returncode: int, stderr_text: str = ""):
    def factory(argv, **kwargs):
        factory.last_argv = argv
        factory.last_kwargs = kwargs
        return _FakeProc(returncode, stderr_text)

    factory.last_argv = None
    factory.last_kwargs = None
    return factory


def test_run_manifest_success(tmp_path: Path):
    manifest = tmp_path / "plan-report.md"
    manifest.write_text("---\npo_plan_version: '0.1'\n---\n", encoding="utf-8")

    factory = _make_popen(0)
    with patch("c3.po.run.subprocess.Popen", side_effect=factory):
        result = run_manifest(manifest)

    assert result == RunResult(
        exit_code=0, status="ok", report_path=None, stderr_tail=None
    )
    assert factory.last_argv[0] == "parallel-orchestra"
    assert factory.last_argv[1] == "run"
    assert factory.last_argv[2] == str(manifest)
    assert factory.last_kwargs["shell"] is False


def test_run_manifest_task_failure_captures_stderr_tail(tmp_path: Path):
    manifest = tmp_path / "plan-report.md"
    manifest.write_text("---\npo_plan_version: '0.1'\n---\n", encoding="utf-8")

    stderr = "task A failed\nstack trace line 1\nstack trace line 2\n"
    factory = _make_popen(1, stderr_text=stderr)
    with patch("c3.po.run.subprocess.Popen", side_effect=factory):
        result = run_manifest(manifest)

    assert result.exit_code == 1
    assert result.status == "task_failure"
    assert result.stderr_tail is not None
    assert "task A failed" in result.stderr_tail


def test_run_manifest_manifest_invalid(tmp_path: Path):
    manifest = tmp_path / "plan-report.md"
    manifest.write_text("---\npo_plan_version: '0.1'\n---\n", encoding="utf-8")

    factory = _make_popen(2, stderr_text="manifest invalid\n")
    with patch("c3.po.run.subprocess.Popen", side_effect=factory):
        result = run_manifest(manifest)

    assert result.exit_code == 2
    assert result.status == "manifest_invalid"


def test_run_manifest_runner_error(tmp_path: Path):
    manifest = tmp_path / "plan-report.md"
    manifest.write_text("---\npo_plan_version: '0.1'\n---\n", encoding="utf-8")

    factory = _make_popen(3, stderr_text="claude binary missing\n")
    with patch("c3.po.run.subprocess.Popen", side_effect=factory):
        result = run_manifest(manifest)

    assert result.exit_code == 3
    assert result.status == "runner_error"


def test_run_manifest_unknown_exit_code_maps_to_runner_error(tmp_path: Path):
    manifest = tmp_path / "plan-report.md"
    manifest.write_text("---\npo_plan_version: '0.1'\n---\n", encoding="utf-8")

    factory = _make_popen(99)
    with patch("c3.po.run.subprocess.Popen", side_effect=factory):
        result = run_manifest(manifest)

    assert result.status == "runner_error"


def test_run_manifest_not_installed(tmp_path: Path):
    manifest = tmp_path / "plan-report.md"
    manifest.write_text("---\npo_plan_version: '0.1'\n---\n", encoding="utf-8")

    with patch("c3.po.run.subprocess.Popen", side_effect=FileNotFoundError):
        result = run_manifest(manifest)

    assert result == RunResult(
        exit_code=-1, status="not_installed", report_path=None, stderr_tail=None
    )


def test_run_manifest_argv_assembly(tmp_path: Path):
    manifest = tmp_path / "plan-report.md"
    report = tmp_path / "report.json"
    manifest.write_text("---\npo_plan_version: '0.1'\n---\n", encoding="utf-8")

    factory = _make_popen(0)
    with patch("c3.po.run.subprocess.Popen", side_effect=factory):
        run_manifest(
            manifest,
            max_workers=4,
            report=report,
            quiet=True,
            dry_run=True,
            claude_exe="/opt/claude",
        )

    argv = factory.last_argv
    assert argv[0] == "parallel-orchestra"
    assert argv[1] == "run"
    assert argv[2] == str(manifest)
    assert "--max-workers" in argv and "4" in argv
    assert "--report" in argv and str(report) in argv
    assert "--quiet" in argv
    assert "--dry-run" in argv
    assert "--claude-exe" in argv and "/opt/claude" in argv
    assert factory.last_kwargs["shell"] is False


def test_run_manifest_decodes_stderr_as_utf8(tmp_path: Path):
    """Regression: on Windows the default locale (cp932) cannot decode PO's
    UTF-8 stderr, causing UnicodeDecodeError. The wrapper must pin the
    decoding to UTF-8 with a permissive error handler.
    """
    manifest = tmp_path / "plan-report.md"
    manifest.write_text("---\npo_plan_version: '0.1'\n---\n", encoding="utf-8")

    factory = _make_popen(0)
    with patch("c3.po.run.subprocess.Popen", side_effect=factory):
        run_manifest(manifest)

    kwargs = factory.last_kwargs
    assert kwargs.get("encoding") == "utf-8", (
        "Popen must pin encoding='utf-8' so PO's UTF-8 stderr decodes "
        "regardless of the platform's locale (Windows cp932)."
    )
    assert kwargs.get("errors") == "replace", (
        "Popen must use errors='replace' so a stray byte does not crash "
        "the wrapper mid-stream."
    )
    assert kwargs.get("text") is True
