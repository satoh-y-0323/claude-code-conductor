"""
tests/skills/test_planner_lightweight.py

v2.13.0 で planner.md を 172 行 → ~66 行に軽量化した検証。
並列実行設計指針・自動検査ルールは skills/dev-workflow/references/plan-design-guidelines.md に外出しされている。
"""
from pathlib import Path

from tests.skills._skill_helpers import WORKTREE_ROOT


PLANNER_AGENT = WORKTREE_ROOT / ".claude" / "agents" / "planner.md"
PLAN_DESIGN_GUIDELINES = WORKTREE_ROOT / ".claude" / "skills" / "dev-workflow" / "references" / "plan-design-guidelines.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_planner_agent_under_80_lines():
    """planner.md は D-012 準拠で 80 行以内に収まる。"""
    content = _read(PLANNER_AGENT)
    line_count = len(content.splitlines())
    assert line_count <= 80, (
        f"planner.md は {line_count} 行（80 行上限超過）。"
        " 処理手順は skills/dev-workflow/references/plan-design-guidelines.md に外出ししてください（D-012）。"
    )


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


def test_plan_design_guidelines_exists_and_has_rules():
    """skills/dev-workflow/references/plan-design-guidelines.md が存在し、ルール 1〜13 と R2〜R6 を含む。"""
    content = _read(PLAN_DESIGN_GUIDELINES)
    assert content, "skills/dev-workflow/references/plan-design-guidelines.md が存在しない"
    required_concepts = (
        "depends_on の付け方",
        "TDD タスクは 3-wave に分解",
        "writes フィールド",
        "自己チェックリスト",
        "R2",
        "R3",
        "R4",
        "R5",
        "R6",
    )
    missing = [kw for kw in required_concepts if kw not in content]
    assert not missing, f"plan-design-guidelines.md に以下のセクション/キーワードが不足: {missing}"


def test_planner_no_longer_contains_extracted_sections():
    """planner.md に外出し済みのセクションが残っていないこと。"""
    content = _read(PLANNER_AGENT)
    forbidden_headings = (
        "## 並列実行のための設計指針",
        "### depends_on の付け方",
        "### TDD タスクは 3-wave に分解",
        "### writes フィールドの埋め方",
        "### 出力直前の自己チェックリスト",
        "### タスクあたりの所要時間制約",
        "### YAML フロントマターの落とし穴",
        "### 直列・並列交互パターンの取り扱い",
    )
    found = [h for h in forbidden_headings if h in content]
    assert not found, (
        f"planner.md に外出し済みのセクションが残っている: {found}. "
        "skills/dev-workflow/references/plan-design-guidelines.md に移動してください。"
    )


def test_planner_preserves_persona_sections():
    """planner.md にペルソナ定義の必須セクションが残っている。"""
    content = _read(PLANNER_AGENT)
    required_headings = (
        "## Core Mandate",
        "## Key Scope",
        "## Workflow",
        "## Tools & Constraints",
        "## Related Agents",
    )
    missing = [h for h in required_headings if h not in content]
    assert not missing, f"planner.md にペルソナ必須セクションが不足: {missing}"
