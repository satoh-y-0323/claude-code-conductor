"""Static checks: enforce loose-coupling guarantees that the codebase claims.

These guard against regressions in the C3 ↔ parallel-orchestra boundary.
"""

from __future__ import annotations

import re
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src" / "c3"


def test_no_parallel_orchestra_imports_in_package():
    """C3 must not import parallel_orchestra at the Python level."""
    pattern = re.compile(r"\b(?:import|from)\s+parallel_orchestra\b")
    offenders: list[str] = []
    for py_file in _SRC.rglob("*.py"):
        if pattern.search(py_file.read_text(encoding="utf-8")):
            offenders.append(str(py_file.relative_to(_PROJECT_ROOT)))
    assert not offenders, (
        "loose-coupling broken: parallel_orchestra is imported in: " + ", ".join(offenders)
    )


def test_pyproject_does_not_pin_parallel_orchestra():
    pyproject = (_PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    # Block both `dependencies = [...]` lists and `optional-dependencies = ...`
    forbidden_patterns = (
        r'^\s*"parallel-orchestra',
        r"^\s*'parallel-orchestra",
    )
    for pat in forbidden_patterns:
        assert not re.search(pat, pyproject, flags=re.MULTILINE), (
            "parallel-orchestra must not be listed as a dependency"
        )


def test_subprocess_calls_use_argv_lists():
    """Reject ``shell=True`` anywhere in the package."""
    offenders: list[str] = []
    for py_file in _SRC.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        if re.search(r"shell\s*=\s*True", text):
            offenders.append(str(py_file.relative_to(_PROJECT_ROOT)))
    assert not offenders, "shell=True is forbidden, found in: " + ", ".join(offenders)
