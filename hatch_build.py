"""Hatchling build hook: stage ``.claude/`` into ``src/c3/_template/.claude/``.

The dev-time ``.claude/`` directory contains personal/working files (reports,
session memory, founding docs) that must not be redistributed via PyPI. Rather
than rely on ``[tool.hatch.build] exclude`` patterns - which do not propagate
into ``force-include`` sources - we copy the wanted subset into a staging
location during ``initialize()`` and the wheel target packages that staged tree
verbatim.

See ``.claude/docs/config-policy.md`` for the distribution decision matrix
and the rationale behind each excluded pattern.
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
    "memory/archive/*",
    "memory/patterns.json",
    "memory/agent-audit.log",
    "memory/consolidated_summary.md",
    "memory/promotion-candidates.md",
    "agent-memory/*",
    "tmp/*",
    "docs/decisions.md",
    "docs/taxonomy.md",
    "docs/game-studios-research.md",
    "docs/c3候補機能への質問に対する回答.md",
    "docs/c3候補機能採用.md",
    "docs/c3追加予定機能リスト.md",
    "docs/ruflo_research_result.md",
    "docs/C3_hnsw_機能追加詳細設計.md",
    "docs/C3_利用状況可視化.md",
    "docs/C3_tier_routing_cost_integration_設計.md",
    "docs/grill-me機能を実装する際の考慮点とC3との相性や超えるべき壁.md",
    "docs/C3のconfig_policyとversion_upgradeの考慮点と超えるべき壁.md",
    "docs/model_settings.md",
    "docs/codex対応/*",
    "hooks/subagent_log.py",
    "settings.local.json",
    "pytest_temp.ini",
    "logs/*",
    # state/* で v2.10.0 の recall.hnsw / recall_meta.json も自動除外
    "state/*",
    # v2.14.1: parallel-agents skill が isolation:"worktree" で生成する一時 worktree。
    # マージ後に削除されるが残骸が残ることがあり、wheel に混入すると利用先に
    # 不要な agent worktree レポート（code-review-report-*.md 等）が配布される問題があった
    "worktrees/*",
    # v2.1.0: tdd-develop / worktree-tdd-workflow 廃止（planner が TDD を 3-wave に分解する設計に統一）
    "agents/tdd-develop.md",
    "skills/worktree-tdd-workflow/*",
    # autonomous-mode skill を配布除外での熟成・配布切替は本行削除で往復可
    "skills/autonomous-mode/*",
)

KEEP_PATTERNS: tuple[str, ...] = (
    "reports/.gitkeep",
    "memory/.gitkeep",
    "memory/sessions/.gitkeep",
    "memory/archive/.gitkeep",
    "tmp/.gitkeep",
    "state/.gitkeep",
    "deletions.txt",  # 新規: c3 update が読む削除指示書
    "breaking-changes.txt",  # v2.19.0: c3 update が読む破壊的変更ログ
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
