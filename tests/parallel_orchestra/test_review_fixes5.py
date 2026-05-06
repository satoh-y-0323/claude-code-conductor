"""Red-phase tests for _write_task_logs WARNING-level logging change.

Analysis
--------
Current implementation:
    except OSError as exc:
        logger.debug(
            "_write_task_logs: failed to write logs for task %r: %s",
            task_id,
            exc,
        )

Planned change:
    except OSError as exc:
        logger.warning(
            "_write_task_logs: failed to write logs for task %r: %s",
            task_id,
            exc,
        )

This test verifies that when ``Path.chmod`` raises ``OSError``, the exception
is logged at WARNING level (not DEBUG level).

Strategy
--------
* Use ``monkeypatch`` to replace ``pathlib.Path.chmod`` with a function that
  always raises ``OSError("chmod not supported")``.
* Call ``_write_task_logs`` directly (it is a module-level function).
* Assert that at least one log record at WARNING level from the
  ``parallel_orchestra.runner`` logger is present in ``caplog``.

This test FAILS against the current implementation (which uses
``logger.debug``) and PASSES once the fix changes it to ``logger.warning``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from parallel_orchestra.runner import LogConfig, _write_task_logs

# ---------------------------------------------------------------------------
# OSError from chmod is logged at WARNING level
# ---------------------------------------------------------------------------


def test_write_task_logs_chmod_oserror_logged_at_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When chmod raises OSError, _write_task_logs must emit a WARNING log record.

    Arrange: patch Path.chmod to always raise OSError so the except branch fires.
    Act:     call _write_task_logs with valid stdout/stderr content.
    Assert:  a WARNING-level record from parallel_orchestra.runner mentioning the
             task_id is present in caplog — proving the log level was upgraded
             from DEBUG to WARNING.
    """
    # Arrange
    task_id = "test-task-chmod-warning"
    log_config = LogConfig(base_dir=tmp_path / "logs")

    def _raise_oserror(self: Path, mode: int) -> None:
        raise OSError("chmod not supported on this filesystem")

    monkeypatch.setattr(Path, "chmod", _raise_oserror)

    # Act — capture WARNING-and-above records from the runner logger
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
