"""Tests for ``c3.adapters`` internal helpers and the MCP skill reader."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from c3 import adapters
import yaml

from c3.adapters import (
    MANAGED_CODEX_BEGIN,
    MANAGED_CODEX_END,
    MANAGED_CODEX_TOML_BEGIN,
    MANAGED_CODEX_TOML_END,
    MANAGED_OPENCODE_BEGIN,
    MANAGED_OPENCODE_END,
    _codex_agent_toml,
    _convert_skill,
    _opencode_agent_md,
    _opencode_agents_section,
    _replace_managed_block,
    _skill_to_opencode_agent_md,
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
# MCP `command` resolution — absolute sys.executable, not bare "python"
# ----------------------------------------------------------------------


def test_codex_config_section_uses_absolute_sys_executable(monkeypatch):
    monkeypatch.setattr(adapters.sys, "executable", r"C:\Python312\python.exe")

    section = adapters._codex_config_section()

    assert 'command = "C:\\\\Python312\\\\python.exe"' in section
    assert 'command = "python"' not in section


def test_codex_config_section_escapes_plain_posix_executable(monkeypatch):
    monkeypatch.setattr(adapters.sys, "executable", "/usr/bin/python3")

    section = adapters._codex_config_section()

    assert 'command = "/usr/bin/python3"' in section


def test_write_codex_config_writes_absolute_windows_command(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(adapters.sys, "executable", r"C:\Users\dev\venv\Scripts\python.exe")

    adapters._write_codex_config(tmp_path, dry_run=False)

    config_text = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert 'command = "C:\\\\Users\\\\dev\\\\venv\\\\Scripts\\\\python.exe"' in config_text


def test_write_cursor_mcp_uses_absolute_sys_executable(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(adapters.sys, "executable", r"C:\Python312\python.exe")

    adapters._write_cursor_mcp(tmp_path, dry_run=False)

    payload = json.loads((tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    command = payload["mcpServers"]["c3"]["command"]
    assert command == r"C:\Python312\python.exe"
    assert Path(command).is_absolute()


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
    assert payload["mcpServers"]["c3"]["command"] == sys.executable
    assert Path(payload["mcpServers"]["c3"]["command"]).is_absolute()


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


# ----------------------------------------------------------------------
# _opencode_agent_md - frontmatter + mode mapping
# ----------------------------------------------------------------------


def test_opencode_agent_md_contains_required_keys():
    result = _opencode_agent_md("tester", "Run tests", "# tester\n\nbody text")
    assert "name: c3-tester" in result
    assert "description: C3 tester agent. Run tests" in result
    # tools block is present and lists the expected tools
    assert "tools:" in result
    for tool in ("bash", "read", "edit", "write", "websearch"):
        assert f"- {tool}" in result
    # original body is embedded under the adapter notes section
    assert "body text" in result
    assert "Original Agent Definition" in result


@pytest.mark.parametrize("name", ["interviewer", "architect", "planner"])
def test_opencode_agent_md_interactive_agents_are_all_purpose(name):
    result = _opencode_agent_md(name, "desc", "body")
    assert "mode: all-purpose" in result


@pytest.mark.parametrize(
    "name", ["developer", "tester", "code-reviewer", "security-reviewer", "doc-writer"]
)
def test_opencode_agent_md_other_agents_are_subagent(name):
    result = _opencode_agent_md(name, "desc", "body")
    assert "mode: subagent" in result


# ----------------------------------------------------------------------
# _skill_to_opencode_agent_md
# ----------------------------------------------------------------------


def test_skill_to_opencode_agent_md_contains_required_keys():
    result = _skill_to_opencode_agent_md("start", "Workflow entry", "# start\n\nbody")
    assert "name: c3-skill-start" in result
    assert "mode: all-purpose" in result
    # After YAML-safe scalar normalisation (CR H-02 / L-06), "C3 skill: Workflow entry"
    # is rendered with single-quotes (colon triggers quoting in yaml.safe_dump).
    assert "description:" in result
    assert "C3 skill: Workflow entry" in result
    assert "tools:" in result
    # the skill is mapped to the `/start` command equivalent
    assert "/start" in result
    assert "body" in result


# ----------------------------------------------------------------------
# _opencode_agents_section
# ----------------------------------------------------------------------


def test_opencode_agents_section_injects_rules_and_claude_md():
    section = _opencode_agents_section("RULE_BODY", "CLAUDE_MD_BODY")
    assert "# C3 Adapter for OpenCode" in section
    # @mention based agent listing
    assert "@c3-interviewer" in section
    assert "## C3 Behavior Rules" in section
    assert "CLAUDE_MD_BODY" in section
    assert "## C3 Injected Rules" in section
    assert "RULE_BODY" in section


def test_opencode_agents_section_omits_empty_sections():
    section = _opencode_agents_section("", "")
    assert "# C3 Adapter for OpenCode" in section
    # with no rules/CLAUDE.md content these headings must not appear
    assert "## C3 Behavior Rules" not in section
    assert "## C3 Injected Rules" not in section


# ----------------------------------------------------------------------
# scaffold_adapters - opencode platform
# ----------------------------------------------------------------------


def _make_minimal_claude_tree(root: Path) -> None:
    (root / ".claude").mkdir()
    agents = root / ".claude" / "agents"
    agents.mkdir()
    (agents / "tester.md").write_text(
        "---\ndescription: Run tests\n---\n\n# tester\n\nbody\n", encoding="utf-8"
    )
    skills = root / ".claude" / "skills" / "start"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\ndescription: Workflow entry\n---\n\n# start\n\nbody\n", encoding="utf-8"
    )


def test_scaffold_adapters_opencode_generates_expected_files(tmp_path: Path):
    _make_minimal_claude_tree(tmp_path)
    actions = scaffold_adapters(tmp_path, ("opencode",))
    paths = {action.path.name for action in actions}
    assert "AGENTS.md" in paths
    assert "c3-tester.md" in paths
    assert "c3-skill-start.md" in paths
    # AGENTS.md is wrapped in the OpenCode managed block
    agents_md = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert MANAGED_OPENCODE_BEGIN in agents_md
    assert MANAGED_OPENCODE_END in agents_md
    # generated agent/skill files land under .opencode/agents/
    assert (tmp_path / ".opencode" / "agents" / "c3-tester.md").exists()
    assert (tmp_path / ".opencode" / "agents" / "c3-skill-start.md").exists()


def test_scaffold_adapters_opencode_is_idempotent(tmp_path: Path):
    _make_minimal_claude_tree(tmp_path)
    scaffold_adapters(tmp_path, ("opencode",))
    second = scaffold_adapters(tmp_path, ("opencode",))
    # second pass should not re-create or rewrite any file
    assert second == []


def test_scaffold_adapters_opencode_requires_claude_dir(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="no .claude"):
        scaffold_adapters(tmp_path, ("opencode",))


# ----------------------------------------------------------------------
# NEW: YAML validity (CR M-06 / SR-V-001)
# ----------------------------------------------------------------------


def _extract_frontmatter(md: str) -> dict:
    """Parse the YAML frontmatter block of a generated .md file."""
    assert md.startswith("---\n"), "expected frontmatter"
    end = md.index("\n---\n", 4)
    raw = md[4:end]
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict)
    return parsed


def test_opencode_agent_md_frontmatter_is_valid_yaml():
    """description with a colon must produce valid, round-trippable YAML."""
    result = _opencode_agent_md("arch", "Design: patterns", "body")
    parsed = _extract_frontmatter(result)
    # After YAML round-trip the description must preserve the colon value
    assert parsed["description"] == "C3 arch agent. Design: patterns"


def test_opencode_agent_md_multiline_description_is_safe():
    """A newline in description must not inject extra YAML keys.

    tester is a subagent. If mode: all-purpose leaks from the description
    field, the YAML parser will overwrite mode to 'all-purpose', which is the
    injected (wrong) value. The safe implementation must keep mode == 'subagent'.
    """
    result = _opencode_agent_md("tester", "Run tests\nmode: all-purpose", "body")
    parsed = _extract_frontmatter(result)
    # 'tester' is a subagent — mode must remain 'subagent', NOT 'all-purpose'
    # (the injected value from the description newline)
    assert parsed.get("mode") == "subagent", (
        f"mode was overwritten by injected content: {parsed.get('mode')!r}"
    )
    # description value must contain 'Run tests'
    assert "Run tests" in str(parsed["description"])


def test_skill_to_opencode_agent_md_frontmatter_is_valid_yaml():
    """description with double-quotes must produce valid YAML."""
    result = _skill_to_opencode_agent_md("s", 'desc with "quotes"', "body")
    parsed = _extract_frontmatter(result)
    assert "C3 skill:" in parsed["description"]


def test_skill_to_opencode_agent_md_doublequote_injection_is_safe():
    """Closing double-quote in description must not inject extra YAML keys."""
    result = _skill_to_opencode_agent_md("s", 'foo" extra: injected', "body")
    parsed = _extract_frontmatter(result)
    assert "extra" not in parsed


# ----------------------------------------------------------------------
# NEW: trailing space defence (CR L-01)
# ----------------------------------------------------------------------


def test_opencode_agent_md_empty_description_has_no_trailing_space():
    """An empty description must not leave a trailing space on the description line."""
    result = _opencode_agent_md("tester", "", "body")
    for line in result.splitlines():
        if line.startswith("description:"):
            assert line == line.rstrip(), (
                f"description line has trailing space: {repr(line)}"
            )
            break
    else:
        pytest.fail("no description: line found in frontmatter")


# ----------------------------------------------------------------------
# NEW: managed block marker boundary / @include stripping
#      (SR-AI-001 / CR M-03 / CR-NEW-01)
# ----------------------------------------------------------------------


def test_opencode_agents_section_strips_at_include_directives():
    """@rules/… lines from CLAUDE.md must be stripped before embedding."""
    claude_md_with_at_include = (
        "# C3 Rules\n\nSome content.\n\n## C3 Managed\n@rules/promoted/index.md\n"
    )
    section = _opencode_agents_section("RULE_BODY", claude_md_with_at_include)
    for line in section.splitlines():
        assert not line.startswith("@rules/"), (
            f"@include directive leaked into section: {repr(line)}"
        )


def _make_minimal_claude_tree_with_marker(root: Path) -> None:
    """Like _make_minimal_claude_tree but CLAUDE.md contains MANAGED_OPENCODE_END."""
    (root / ".claude").mkdir()
    agents = root / ".claude" / "agents"
    agents.mkdir()
    (agents / "tester.md").write_text(
        "---\ndescription: Run tests\n---\n\n# tester\n\nbody\n", encoding="utf-8"
    )
    skills = root / ".claude" / "skills" / "start"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\ndescription: Workflow entry\n---\n\n# start\n\nbody\n", encoding="utf-8"
    )
    # CLAUDE.md contains the managed-block END marker — this must NOT break the
    # boundary when scaffold re-runs (idempotent) and must not escape the block.
    (root / ".claude" / "CLAUDE.md").write_text(
        f"# C3 Rules\n\nSome rules.\n\n{MANAGED_OPENCODE_END}\n",
        encoding="utf-8",
    )


def test_scaffold_adapters_opencode_marker_in_content_preserves_boundary(
    tmp_path: Path,
):
    """CLAUDE.md containing MANAGED_OPENCODE_END must not break managed block boundary."""
    _make_minimal_claude_tree_with_marker(tmp_path)
    scaffold_adapters(tmp_path, ("opencode",))
    agents_md = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")

    # The managed block must still be properly delimited
    assert MANAGED_OPENCODE_BEGIN in agents_md
    assert MANAGED_OPENCODE_END in agents_md

    begin_idx = agents_md.index(MANAGED_OPENCODE_BEGIN)
    end_idx = agents_md.index(MANAGED_OPENCODE_END)
    # END marker must appear AFTER BEGIN marker (not injected before it)
    assert begin_idx < end_idx, "MANAGED_OPENCODE_END appeared before BEGIN"

    # After the END marker there must be no content from the injected CLAUDE.md
    # (the marker inside CLAUDE.md must not escape the block)
    after_end = agents_md[end_idx + len(MANAGED_OPENCODE_END):]
    # The injected marker text must not appear outside the managed block
    assert MANAGED_OPENCODE_END not in after_end, (
        "MANAGED_OPENCODE_END leaked outside the managed block"
    )


# ----------------------------------------------------------------------
# NEW: should_skip filter symmetry (CR M-04 / SR-NEW)
# ----------------------------------------------------------------------


def test_write_opencode_agents_skips_excluded(tmp_path: Path):
    """Agents matching should_skip must not be written to .opencode/agents/."""
    from c3._excludes import should_skip as _should_skip

    # agents/tdd-develop.md is explicitly in EXCLUDE_PATTERNS
    excluded_name = "tdd-develop"
    assert _should_skip(f"agents/{excluded_name}.md"), (
        f"precondition failed: agents/{excluded_name}.md should be excluded"
    )

    (tmp_path / ".claude").mkdir()
    agents_dir = tmp_path / ".claude" / "agents"
    agents_dir.mkdir()
    (agents_dir / f"{excluded_name}.md").write_text(
        "---\ndescription: TDD\n---\n\nbody\n", encoding="utf-8"
    )

    adapters._write_opencode_agents(tmp_path, dry_run=False)

    dest = tmp_path / ".opencode" / "agents" / f"c3-{excluded_name}.md"
    assert not dest.exists(), (
        f"{dest} was generated despite should_skip returning True"
    )


def test_write_opencode_skills_skips_excluded(tmp_path: Path):
    """Skills matching should_skip must not be written to .opencode/agents/."""
    from c3._excludes import should_skip as _should_skip

    # skills/worktree-tdd-workflow/* is in EXCLUDE_PATTERNS
    excluded_skill = "worktree-tdd-workflow"
    assert _should_skip(f"skills/{excluded_skill}/SKILL.md"), (
        f"precondition: skills/{excluded_skill}/SKILL.md should be excluded"
    )

    (tmp_path / ".claude").mkdir()
    skill_dir = tmp_path / ".claude" / "skills" / excluded_skill
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\ndescription: TDD workflow\n---\n\nbody\n", encoding="utf-8"
    )

    adapters._write_opencode_skills(tmp_path, dry_run=False)

    dest = tmp_path / ".opencode" / "agents" / f"c3-skill-{excluded_skill}.md"
    assert not dest.exists(), (
        f"{dest} was generated despite should_skip returning True"
    )


# ----------------------------------------------------------------------
# NEW: symlink guard (SR-V-002)
# ----------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation requires admin on Windows")
def test_write_opencode_agents_rejects_symlink_outside(tmp_path: Path):
    """A symlink in .claude/agents/ pointing outside the tree must be skipped."""
    (tmp_path / ".claude").mkdir()
    agents_dir = tmp_path / ".claude" / "agents"
    agents_dir.mkdir()

    # Create a legitimate target outside the .claude tree
    secret = tmp_path / "secret_agent.md"
    secret.write_text("SECRET AGENT DATA", encoding="utf-8")

    # Symlink inside agents/ that points outside
    evil_link = agents_dir / "evil-agent.md"
    os.symlink(secret, evil_link)

    adapters._write_opencode_agents(tmp_path, dry_run=False)

    dest = tmp_path / ".opencode" / "agents" / "c3-evil-agent.md"
    assert not dest.exists(), (
        "symlink pointing outside .claude/agents/ was followed and written"
    )


# ----------------------------------------------------------------------
# NEW: --platform all coexistence (CR M-07)
# ----------------------------------------------------------------------


def test_scaffold_adapters_codex_and_opencode_coexist_in_agents_md(tmp_path: Path):
    """Running scaffold_adapters with both codex and opencode must produce an
    AGENTS.md that contains both managed blocks."""
    _make_minimal_claude_tree(tmp_path)
    scaffold_adapters(tmp_path, ("codex", "opencode"))
    agents_md = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert MANAGED_CODEX_BEGIN in agents_md, "MANAGED_CODEX_BEGIN missing"
    assert MANAGED_OPENCODE_BEGIN in agents_md, "MANAGED_OPENCODE_BEGIN missing"


# ----------------------------------------------------------------------
# NEW: dry_run (CR M-08)
# ----------------------------------------------------------------------


def test_scaffold_adapters_opencode_dry_run_creates_no_files(tmp_path: Path):
    """dry_run=True must report actions but write nothing to disk."""
    _make_minimal_claude_tree(tmp_path)
    actions = scaffold_adapters(tmp_path, ("opencode",), dry_run=True)

    # Must report at least one planned action
    assert len(actions) >= 1, "dry_run returned no actions"

    # AGENTS.md must NOT be created
    assert not (tmp_path / "AGENTS.md").exists(), (
        "AGENTS.md was created despite dry_run=True"
    )
    # .opencode/agents/ must NOT be created
    opencode_agents = tmp_path / ".opencode" / "agents"
    assert not opencode_agents.exists(), (
        ".opencode/agents/ was created despite dry_run=True"
    )


# ----------------------------------------------------------------------
# NEW (Round 2): SR-V-002 — _collect_rules_for_opencode symlink guard
# ----------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="symlink creation requires admin on Windows",
)
def test_collect_rules_for_opencode_rejects_symlink_outside(tmp_path: Path):
    """A symlink in .claude/rules/ pointing outside the rules dir must be skipped.

    SR-V-002: _collect_rules_for_opencode lacks the same symlink guard that
    _write_opencode_agents and _write_opencode_skills already have.  A rules/
    directory containing a symlink to an outside file should not expose that
    file's content in the returned string.
    """
    from c3.adapters import _collect_rules_for_opencode

    claude_root = tmp_path / ".claude"
    rules_dir = claude_root / "rules"
    rules_dir.mkdir(parents=True)

    # Secret file outside .claude/rules/
    secret = tmp_path / "outside_secret.md"
    secret.write_text("OUTSIDE SECRET CONTENT", encoding="utf-8")

    # symlink inside rules/ pointing outside
    evil_link = rules_dir / "evil.md"
    try:
        os.symlink(secret, evil_link)
    except (PermissionError, OSError):
        pytest.skip("symlink creation failed on this platform")

    result = _collect_rules_for_opencode(claude_root)
    assert "OUTSIDE SECRET CONTENT" not in result, (
        "symlink pointing outside rules/ was followed and its content included"
    )


def test_collect_rules_for_opencode_includes_regular_md(tmp_path: Path):
    """Normal (non-symlink) .claude/rules/*.md files must be included.

    Regression guard: the symlink fix for SR-V-002 must not accidentally
    exclude legitimate rule files.
    """
    from c3.adapters import _collect_rules_for_opencode

    claude_root = tmp_path / ".claude"
    rules_dir = claude_root / "rules"
    rules_dir.mkdir(parents=True)

    (rules_dir / "my-rule.md").write_text("# My Rule\n\nRule content here.", encoding="utf-8")

    result = _collect_rules_for_opencode(claude_root)
    assert "Rule content here." in result, (
        "regular .md file in rules/ was not included in _collect_rules_for_opencode result"
    )


# ----------------------------------------------------------------------
# NEW (Round 2): SR-AI-001 — _sanitize_for_managed_block strips CODEX markers
# ----------------------------------------------------------------------


def test_sanitize_for_managed_block_strips_codex_markers():
    """_sanitize_for_managed_block must remove MANAGED_CODEX_BEGIN/END lines.

    SR-AI-001: when platform=all is used, CODEX markers embedded in shared
    content (CLAUDE.md / rules) could otherwise corrupt the codex managed block.
    _sanitize_for_managed_block strips every adapter marker, not just OPENCODE.
    """
    from c3.adapters import _sanitize_for_managed_block

    content = (
        f"line before\n"
        f"{MANAGED_CODEX_BEGIN}\n"
        f"inside codex block\n"
        f"{MANAGED_CODEX_END}\n"
        f"line after\n"
    )
    result = _sanitize_for_managed_block(content)
    for line in result.splitlines():
        assert MANAGED_CODEX_BEGIN not in line, (
            f"MANAGED_CODEX_BEGIN was not stripped: {repr(line)}"
        )
        assert MANAGED_CODEX_END not in line, (
            f"MANAGED_CODEX_END was not stripped: {repr(line)}"
        )


def test_sanitize_for_managed_block_still_strips_opencode_markers():
    """OPENCODE marker stripping must not regress when CODEX marker support is added.

    SR-AI-001 regression guard: after adding CODEX marker removal,
    MANAGED_OPENCODE_BEGIN and MANAGED_OPENCODE_END must still be removed.
    """
    from c3.adapters import _sanitize_for_managed_block

    content = (
        f"preamble\n"
        f"{MANAGED_OPENCODE_BEGIN}\n"
        f"opencode inside\n"
        f"{MANAGED_OPENCODE_END}\n"
        f"postamble\n"
    )
    result = _sanitize_for_managed_block(content)
    for line in result.splitlines():
        assert MANAGED_OPENCODE_BEGIN not in line, (
            f"MANAGED_OPENCODE_BEGIN was not stripped: {repr(line)}"
        )
        assert MANAGED_OPENCODE_END not in line, (
            f"MANAGED_OPENCODE_END was not stripped: {repr(line)}"
        )


def test_opencode_agents_section_strips_codex_markers():
    """_opencode_agents_section must strip CODEX markers from embedded claude_md.

    SR-AI-001 end-to-end: when platform=all is used, CLAUDE.md may already
    contain a CODEX managed block. The opencode section must sanitize it out
    so the resulting AGENTS.md does not carry stray CODEX marker lines inside
    the OPENCODE managed block.
    """
    claude_md_with_codex = (
        f"# C3 Rules\n\nSome rules.\n\n"
        f"{MANAGED_CODEX_BEGIN}\n"
        f"codex block content\n"
        f"{MANAGED_CODEX_END}\n"
        f"trailing rules.\n"
    )
    section = _opencode_agents_section("", claude_md_with_codex)
    for line in section.splitlines():
        assert MANAGED_CODEX_BEGIN not in line, (
            f"MANAGED_CODEX_BEGIN leaked into opencode section: {repr(line)}"
        )
        assert MANAGED_CODEX_END not in line, (
            f"MANAGED_CODEX_END leaked into opencode section: {repr(line)}"
        )
