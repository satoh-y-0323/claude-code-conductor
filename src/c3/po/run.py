"""Subprocess wrapper for ``parallel-orchestra run``.

Always invoked with ``shell=False`` and an argv list. The wrapper does not
import ``parallel_orchestra`` directly - the only dependency on PO is the
CLI executable on PATH.
"""

from __future__ import annotations

import collections
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

RunStatus = Literal[
    "ok", "task_failure", "manifest_invalid", "runner_error", "not_installed"
]

_EXIT_TO_STATUS: dict[int, RunStatus] = {
    0: "ok",
    1: "task_failure",
    2: "manifest_invalid",
    3: "runner_error",
}


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    status: RunStatus
    report_path: Path | None
    stderr_tail: str | None  # last ~40 lines, populated only on non-zero exit


def run_manifest(
    manifest_path: Path | str,
    *,
    max_workers: int | None = None,
    report: Path | str | None = None,
    quiet: bool = False,
    dry_run: bool = False,
    claude_exe: str | None = None,
    cli: str = "parallel-orchestra",
) -> RunResult:
    """Invoke ``parallel-orchestra run <manifest>`` and return a typed result.

    Streams stdout/stderr to the parent terminal so PO's progress dashboard
    is visible to the user. The last 40 stderr lines are captured separately
    for failure summaries.
    """
    manifest_path = Path(manifest_path)
    report_path = Path(report) if report is not None else None

    argv: list[str] = [cli, "run", str(manifest_path)]
    if max_workers is not None:
        argv.extend(["--max-workers", str(max_workers)])
    if report_path is not None:
        argv.extend(["--report", str(report_path)])
    if quiet:
        argv.append("--quiet")
    if dry_run:
        argv.append("--dry-run")
    if claude_exe is not None:
        argv.extend(["--claude-exe", claude_exe])

    stderr_tail = collections.deque(maxlen=40)

    try:
        process = subprocess.Popen(
            argv,
            shell=False,
            text=True,
            stdout=None,
            stderr=subprocess.PIPE,
            bufsize=1,
        )
    except FileNotFoundError:
        return RunResult(
            exit_code=-1,
            status="not_installed",
            report_path=None,
            stderr_tail=None,
        )

    assert process.stderr is not None
    try:
        for line in process.stderr:
            print(line, end="", flush=True, file=__import__("sys").stderr)
            stderr_tail.append(line.rstrip("\n"))
    finally:
        process.stderr.close()
    exit_code = process.wait()

    status: RunStatus = _EXIT_TO_STATUS.get(exit_code, "runner_error")
    tail: str | None = None
    if exit_code != 0 and stderr_tail:
        tail = "\n".join(stderr_tail)

    return RunResult(
        exit_code=exit_code,
        status=status,
        report_path=report_path,
        stderr_tail=tail,
    )
