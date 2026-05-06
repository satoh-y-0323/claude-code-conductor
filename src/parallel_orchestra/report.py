"""Run summary report generation for parallel-orchestra."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._exceptions import ParallelOrchestraError

if TYPE_CHECKING:
    from .runner import RunResult, TaskResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STATUS_ICON: dict[str, str] = {
    "succeeded": "✓",
    "failed": "✗",
    "skipped": "⊘",
    "resumed": "↩",
}

_SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".json", ".md", ".markdown"})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _md_escape(text: str) -> str:
    return text.replace("|", r"\|")


def _task_status(result: TaskResult) -> str:
    if result.resumed:
        return "resumed"
    if result.skipped:
        return "skipped"
    if result.ok:
        return "succeeded"
    return "failed"


def _build_task_dict(result: TaskResult) -> dict[str, Any]:
    status = _task_status(result)
    return {
        "id": result.task_id,
        "agent": result.agent,
        "status": status,
        "duration_sec": round(result.duration_sec, 1),
        "retry_count": result.retry_count,
        "failure_category": result.failure_category,
    }


def _build_report_dict(
    run_result: RunResult,
    *,
    manifest_name: str,
    started_at: datetime,
    finished_at: datetime,
) -> dict[str, Any]:
    results = run_result.results
    statuses = [_task_status(r) for r in results]
    total = len(results)
    succeeded = statuses.count("succeeded")
    failed = statuses.count("failed")
    skipped = statuses.count("skipped")
    resumed = statuses.count("resumed")

    duration_sec = (finished_at - started_at).total_seconds()

    return {
        "manifest": manifest_name,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_sec": round(duration_sec, 1),
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped + resumed,
        "_resumed": resumed,
        "tasks": [_build_task_dict(r) for r in results],
    }


def _format_json(report_dict: dict[str, Any]) -> str:
    public = {k: v for k, v in report_dict.items() if not k.startswith("_")}
    return json.dumps(public, ensure_ascii=False, indent=2) + "\n"


def _format_markdown(report_dict: dict[str, Any]) -> str:
    manifest_name = report_dict["manifest"]
    started_at = report_dict["started_at"]
    finished_at = report_dict["finished_at"]
    duration_sec = report_dict["duration_sec"]
    succeeded = report_dict["succeeded"]
    failed = report_dict["failed"]
    resumed = report_dict.get("_resumed", 0)
    skipped = report_dict["skipped"] - resumed

    results_parts = [
        f"{succeeded} succeeded",
        f"{failed} failed",
        f"{skipped} skipped",
    ]
    if resumed:
        results_parts.append(f"{resumed} resumed")
    results_line = " / ".join(results_parts)

    lines: list[str] = [
        f"# Run Summary: {manifest_name}",
        "",
        f"**Started:** {started_at}",
        f"**Finished:** {finished_at}",
        f"**Duration:** {duration_sec}s",
        "",
        f"## Results: {results_line}",
        "",
        "| Task | Agent | Status | Duration | Retries | Failure |",
        "|------|-------|--------|----------|---------|---------|",
    ]

    for task in report_dict["tasks"]:
        status = task["status"]
        icon = _STATUS_ICON.get(status, "?")
        label = f"{icon} {status}"

        if status in ("skipped", "resumed"):
            duration_cell = "—"
            retries_cell = "—"
            failure_cell = "—"
        else:
            duration_cell = f"{task['duration_sec']}s"
            retries_cell = str(task["retry_count"])
            fc = task["failure_category"]
            failure_cell = "—" if fc == "none" else fc

        lines.append(
            f"| {_md_escape(task['id'])} | {_md_escape(task['agent'])} | {label}"
            f" | {duration_cell} | {retries_cell} | {failure_cell} |"
        )

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_report(
    run_result: RunResult,
    report_path: Path,
    *,
    manifest_name: str,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> None:
    """Write a run summary report to *report_path*.

    Args:
        run_result: The aggregated result of the completed run.
        report_path: Destination file path (.json, .md, or .markdown).
        manifest_name: Human-readable name of the manifest.
        started_at: Timestamp of run start (defaults to current UTC time).
        finished_at: Timestamp of run completion (defaults to current UTC time).

    Raises:
        ParallelOrchestraError: If *report_path* is a symbolic link, if the
            extension is not supported, or if the file cannot be written.
    """
    if report_path.is_symlink():
        raise ParallelOrchestraError(
            "--report path is a symbolic link; "
            "refusing to write to avoid symlink attacks."
        )

    now = datetime.now(tz=timezone.utc)
    if started_at is None:
        started_at = now
    if finished_at is None:
        finished_at = now

    ext = report_path.suffix.lower()
    if ext not in _SUPPORTED_EXTENSIONS:
        raise ParallelOrchestraError(
            f"Unsupported report extension {ext!r}. "
            f"Use one of: {sorted(_SUPPORTED_EXTENSIONS)}."
        )

    report_dict = _build_report_dict(
        run_result,
        manifest_name=manifest_name,
        started_at=started_at,
        finished_at=finished_at,
    )

    if ext == ".json":
        content = _format_json(report_dict)
    else:
        content = _format_markdown(report_dict)

    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise ParallelOrchestraError(
            "Failed to write report: permission denied or I/O error."
        ) from exc
