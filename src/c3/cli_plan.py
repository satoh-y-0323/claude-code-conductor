"""``c3 plan`` - validate plan-report YAML and split into topological waves.

Introduced in v1.14.0 as the PO-independent replacement for ``c3 po dry-run``
and ``c3 po waves``. Wraps the pure-Python helpers in :mod:`c3.plan_validator`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from c3.paths import claude_root_for
from c3.plan_validator import split_waves, validate_plan_report

# 旧 c3 po dry-run の exit 2 (= manifest error) と互換。
# validate / waves サブコマンド共通の終了コード。
_EXIT_MANIFEST_ERROR = 2


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "plan",
        help="Validate plan-report YAML and split into topological waves",
    )
    inner = parser.add_subparsers(dest="plan_command", metavar="<subcommand>")
    inner.required = True

    validate = inner.add_parser(
        "validate",
        help="Check YAML frontmatter for required fields and agent file existence",
    )
    validate.add_argument("plan_report", type=Path)
    validate.set_defaults(handler=_handle_validate)

    waves = inner.add_parser(
        "waves",
        help="Print the topological-wave decomposition of the plan-report as JSON",
    )
    waves.add_argument("plan_report", type=Path)
    waves.set_defaults(handler=_handle_waves)


def _resolve_root(path: Path) -> Path | None:
    return claude_root_for(path.parent) or claude_root_for(Path.cwd())


def _handle_validate(args: argparse.Namespace) -> int:
    if not args.plan_report.is_file():
        print(f"plan-report not found: {args.plan_report}", file=sys.stderr)
        return _EXIT_MANIFEST_ERROR
    root = _resolve_root(args.plan_report)
    if root is None:
        print("could not locate .claude/ directory for agent lookup", file=sys.stderr)
        return _EXIT_MANIFEST_ERROR
    errors = validate_plan_report(args.plan_report, root)
    if not errors:
        return 0
    for err in errors:
        print(err, file=sys.stderr)
    return _EXIT_MANIFEST_ERROR


def _handle_waves(args: argparse.Namespace) -> int:
    if not args.plan_report.is_file():
        print(f"plan-report not found: {args.plan_report}", file=sys.stderr)
        return _EXIT_MANIFEST_ERROR
    root = _resolve_root(args.plan_report)
    if root is None:
        print("could not locate .claude/ directory for agent lookup", file=sys.stderr)
        return _EXIT_MANIFEST_ERROR
    # 旧 c3 po waves と同じく、まず validate を走らせてから分解する
    errors = validate_plan_report(args.plan_report, root)
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return _EXIT_MANIFEST_ERROR
    try:
        output = split_waves(args.plan_report)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return _EXIT_MANIFEST_ERROR
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0
