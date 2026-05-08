"""Files inside ``.claude/`` that are personal/working state.

Used by:

- ``c3 init``    — never copied to the destination project
- ``c3 update``  — never overwritten in the destination project
- ``hatch_build.py`` — never bundled into the wheel template (the patterns are
  duplicated there because the build hook runs *before* the package is
  importable; keep both lists in sync)

Patterns are POSIX-style and relative to the ``.claude/`` directory itself
(e.g. ``"reports/*"``, not ``".claude/reports/*"``). ``KEEP_PATTERNS`` win
over ``EXCLUDE_PATTERNS`` so that placeholder ``.gitkeep`` files survive.
"""

from __future__ import annotations

import fnmatch

EXCLUDE_PATTERNS: tuple[str, ...] = (
    "reports/*",
    "memory/sessions/*",
    "memory/patterns.json",
    "memory/agent-audit.log",
    "agent-memory/*",
    "tmp/*",
    "docs/decisions.md",
    "docs/taxonomy.md",
    "docs/game-studios-research.md",
    "settings.local.json",
    "pytest_temp.ini",
    "logs/*",
)

KEEP_PATTERNS: tuple[str, ...] = (
    "reports/.gitkeep",
    "memory/.gitkeep",
    "memory/sessions/.gitkeep",
    "tmp/.gitkeep",
)


def should_skip(rel_posix: str) -> bool:
    """Return True if the path (relative to ``.claude/``) is personal state."""
    parts = rel_posix.split("/")
    if "__pycache__" in parts or rel_posix.endswith((".pyc", ".pyo")):
        return True
    if any(fnmatch.fnmatchcase(rel_posix, p) for p in KEEP_PATTERNS):
        return False
    return any(fnmatch.fnmatchcase(rel_posix, p) for p in EXCLUDE_PATTERNS)
