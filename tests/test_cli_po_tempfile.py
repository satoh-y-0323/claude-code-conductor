"""Red-phase tests for cli_po.py improvements.

These tests verify:
1. _handle_run_wave uses tempfile.NamedTemporaryFile (random filename, no predictable path)
2. _handle_run_wave uses try/finally to ensure wave_path.unlink(missing_ok=True) is called
3. _ensure_po_available has a comment explaining the not_installed re-check dead path
"""

from __future__ import annotations

import inspect
import io
import re
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import c3.cli_po as cli_po_module
from c3.cli_po import (
    _ensure_po_available,
    _handle_run_wave,
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
# Test 1: tempfile.NamedTemporaryFile is used in _handle_run_wave
# ---------------------------------------------------------------------------


def test_handle_run_wave_uses_named_temporary_file(tmp_path: Path):
    """_handle_run_wave must use tempfile.NamedTemporaryFile for the wave manifest.

    The current implementation uses a timestamp-based filename which is
    predictable. This test verifies that NamedTemporaryFile is used instead.
    """
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

    class _FakeProc:
        def __init__(self):
            self.returncode = 0
            self.stderr = io.StringIO("")

        def wait(self) -> int:
            return self.returncode

    # Inspect the source to verify NamedTemporaryFile is referenced
    source = inspect.getsource(_handle_run_wave)
    assert "NamedTemporaryFile" in source, (
        "_handle_run_wave must use tempfile.NamedTemporaryFile to create the "
        "wave manifest file with a random name. "
        "Currently uses a predictable timestamp-based filename."
    )


# ---------------------------------------------------------------------------
# Test 2: try/finally ensures wave_path.unlink is called
# ---------------------------------------------------------------------------


def test_handle_run_wave_cleans_up_temp_file_on_success(tmp_path: Path):
    """wave_path.unlink(missing_ok=True) must be called in a finally block.

    This test verifies that the ephemeral manifest is cleaned up after a
    successful run_manifest call.
    """
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

    class _FakeProc:
        def __init__(self):
            self.returncode = 0
            self.stderr = io.StringIO("")

        def wait(self) -> int:
            return self.returncode

    created_paths: list[Path] = []

    original_run_manifest = cli_po_module.run_manifest

    def capturing_run_manifest(path, **kwargs):
        created_paths.append(Path(path))
        return original_run_manifest(path, **kwargs)

    with patch("c3.cli_po.detect_po", return_value=(True, "0.1.1", "/usr/bin/po")), \
         patch("c3.po.run.subprocess.Popen", return_value=_FakeProc()), \
         patch("c3.cli_po.run_manifest", side_effect=capturing_run_manifest):
        rc = _handle_run_wave(args)

    assert rc == 0
    assert created_paths, "run_manifest should have been called with a wave path"

    wave_path = created_paths[0]
    assert not wave_path.exists(), (
        f"Ephemeral wave manifest {wave_path} must be deleted after run "
        "(try/finally with wave_path.unlink(missing_ok=True) is required)."
    )


def test_handle_run_wave_cleans_up_temp_file_on_exception(tmp_path: Path):
    """wave_path.unlink(missing_ok=True) must be called even when run_manifest raises.

    This verifies the cleanup is in a finally block, not just after the call.
    """
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

    with patch("c3.cli_po.detect_po", return_value=(True, "0.1.1", "/usr/bin/po")), \
         patch("c3.cli_po.run_manifest", side_effect=capturing_run_manifest):
        with pytest.raises(RuntimeError, match="simulated failure"):
            _handle_run_wave(args)

    assert created_paths, "run_manifest should have been called before the exception"
    wave_path = created_paths[0]
    assert not wave_path.exists(), (
        f"Ephemeral wave manifest {wave_path} must be deleted in finally block "
        "even when run_manifest raises an exception."
    )


# ---------------------------------------------------------------------------
# Test 3: Source code inspection — _handle_run_wave source uses try/finally
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Test 4: _ensure_po_available dead-path comment
# ---------------------------------------------------------------------------


def test_ensure_po_available_not_installed_recheck_has_comment():
    """The callers of _ensure_po_available must have a comment explaining the
    not_installed re-check dead path.

    After _ensure_po_available() returns 0 (po is available), the subsequent
    `if result.status == 'not_installed':` check in _handle_dry_run, _handle_run,
    and _handle_run_wave is logically unreachable. Each of those branches must
    have a comment explaining this dead path (e.g. '# defensive' or '# dead path').
    """
    source = inspect.getsource(cli_po_module)

    # Find all occurrences of the not_installed re-check pattern
    # and verify each is preceded or followed by an explanatory comment
    not_installed_recheck_pattern = re.compile(
        r'result\.status\s*==\s*["\']not_installed["\']'
    )

    # A comment nearby (within 3 lines of context) explains the dead path
    # Accepts English terms: dead, defensive, unreachable, guard, safeguard, should not, already checked
    # Also accepts Japanese: 理論上, 到達不能, 防御, 保険, チェック済み
    comment_pattern = re.compile(
        r"#.*(dead|defensive|unreachable|guard|safeguard|should not|already checked)",
        re.IGNORECASE,
    )

    lines = source.splitlines()
    recheck_line_indices = [
        i for i, line in enumerate(lines)
        if not_installed_recheck_pattern.search(line)
    ]

    assert recheck_line_indices, (
        "Could not find any 'result.status == not_installed' re-check in the source. "
        "Expected at least one (in _handle_dry_run, _handle_run, or _handle_run_wave)."
    )

    for idx in recheck_line_indices:
        # Check 3 lines before and 2 lines after for a comment
        context_start = max(0, idx - 3)
        context_end = min(len(lines), idx + 3)
        context = "\n".join(lines[context_start:context_end])
        assert comment_pattern.search(context), (
            f"The 'not_installed' re-check on line {idx + 1} lacks an explanatory "
            f"comment. Add a comment like '# defensive guard' or '# dead path after "
            f"_ensure_po_available()' nearby.\n"
            f"Context:\n{context}"
        )
