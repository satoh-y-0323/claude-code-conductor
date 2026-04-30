"""``c3 init`` - scaffold ``.claude/`` into the current project."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from c3._excludes import should_skip
from c3.paths import templates_dir


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "init",
        help="Scaffold a fresh .claude/ directory into the current project",
        description=(
            "Copy the bundled C3 .claude/ template into the current working "
            "directory. Refuses to overwrite an existing .claude/ unless "
            "--force is given."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing .claude/ directory without confirmation",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=None,
        help="Destination directory (defaults to the current working directory)",
    )
    parser.set_defaults(handler=handle)


def handle(args: argparse.Namespace) -> int:
    target_root: Path = (args.target or Path.cwd()).resolve()
    dest = target_root / ".claude"

    if dest.exists() and not args.force:
        print(
            f"refusing to overwrite existing directory: {dest}\n"
            "Pass --force to overwrite or run `c3 update` for a diff-aware merge.",
            file=sys.stderr,
        )
        return 1

    template = templates_dir()
    if dest.exists() and args.force:
        shutil.rmtree(dest)

    target_root.mkdir(parents=True, exist_ok=True)
    copied = _copytree(template, dest)
    print(f"initialized {dest} ({copied} files copied)")
    return 0


def _copytree(src: Path, dst: Path, *, root: Path | None = None) -> int:
    """Copy ``src`` -> ``dst`` recursively, skipping personal/working files.

    ``root`` defaults to ``src`` and represents the ``.claude/`` directory; the
    relative path from ``root`` is what ``should_skip`` matches against.
    Returns the number of regular files written.
    """
    if root is None:
        root = src
    dst.mkdir(parents=True, exist_ok=True)
    count = 0
    for entry in src.iterdir():
        rel = entry.relative_to(root).as_posix()
        target = dst / entry.name
        if entry.is_dir():
            count += _copytree(entry, target, root=root)
            # Drop directories that ended up empty (everything inside was skipped).
            if not any(target.iterdir()):
                target.rmdir()
        elif entry.is_file():
            if should_skip(rel):
                continue
            shutil.copy2(entry, target)
            count += 1
    return count
