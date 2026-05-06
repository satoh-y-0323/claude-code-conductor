"""parallel-orchestra (PO) integration helpers.

PO is bundled inside this package (see ``src/parallel_orchestra/``); callers
should treat it as an internal module rather than an external dependency.
``run_manifest`` returns a C3-friendly :class:`RunResult` so the CLI layer
can map outcomes to exit codes without depending on PO's Python types.
"""

from c3.po.run import RunResult, run_manifest

__all__ = ["run_manifest", "RunResult"]
