"""``c3 po`` - thin wrapper around the parallel-orchestra CLI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from c3.paths import claude_root_for
from c3.po.detect import detect_po
from c3.po.manifest import validate_manifest
from c3.po.run import run_manifest


_NOT_INSTALLED_MSG = (
    "parallel-orchestra is not installed. "
    "並列実行を使うには `pip install parallel-orchestra` を実行してください。"
    "詳細: https://pypi.org/project/parallel-orchestra/"
)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "po",
        help="Run a plan-report as a parallel-orchestra manifest",
    )
    inner = parser.add_subparsers(dest="po_command", metavar="<subcommand>")
    inner.required = True

    dry = inner.add_parser(
        "dry-run",
        help="Validate the manifest without executing tasks",
    )
    dry.add_argument("manifest", type=Path)
    dry.set_defaults(handler=_handle_dry_run)

    run = inner.add_parser(
        "run",
        help="Execute the manifest",
    )
    run.add_argument("manifest", type=Path)
    run.add_argument("--max-workers", type=int, default=None)
    run.add_argument("--report", type=Path, default=None)
    run.add_argument("--quiet", action="store_true")
    run.add_argument("--claude-exe", default=None)
    run.set_defaults(handler=_handle_run)


def _ensure_po_available() -> int:
    available, _, _ = detect_po()
    if not available:
        print(_NOT_INSTALLED_MSG, file=sys.stderr)
        return 1
    return 0


def _preflight(manifest: Path) -> int:
    if not manifest.is_file():
        print(f"manifest not found: {manifest}", file=sys.stderr)
        return 2
    root = claude_root_for(manifest.parent) or claude_root_for(Path.cwd())
    if root is None:
        print("could not locate .claude/ directory for agent lookup", file=sys.stderr)
        return 2
    errors = validate_manifest(manifest, root)
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 2
    return 0


def _handle_dry_run(args: argparse.Namespace) -> int:
    rc = _preflight(args.manifest)
    if rc != 0:
        return rc
    if (rc := _ensure_po_available()) != 0:
        return rc
    result = run_manifest(args.manifest, dry_run=True)
    if result.status == "not_installed":
        print(_NOT_INSTALLED_MSG, file=sys.stderr)
        return 1
    return result.exit_code if result.exit_code >= 0 else 1


def _handle_run(args: argparse.Namespace) -> int:
    rc = _preflight(args.manifest)
    if rc != 0:
        return rc
    if (rc := _ensure_po_available()) != 0:
        return rc
    result = run_manifest(
        args.manifest,
        max_workers=args.max_workers,
        report=args.report,
        quiet=args.quiet,
        claude_exe=args.claude_exe,
    )
    if result.status == "not_installed":
        print(_NOT_INSTALLED_MSG, file=sys.stderr)
        return 1
    return result.exit_code if result.exit_code >= 0 else 1
