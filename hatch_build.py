"""Hatchling build hook: stage ``.claude/`` into ``src/c3/_template/.claude/``.

The dev-time ``.claude/`` directory contains personal/working files (reports,
session memory, founding docs) that must not be redistributed via PyPI. Rather
than rely on ``[tool.hatch.build] exclude`` patterns - which do not propagate
into ``force-include`` sources - we copy the wanted subset into a staging
location during ``initialize()`` and the wheel target packages that staged tree
verbatim.
"""

from __future__ import annotations

import fnmatch
import shutil
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


# Patterns matched against paths relative to ``.claude/``.
# IMPORTANT: keep these in sync with ``src/c3/_excludes.py``. The build hook
# runs before the package is importable, so we duplicate rather than import.
EXCLUDE_PATTERNS: tuple[str, ...] = (
    "reports/*",
    "memory/sessions/*",
    "memory/patterns.json",
    "memory/agent-audit.log",
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


class StageTemplateHook(BuildHookInterface):
    """Run before any wheel/sdist build to (re)stage ``.claude/``."""

    PLUGIN_NAME = "stage_template"

    def initialize(self, version, build_data):
        root = Path(self.root)
        source = root / ".claude"
        if not source.is_dir():
            return
        dest = root / "src" / "c3" / "_template" / ".claude"
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        _copy_filtered(source, dest, source)


def _copy_filtered(src: Path, dst: Path, claude_root: Path) -> None:
    for entry in src.iterdir():
        rel = entry.relative_to(claude_root).as_posix()
        if entry.is_dir():
            sub_dst = dst / entry.name
            sub_dst.mkdir(exist_ok=True)
            _copy_filtered(entry, sub_dst, claude_root)
            if not any(sub_dst.iterdir()):
                sub_dst.rmdir()
        elif entry.is_file():
            if _should_skip(rel):
                continue
            shutil.copy2(entry, dst / entry.name)


def _should_skip(rel: str) -> bool:
    parts = rel.split("/")
    if "__pycache__" in parts or rel.endswith((".pyc", ".pyo")):
        return True
    if any(fnmatch.fnmatchcase(rel, p) for p in KEEP_PATTERNS):
        return False
    return any(fnmatch.fnmatchcase(rel, p) for p in EXCLUDE_PATTERNS)
