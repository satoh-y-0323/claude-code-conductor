"""Tests for c3.cli_po: c3 po waves / c3 po run-wave."""

from __future__ import annotations

import io
import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from c3.cli_po import (
    _handle_run_wave,
    _handle_waves,
)


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


# ---------------------------------------------------------------------------
# c3 po waves
# ---------------------------------------------------------------------------


def test_waves_emits_two_waves(tmp_path: Path, capsys):
    project, plan = _make_project(tmp_path)
    args = type("A", (), {"manifest": plan})()
    rc = _handle_waves(args)
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert len(payload["waves"]) == 2
    wave0_ids = [t["id"] for t in payload["waves"][0]["tasks"]]
    wave1_ids = [t["id"] for t in payload["waves"][1]["tasks"]]
    assert wave0_ids == ["tdd-login", "tdd-logout"]
    assert wave1_ids == ["review"]


def test_waves_includes_per_task_metadata(tmp_path: Path, capsys):
    project, plan = _make_project(tmp_path)
    args = type("A", (), {"manifest": plan})()
    rc = _handle_waves(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    first = payload["waves"][0]["tasks"][0]
    assert first["agent"] == "tdd-develop"
    assert first["read_only"] is False
    assert "src/auth/login.py" in first["writes"]


# ---------------------------------------------------------------------------
# c3 po run-wave
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.stderr = io.StringIO("")

    def wait(self) -> int:
        return self.returncode


def test_run_wave_writes_ephemeral_manifest_then_invokes_po(tmp_path: Path):
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

    captured: dict = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        # Verify the manifest path passed to PO is under .claude/tmp/
        manifest_path = Path(argv[2])
        captured["manifest_path"] = manifest_path
        captured["manifest_text"] = manifest_path.read_text(encoding="utf-8")
        return _FakeProc(0)

    with patch("c3.po.run.subprocess.Popen", side_effect=fake_popen), \
         patch("c3.cli_po.detect_po", return_value=(True, "0.1.1", "/usr/bin/parallel-orchestra")):
        rc = _handle_run_wave(args)

    assert rc == 0
    # The wave manifest should be inside .claude/tmp/
    assert ".claude" in captured["manifest_path"].parts
    assert "tmp" in captured["manifest_path"].parts
    # Only wave 0's tasks (tdd-login, tdd-logout) appear; review is wave 1
    text = captured["manifest_text"]
    assert "id: tdd-login" in text
    assert "id: tdd-logout" in text
    assert "id: review" not in text
    # depends_on must not be emitted in a wave manifest
    assert "depends_on" not in text


def test_run_wave_index_out_of_range_returns_2(tmp_path: Path, capsys):
    project, plan = _make_project(tmp_path)
    args = type(
        "A",
        (),
        {
            "manifest": plan,
            "wave_index": 99,
            "max_workers": None,
            "report": None,
            "quiet": False,
            "claude_exe": None,
        },
    )()
    with patch("c3.cli_po.detect_po", return_value=(True, "0.1.1", "/usr/bin/parallel-orchestra")):
        rc = _handle_run_wave(args)
    assert rc == 2
    err = capsys.readouterr().err
    assert "out of range" in err


def test_run_wave_when_po_missing_returns_1(tmp_path: Path, capsys):
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
    with patch("c3.cli_po.detect_po", return_value=(False, None, None)):
        rc = _handle_run_wave(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "parallel-orchestra is not installed" in err
