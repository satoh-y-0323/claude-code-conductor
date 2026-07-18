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

See ``.claude/docs/config-policy.md`` for the distribution decision matrix
and the rationale behind each excluded pattern.
"""

from __future__ import annotations

import fnmatch

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
    "docs/環境条件別c3の問題点.md",
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


def should_skip(rel_posix: str) -> bool:
    """Return True if the path (relative to ``.claude/``) is personal state."""
    parts = rel_posix.split("/")
    if "__pycache__" in parts or rel_posix.endswith((".pyc", ".pyo")):
        return True
    if any(fnmatch.fnmatchcase(rel_posix, p) for p in KEEP_PATTERNS):
        return False
    return any(fnmatch.fnmatchcase(rel_posix, p) for p in EXCLUDE_PATTERNS)
