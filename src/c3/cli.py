"""C3 CLI entry point.

Each subcommand registers its parser through ``register(subparsers)`` and
exposes a ``handle(args) -> int`` function. Keeping each subcommand in its own
module (``cli_*.py``) keeps the dispatch table small and isolates the
implementation details.
"""

from __future__ import annotations

import argparse
import sys

from c3 import __version__
from c3 import cli_doctor, cli_init, cli_list, cli_po, cli_tier, cli_update


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="c3",
        description="Claude Code Conductor - multi-agent orchestration framework",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"c3 {__version__}",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    cli_init.register(sub)
    cli_update.register(sub)
    cli_list.register(sub)
    cli_doctor.register(sub)
    cli_po.register(sub)
    cli_tier.register(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


def _force_utf8_streams() -> None:
    """Reconfigure stdout/stderr to UTF-8 so Japanese output renders correctly on Windows.

    No-op on platforms where stdout is already UTF-8 or where ``reconfigure`` is unavailable.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


if __name__ == "__main__":
    sys.exit(main())
