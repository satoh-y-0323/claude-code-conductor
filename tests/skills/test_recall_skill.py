"""Verify ``.claude/skills/recall/SKILL.md`` structure.

The recall skill follows the design-doc frontmatter (``name`` /
``description`` / ``allowed-tools``) rather than the legacy
``description``-only style used by older skills. Claude Code reads the
frontmatter mechanically, so the tests pin it.
"""

from __future__ import annotations

from tests.skills._skill_helpers import SKILLS_DIR

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
