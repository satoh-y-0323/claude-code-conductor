"""Tests for references migration: rules/ -> skills/dev-workflow/references/.

Verifies that:
- The 3 reference files exist at their new locations under skills/dev-workflow/references/
- The 3 old locations under rules/ no longer exist
- Agent definitions reference the new paths and not the old paths
"""

from __future__ import annotations

from pathlib import Path

WORKTREE_ROOT = Path(__file__).resolve().parents[1]

_CLAUDE = WORKTREE_ROOT / ".claude"


# ---------------------------------------------------------------------------
# 1-3: New reference files exist under skills/dev-workflow/references/
# ---------------------------------------------------------------------------


def test_plan_design_guidelines_exists_at_new_location():
    path = _CLAUDE / "skills" / "dev-workflow" / "references" / "plan-design-guidelines.md"
    assert path.exists(), f"Expected file not found: {path}"


def test_code_review_checklist_exists_at_new_location():
    path = _CLAUDE / "skills" / "dev-workflow" / "references" / "code-review-checklist.md"
    assert path.exists(), f"Expected file not found: {path}"


def test_security_review_checklist_exists_at_new_location():
    path = _CLAUDE / "skills" / "dev-workflow" / "references" / "security-review-checklist.md"
    assert path.exists(), f"Expected file not found: {path}"


# ---------------------------------------------------------------------------
# 4-6: Old locations under rules/ no longer exist
# ---------------------------------------------------------------------------


def test_plan_design_guidelines_absent_from_rules():
    path = _CLAUDE / "rules" / "plan-design-guidelines.md"
    assert not path.exists(), f"File should have been removed from rules/: {path}"


def test_code_review_checklist_absent_from_rules():
    path = _CLAUDE / "rules" / "code-review-checklist.md"
    assert not path.exists(), f"File should have been removed from rules/: {path}"


def test_security_review_checklist_absent_from_rules():
    path = _CLAUDE / "rules" / "security-review-checklist.md"
    assert not path.exists(), f"File should have been removed from rules/: {path}"


# ---------------------------------------------------------------------------
# 7-9: Agent definitions reference the new paths
# ---------------------------------------------------------------------------


def test_planner_references_new_plan_design_guidelines():
    agent_file = _CLAUDE / "agents" / "planner.md"
    assert agent_file.exists(), f"Agent file not found: {agent_file}"
    content = agent_file.read_text(encoding="utf-8")
    assert "skills/dev-workflow/references/plan-design-guidelines.md" in content, (
        "planner.md should reference skills/dev-workflow/references/plan-design-guidelines.md"
    )


def test_code_reviewer_references_new_code_review_checklist():
    agent_file = _CLAUDE / "agents" / "code-reviewer.md"
    assert agent_file.exists(), f"Agent file not found: {agent_file}"
    content = agent_file.read_text(encoding="utf-8")
    assert "skills/dev-workflow/references/code-review-checklist.md" in content, (
        "code-reviewer.md should reference skills/dev-workflow/references/code-review-checklist.md"
    )


def test_security_reviewer_references_new_security_review_checklist():
    agent_file = _CLAUDE / "agents" / "security-reviewer.md"
    assert agent_file.exists(), f"Agent file not found: {agent_file}"
    content = agent_file.read_text(encoding="utf-8")
    assert "skills/dev-workflow/references/security-review-checklist.md" in content, (
        "security-reviewer.md should reference skills/dev-workflow/references/security-review-checklist.md"
    )


# ---------------------------------------------------------------------------
# 10-12: Agent definitions do NOT reference the old rules/ paths
# ---------------------------------------------------------------------------


def test_planner_does_not_reference_old_plan_design_guidelines():
    agent_file = _CLAUDE / "agents" / "planner.md"
    assert agent_file.exists(), f"Agent file not found: {agent_file}"
    content = agent_file.read_text(encoding="utf-8")
    assert "rules/plan-design-guidelines.md" not in content, (
        "planner.md must not reference old path rules/plan-design-guidelines.md"
    )


def test_code_reviewer_does_not_reference_old_code_review_checklist():
    agent_file = _CLAUDE / "agents" / "code-reviewer.md"
    assert agent_file.exists(), f"Agent file not found: {agent_file}"
    content = agent_file.read_text(encoding="utf-8")
    assert "rules/code-review-checklist.md" not in content, (
        "code-reviewer.md must not reference old path rules/code-review-checklist.md"
    )


def test_security_reviewer_does_not_reference_old_security_review_checklist():
    agent_file = _CLAUDE / "agents" / "security-reviewer.md"
    assert agent_file.exists(), f"Agent file not found: {agent_file}"
    content = agent_file.read_text(encoding="utf-8")
    assert "rules/security-review-checklist.md" not in content, (
        "security-reviewer.md must not reference old path rules/security-review-checklist.md"
    )
