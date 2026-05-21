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


def test_project_setup_agent_under_80_lines():
    """project-setup.md は D-012 準拠で 80 行以内に収まる。"""
    content = _read(PROJECT_SETUP_AGENT)
    line_count = len(content.splitlines())
    assert line_count <= 80, (
        f"project-setup.md は {line_count} 行（80 行上限超過）。"
        " テンプレート/参照を skills/setup/templates/ と reference.md に外出ししてください（D-012）。"
    )


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


def test_reference_file_exists_with_language_mappings():
    """skills/setup/reference.md が存在し、言語→拡張子マッピングを含む。"""
    content = _read(REFERENCE_FILE)
    assert content, f"reference.md が見つからない: {REFERENCE_FILE}"
    # 主要言語のマッピングが含まれる
    required_languages = ("Python", "TypeScript", "JavaScript", "Go", "Java", "Rust", "Ruby")
    missing = [lang for lang in required_languages if lang not in content]
    assert not missing, f"reference.md に言語マッピングが不足: {missing}"
    # 拡張子サンプル
    required_extensions = ("**/*.py", "**/*.ts", "**/*.go", "**/*.rs")
    missing_ext = [ext for ext in required_extensions if ext not in content]
    assert not missing_ext, f"reference.md に拡張子 glob が不足: {missing_ext}"


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
