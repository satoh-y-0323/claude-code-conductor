"""Tests for cli_po.py temporary-file handling.

These tests verify:
1. _handle_run_wave uses tempfile.NamedTemporaryFile (random filename, no predictable path)
2. _handle_run_wave uses try/finally to ensure wave_path.unlink(missing_ok=True) is called
"""

from __future__ import annotations

import inspect
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

import c3.cli_po as cli_po_module
from c3.cli_po import _handle_run_wave
from c3.po.run import RunResult


_PLAN_REPORT = textwrap.dedent(
    """\
    ---
    po_plan_version: "0.1"
    name: smoke
    cwd: ../..

    tasks:
      - id: tdd-login
        agent: tdd-develop
        read_only: false
        prompt: |
          login TDD
        writes:
          - src/auth/login.py
      - id: tdd-logout
        agent: tdd-develop
        read_only: false
        prompt: |
          logout TDD
        writes:
          - src/auth/logout.py
      - id: review
        agent: code-reviewer
        read_only: true
        prompt: review the auth module
        depends_on:
          - tdd-login
          - tdd-logout
    ---

    # smoke
    """
)


def _make_project(tmp_path: Path) -> tuple[Path, Path]:
    """Return (project_root, plan-report-path) with required agents stubbed."""
    project = tmp_path / "project"
    agents = project / ".claude" / "agents"
    agents.mkdir(parents=True)
    for name in ("tdd-develop", "code-reviewer"):
        (agents / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")
    reports = project / ".claude" / "reports"
    reports.mkdir(parents=True)
    plan = reports / "plan-report.md"
    plan.write_text(_PLAN_REPORT, encoding="utf-8")
    return project, plan


def test_handle_run_wave_uses_named_temporary_file(tmp_path: Path):
    """_handle_run_wave must use tempfile.NamedTemporaryFile for the wave manifest."""
    source = inspect.getsource(_handle_run_wave)
    assert "NamedTemporaryFile" in source, (
        "_handle_run_wave must use tempfile.NamedTemporaryFile to create the "
        "wave manifest file with a random name."
    )


def test_handle_run_wave_cleans_up_temp_file_on_success(tmp_path: Path):
    """wave_path.unlink(missing_ok=True) must be called in a finally block."""
    project, plan = _make_project(tmp_path)
    args = type(
        "A",
        (),
        {
            "manifest": plan,
            "wave_index": 0,
            "max_workers": None,
            "report": None,
            "quiet": False,
            "claude_exe": None,
        },
    )()

    created_paths: list[Path] = []

    def capturing_run_manifest(path, **kwargs):
        created_paths.append(Path(path))
        return RunResult(exit_code=0, status="ok", report_path=None, stderr_tail=None)

    with patch("c3.cli_po.run_manifest", side_effect=capturing_run_manifest):
        rc = _handle_run_wave(args)

    assert rc == 0
    assert created_paths, "run_manifest should have been called with a wave path"

    wave_path = created_paths[0]
    assert not wave_path.exists(), (
        f"Ephemeral wave manifest {wave_path} must be deleted after run "
        "(try/finally with wave_path.unlink(missing_ok=True) is required)."
    )


def test_handle_run_wave_cleans_up_temp_file_on_exception(tmp_path: Path):
    """wave_path.unlink(missing_ok=True) must be called even when run_manifest raises."""
    project, plan = _make_project(tmp_path)
    args = type(
        "A",
        (),
        {
            "manifest": plan,
            "wave_index": 0,
            "max_workers": None,
            "report": None,
            "quiet": False,
            "claude_exe": None,
        },
    )()

    created_paths: list[Path] = []

    def capturing_run_manifest(path, **kwargs):
        created_paths.append(Path(path))
        raise RuntimeError("simulated failure in run_manifest")

    with patch("c3.cli_po.run_manifest", side_effect=capturing_run_manifest):
        with pytest.raises(RuntimeError, match="simulated failure"):
            _handle_run_wave(args)

    assert created_paths, "run_manifest should have been called before the exception"
    wave_path = created_paths[0]
    assert not wave_path.exists(), (
        f"Ephemeral wave manifest {wave_path} must be deleted in finally block "
        "even when run_manifest raises an exception."
    )


def test_handle_run_wave_source_contains_try_finally():
    """The source of _handle_run_wave must contain a try/finally block for cleanup."""
    source = inspect.getsource(_handle_run_wave)
    assert "try:" in source and "finally:" in source, (
        "_handle_run_wave must contain a try/finally block to guarantee "
        "cleanup of the ephemeral wave manifest file."
    )


def test_handle_run_wave_source_contains_unlink():
    """The source of _handle_run_wave must call unlink(missing_ok=True)."""
    source = inspect.getsource(_handle_run_wave)
    assert "unlink" in source, (
        "_handle_run_wave must call wave_path.unlink(missing_ok=True) "
        "to clean up the temporary wave manifest file."
    )
