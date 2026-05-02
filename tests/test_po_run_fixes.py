"""TDD Red-phase tests for two fixes in c3.po.run.

[Code High-1] L88: assert -> RuntimeError when process.stderr is None
[Code Medium-2] L91: __import__("sys").stderr -> sys.stderr

These tests are written BEFORE the fixes are applied and are expected to
FAIL against the current production code (Red phase).
"""

from __future__ import annotations

import inspect
import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c3.po.run import run_manifest
import c3.po.run as run_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeProcWithNoneStderr:
    """Simulates a Popen result where stderr is None (should not happen
    when stderr=PIPE is passed, but we want to guard against it robustly)."""

    def __init__(self):
        self.stderr = None  # deliberately None

    def wait(self) -> int:
        return 0


class _FakeProcNormal:
    """Normal fake proc with a valid stderr stream."""

    def __init__(self, returncode: int = 0, stderr_text: str = ""):
        self.returncode = returncode
        self.stderr = io.StringIO(stderr_text)

    def wait(self) -> int:
        return self.returncode


# ---------------------------------------------------------------------------
# [Code High-1] RuntimeError when process.stderr is None
# ---------------------------------------------------------------------------

def test_run_manifest_raises_runtime_error_when_stderr_is_none(tmp_path: Path):
    """[Code High-1] When process.stderr is None, run_manifest must raise
    RuntimeError (not AssertionError).

    Current code: assert process.stderr is not None  -> raises AssertionError
    Fixed code:   if process.stderr is None: raise RuntimeError(...)

    This test FAILS on the current code because AssertionError is raised
    instead of RuntimeError.
    """
    manifest = tmp_path / "plan-report.md"
    manifest.write_text("---\npo_plan_version: '0.1'\n---\n", encoding="utf-8")

    with patch(
        "c3.po.run.subprocess.Popen",
        side_effect=lambda *a, **kw: _FakeProcWithNoneStderr(),
    ):
        with pytest.raises(RuntimeError, match="subprocess.stderr is None"):
            run_manifest(manifest)


def test_run_manifest_none_stderr_does_not_raise_assertion_error(tmp_path: Path):
    """[Code High-1] Complementary check: AssertionError must NOT be raised.

    After the fix, the guard should be a RuntimeError, not an AssertionError
    from the bare `assert` statement.

    This test FAILS on the current code because AssertionError IS raised.
    """
    manifest = tmp_path / "plan-report.md"
    manifest.write_text("---\npo_plan_version: '0.1'\n---\n", encoding="utf-8")

    with patch(
        "c3.po.run.subprocess.Popen",
        side_effect=lambda *a, **kw: _FakeProcWithNoneStderr(),
    ):
        try:
            run_manifest(manifest)
        except RuntimeError:
            # Expected after fix - pass
            return
        except AssertionError:
            pytest.fail(
                "run_manifest raised AssertionError instead of RuntimeError "
                "when process.stderr is None. "
                "Fix: replace `assert` with explicit `raise RuntimeError(...)`."
            )


# ---------------------------------------------------------------------------
# [Code Medium-2] sys.stderr used directly (no __import__)
# ---------------------------------------------------------------------------

def test_run_module_does_not_use_dunder_import_for_sys(tmp_path: Path):
    """[Code Medium-2] The run_manifest function body must not contain
    __import__("sys") — sys is already imported at the top of the module
    and should be referenced directly as `sys.stderr`.

    This test inspects the source of run_manifest.

    This test FAILS on the current code because __import__("sys") is present.
    """
    source = inspect.getsource(run_manifest)
    assert '__import__("sys")' not in source, (
        'run_manifest still contains __import__("sys").stderr. '
        "Fix: replace with the already-imported `sys.stderr`."
    )


def test_run_module_imports_sys_at_top_level():
    """[Code Medium-2] Verify that `sys` is imported at module level in
    c3.po.run so that `sys.stderr` is available without dynamic import.

    Current code does NOT import sys at the top level (uses __import__ inline).
    After the fix, `import sys` must be added at the top level.

    This test FAILS on the current code because sys is not a top-level name.
    """
    assert hasattr(run_module, "sys") or "sys" in vars(run_module), (
        "c3.po.run must import sys at module level for `sys.stderr` to work. "
        "Add `import sys` to the top-level imports."
    )


def test_run_manifest_stderr_output_goes_to_sys_stderr(tmp_path: Path, capsys):
    """[Code Medium-2] Behavioral check: stderr lines from the subprocess must
    be forwarded to sys.stderr.

    This passes on both old (__import__) and new (sys.stderr) code because
    both resolve to the same object; included as a regression guard.
    """
    manifest = tmp_path / "plan-report.md"
    manifest.write_text("---\npo_plan_version: '0.1'\n---\n", encoding="utf-8")

    with patch(
        "c3.po.run.subprocess.Popen",
        side_effect=lambda *a, **kw: _FakeProcNormal(0, "hello from subprocess\n"),
    ):
        run_manifest(manifest)

    captured = capsys.readouterr()
    assert "hello from subprocess" in captured.err
