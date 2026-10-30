"""
tests/skills/test_setup_templates.py

v2.13.0 で project-setup agent のテンプレートを skill サブディレクトリに移譲した検証。
公式 Claude Code skill 規約に従い templates/ と reference.md（単数ファイル）を採用。
"""
from pathlib import Path

from tests.skills._skill_helpers import WORKTREE_ROOT


PROJECT_SETUP_AGENT = WORKTREE_ROOT / ".claude" / "agents" / "project-setup.md"
SETUP_SKILL_DIR = WORKTREE_ROOT / ".claude" / "skills" / "setup"
TEMPLATES_DIR = SETUP_SKILL_DIR / "templates"
CODING_TEMPLATE = TEMPLATES_DIR / "coding-standards-template.md"
CONVENTIONS_TEMPLATE = TEMPLATES_DIR / "project-conventions-template.md"
REFERENCE_FILE = SETUP_SKILL_DIR / "reference.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_templates_directory_exists():
    """skills/setup/templates/ ディレクトリが存在する。"""
    assert TEMPLATES_DIR.is_dir(), \
        f"templates/ ディレクトリが見つからない: {TEMPLATES_DIR}"


def test_coding_standards_template_exists_with_placeholders():
    """coding-standards-template.md が存在し、必須プレースホルダを含む。"""
    content = _read(CODING_TEMPLATE)
    assert content, f"coding-standards-template.md が見つからない: {CODING_TEMPLATE}"
    required = (
        "{LANG_PATHS}",
        "{STACK_NAME}",
        "{LANGUAGE}",
        "{FRAMEWORK}",
        "{LAST_UPDATED}",
        "{STYLE_GUIDE_NOTES}",
        "{NAMING_RULES}",
        "{TEST_RULES}",
        "{SECURITY_BASELINE}",
    )
    missing = [p for p in required if p not in content]
    assert not missing, f"coding-standards-template.md にプレースホルダが不足: {missing}"


def test_project_conventions_template_exists_with_placeholders():
    """project-conventions-template.md が存在し、必須プレースホルダを含む。"""
    content = _read(CONVENTIONS_TEMPLATE)
    assert content, f"project-conventions-template.md が見つからない: {CONVENTIONS_TEMPLATE}"
    required = (
        "{LAST_UPDATED}",
        "{PROJECT_NAMING_RULES}",
        "{COMMENT_POLICY}",
        "{TEST_COVERAGE_GOAL}",
        "{BRANCH_COMMIT_RULES}",
    )
    missing = [p for p in required if p not in content]
    assert not missing, f"project-conventions-template.md にプレースホルダが不足: {missing}"


def test_project_setup_agent_references_templates():
    """project-setup.md が templates/ と reference.md への参照を持つ。"""
    content = _read(PROJECT_SETUP_AGENT)
    required_refs = (
        "coding-standards-template.md",
        "project-conventions-template.md",
        "reference.md",
    )
    missing = [ref for ref in required_refs if ref not in content]
    assert not missing, f"project-setup.md に templates/reference への参照が不足: {missing}"
