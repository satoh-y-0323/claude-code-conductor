"""End-to-end ``parallel-orchestra run --dry-run`` test.

Skipped if ``parallel-orchestra`` is not on PATH so the unit suite still
passes for users who only installed C3.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


_HAS_PO = shutil.which("parallel-orchestra") is not None


@pytest.mark.skipif(not _HAS_PO, reason="parallel-orchestra not installed")
def test_dry_run_validates_canonical_manifest(tmp_path: Path):
    project_root = tmp_path / "project"
    agents_dir = project_root / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "tdd-develop.md").write_text("# tdd-develop\n", encoding="utf-8")

    manifest = project_root / ".claude" / "reports" / "plan-report.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        textwrap.dedent(
            """\
            ---
            po_plan_version: "0.1"
            name: smoke
            cwd: ../..

            tasks:
              - id: smoke-task
                agent: tdd-develop
                read_only: false
                prompt: |
                  smoke prompt
            ---

            # smoke
            """
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["parallel-orchestra", "run", str(manifest), "--dry-run"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, (
        f"parallel-orchestra rejected the manifest:\n"
        f"stdout=\n{completed.stdout}\n\nstderr=\n{completed.stderr}"
    )
