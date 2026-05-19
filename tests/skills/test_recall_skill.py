"""Verify ``.claude/skills/recall/SKILL.md`` structure.

The recall skill follows the design-doc frontmatter (``name`` /
``description`` / ``allowed-tools``) rather than the legacy
``description``-only style used by older skills. The user explicitly
chose this format during planning, so the tests pin it.
"""

from __future__ import annotations

from tests.skills._skill_helpers import SKILLS_DIR, extract_section

RECALL_SKILL = SKILLS_DIR / "recall" / "SKILL.md"


def _read() -> str:
    assert RECALL_SKILL.exists(), f"{RECALL_SKILL} should exist"
    return RECALL_SKILL.read_text(encoding="utf-8")


def test_frontmatter_present() -> None:
    content = _read()
    assert content.startswith("---\n"), "SKILL.md must start with YAML frontmatter"
    end = content.find("\n---", 4)
    assert end > 0, "frontmatter must be closed with ---"


def test_frontmatter_name_field() -> None:
    content = _read()
    assert "name: recall" in content.splitlines()[1:10], "frontmatter must declare name: recall"


def test_frontmatter_description_field() -> None:
    content = _read()
    head = content.split("---", 2)[1]
    assert "description:" in head
    # description should mention the purpose
    assert "意味検索" in head or "検索" in head


def test_frontmatter_allowed_tools_field() -> None:
    content = _read()
    head = content.split("---", 2)[1]
    assert "allowed-tools:" in head
    assert "Bash" in head


def test_skill_documents_invocation_command() -> None:
    content = _read()
    assert "c3 recall search" in content


def test_skill_documents_rebuild_path() -> None:
    content = _read()
    assert "c3 recall rebuild" in content


def test_skill_has_when_to_run_section() -> None:
    content = _read()
    section = extract_section(content, "## 起動タイミング")
    assert section, "'## 起動タイミング' section must exist"
    assert "ユーザーに尋ねず" in section or "自律" in section


def test_skill_has_when_not_to_run_section() -> None:
    content = _read()
    section = extract_section(content, "## 利用しない方が良い場合")
    assert section, "'## 利用しない方が良い場合' section must exist"


def test_skill_has_procedure_section() -> None:
    content = _read()
    section = extract_section(content, "## 手順")
    assert section, "'## 手順' section must exist"
    # mentions the JSON contract for downstream parsing
    assert "--json" in section


def test_skill_documents_user_transparency() -> None:
    content = _read()
    section = extract_section(content, "## ユーザーへの透明性")
    assert section, "ユーザー透明性セクションが必要"
    assert "ヒット" in section


def test_skill_warns_against_prompt_injection_reuse() -> None:
    content = _read()
    # The recall feature surfaces arbitrary historical text; the skill must
    # remind the LLM not to execute it as instructions.
    assert "プロンプトインジェクション" in content or "そのまま指示として実行しない" in content


def test_skill_documents_json_schema_hint() -> None:
    content = _read()
    # The agent should know which fields to look at.
    assert "score" in content
    assert "source_type" in content
    assert "path" in content
