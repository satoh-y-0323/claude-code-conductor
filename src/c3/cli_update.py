"""``c3 update`` - bring the project's ``.claude/`` up to date with the package template."""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

from c3._excludes import should_skip
from c3.adapters import print_adapter_actions, scaffold_adapters
from c3.paths import templates_dir
from c3.platforms import PLATFORM_CHOICES, expand_platforms


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "update",
        help="Refresh .claude/ from the bundled template (skips local files)",
        description=(
            "Compare the project's .claude/ to the bundled template and "
            "overwrite framework files that differ. User-managed files "
            "(reports/, memory/sessions/, docs/decisions.md, etc.) are "
            "always skipped."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing anything",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=None,
        help="Destination directory (defaults to the current working directory)",
    )
    parser.add_argument(
        "--platform",
        choices=PLATFORM_CHOICES,
        default="claude",
        help="Target host adapter to update. Defaults to claude.",
    )
    parser.set_defaults(handler=handle)


def handle(args: argparse.Namespace) -> int:
    target_root: Path = (args.target or Path.cwd()).resolve()
    platforms = expand_platforms(args.platform)
    dest = target_root / ".claude"
    if not dest.is_dir():
        print(
            f"no .claude/ directory found in {target_root}. "
            "Run `c3 init` first.",
            file=sys.stderr,
        )
        return 1

    changed = 0
    if "claude" in platforms:
        template = templates_dir()
        actions = list(_walk_diff(template, dest))
        changed += len(actions)

        if args.dry_run:
            if actions:
                print(f"{len(actions)} file(s) would change:")
                for action, path in actions:
                    print(f"  {action}: {path.relative_to(dest)}")
            else:
                print("claude template up to date")
        else:
            for action, abs_path in actions:
                rel = abs_path.relative_to(dest)
                src = template / rel
                if action == "add" or action == "update":
                    abs_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, abs_path)
                    print(f"  {action}: {rel}")

    adapter_platforms = tuple(p for p in platforms if p != "claude")
    if adapter_platforms:
        try:
            adapter_actions = scaffold_adapters(
                target_root,
                adapter_platforms,
                dry_run=args.dry_run,
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"adapter update failed: {exc}", file=sys.stderr)
            return 1
        changed += len(adapter_actions)
        print_adapter_actions(adapter_actions, dry_run=args.dry_run)

    if changed == 0:
        print("up to date")
    elif not args.dry_run:
        print(f"{changed} file(s) updated")
    return 0


def _walk_diff(template: Path, dest: Path):
    """Yield (action, absolute_dest_path) tuples for files that differ.

    Only ``add`` and ``update`` are emitted; we never delete files in dest.
    Personal/working files (per ``c3._excludes``) are skipped both as bundle
    sources and as overwrite targets.
    """
    for src_file in _iter_files(template):
        rel = src_file.relative_to(template)
        if should_skip(rel.as_posix()):
            continue
        target = dest / rel
        if not target.exists():
            yield "add", target
        elif not filecmp.cmp(src_file, target, shallow=False):
            yield "update", target


def _iter_files(root: Path):
    for entry in root.iterdir():
        if entry.is_dir():
            yield from _iter_files(entry)
        elif entry.is_file():
            yield entry
