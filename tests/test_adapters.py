"""Tests for ``c3.adapters`` internal helpers and the MCP skill reader."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from c3 import adapters
from c3.adapters import (
    MANAGED_CODEX_BEGIN,
    MANAGED_CODEX_END,
    MANAGED_CODEX_TOML_BEGIN,
    MANAGED_CODEX_TOML_END,
    _codex_agent_toml,
    _convert_skill,
    _replace_managed_block,
    _toml_escape,
    _toml_multiline_escape,
    _write_cursor_mcp,
    scaffold_adapters,
)
from c3.mcp_server import C3MCPServer


# ----------------------------------------------------------------------
# _toml_escape / _toml_multiline_escape
# ----------------------------------------------------------------------


def test_toml_escape_handles_backslash_and_quote():
    assert _toml_escape(r"path\with\back") == r"path\\with\\back"
    assert _toml_escape('say "hi"') == r'say \"hi\"'


def test_toml_escape_leaves_plain_text_untouched():
    assert _toml_escape("hello world") == "hello world"


def test_toml_multiline_escape_protects_triple_quote():
    assert _toml_multiline_escape('end """ here') == r'end \"\"\" here'


def test_toml_multiline_escape_keeps_newlines():
    # multiline literal allows raw newlines inside `"""..."""`, so the
    # escape helper must NOT collapse them.
    text = "line1\nline2\nline3"
    assert _toml_multiline_escape(text) == text


# ----------------------------------------------------------------------
# _convert_skill
# ----------------------------------------------------------------------


def test_convert_skill_preserves_existing_frontmatter():
    text = "---\nname: foo\ndescription: existing\n---\n\n# Body\n"
    result = _convert_skill(text, "foo")
    assert result.startswith("---\n")
    assert "name: foo" in result
    assert "description: existing" in result
    assert "C3 Adapter Notes" in result
    assert "Body" in result


def test_convert_skill_synthesizes_when_no_frontmatter():
    text = "# Hello Skill\n\nBody text\n"
    result = _convert_skill(text, "hello")
    # name and description should be auto-filled
    assert "name: hello" in result
    assert "description: Hello Skill" in result
    assert "Body text" in result


def test_convert_skill_falls_back_to_name_when_no_heading():
    text = "Plain body without heading\n"
    result = _convert_skill(text, "plain")
    assert "name: plain" in result
    assert "description: plain" in result


# ----------------------------------------------------------------------
# _codex_agent_toml
# ----------------------------------------------------------------------


def test_codex_agent_toml_contains_required_keys():
    result = _codex_agent_toml("tester", "Run tests", "# tester\n\nbody")
    assert 'name = "tester"' in result
    assert 'description = "Run tests"' in result
    assert "developer_instructions = " in result
    assert '"""' in result


def test_codex_agent_toml_escapes_quoted_description():
    result = _codex_agent_toml("x", 'has "quotes"', "body")
    assert r'description = "has \"quotes\""' in result


def test_codex_agent_toml_multiline_body_preserves_newlines():
    body = "line a\nline b"
    result = _codex_agent_toml("x", "d", body)
    # newlines stay raw inside the multiline literal
    assert "line a\nline b" in result


# ----------------------------------------------------------------------
# _replace_managed_block
# ----------------------------------------------------------------------


def test_replace_managed_block_swaps_existing_block():
    current = (
        f"prefix\n{MANAGED_CODEX_BEGIN}\nold inside\n{MANAGED_CODEX_END}\nsuffix\n"
    )
    new_block = f"{MANAGED_CODEX_BEGIN}\nnew inside\n{MANAGED_CODEX_END}\n"
    result = _replace_managed_block(
        current, new_block, MANAGED_CODEX_BEGIN, MANAGED_CODEX_END
    )
    assert "old inside" not in result
    assert "new inside" in result
    assert result.startswith("prefix\n")
    assert result.endswith("suffix\n")


def test_replace_managed_block_replaces_only_first_occurrence():
    """Document the current behaviour: ``count=1`` means a duplicated managed
    block only gets its first occurrence updated. This is intentional — a
    second managed block in the same file is a corrupt state — but the
    regression should fail if the contract changes silently."""
    current = (
        f"{MANAGED_CODEX_BEGIN}\nA\n{MANAGED_CODEX_END}\n"
        f"middle\n"
        f"{MANAGED_CODEX_BEGIN}\nB\n{MANAGED_CODEX_END}\n"
    )
    new_block = f"{MANAGED_CODEX_BEGIN}\nNEW\n{MANAGED_CODEX_END}\n"
    result = _replace_managed_block(
        current, new_block, MANAGED_CODEX_BEGIN, MANAGED_CODEX_END
    )
    assert "NEW" in result
    # second managed block still says B
    assert "B\n" in result


# ----------------------------------------------------------------------
# _write_cursor_mcp - merge behaviour
# ----------------------------------------------------------------------


def test_write_cursor_mcp_preserves_other_servers(tmp_path: Path):
    target = tmp_path
    mcp_path = target / ".cursor" / "mcp.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text(
        json.dumps({"mcpServers": {"other": {"command": "node"}}}),
        encoding="utf-8",
    )
    _write_cursor_mcp(target, dry_run=False)
    payload = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert "other" in payload["mcpServers"]
    assert payload["mcpServers"]["other"]["command"] == "node"
    assert "c3" in payload["mcpServers"]
    assert payload["mcpServers"]["c3"]["args"] == ["-m", "c3.mcp_server"]


def test_write_cursor_mcp_creates_file_when_missing(tmp_path: Path):
    target = tmp_path
    actions = _write_cursor_mcp(target, dry_run=False)
    assert any(a.action == "add" for a in actions)
    payload = json.loads((target / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert payload["mcpServers"]["c3"]["command"] == "python"


def test_write_cursor_mcp_rejects_invalid_json(tmp_path: Path):
    target = tmp_path
    mcp_path = target / ".cursor" / "mcp.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text("{not valid", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        _write_cursor_mcp(target, dry_run=False)


def test_write_cursor_mcp_rejects_non_object_root(tmp_path: Path):
    target = tmp_path
    mcp_path = target / ".cursor" / "mcp.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        _write_cursor_mcp(target, dry_run=False)


# ----------------------------------------------------------------------
# scaffold_adapters - top-level guard
# ----------------------------------------------------------------------


def test_scaffold_adapters_requires_claude_dir(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="no .claude"):
        scaffold_adapters(tmp_path, ("codex",))


def test_scaffold_adapters_is_idempotent(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "skills").mkdir()
    (tmp_path / ".claude" / "agents").mkdir()
    first = scaffold_adapters(tmp_path, ("codex",))
    second = scaffold_adapters(tmp_path, ("codex",))
    # second pass should not re-create any file
    assert second == []
    # first pass touched at least AGENTS.md and .codex/config.toml
    paths = {action.path.name for action in first}
    assert "AGENTS.md" in paths
    assert "config.toml" in paths


# ----------------------------------------------------------------------
# MCP server _read_skill - path traversal hardening (M2)
# ----------------------------------------------------------------------


def _make_skill(root: Path, name: str, content: str = "# skill\n") -> Path:
    skill_dir = root / ".claude" / "skills" / name
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(content, encoding="utf-8")
    return skill_file


def test_mcp_read_skill_returns_content_for_valid_skill(tmp_path: Path, monkeypatch):
    _make_skill(tmp_path, "ok", "# ok skill\n\nbody\n")
    server = C3MCPServer()
    monkeypatch.setattr(server, "project_root", tmp_path.resolve())
    assert "ok skill" in server._read_skill("ok")


def test_mcp_read_skill_rejects_dotdot_traversal(tmp_path: Path, monkeypatch):
    _make_skill(tmp_path, "good")
    server = C3MCPServer()
    monkeypatch.setattr(server, "project_root", tmp_path.resolve())
    assert server._read_skill("../good") is None
    assert server._read_skill("..") is None
    assert server._read_skill(".") is None


def test_mcp_read_skill_rejects_empty_input(tmp_path: Path, monkeypatch):
    server = C3MCPServer()
    monkeypatch.setattr(server, "project_root", tmp_path.resolve())
    assert server._read_skill("") is None


def test_mcp_read_skill_returns_none_for_missing(tmp_path: Path, monkeypatch):
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    server = C3MCPServer()
    monkeypatch.setattr(server, "project_root", tmp_path.resolve())
    assert server._read_skill("does-not-exist") is None


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation requires admin on Windows")
def test_mcp_read_skill_rejects_symlink_outside_skills(tmp_path: Path, monkeypatch):
    """An attacker-controlled symlink inside .claude/skills must not be
    able to escape the skills root."""
    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    secret = tmp_path / "secret.md"
    secret.write_text("SECRET DATA", encoding="utf-8")
    # ``evil/SKILL.md`` is a symlink to a file outside the skills root.
    evil_dir = skills_dir / "evil"
    evil_dir.mkdir()
    os.symlink(secret, evil_dir / "SKILL.md")

    server = C3MCPServer()
    monkeypatch.setattr(server, "project_root", tmp_path.resolve())
    assert server._read_skill("evil") is None
