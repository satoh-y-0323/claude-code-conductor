"""Red-phase tests for plan-report-20260502-234627.md — Task 1 and Task 2.

Analysis
--------
Task 1 — ``_mask_sensitive_env_values`` inline comment change
    Before: ``if value:  # mask any non-blank value``
    After:  ``if value:  # mask any non-empty value``

    This is a comment-only change with zero runtime-behaviour difference.
    No failing test can be written for a comment update.

Task 2 — ``_write_task_logs`` suppress-OSError change
    Before:
        except OSError:
            pass

    After:
        except OSError as exc:
            logger.debug(
                "_write_task_logs: failed to write logs for task %r: %s",
                task_id,
                exc,
            )

    The OSError is raised when ``Path.chmod()`` fails (e.g. on Windows NTFS
    or a read-only filesystem).  The function already creates the file
    successfully and only the chmod call raises.  This is observable via
    ``pytest``'s ``caplog`` fixture: after the fix, a DEBUG-level record must
    appear; before the fix, the ``except OSError: pass`` branch swallows the
    error silently and no log record is emitted.

    Strategy
    --------
    * Use ``monkeypatch`` to replace ``pathlib.Path.chmod`` with a function
      that always raises ``OSError("chmod not supported")``.
    * Call ``_write_task_logs`` directly (it is a module-level function).
    * Assert that at least one log record at DEBUG level from the
      ``parallel_orchestra.runner`` logger contains the task_id.

    This test FAILS against the current implementation (``except OSError: pass``
    produces no log record) and PASSES once the fix is applied.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from parallel_orchestra.runner import LogConfig, _write_task_logs

# ---------------------------------------------------------------------------
# Task 2 — OSError from chmod is logged at DEBUG level
# ---------------------------------------------------------------------------


def test_write_task_logs_chmod_oserror_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When chmod raises OSError, _write_task_logs must emit a WARNING log record.

    Arrange: patch Path.chmod to always raise OSError so the except branch fires.
    Act:     call _write_task_logs with valid stdout/stderr content.
    Assert:  a WARNING-level record from parallel_orchestra.runner mentioning the
             task_id is present in caplog — proving the exception was not silently
             swallowed by a bare ``pass``.
    """
    # Arrange
    task_id = "test-task-chmod-fail"
    log_config = LogConfig(base_dir=tmp_path / "logs")

    def _raise_oserror(self: Path, mode: int) -> None:  # noqa: ANN001
        raise OSError("chmod not supported on this filesystem")

    monkeypatch.setattr(Path, "chmod", _raise_oserror)

    # Act — capture all WARNING-and-above records from the runner logger
    with caplog.at_level(logging.WARNING, logger="parallel_orchestra.runner"):
        _write_task_logs(
            task_id,
            "stdout content",
            "stderr content",
            attempt=0,
            log_config=log_config,
        )

    # Assert — at least one WARNING record must mention the task_id
    warning_records = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and task_id in r.getMessage()
    ]
    assert warning_records, (
        f"Expected a WARNING log record containing {task_id!r} after chmod OSError, "
        f"but no such record was found. Records captured: {caplog.records!r}"
    )
