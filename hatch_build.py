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


# Files / directories that must NOT be redistributed (matched against paths
# relative to the project root). Globs use fnmatch semantics.
EXCLUDE_PATTERNS: tuple[str, ...] = (
    ".claude/reports/*",
    ".claude/memory/sessions/*",
    ".claude/memory/patterns.json",
    ".claude/memory/agent-audit.log",
    ".claude/tmp/*",
    ".claude/docs/decisions.md",
    ".claude/docs/taxonomy.md",
    ".claude/docs/game-studios-research.md",
)

# Patterns that should always survive even if their parent matches an exclude.
KEEP_PATTERNS: tuple[str, ...] = (
    ".claude/reports/.gitkeep",
    ".claude/memory/.gitkeep",
    ".claude/memory/sessions/.gitkeep",
    ".claude/tmp/.gitkeep",
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
        _copy_filtered(source, dest, root)


def _copy_filtered(src: Path, dst: Path, project_root: Path) -> None:
    for entry in src.iterdir():
        rel = entry.relative_to(project_root).as_posix()
        if entry.is_dir():
            sub_dst = dst / entry.name
            sub_dst.mkdir(exist_ok=True)
            _copy_filtered(entry, sub_dst, project_root)
            # Drop any empty dirs that ended up with nothing to keep.
            if not any(sub_dst.iterdir()):
                sub_dst.rmdir()
        elif entry.is_file():
            if _should_skip(rel):
                continue
            shutil.copy2(entry, dst / entry.name)


def _should_skip(rel: str) -> bool:
    if any(fnmatch.fnmatch(rel, p) for p in KEEP_PATTERNS):
        return False
    return any(fnmatch.fnmatch(rel, p) for p in EXCLUDE_PATTERNS)
