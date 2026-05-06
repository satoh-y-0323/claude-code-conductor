"""In-process wrapper for parallel-orchestra's ``run_manifest``.

Calls the bundled ``parallel_orchestra`` package directly via its Python API.
The wrapper maps PO's ``RunResult`` to a C3-friendly status enum so callers
in ``cli_po.py`` can react to common outcomes without depending on PO
internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from parallel_orchestra import (
    ManifestError,
    RunnerError,
    load_manifest,
    run_manifest as _po_run_manifest,
)

RunStatus = Literal["ok", "task_failure", "manifest_invalid", "runner_error"]


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    status: RunStatus
    report_path: Path | None
    stderr_tail: str | None  # populated only on task failure (PO failure summary)


def _exit_code_for(status: RunStatus) -> int:
    return {
        "ok": 0,
        "task_failure": 1,
        "manifest_invalid": 2,
        "runner_error": 3,
    }[status]


def _format_failure_tail(po_result) -> str | None:
    failed = [r for r in po_result.results if not r.ok]
    if not failed:
        return None
    lines: list[str] = []
    for r in failed[-5:]:
        head = (
            f"[task={r.task_id} agent={r.agent} rc={r.returncode} "
            f"timed_out={r.timed_out}]"
        )
        lines.append(head)
        if r.stderr:
            tail_lines = r.stderr.rstrip("\n").splitlines()[-8:]
            lines.extend(tail_lines)
    return "\n".join(lines) if lines else None


def run_manifest(
    manifest_path: Path | str,
    *,
    max_workers: int | None = None,
    report: Path | str | None = None,
    quiet: bool = False,
    dry_run: bool = False,
    claude_exe: str | None = None,
) -> RunResult:
    """Validate or execute a manifest via parallel-orchestra's Python API.

    ``dry_run=True`` only loads/validates the manifest without spawning agents.
    ``quiet=True`` suppresses PO's ANSI dashboard.
    """
    manifest_path = Path(manifest_path)
    report_path = Path(report) if report is not None else None

    if dry_run:
        try:
            load_manifest(manifest_path)
        except ManifestError as exc:
            return RunResult(
                exit_code=_exit_code_for("manifest_invalid"),
                status="manifest_invalid",
                report_path=None,
                stderr_tail=str(exc),
            )
        return RunResult(
            exit_code=_exit_code_for("ok"),
            status="ok",
            report_path=None,
            stderr_tail=None,
        )

    runner_kwargs: dict = {}
    if max_workers is not None:
        runner_kwargs["max_workers"] = max_workers
    if report_path is not None:
        runner_kwargs["report_path"] = report_path
    if quiet:
        runner_kwargs["dashboard_enabled"] = False
    if claude_exe is not None:
        runner_kwargs["claude_executable"] = claude_exe

    try:
        po_result = _po_run_manifest(manifest_path, **runner_kwargs)
    except ManifestError as exc:
        return RunResult(
            exit_code=_exit_code_for("manifest_invalid"),
            status="manifest_invalid",
            report_path=None,
            stderr_tail=str(exc),
        )
    except RunnerError as exc:
        return RunResult(
            exit_code=_exit_code_for("runner_error"),
            status="runner_error",
            report_path=None,
            stderr_tail=str(exc),
        )

    if po_result.overall_ok:
        return RunResult(
            exit_code=_exit_code_for("ok"),
            status="ok",
            report_path=report_path,
            stderr_tail=None,
        )

    return RunResult(
        exit_code=_exit_code_for("task_failure"),
        status="task_failure",
        report_path=report_path,
        stderr_tail=_format_failure_tail(po_result),
    )
