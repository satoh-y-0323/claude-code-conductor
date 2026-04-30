"""parallel-orchestra (PO) integration helpers.

This package only invokes PO via its CLI (subprocess). It deliberately does
not import ``parallel_orchestra`` so that C3 stays loosely coupled and is
unaffected by PO's internal API changes.
"""

from c3.po.detect import detect_po
from c3.po.run import RunResult, run_manifest

__all__ = ["detect_po", "run_manifest", "RunResult"]
