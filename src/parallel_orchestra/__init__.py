"""parallel-orchestra: Run Claude Code agents in parallel."""

from importlib.metadata import PackageNotFoundError, version

from ._exceptions import ParallelOrchestraError
from .manifest import (
    SUPPORTED_PLAN_VERSIONS,
    Defaults,
    Manifest,
    ManifestError,
    Task,
    WebhookConfig,
    load_manifest,
)
from .report import generate_report
from .runner import RunnerError, RunResult, TaskResult, run_manifest

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
    "SUPPORTED_PLAN_VERSIONS",
    "Defaults",
    "Manifest",
    "ManifestError",
    "Task",
    "WebhookConfig",
    "load_manifest",
    "generate_report",
    "RunnerError",
    "RunResult",
    "TaskResult",
    "run_manifest",
]
