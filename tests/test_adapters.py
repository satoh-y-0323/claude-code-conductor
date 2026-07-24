"""Tests for ``c3.adapters`` internal helpers and the MCP skill reader."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path, PurePosixPath

import pytest

from c3 import adapters, cli_doctor
import yaml

from c3.adapters import (
    MANAGED_CODEX_BEGIN,
    MANAGED_CODEX_END,
    MANAGED_CODEX_TOML_BEGIN,
    MANAGED_CODEX_TOML_END,
    MANAGED_OPENCODE_BEGIN,
    MANAGED_OPENCODE_END,
    _adapter_skip,
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
    # Use an OS-native absolute path so ``Path(command).is_absolute()`` holds on
    # both platforms: a Windows-style literal is not absolute under PosixPath
    # (Linux CI), and a POSIX path is not absolute under WindowsPath.
    fake_executable = (
        r"C:\Python312\python.exe" if os.name == "nt" else "/usr/local/bin/python3.12"
    )
    monkeypatch.setattr(adapters.sys, "executable", fake_executable)

    adapters._write_cursor_mcp(tmp_path, dry_run=False)

    payload = json.loads((tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    command = payload["mcpServers"]["c3"]["command"]
    assert command == fake_executable
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


# ============================================================================
# NEW (B1 — TOML エスケープの制御文字対応): Red フェーズ (TDD)
#
# plan-report-20260723-193847.md T1 / architecture-report-20260723-193422.md
# 参照。以下は「Red であるべき群」「Red フェーズでも緑であるべき群」の 2 系統が
# 混在する。区別は test-report を参照（各群の一覧・件数を分けて記載）。
# ============================================================================


# ----------------------------------------------------------------------
# NEW: _toml_escape — control-character escaping (S-1, ADR-2)
# ----------------------------------------------------------------------


def test_toml_escape_escapes_lf():
    assert _toml_escape("a\nb") == "a\\nb"


def test_toml_escape_escapes_cr():
    assert _toml_escape("a\rb") == "a\\rb"


def test_toml_escape_escapes_tab():
    assert _toml_escape("a\tb") == "a\\tb"


def test_toml_escape_escapes_nul():
    assert _toml_escape("a\x00b") == "a\\u0000b"


def test_toml_escape_escapes_backspace_and_formfeed():
    assert _toml_escape("\x08") == "\\b"
    assert _toml_escape("\x0c") == "\\f"


def test_toml_escape_escapes_other_c0_control_chars():
    assert _toml_escape("\x01") == "\\u0001"
    assert _toml_escape("\x1f") == "\\u001f"


def test_toml_escape_escapes_del():
    assert _toml_escape("\x7f") == "\\u007f"


def test_toml_escape_escapes_nel():
    # U+0085 NEL is a C1 control char and one of str.splitlines()'s 10
    # line-boundary characters (architecture F-3).
    assert _toml_escape("\x85") == "\\u0085"


def test_toml_escape_escapes_line_and_paragraph_separators():
    assert _toml_escape(" ") == "\\u2028"
    assert _toml_escape(" ") == "\\u2029"


def test_toml_escape_leaves_plain_japanese_text_untouched():
    # Identity case (DC-AM-001): this may already pass under the current
    # implementation — that is expected, not an anomaly. It documents that
    # C1 range handling does not corrupt ordinary non-ASCII text (K-4).
    assert _toml_escape("日本語 path/to/x") == "日本語 path/to/x"


def test_toml_escape_handles_empty_string():
    # Identity case (DC-AM-001): may already pass under the current
    # implementation.
    assert _toml_escape("") == ""


def test_toml_escape_handles_mixed_control_quote_backslash():
    """Control chars, a quote, and a backslash in one value must all be
    escaped correctly with no ordering mishap (str.translate is single-pass,
    architecture §4)."""
    mixed = 'a\x00"b\\c\x1f d'
    assert _toml_escape(mixed) == 'a\\u0000\\"b\\\\c\\u001f d'


def test_toml_escape_escapes_all_ten_line_boundary_chars():
    """N-1a direct verification: every character str.splitlines() treats as
    a line boundary (architecture F-3 / §0 — LF, VT, FF, CR, FS, GS, RS, NEL,
    LS, PS) must be neutralised so the escaped output is always one line."""
    boundary_chars = "\n\x0b\x0c\r\x1c\x1d\x1e\x85  "
    value = "x".join(boundary_chars)
    escaped = _toml_escape(value)
    assert len(escaped.splitlines()) == 1, (
        f"escaped value still splits into multiple lines: {escaped.splitlines()!r}"
    )


# ----------------------------------------------------------------------
# NEW: _toml_multiline_escape — control-character escaping (S-2, ADR-3)
# ----------------------------------------------------------------------


def test_toml_multiline_escape_keeps_tab():
    # LF is already covered by the existing
    # test_toml_multiline_escape_keeps_newlines. TAB is the other character
    # ADR-3 intentionally leaves raw.
    assert _toml_multiline_escape("a\tb") == "a\tb"


def test_toml_multiline_escape_escapes_isolated_cr():
    assert _toml_multiline_escape("a\rb") == "a\\rb"


def test_toml_multiline_escape_escapes_various_control_chars():
    cases = {
        "\x00": "\\u0000",
        "\x1f": "\\u001f",
        "\x7f": "\\u007f",
        "\x85": "\\u0085",
        " ": "\\u2028",
    }
    for raw, escaped in cases.items():
        value = f"a{raw}b"
        result = _toml_multiline_escape(value)
        assert result == f"a{escaped}b", f"char {raw!r} not escaped as expected: {result!r}"


def test_toml_multiline_escape_quote_runs_of_one_or_two_mid_value_are_untouched():
    """TOML allows 1-2 consecutive quotes inside a multiline literal.

    DC-AM-001: samples must NOT end in a quote (that is a different case,
    covered separately below) and must not reuse the basic-string test's
    ``say "hi"`` sample (tests/test_adapters.py:43), to avoid conflating the
    two independent contracts.
    """
    assert _toml_multiline_escape('say "hi" ok') == 'say "hi" ok'
    assert _toml_multiline_escape('a""b') == 'a""b'


def test_toml_multiline_escape_escapes_four_consecutive_quotes():
    # Existing test_toml_multiline_escape_protects_triple_quote covers the
    # 3-quote case. 4+ is the documented gap (ADR-4 / current bug).
    assert _toml_multiline_escape('a"""" b') == 'a\\"\\"\\"\\" b'


def test_toml_multiline_escape_escapes_trailing_single_quote():
    # Documented gap: a value ending in a single `"` would otherwise merge
    # with the closing ``"""`` delimiter (ADR-4 / U-3).
    assert _toml_multiline_escape('abc"') == 'abc\\"'


def test_toml_multiline_escape_escapes_trailing_double_quote():
    assert _toml_multiline_escape('abc""') == 'abc\\"\\"'


# ----------------------------------------------------------------------
# NEW: _escape_toml_quote_runs — private helper introduced by T2 (ADR-5)
#
# DC-AM-004 / K-7: at the time this section was added (Red phase), this
# symbol did not yet exist in the implementation. It was deliberately NOT
# added to the module-level import list at the top of this file at that
# time — doing so would have raised ImportError at collection time and
# taken down every test in this file (including the snapshot tests below,
# which needed to remain executable during the Red phase). Each test below
# imports it locally instead; the local-import style is kept for
# consistency now that the symbol exists in the implementation.
# ----------------------------------------------------------------------


def test_escape_toml_quote_runs_single_run_mid_value_is_untouched():
    from c3.adapters import _escape_toml_quote_runs

    assert _escape_toml_quote_runs('a"b') == 'a"b'


def test_escape_toml_quote_runs_double_run_mid_value_is_untouched():
    from c3.adapters import _escape_toml_quote_runs

    assert _escape_toml_quote_runs('a""b') == 'a""b'


def test_escape_toml_quote_runs_triple_run_is_escaped():
    from c3.adapters import _escape_toml_quote_runs

    assert _escape_toml_quote_runs('a"""b') == 'a\\"\\"\\"b'


def test_escape_toml_quote_runs_quadruple_run_is_escaped():
    from c3.adapters import _escape_toml_quote_runs

    assert _escape_toml_quote_runs('a""""b') == 'a\\"\\"\\"\\"b'


def test_escape_toml_quote_runs_leading_single_quote_is_untouched():
    # U-A boundary: a run at the very start of the value is neither a 3+ run
    # nor a trailing run, so it must be left alone.
    from c3.adapters import _escape_toml_quote_runs

    assert _escape_toml_quote_runs('"abc') == '"abc'


def test_escape_toml_quote_runs_trailing_single_quote_is_escaped():
    from c3.adapters import _escape_toml_quote_runs

    assert _escape_toml_quote_runs('abc"') == 'abc\\"'


def test_escape_toml_quote_runs_trailing_double_quote_is_escaped():
    from c3.adapters import _escape_toml_quote_runs

    assert _escape_toml_quote_runs('abc""') == 'abc\\"\\"'


def test_escape_toml_quote_runs_empty_string_returns_empty():
    from c3.adapters import _escape_toml_quote_runs

    assert _escape_toml_quote_runs("") == ""


# ----------------------------------------------------------------------
# NEW: backward-compat snapshots (C-2 / K-1 / K-6)
#
# Captured from the PRE-REFACTOR (current, buggy) implementation on
# 2026-07-23 during the Red phase — see test-report for the exact capture
# method. These must stay GREEN throughout Red and after the Green/Refactor
# phases: a control-char-free, quote-run-free input must produce byte-for-
# byte identical output before and after the fix.
#
# Both ``sys.executable`` AND ``_dev_source_pythonpath`` are monkeypatched
# (DC-AS-002) so the snapshot does not depend on the local dev checkout path
# (which would make CI, running from a different absolute path, red).
# ----------------------------------------------------------------------


def test_codex_config_section_snapshot_with_pythonpath(monkeypatch):
    monkeypatch.setattr(adapters.sys, "executable", "/tmp/py/python")
    monkeypatch.setattr(adapters, "_dev_source_pythonpath", lambda: PurePosixPath("/repo/src"))

    section = adapters._codex_config_section()

    assert section == (
        '[mcp_servers.c3]\n'
        'command = "/tmp/py/python"\n'
        'args = ["-m", "c3.mcp_server"]\n'
        'startup_timeout_sec = 10\n'
        'tool_timeout_sec = 600\n'
        '\n'
        '[mcp_servers.c3.env]\n'
        'C3_PROJECT_ROOT = "."\n'
        'PYTHONPATH = "/repo/src"\n'
    )


def test_codex_config_section_snapshot_without_pythonpath(monkeypatch):
    monkeypatch.setattr(adapters.sys, "executable", "/tmp/py/python")
    monkeypatch.setattr(adapters, "_dev_source_pythonpath", lambda: None)

    section = adapters._codex_config_section()

    assert section == (
        '[mcp_servers.c3]\n'
        'command = "/tmp/py/python"\n'
        'args = ["-m", "c3.mcp_server"]\n'
        'startup_timeout_sec = 10\n'
        'tool_timeout_sec = 600\n'
        '\n'
        '[mcp_servers.c3.env]\n'
        'C3_PROJECT_ROOT = "."\n'
    )


def test_codex_agent_toml_snapshot_dummy_body_with_quotes():
    # DC-AM-003: input is a dummy body defined inline (NOT a real
    # .claude/agents/*.md file), so future edits to real agent files cannot
    # make this snapshot go permanently red.
    result = adapters._codex_agent_toml(
        "dummyagent",
        'a "b" c',
        '---\ndescription: a "b" c\n---\n\n# dummy\n\nbody with "quotes" here\n',
    )

    assert result == (
        'name = "dummyagent"\n'
        'description = "a \\"b\\" c"\n'
        'developer_instructions = """Generated from `.claude/agents/dummyagent.md`.\n'
        '\n'
        'Use `.claude/` as the C3 state root. Preserve C3 report and memory paths so\n'
        'Claude Code, Codex, and Cursor remain compatible.\n'
        '\n'
        '---\n'
        'description: a "b" c\n'
        '---\n'
        '\n'
        '# dummy\n'
        '\n'
        'body with "quotes" here\n'
        '"""\n'
    )


# ----------------------------------------------------------------------
# NEW: 14-agent invariant guard (C-2 / DC-AS-001)
#
# Machine-checked backward-compat guard over the real distributed agent
# definitions. The forbidden character set is DIFFERENT per field/function
# (architecture ADR-2 table) — mixing them up makes this test permanently
# unsatisfiable regardless of the implementation (K-8):
#   - name / description  -> _toml_escape (basic string): C0 in full,
#     INCLUDING LF/TAB, + DEL + C1 + U+2028/U+2029
#   - full .md source text -> _toml_multiline_escape (multiline string):
#     same set MINUS TAB and LF (ADR-3 keeps those raw on purpose), plus a
#     "no 3+ consecutive double-quotes" check (ADR-4)
# ----------------------------------------------------------------------


_AGENTS_DIR = Path(__file__).resolve().parents[1] / ".claude" / "agents"


def _basic_string_forbidden_codepoints(s: str) -> list[int]:
    """Codepoints forbidden in a value routed through ``_toml_escape``."""
    forbidden = []
    for ch in s:
        cp = ord(ch)
        if cp <= 0x1F or cp == 0x7F or 0x80 <= cp <= 0x9F or cp in (0x2028, 0x2029):
            forbidden.append(cp)
    return forbidden


def _multiline_string_forbidden_codepoints(s: str) -> list[int]:
    """Same as above but TAB (0x09) and LF (0x0A) are allowed — the
    ``_toml_multiline_escape`` contract keeps those raw on purpose."""
    return [cp for cp in _basic_string_forbidden_codepoints(s) if cp not in (0x09, 0x0A)]


def test_agents_dir_glob_is_not_empty():
    """Guard against a typo'd glob path silently making the invariant tests
    below a no-op (DC-AS-001 / pattern established in
    tests/test_nul_boundary_lint.py:458-474,482)."""
    files = sorted(_AGENTS_DIR.glob("*.md"))
    assert files, f"{_AGENTS_DIR} に *.md が1件も見つかりません（走査対象パスの確認が必要）"
    assert len(files) >= 14, (
        f"配布元 agent 定義は現在 14 件の想定（実測 {len(files)} 件）。"
        "件数が減っている場合は走査対象パスを確認すること。"
    )


def test_agent_name_and_description_have_no_basic_string_forbidden_chars():
    files = sorted(_AGENTS_DIR.glob("*.md"))
    assert files
    for f in files:
        text = f.read_text(encoding="utf-8")
        metadata, body = adapters._split_frontmatter(text)
        name = f.stem
        description = str(metadata.get("description") or adapters._first_heading(body) or name)
        bad_name = _basic_string_forbidden_codepoints(name)
        bad_desc = _basic_string_forbidden_codepoints(description)
        assert not bad_name, f"{f.name}: name contains forbidden codepoints {bad_name!r}"
        assert not bad_desc, f"{f.name}: description contains forbidden codepoints {bad_desc!r}"


def test_agent_source_text_has_no_multiline_forbidden_chars_or_long_quote_runs():
    files = sorted(_AGENTS_DIR.glob("*.md"))
    assert files
    for f in files:
        text = f.read_text(encoding="utf-8")
        bad_body = _multiline_string_forbidden_codepoints(text)
        assert not bad_body, f"{f.name}: source text contains forbidden codepoints {bad_body!r}"
        quote_runs = re.findall(r'"{3,}', text)
        assert not quote_runs, (
            f"{f.name}: source text contains 3+ consecutive double-quotes {quote_runs!r}"
        )


# ----------------------------------------------------------------------
# NEW: integration — _codex_config_section() line-structure safety (S-5, N-1a)
# ----------------------------------------------------------------------


def test_codex_config_section_command_line_stays_single_physical_line(monkeypatch):
    monkeypatch.setattr(adapters.sys, "executable", "/tmp/py\nlab/python")
    monkeypatch.setattr(adapters, "_dev_source_pythonpath", lambda: None)

    section = adapters._codex_config_section()

    command_lines = [line for line in section.splitlines() if line.startswith('command = "')]
    assert len(command_lines) == 1, (
        f"expected exactly one physical line starting with 'command = \"', got {command_lines!r}"
    )
    assert command_lines[0].endswith('"'), (
        "command line does not close on the same physical line (a raw newline "
        f"leaked into the value): {command_lines[0]!r}"
    )


def test_codex_config_section_extracted_command_has_no_raw_newline(monkeypatch):
    monkeypatch.setattr(adapters.sys, "executable", "/tmp/py\nlab/python")
    monkeypatch.setattr(adapters, "_dev_source_pythonpath", lambda: None)

    section = adapters._codex_config_section()
    extracted = cli_doctor._extract_codex_mcp_command(section)

    assert extracted is not None
    # S-5 (confirmed definition): decoding is NOT required here — the reader
    # (cli_doctor.py:369) only decodes `\"` and `\\`. `\n` must survive as the
    # two-character escape sequence; what matters for security is that a RAW
    # newline never leaks into the extracted value (that would forge a fake
    # config line / section header).
    assert "\n" not in extracted, (
        f"extracted command contains a raw newline (line-structure forgery): {extracted!r}"
    )
    assert "\\n" in extracted, (
        f"expected the escaped '\\n' sequence to survive un-decoded: {extracted!r}"
    )


def test_codex_config_section_parses_as_valid_toml_with_injected_newline(monkeypatch):
    tomllib = pytest.importorskip("tomllib")
    monkeypatch.setattr(adapters.sys, "executable", "/tmp/py\nlab/python")
    monkeypatch.setattr(adapters, "_dev_source_pythonpath", lambda: None)

    section = adapters._codex_config_section()
    parsed = tomllib.loads(section)

    # N-2 / DC-AM-002: unlike the extraction check above, a full TOML parser
    # DOES decode `\n`, so the parsed value equals the ORIGINAL (unescaped)
    # executable path — this is the deliberate contrast documented in the
    # test-report (S-5's confirmed definition).
    assert parsed["mcp_servers"]["c3"]["command"] == "/tmp/py\nlab/python"


# ----------------------------------------------------------------------
# NEW: integration — _codex_agent_toml() multiline description (S-4, N-1b)
# ----------------------------------------------------------------------


def test_codex_agent_toml_multiline_description_round_trips_via_tomllib():
    tomllib = pytest.importorskip("tomllib")
    description = 'line1\nline2 with "quotes"'

    result = _codex_agent_toml("x", description, "body")
    parsed = tomllib.loads(result)

    assert parsed["description"] == description


def test_write_codex_agents_end_to_end_preserves_multiline_description_via_toml(tmp_path: Path):
    """S-4 acceptance test (DC-AM-001): a real agent definition with a
    multiline ``description`` (via YAML literal block scalar) must produce a
    ``.codex/agents/*.toml`` that a TOML parser can read back, with the
    description value equal to the original."""
    tomllib = pytest.importorskip("tomllib")
    agents_dir = tmp_path / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    md = (
        "---\n"
        "description: |\n"
        "  line one\n"
        "  line two\n"
        "---\n\n"
        "# x\n\n"
        "body text here\n"
    )
    (agents_dir / "x.md").write_text(md, encoding="utf-8")
    metadata, _ = adapters._split_frontmatter(md)
    expected_description = metadata["description"]
    assert "\n" in expected_description, "precondition: description must contain a newline"

    adapters._write_codex_agents(tmp_path, dry_run=False)

    toml_path = tmp_path / ".codex" / "agents" / "x.toml"
    parsed = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    assert parsed["description"] == expected_description


# ============================================================================
# NEW (B1 — E 周回 1 差し戻し): Red フェーズ (TDD, T6)
#
# plan-report-20260723-193847.md §2-B T6 参照。フェーズ E で code-reviewer /
# security-reviewer が実装コードを実際に実行して発見した実装バグ 2 件
# （CR-NEW-1 ／ SR-NEW Finding 1 の末尾クォート二重処理・CR-NEW-2 の
# `_TOML_MULTILINE_ESCAPE_MAP` からの VT 脱落）に対する境界値テストを追加する。
# design-critic 3 サイクル・T1〜T5 のケース表はいずれも「末尾かつ 3 連以上」
# という条件の交差ケースを 1 件もカバーしておらず、素通りした（K-9）。
# 本セクションは追加時点では全て「Red であるべき群」だった（当時の実装では
# 失敗し、T7 の修正後に緑になる想定）。全件、T7 で予定されていた修正
# （ADR-6 の派生生成 ＋ ADR-7 の 1 パス化）を適用すると緑になることを
# Red フェーズの時点で事前に検証済み（test-report 参照）。T7 は本 diff に
# 既に適用済みであり、現在は全件 green である。
# ============================================================================


# ----------------------------------------------------------------------
# NEW: _escape_toml_quote_runs — trailing run of 3+ quotes
# (CR-T-001 / SR-NEW Finding 2)。
#
# 等値比較のみ（DC-AS-001・原理的な制約）: このヘルパーはクォートしか
# 処理しないため、バックスラッシュや制御文字を含む値を直接渡すと、それら
# が未処理のまま TOML に渡り、tomllib 往復は「クォート連バグ」とは無関係の
# 理由で失敗してしまう。末尾クォート連の tomllib 往復は choke point
# （`_toml_multiline_escape`）側のテスト群（このすぐ下）が担当する。
# ----------------------------------------------------------------------


def test_escape_toml_quote_runs_trailing_triple_run_is_escaped():
    from c3.adapters import _escape_toml_quote_runs

    assert _escape_toml_quote_runs('abc"""') == 'abc\\"\\"\\"'


def test_escape_toml_quote_runs_trailing_quadruple_run_is_escaped():
    from c3.adapters import _escape_toml_quote_runs

    assert _escape_toml_quote_runs('abc""""') == 'abc\\"\\"\\"\\"'


def test_escape_toml_quote_runs_value_is_entirely_quotes_is_escaped():
    # The whole value is a single run that is simultaneously leading,
    # trailing, AND 3+ in length -- confirms neither condition needs to be
    # exclusive of the other for the escape to apply.
    from c3.adapters import _escape_toml_quote_runs

    assert _escape_toml_quote_runs('"""""') == '\\"\\"\\"\\"\\"'


# ----------------------------------------------------------------------
# NEW: _toml_multiline_escape (choke point) — tomllib round trip for a
# trailing run of 3+ quotes (CR-NEW-1 / SR-NEW Finding 1).
#
# This is the S-8 (requirements §5) acceptance test body for the
# trailing-run case: unlike the equality tests above, this goes through the
# actual choke point used by `_codex_agent_toml()` and asserts that the
# DECODED value equals the original input, not merely that the escaped text
# has some expected shape. It is deliberately symmetric with the existing
# mid-value / short-run quote tests at tests/test_adapters.py:990,996,1002
# (DC-GP-003) but targets the combination those omitted: "3+ AND trailing".
# ----------------------------------------------------------------------


def test_toml_multiline_escape_round_trips_trailing_triple_quote_run():
    tomllib = pytest.importorskip("tomllib")
    value = 'abc"""'
    escaped = _toml_multiline_escape(value)
    parsed = tomllib.loads('x = """' + escaped + '"""\n')
    assert parsed["x"] == value


def test_toml_multiline_escape_round_trips_trailing_quadruple_quote_run():
    tomllib = pytest.importorskip("tomllib")
    value = 'abc""""'
    escaped = _toml_multiline_escape(value)
    parsed = tomllib.loads('x = """' + escaped + '"""\n')
    assert parsed["x"] == value


def test_toml_multiline_escape_round_trips_value_that_is_entirely_quotes():
    tomllib = pytest.importorskip("tomllib")
    value = '"""""'
    escaped = _toml_multiline_escape(value)
    parsed = tomllib.loads('x = """' + escaped + '"""\n')
    assert parsed["x"] == value


def test_toml_multiline_escape_round_trips_trailing_run_preceded_by_backslash():
    # Mixed case (plan T6 手順 2-(ii)): a backslash immediately before the
    # trailing quote run. Only reachable through the choke point, because
    # `translate()` must turn the backslash into `\\` BEFORE the quote-run
    # pass runs -- the equality-only helper tests above cannot exercise
    # this interaction.
    tomllib = pytest.importorskip("tomllib")
    value = 'a\\b"""'
    escaped = _toml_multiline_escape(value)
    parsed = tomllib.loads('x = """' + escaped + '"""\n')
    assert parsed["x"] == value


def test_toml_multiline_escape_round_trips_trailing_run_preceded_by_control_char():
    tomllib = pytest.importorskip("tomllib")
    value = "a\x00b\"\"\""
    escaped = _toml_multiline_escape(value)
    parsed = tomllib.loads('x = """' + escaped + '"""\n')
    assert parsed["x"] == value


# ----------------------------------------------------------------------
# NEW: _toml_multiline_escape — line-boundary coverage excluding LF
# (CR-NEW-2 / CR-T-001-2).
#
# "Symmetry" with test_toml_escape_escapes_all_ten_line_boundary_chars
# (tests/test_adapters.py:936) means coverage PARITY over the 10
# architecture-F-3 boundary characters, NOT the same assert shape
# (DC-AM-001): `len(escaped.splitlines()) == 1` cannot hold on the
# multiline side because LF is intentionally left raw (ADR-3). Each of the
# remaining 9 characters is instead injected individually and compared
# against its expected escape sequence. VT (U+000B) is the character
# CR-NEW-2 found missing from `_TOML_MULTILINE_ESCAPE_MAP`; the other 8 are
# included for coverage parity, not because they were individually
# suspected buggy. Short-form vs \uXXXX choice matches _TOML_ESCAPE_MAP
# (FF -> \f, CR -> \r); the CR expectation must not contradict the existing
# test_toml_multiline_escape_escapes_isolated_cr (tests/test_adapters.py:960).
# TAB staying raw is a SEPARATE, non-line-boundary requirement (ADR-3)
# already covered by the existing test_toml_multiline_escape_keeps_tab
# (tests/test_adapters.py:953) -- not duplicated here.
# ----------------------------------------------------------------------


def test_toml_multiline_escape_escapes_nine_line_boundary_chars_excluding_lf():
    cases = {
        "\x0b": "\\u000b",  # VT -- CR-NEW-2's missing entry
        "\x0c": "\\f",  # FF (existing short form; must not regress)
        "\r": "\\r",  # CR (matches tests/test_adapters.py:960/961)
        "\x1c": "\\u001c",  # FS
        "\x1d": "\\u001d",  # GS
        "\x1e": "\\u001e",  # RS
        "\x85": "\\u0085",  # NEL
        " ": "\\u2028",  # LS
        " ": "\\u2029",  # PS
    }
    for raw, escaped in cases.items():
        value = f"a{raw}b"
        result = _toml_multiline_escape(value)
        assert result == f"a{escaped}b", (
            f"char {raw!r} not escaped as expected: {result!r}"
        )


# ----------------------------------------------------------------------
# NEW: 2-map symmetry (CR-M-001 / ADR-6).
#
# Full dict equality, not a key-set comparison (DC-AM-002): a key-set-only
# check would miss a key that survives with the WRONG value (e.g. 0x0D
# re-written as "\\u000d" instead of the short form "\\r", silently
# changing existing output for CR). Once `_TOML_MULTILINE_ESCAPE_MAP` is a
# derived dict comprehension over `_TOML_ESCAPE_MAP` (ADR-6), this equality
# holds unconditionally and VT-type single-codepoint omissions become
# structurally impossible to reintroduce.
# ----------------------------------------------------------------------


def test_toml_multiline_escape_map_is_derived_from_basic_escape_map():
    from c3.adapters import _TOML_ESCAPE_MAP, _TOML_MULTILINE_ESCAPE_MAP

    expected = {
        k: v for k, v in _TOML_ESCAPE_MAP.items() if k not in (0x09, 0x0A, 0x22)
    }
    assert _TOML_MULTILINE_ESCAPE_MAP == expected


# -----------------------------------------------------------------------
# NEW: Adapter exclusion mechanism (AD-1 ~ AD-4 / S-11)
# -----------------------------------------------------------------------

# AD-1: autonomous-mode を除外
def test_adapter_skip_excludes_autonomous_mode():
    """_adapter_skip must exclude autonomous-mode from adapter outputs."""
    assert _adapter_skip("skills/autonomous-mode/SKILL.md") is True
    assert _adapter_skip("skills/autonomous-mode/scripts/mode_line.py") is True


# AD-2: 大文字パスは大小文字区別により除外されない
def test_adapter_skip_uppercase_path_not_excluded():
    """_adapter_skip must be case-sensitive (fnmatchcase): an uppercase
    variant of the autonomous-mode path must NOT be excluded, matching
    should_skip's case-sensitive policy (CR-Q-004 regression guard)."""
    assert _adapter_skip("skills/AUTONOMOUS-MODE/SKILL.md") is False


# AD-3: 既存 skill は除外しない
def test_adapter_skip_does_not_exclude_existing_skills():
    """_adapter_skip must allow existing skills like brainstorm."""
    assert _adapter_skip("skills/brainstorm/SKILL.md") is False


# AD-4: should_skip 既存 挙動 を維持（OR 結合）
def test_adapter_skip_inherits_should_skip_rules():
    """_adapter_skip must preserve should_skip exclusions (reports, docs, etc)."""
    # reports are excluded by should_skip
    assert _adapter_skip("reports/plan-report-20260427-232152.md") is True
    # personal memory is excluded by should_skip
    assert _adapter_skip("memory/patterns.json") is True


# AD-5: e2e 自動回帰テスト（_write_codex_skills / _write_opencode_skills）
def test_adapter_skip_e2e_codex_excludes_autonomous_mode(tmp_path: Path):
    """_adapter_skip must exclude autonomous-mode from codex skills generation."""
    # Create minimal .claude tree with autonomous-mode and brainstorm skills
    (tmp_path / ".claude").mkdir()
    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir()

    # Create autonomous-mode skill (should be excluded from adapter outputs)
    autonomous_dir = skills_dir / "autonomous-mode"
    autonomous_dir.mkdir()
    (autonomous_dir / "SKILL.md").write_text(
        "---\ndescription: Autonomous mode\n---\n# autonomous-mode\nBody\n",
        encoding="utf-8",
    )

    # Create brainstorm skill (should be included in adapter outputs)
    brainstorm_dir = skills_dir / "brainstorm"
    brainstorm_dir.mkdir()
    (brainstorm_dir / "SKILL.md").write_text(
        "---\ndescription: Brainstorm\n---\n# brainstorm\nBody\n",
        encoding="utf-8",
    )

    # Test codex skills generation
    adapters._write_codex_skills(tmp_path, dry_run=False)
    assert not (
        tmp_path / ".agents" / "skills" / "autonomous-mode"
    ).exists(), "autonomous-mode should be excluded from codex skills"
    assert (tmp_path / ".agents" / "skills" / "brainstorm").exists(), (
        "brainstorm should be included in codex skills"
    )


# AD-6: e2e 自動回帰テスト（_write_opencode_skills）
def test_adapter_skip_e2e_opencode_excludes_autonomous_mode(tmp_path: Path):
    """_adapter_skip must exclude autonomous-mode from opencode skills generation."""
    # Create minimal .claude tree with autonomous-mode and brainstorm skills
    (tmp_path / ".claude").mkdir()
    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir()

    # Create autonomous-mode skill (should be excluded from adapter outputs)
    autonomous_dir = skills_dir / "autonomous-mode"
    autonomous_dir.mkdir()
    (autonomous_dir / "SKILL.md").write_text(
        "---\ndescription: Autonomous mode\n---\n# autonomous-mode\nBody\n",
        encoding="utf-8",
    )

    # Create brainstorm skill (should be included in adapter outputs)
    brainstorm_dir = skills_dir / "brainstorm"
    brainstorm_dir.mkdir()
    (brainstorm_dir / "SKILL.md").write_text(
        "---\ndescription: Brainstorm\n---\n# brainstorm\nBody\n",
        encoding="utf-8",
    )

    # Test opencode skills generation
    adapters._write_opencode_skills(tmp_path, dry_run=False)
    assert not (
        tmp_path / ".opencode" / "agents" / "c3-skill-autonomous-mode.md"
    ).exists(), "autonomous-mode should be excluded from opencode skills"
    assert (tmp_path / ".opencode" / "agents" / "c3-skill-brainstorm.md").exists(), (
        "brainstorm should be included in opencode skills"
    )
