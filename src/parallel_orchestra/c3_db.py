"""Backwards-compatible shim re-exporting helpers from ``c3.db``.

This module was migrated to :mod:`c3.db` in v1.11.0 as part of the PO
retirement plan (plan: atomic-foraging-sprout). Existing imports such as
``from parallel_orchestra.c3_db import locate_c3_db`` keep working via the
re-exports below until the ``parallel_orchestra`` package is fully removed
in v2.0.0. New code should import from :mod:`c3.db` directly.
"""

from __future__ import annotations

from c3.db import (  # noqa: F401 -- explicit re-exports for legacy imports
    READ_ONLY_WORKTREE_ID,
    aggregate_decisions,
    fetch_po_results,
    fetch_po_status,
    fetch_review_decisions,
    insert_review_decision,
    locate_c3_db,
    read_tier_failure_rate,
    read_tier_params,
    record_task_results,
    record_tier_recent_outcome,
    update_tier_params,
    upsert_po_status,
)
