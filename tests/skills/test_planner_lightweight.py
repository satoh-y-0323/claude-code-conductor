"""
tests/skills/test_planner_lightweight.py

planner.md が plan-design-guidelines.md を必読として参照する導線の検査。
行数・見出し・文言の固定は行わない。
"""
from pathlib import Path

from tests.skills._skill_helpers import WORKTREE_ROOT


PLANNER_AGENT = WORKTREE_ROOT / ".claude" / "agents" / "planner.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_planner_references_plan_design_guidelines():
    """planner.md の Workflow Before で plan-design-guidelines.md を必読として参照する。

    rules/*.md の paths フロントマター自動注入に依存しない二重防御として明示 Read を入れる。
    """
    content = _read(PLANNER_AGENT)
    assert "plan-design-guidelines.md" in content, \
        "planner.md が plan-design-guidelines.md への参照を持っていない"
    # Workflow Before セクション内で参照されていることを確認
    workflow_idx = content.find("## Workflow")
    after_idx = content.find("**After:**", workflow_idx)
    assert workflow_idx >= 0, "## Workflow セクションが見つからない"
    workflow_section = content[workflow_idx: after_idx] if after_idx > workflow_idx else content[workflow_idx:]
    assert "plan-design-guidelines.md" in workflow_section, \
        "planner.md の Workflow セクション内に plan-design-guidelines.md への参照がない"
