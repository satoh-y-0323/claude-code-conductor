"""``c3 list-agents`` / ``list-skills`` / ``list-commands`` - inspect installed assets."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from c3.paths import claude_root_for

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_DESCRIPTION_RE = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def register(subparsers: argparse._SubParsersAction) -> None:
    for kind in ("agents", "skills", "commands"):
        parser = subparsers.add_parser(
            f"list-{kind}",
            help=f"List installed {kind} in the project's .claude/{kind}/",
        )
        parser.add_argument(
            "--target",
            type=Path,
            default=None,
            help="Project root (defaults to walking up from cwd to find .claude/)",
        )
        parser.set_defaults(handler=handle, kind=kind)


def handle(args: argparse.Namespace) -> int:
    start = (args.target or Path.cwd()).resolve()
    root = claude_root_for(start)
    if root is None:
        print(
            f"no .claude/ directory found at or above {start}",
            file=sys.stderr,
        )
        return 1

    target_dir = root / ".claude" / args.kind
    if not target_dir.is_dir():
        print(f"no .claude/{args.kind}/ directory at {root}", file=sys.stderr)
        return 1

    files = sorted(p for p in target_dir.glob("*.md") if p.is_file())
    if not files:
        print(f"(no {args.kind} found)")
        return 0

    width = max(len(p.stem) for p in files)
    for path in files:
        summary = _summary(path)
        print(f"  {path.stem.ljust(width)}  {summary}")
    return 0


def _summary(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    fm_match = _FRONTMATTER_RE.match(text)
    if fm_match:
        desc_match = _DESCRIPTION_RE.search(fm_match.group(1))
        if desc_match:
            return desc_match.group(1).strip("\"'")
    h1_match = _H1_RE.search(text)
    if h1_match:
        return h1_match.group(1)
    return ""
