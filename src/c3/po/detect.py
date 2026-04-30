"""Runtime detection of the parallel-orchestra (PO) installation.

The authoritative signal is ``shutil.which`` because PO is invoked as a
subprocess. ``importlib.metadata`` provides the version for diagnostics but
its absence does not flip availability to False (e.g. pipx-installed PO can
expose the binary while hiding the metadata from the caller's interpreter).
"""

from __future__ import annotations

import shutil
import sys
from importlib.metadata import PackageNotFoundError, version


def detect_po() -> tuple[bool, str | None, str | None]:
    """Return ``(is_available, version, cli_path)``.

    ``is_available`` is ``True`` iff ``parallel-orchestra`` is on PATH.
    ``version`` is the package version reported by ``importlib.metadata``,
    or ``None`` if the metadata is not queryable from this interpreter.
    ``cli_path`` is the absolute path returned by ``shutil.which`` (or ``None``).

    Never raises.
    """
    cli_path = shutil.which("parallel-orchestra")
    try:
        ver = version("parallel-orchestra")
    except PackageNotFoundError:
        ver = None
    return cli_path is not None, ver, cli_path


def main() -> int:
    """CLI helper: print key=value lines and exit 0/1 based on availability."""
    available, ver, cli_path = detect_po()
    print(f"available={'true' if available else 'false'}")
    print(f"version={ver if ver is not None else 'None'}")
    print(f"cli_path={cli_path if cli_path is not None else 'None'}")
    return 0 if available else 1


if __name__ == "__main__":
    sys.exit(main())
