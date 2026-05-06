"""parallel-orchestra: Run Claude Code agents in parallel.

Bundled inside ``claude-code-conductor``. The top-level package re-exports
only the symbols that callers actually need to construct CLI wrappers
(:class:`RunResult`, :class:`ManifestError`, :class:`RunnerError`,
:func:`run_manifest`, :func:`load_manifest`). Internal types such as
``Manifest`` / ``Task`` / ``Defaults`` remain accessible via
``parallel_orchestra.manifest`` for advanced callers.
"""

from importlib.metadata import PackageNotFoundError, version

from ._exceptions import ParallelOrchestraError
from .manifest import ManifestError, load_manifest
from .runner import RunnerError, RunResult, run_manifest

try:
    __version__: str = version("parallel-orchestra")
except PackageNotFoundError:
    # Bundled inside claude-code-conductor: fall back to the host package version.
    try:
        __version__ = version("claude-code-conductor")
    except PackageNotFoundError:  # pragma: no cover
        __version__ = "unknown"

__all__ = [
    "ParallelOrchestraError",
    "ManifestError",
    "load_manifest",
    "RunnerError",
    "RunResult",
    "run_manifest",
]
