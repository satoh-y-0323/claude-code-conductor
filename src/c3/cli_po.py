"""``c3 po`` - thin wrapper around the bundled parallel-orchestra runner."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from c3.paths import claude_root_for
from c3.po.manifest import (
    build_wave_manifest_text,
    compute_waves,
    extract_frontmatter,
    validate_manifest,
)
from c3.po.run import run_manifest


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
        help="Execute the full manifest in one PO invocation",
    )
    run.add_argument("manifest", type=Path)
    run.add_argument("--max-workers", type=int, default=None)
    run.add_argument("--report", type=Path, default=None)
    run.add_argument("--quiet", action="store_true")
    run.add_argument("--claude-exe", default=None)
    run.set_defaults(handler=_handle_run)

    waves = inner.add_parser(
        "waves",
        help="Print the topological-wave decomposition of the manifest as JSON",
    )
    waves.add_argument("manifest", type=Path)
    waves.set_defaults(handler=_handle_waves)

    run_wave = inner.add_parser(
        "run-wave",
        help="Execute only the wave at the given index via PO",
    )
    run_wave.add_argument("manifest", type=Path)
    run_wave.add_argument("--wave-index", type=int, required=True)
    run_wave.add_argument("--max-workers", type=int, default=None)
    run_wave.add_argument("--report", type=Path, default=None)
    run_wave.add_argument("--quiet", action="store_true")
    run_wave.add_argument("--claude-exe", default=None)
    run_wave.set_defaults(handler=_handle_run_wave)


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
    result = run_manifest(args.manifest, dry_run=True)
    return result.exit_code if result.exit_code >= 0 else 1


def _handle_run(args: argparse.Namespace) -> int:
    rc = _preflight(args.manifest)
    if rc != 0:
        return rc
    result = run_manifest(
        args.manifest,
        max_workers=args.max_workers,
        report=args.report,
        quiet=args.quiet,
        claude_exe=args.claude_exe,
    )
    return result.exit_code if result.exit_code >= 0 else 1


def _handle_waves(args: argparse.Namespace) -> int:
    rc = _preflight(args.manifest)
    if rc != 0:
        return rc
    fm = extract_frontmatter(args.manifest)
    if fm is None:
        # Should not happen after _preflight passed, but be defensive.
        print("could not parse frontmatter", file=sys.stderr)
        return 2
    try:
        waves = compute_waves(fm)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    output = {
        "waves": [
            {
                "index": index,
                "tasks": [
                    {
                        "id": task["id"],
                        "agent": task.get("agent"),
                        "read_only": task.get("read_only"),
                        "writes": task.get("writes") or [],
                        "prompt": task.get("prompt", ""),
                    }
                    for task in wave_tasks
                ],
            }
            for index, wave_tasks in enumerate(waves)
        ]
    }
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def _handle_run_wave(args: argparse.Namespace) -> int:
    rc = _preflight(args.manifest)
    if rc != 0:
        return rc

    fm = extract_frontmatter(args.manifest)
    if fm is None:
        print("could not parse frontmatter", file=sys.stderr)
        return 2
    try:
        wave_text = build_wave_manifest_text(fm, args.wave_index)
    except (IndexError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    root = claude_root_for(args.manifest.parent) or claude_root_for(Path.cwd())
    if root is None:
        print(
            "could not locate .claude/ directory to materialise the wave manifest",
            file=sys.stderr,
        )
        return 2
    tmp_dir = root / ".claude" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        delete=False,
        suffix=".md",
        dir=tmp_dir,
    ) as tmp_file:
        wave_path = Path(tmp_file.name)
    wave_path.write_text(wave_text, encoding="utf-8")

    try:
        result = run_manifest(
            wave_path,
            max_workers=args.max_workers,
            report=args.report,
            quiet=args.quiet,
            claude_exe=args.claude_exe,
        )
    finally:
        wave_path.unlink(missing_ok=True)
    return result.exit_code if result.exit_code >= 0 else 1
