"""Command-line interface for parallel-orchestra."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import parallel_orchestra

from .manifest import ManifestError, load_manifest
from .runner import (
    _DEFAULT_MAX_WORKERS,
    RunnerError,
    RunResult,
    TaskResult,
    format_dry_run,
    run_manifest,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EXIT_SUCCESS = 0
_EXIT_PARTIAL_FAILURE = 1
_EXIT_MANIFEST_ERROR = 2
_EXIT_RUNNER_ERROR = 3

_DEFAULT_CLAUDE_EXE = "claude"
_TIMEOUT_TAIL_LINES = 20


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="parallel-orchestra",
        description="Run Claude Code agents in parallel.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=parallel_orchestra.__version__,
    )

    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser(
        "run",
        help="Run all tasks defined in a manifest file.",
    )
    run_parser.add_argument(
        "manifest_path",
        help="Path to the manifest (.md) file.",
    )
    run_parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        metavar="N",
        help="Maximum number of parallel worker threads (default: 3).",
    )
    run_parser.add_argument(
        "--claude-exe",
        default=_DEFAULT_CLAUDE_EXE,
        metavar="PATH",
        help="Name or path of the claude executable (default: claude).",
    )
    run_parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress output for successful tasks.",
    )
    run_parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Directory for per-task stdout/stderr logs"
            " (default: <git-root>/.claude/logs). "
            "Logs may contain sensitive information - do not share publicly."
        ),
    )
    run_parser.add_argument(
        "--no-log",
        action="store_true",
        help=(
            "Disable per-task log file persistence. "
            "Recommended when running in sensitive or shared environments."
        ),
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the execution plan (task order, timeout, dependencies) "
            "without running any tasks."
        ),
    )
    run_parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Skip tasks that already succeeded in a previous run by loading"
            " the .po-run-state-<manifest-stem>.json file next to the manifest."
            " If the state file is missing or the manifest has changed,"
            " a warning is emitted and all tasks are run normally."
        ),
    )
    run_parser.add_argument(
        "--report",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Write a run summary report to PATH after all tasks complete. "
            "The format is determined by the file extension: "
            ".json for JSON, .md or .markdown for Markdown."
        ),
    )
    dashboard_group = run_parser.add_mutually_exclusive_group()
    dashboard_group.add_argument(
        "--dashboard",
        action="store_true",
        dest="force_dashboard",
        default=False,
        help=(
            "Force-enable the ANSI progress dashboard "
            "regardless of TTY detection."
        ),
    )
    dashboard_group.add_argument(
        "--no-dashboard",
        action="store_true",
        dest="no_dashboard",
        default=False,
        help="Disable the ANSI progress dashboard.",
    )

    return parser


def _status_label(result: TaskResult) -> str:
    if result.resumed:
        return "skip"
    if result.timed_out:
        return "timeout"
    if result.returncode == 0:
        return "ok"
    return "fail"


def _format_summary_line(result: TaskResult) -> str:
    label = _status_label(result)
    returncode_str = str(result.returncode) if result.returncode is not None else "None"
    reason = f" ({result.timeout_reason} timeout)" if result.timeout_reason else ""
    retries = f" retries={result.retry_count}" if result.retry_count > 0 else ""
    category = (
        f" category={result.failure_category}"
        if result.failure_category != "none"
        else ""
    )
    # Structured agent output (e.g. tdd-develop JSON: status/cycles/report/reason)
    agent_info = ""
    if result.agent_status:
        agent_info += f" status={result.agent_status}"
        if result.agent_cycles is not None:
            agent_info += f" cycles={result.agent_cycles}"
        if result.agent_report:
            agent_info += f" report={Path(result.agent_report).name}"
    if result.agent_reason:
        agent_info += f" reason={result.agent_reason}"
    return (
        f"[{label}] {result.task_id} ({result.agent})"
        f" duration={result.duration_sec:.2f}"
        f" returncode={returncode_str}"
        f"{reason}"
        f"{retries}"
        f"{category}"
        f"{agent_info}"
    )


def _print_timeout_tail(result: TaskResult) -> None:
    lines = result.stdout.splitlines()
    tail = lines[-_TIMEOUT_TAIL_LINES:] if lines else []
    if not tail:
        return
    print(f"  Last {len(tail)} lines before timeout:", file=sys.stderr)
    for line in tail:
        print(f"  > {line}", file=sys.stderr)


def _print_summary(run_result: RunResult, *, quiet: bool) -> None:
    for result in run_result.results:
        if result.resumed:
            print(f"[skip] {result.task_id} ({result.agent}) resumed")
            continue
        is_success = result.ok
        if quiet and is_success:
            continue
        print(_format_summary_line(result))
        if result.timed_out:
            _print_timeout_tail(result)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point for the parallel-orchestra CLI.

    Returns:
        Integer exit code:
        - 0: All tasks succeeded.
        - 1: One or more tasks failed.
        - 2: Manifest error (invalid or missing manifest).
        - 3: Runner error (e.g., claude binary not found).
    """
    if argv is None:
        argv = sys.argv[1:]

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_usage(sys.stderr)
        return _EXIT_MANIFEST_ERROR

    try:
        manifest = load_manifest(args.manifest_path)
    except ManifestError as exc:
        print(f"ManifestError: {exc}", file=sys.stderr)
        return _EXIT_MANIFEST_ERROR

    effective_max_workers = (
        args.max_workers if args.max_workers is not None else _DEFAULT_MAX_WORKERS
    )

    if args.dry_run:
        print(format_dry_run(manifest, max_workers=effective_max_workers))
        return _EXIT_SUCCESS

    if args.force_dashboard:
        dashboard_enabled: bool | None = True
    elif args.no_dashboard:
        dashboard_enabled = False
    else:
        dashboard_enabled = None

    try:
        run_result = run_manifest(
            manifest,
            max_workers=args.max_workers,
            claude_executable=args.claude_exe,
            log_enabled=not args.no_log,
            log_dir=args.log_dir,
            resume=args.resume,
            report_path=args.report,
            dashboard_enabled=dashboard_enabled,
        )
    except RunnerError as exc:
        print(f"RunnerError: {exc}", file=sys.stderr)
        return _EXIT_RUNNER_ERROR

    _print_summary(run_result, quiet=args.quiet)

    return _EXIT_SUCCESS if run_result.overall_ok else _EXIT_PARTIAL_FAILURE


if __name__ == "__main__":
    sys.exit(main())
