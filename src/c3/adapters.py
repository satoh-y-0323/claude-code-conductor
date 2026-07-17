"""Generate Codex, Cursor, and OpenCode adapter files from the canonical ``.claude/`` tree."""

from __future__ import annotations

import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from c3._excludes import should_skip

# ---------------------------------------------------------------------------
# Managed-block marker constants (CR L-04: defined before helpers that use them)
# ---------------------------------------------------------------------------

MANAGED_CODEX_BEGIN = "<!-- BEGIN C3 CODEX ADAPTER -->"
MANAGED_CODEX_END = "<!-- END C3 CODEX ADAPTER -->"
MANAGED_OPENCODE_BEGIN = "<!-- BEGIN C3 OPENCODE ADAPTER -->"
MANAGED_OPENCODE_END = "<!-- END C3 OPENCODE ADAPTER -->"
MANAGED_CODEX_TOML_BEGIN = "# BEGIN C3 CODEX ADAPTER"
MANAGED_CODEX_TOML_END = "# END C3 CODEX ADAPTER"

# All Markdown managed-block delimiter strings across every adapter platform
# (SR-AI-001). Used by _sanitize_for_managed_block to strip any marker regardless
# of platform. The TOML markers (MANAGED_CODEX_TOML_BEGIN/END) are intentionally
# excluded: they delimit blocks in .codex/config.toml, never in the Markdown
# content (CLAUDE.md / rules) that _sanitize_for_managed_block operates on.
_ALL_MANAGED_MARKERS = (
    MANAGED_CODEX_BEGIN,
    MANAGED_CODEX_END,
    MANAGED_OPENCODE_BEGIN,
    MANAGED_OPENCODE_END,
)

# ---------------------------------------------------------------------------
# YAML / sanitization helpers
# ---------------------------------------------------------------------------


def _yaml_inline_scalar(value: str) -> str:
    """Return *value* as a YAML-safe inline scalar (single-line, no trailing newline).

    Uses ``yaml.safe_dump`` so that special characters (colons, double-quotes,
    newlines) are automatically quoted in a way that round-trips cleanly.
    The trailing newline and optional YAML document-end marker (``...``) that
    ``safe_dump`` appends are stripped so the result can be embedded directly
    after ``key: `` in a YAML frontmatter line.
    """
    dumped = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True)
    result = dumped.strip()
    # safe_dump may append '\n...' as a document-end marker; remove it.
    if result.endswith("\n..."):
        result = result[:-4].rstrip()
    return result


def _sanitize_for_managed_block(content: str) -> str:
    """Strip lines that could corrupt a managed block boundary.

    Removes:
    - Lines that are exactly any CODEX or OPENCODE managed-block marker string
      (``MANAGED_CODEX_BEGIN``, ``MANAGED_CODEX_END``, ``MANAGED_OPENCODE_BEGIN``,
      ``MANAGED_OPENCODE_END``).  Stripping all adapter markers prevents stray
      delimiter lines from escaping a managed block when platform=all is used
      (SR-AI-001 / CR M-03 / CR-NEW-01).
    - Lines whose first character is ``@`` (Claude Code ``@``-include directives
      such as ``@rules/promoted/index.md`` that are not meaningful to OpenCode
      and could confuse parsers).
    """
    clean_lines: list[str] = []
    for line in content.splitlines():
        if line in _ALL_MANAGED_MARKERS:
            continue
        if line.startswith("@"):
            continue
        clean_lines.append(line)
    return "\n".join(clean_lines)


@dataclass(frozen=True)
class AdapterAction:
    action: str
    path: Path


def scaffold_adapters(
    target_root: Path,
    platforms: tuple[str, ...],
    *,
    dry_run: bool = False,
) -> list[AdapterAction]:
    """Create or refresh adapter files for the requested platforms."""
    actions: list[AdapterAction] = []
    target_root = target_root.resolve()
    claude_root = target_root / ".claude"
    if not claude_root.is_dir():
        raise FileNotFoundError(f"no .claude/ directory found at {target_root}")

    if "codex" in platforms:
        actions.extend(_write_codex_adapter(target_root, dry_run=dry_run))
    if "cursor" in platforms:
        actions.extend(_write_cursor_adapter(target_root, dry_run=dry_run))
    if "opencode" in platforms:
        actions.extend(_write_opencode_adapter(target_root, dry_run=dry_run))
    return actions


def print_adapter_actions(actions: list[AdapterAction], *, dry_run: bool = False) -> None:
    if not actions:
        print("adapters up to date" if dry_run else "adapters unchanged")
        return
    suffix = "would change" if dry_run else "updated"
    print(f"{len(actions)} adapter file(s) {suffix}:")
    for action in actions:
        print(f"  {action.action}: {action.path}")


def _write_opencode_adapter(target_root: Path, *, dry_run: bool) -> list[AdapterAction]:
    """Generate the three OpenCode adapter artefacts:

    1. ``AGENTS.md`` managed block (``MANAGED_OPENCODE_BEGIN`` … ``MANAGED_OPENCODE_END``)
       containing C3 usage instructions, CLAUDE.md content, and promoted rules.
    2. ``.opencode/agents/c3-<name>.md`` — one file per agent defined in
       ``.claude/agents/*.md``.
    3. ``.opencode/agents/c3-skill-<name>.md`` — one file per skill defined in
       ``.claude/skills/<name>/SKILL.md``.
    """
    actions: list[AdapterAction] = []
    actions.extend(_write_opencode_agents_md(target_root, dry_run=dry_run))
    actions.extend(_write_opencode_agents(target_root, dry_run=dry_run))
    actions.extend(_write_opencode_skills(target_root, dry_run=dry_run))
    return actions


def _write_codex_adapter(target_root: Path, *, dry_run: bool) -> list[AdapterAction]:
    actions: list[AdapterAction] = []
    actions.extend(_write_managed_block(
        target_root / "AGENTS.md",
        _codex_agents_section(),
        MANAGED_CODEX_BEGIN,
        MANAGED_CODEX_END,
        dry_run=dry_run,
    ))
    actions.extend(_write_codex_config(target_root, dry_run=dry_run))
    actions.extend(_write_codex_skills(target_root, dry_run=dry_run))
    actions.extend(_write_codex_agents(target_root, dry_run=dry_run))
    return actions


def _write_cursor_adapter(target_root: Path, *, dry_run: bool) -> list[AdapterAction]:
    actions: list[AdapterAction] = []
    actions.extend(_write_file_if_changed(
        target_root / ".cursor" / "rules" / "c3-core.mdc",
        _cursor_core_rule(),
        dry_run=dry_run,
    ))
    actions.extend(_write_cursor_mcp(target_root, dry_run=dry_run))
    return actions


def _write_codex_skills(target_root: Path, *, dry_run: bool) -> list[AdapterAction]:
    source_root = target_root / ".claude" / "skills"
    if not source_root.is_dir():
        return []
    actions: list[AdapterAction] = []
    for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
        rel = source.relative_to(source_root)
        if should_skip(f"skills/{rel.as_posix()}"):
            continue
        dest = target_root / ".agents" / "skills" / rel
        if source.name == "SKILL.md":
            skill_name = rel.parts[0]
            text = _convert_skill(source.read_text(encoding="utf-8"), skill_name)
            actions.extend(_write_file_if_changed(dest, text, dry_run=dry_run))
        else:
            actions.extend(_copy_file_if_changed(source, dest, dry_run=dry_run))
    return actions


def _write_codex_agents(target_root: Path, *, dry_run: bool) -> list[AdapterAction]:
    source_root = target_root / ".claude" / "agents"
    if not source_root.is_dir():
        return []
    actions: list[AdapterAction] = []
    for source in sorted(path for path in source_root.glob("*.md") if path.is_file()):
        rel = source.relative_to(target_root / ".claude")
        if should_skip(rel.as_posix()):
            continue
        name = source.stem
        text = source.read_text(encoding="utf-8")
        metadata, body = _split_frontmatter(text)
        description = str(metadata.get("description") or _first_heading(body) or name)
        dest = target_root / ".codex" / "agents" / f"{name}.toml"
        actions.extend(_write_file_if_changed(
            dest,
            _codex_agent_toml(name, description, text),
            dry_run=dry_run,
        ))
    return actions


def _write_cursor_mcp(target_root: Path, *, dry_run: bool) -> list[AdapterAction]:
    path = target_root / ".cursor" / "mcp.json"
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    else:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    servers = payload.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"{path}: mcpServers must be an object")
    env = {"C3_PROJECT_ROOT": "${workspaceFolder}"}
    pythonpath = _dev_source_pythonpath()
    if pythonpath is not None:
        env["PYTHONPATH"] = str(pythonpath)
    servers["c3"] = {
        "command": sys.executable,
        "args": ["-m", "c3.mcp_server"],
        "env": env,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return _write_file_if_changed(path, text, dry_run=dry_run)


def _write_codex_config(target_root: Path, *, dry_run: bool) -> list[AdapterAction]:
    path = target_root / ".codex" / "config.toml"
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if (
            MANAGED_CODEX_TOML_BEGIN not in current
            and re.search(r"(?m)^\[mcp_servers\.c3\]\s*$", current)
        ):
            raise ValueError(
                f"{path} already defines [mcp_servers.c3] outside the C3 managed block"
            )
    return _write_managed_block(
        path,
        _codex_config_section(),
        MANAGED_CODEX_TOML_BEGIN,
        MANAGED_CODEX_TOML_END,
        dry_run=dry_run,
    )


def _write_managed_block(
    path: Path,
    section: str,
    begin: str,
    end: str,
    *,
    dry_run: bool,
) -> list[AdapterAction]:
    block = f"{begin}\n{section.rstrip()}\n{end}\n"
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if begin in current and end in current:
            text = _replace_managed_block(current, block, begin, end)
        else:
            text = current.rstrip() + "\n\n" + block
    else:
        text = block
    return _write_file_if_changed(path, text, dry_run=dry_run)


def _replace_managed_block(current: str, block: str, begin: str, end: str) -> str:
    pattern = re.compile(
        re.escape(begin) + r".*?" + re.escape(end) + r"\n?",
        flags=re.DOTALL,
    )
    return pattern.sub(lambda _match: block, current, count=1)


def _write_file_if_changed(path: Path, text: str, *, dry_run: bool) -> list[AdapterAction]:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return []
    action = "add" if not path.exists() else "update"
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return [AdapterAction(action, path)]


def _copy_file_if_changed(source: Path, dest: Path, *, dry_run: bool) -> list[AdapterAction]:
    if dest.exists() and source.read_bytes() == dest.read_bytes():
        return []
    action = "add" if not dest.exists() else "update"
    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    return [AdapterAction(action, dest)]


def _codex_agents_section() -> str:
    return """# C3 Adapter for Codex

This repository uses Claude Code Conductor (C3). The canonical C3 workflow is
kept under `.claude/`; Codex adapter files are generated from that source.

When the user asks for C3 workflows, use the generated Codex skills under
`.agents/skills/`. `$start` is the normal entry point.

When C3 instructions contain an `AskUserQuestion` JSON block:

1. Prefer the MCP tool `c3_ask_user_question` with the full JSON payload.
2. Preserve `multiSelect: true` as a multi-select question.
3. If MCP elicitation is unavailable, ask the user to run `c3 ask --file <json-file>`
   and continue from the JSON answer.

Keep C3 reports, state, and memory in `.claude/` so Claude Code, Codex, and
Cursor see the same workflow state.

On Windows, read generated C3 Markdown/JSON files with UTF-8 explicitly, for
example `Get-Content -Encoding UTF8`, to avoid mojibake in Japanese text.

When C3 instructions mention the Claude Code `Agent` tool, use Codex subagents
with the generated custom agents in `.codex/agents/`. Keep the C3 report file
contracts unchanged. When a C3 instruction mentions the Claude Code `Skill`
tool, read the matching generated Codex skill from `.agents/skills/<name>/SKILL.md`.
"""


def _codex_config_section() -> str:
    lines = [
        "[mcp_servers.c3]",
        f'command = "{_toml_escape(sys.executable)}"',
        'args = ["-m", "c3.mcp_server"]',
        "startup_timeout_sec = 10",
        "tool_timeout_sec = 600",
        "",
        "[mcp_servers.c3.env]",
        'C3_PROJECT_ROOT = "."',
    ]
    pythonpath = _dev_source_pythonpath()
    if pythonpath is not None:
        lines.append(f'PYTHONPATH = "{_toml_escape(str(pythonpath))}"')
    return "\n".join(lines) + "\n"


def _cursor_core_rule() -> str:
    return """---
description: C3 multi-agent workflow adapter for Cursor
alwaysApply: true
---

# C3 Adapter for Cursor

This repository uses Claude Code Conductor (C3). The canonical workflow,
skills, agents, reports, memory, and state are kept under `.claude/`.

Use `.claude/skills/start/SKILL.md` as the normal C3 entry point. When the user
asks for `/start`, `start`, `C3`, development workflow orchestration, task
routing, or promotion workflows, read the matching `.claude/skills/<name>/SKILL.md`
file and follow it as the source of truth.

When C3 instructions contain an `AskUserQuestion` JSON block:

1. Prefer the MCP tool `c3_ask_user_question` with the full JSON payload.
2. Preserve `multiSelect: true` as a multi-select question.
3. If MCP elicitation is unavailable, stop and ask the user to run
   `c3 ask --file <json-file>`, then continue from the JSON answer.

Do not move C3 state out of `.claude/`. Keep `.claude/reports`,
`.claude/state`, and `.claude/agent-memory` compatible with Claude Code.

When C3 instructions mention the Claude Code `Agent` tool, use Cursor's
available agent/subagent workflow if enabled. If the current Cursor runtime
cannot spawn a dedicated agent, execute the phase in the current agent while
preserving the same report filenames and approval points. When C3 instructions
mention the Claude Code `Skill` tool, read the matching file under
`.claude/skills/<name>/SKILL.md`.
"""


def _write_opencode_agents_md(target_root: Path, *, dry_run: bool) -> list[AdapterAction]:
    """Write C3 adapter instructions into AGENTS.md for OpenCode."""
    claude_root = target_root / ".claude"
    rules_content = _collect_rules_for_opencode(claude_root)
    claude_md = claude_root / "CLAUDE.md"
    claude_md_content = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
    # CR L-03: sanitization is delegated entirely to _opencode_agents_section,
    # which calls _sanitize_for_managed_block on both arguments.  Calling it
    # here a second time would be redundant (the function is idempotent), but
    # centralising the responsibility in _opencode_agents_section is cleaner.
    section = _opencode_agents_section(rules_content, claude_md_content)
    return _write_managed_block(
        target_root / "AGENTS.md",
        section,
        MANAGED_OPENCODE_BEGIN,
        MANAGED_OPENCODE_END,
        dry_run=dry_run,
    )


def _collect_rules_for_opencode(claude_root: Path) -> str:
    """Read ``.claude/rules/*.md`` files (top-level only; subdirectories are not walked).

    Intentionally non-recursive: ``rules/promoted/`` contains a managed index
    frame (``index.md``) that is nearly empty, and rglob-ing it would inject
    noise into the OpenCode managed block.

    SR-V-002: applies the same symlink guard used by ``_write_opencode_agents``
    and ``_write_opencode_skills``; files that resolve outside ``rules_dir`` are
    skipped silently.
    """
    rules_dir = claude_root / "rules"
    if not rules_dir.is_dir():
        return ""
    rules_dir_resolved = rules_dir.resolve()
    parts: list[str] = []
    for f in sorted(rules_dir.glob("*.md")):
        # SR-V-002: symlink guard — skip entries that resolve outside rules_dir.
        try:
            resolved = f.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_relative_to(rules_dir_resolved):
            continue
        content = f.read_text(encoding="utf-8").strip()
        if content:
            parts.append(f"### {f.stem}\n\n{content}")
    return "\n\n".join(parts)


def _opencode_agents_section(rules: str, claude_md: str) -> str:
    # NOTE: The @mention list below is intentionally static and lists only the
    # user-facing entry-point agents.  Internal agents such as ``wt_*`` variants
    # and ``project-setup`` are excluded because they are spawned programmatically
    # by the parallel-agents skill and are not meant to be invoked directly by
    # the user.  (CR M-01 / L-03: design-intent comment; no dynamic generation.)
    section = """# C3 Adapter for OpenCode

This repository uses Claude Code Conductor (C3). The canonical C3 workflow is
kept under `.claude/`; OpenCode adapter files are generated from that source.

## How to Use C3 with OpenCode

- `@c3-interviewer` - Start a requirements interview
- `@c3-architect` - Generate architecture design
- `@c3-planner` - Create a task plan
- `@c3-developer` - Implement code (TDD)
- `@c3-tester` - Write and run tests
- `@c3-code-reviewer` - Review code quality
- `@c3-security-reviewer` - Security review
- `@c3-doc-writer` - Generate documentation
- `@c3-systematic-debugger` - Debug complex issues

## Key Concepts

1. C3 state (reports, memory, sessions) lives in `.claude/`
2. Agents are invoked via `@mention` in OpenCode
3. C3 reports are the handoff mechanism between agents
4. User approval is required between phases

When C3 instructions contain an `AskUserQuestion` JSON block, ask the user
directly and preserve `multiSelect: true` as a multi-select question.

When C3 instructions mention the Claude Code `Agent` tool, use OpenCode
subagents via `@mention`. When they mention the `Skill` tool, read the
matching `.claude/skills/<name>/SKILL.md` file.
"""
    # Sanitize embedded content to strip @-include directives and managed-block
    # marker strings that could corrupt the OpenCode managed block boundary
    # (SR-AI-001 / CR M-03 / CR-NEW-01).
    safe_claude_md = _sanitize_for_managed_block(claude_md)
    safe_rules = _sanitize_for_managed_block(rules)
    if safe_claude_md.strip():
        section += f"\n\n## C3 Behavior Rules\n\n{safe_claude_md.strip()}"
    if safe_rules.strip():
        section += f"\n\n## C3 Injected Rules\n\n{safe_rules}"
    return section


def _write_opencode_agents(target_root: Path, *, dry_run: bool) -> list[AdapterAction]:
    """Convert .claude/agents/*.md to .opencode/agents/c3-*.md"""
    source_root = target_root / ".claude" / "agents"
    if not source_root.is_dir():
        return []
    actions: list[AdapterAction] = []
    source_root_resolved = source_root.resolve()
    for source in sorted(source_root.glob("*.md")):
        # CR M-04 / SR-NEW: mirror the should_skip guard from _write_codex_agents.
        # rel is relative to .claude/ (e.g. "agents/tdd-develop.md") to match
        # the EXCLUDE_PATTERNS convention used by _write_codex_agents.
        rel = source.relative_to(target_root / ".claude")
        if should_skip(rel.as_posix()):
            continue
        # SR-V-002: symlink guard — skip sources that resolve outside source_root.
        try:
            resolved = source.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_relative_to(source_root_resolved):
            continue
        name = source.stem
        text = source.read_text(encoding="utf-8")
        metadata, body = _split_frontmatter(text)
        description = str(metadata.get("description") or _first_heading(body) or name)
        agent_md = _opencode_agent_md(name, description, body)
        dest = target_root / ".opencode" / "agents" / f"c3-{name}.md"
        actions.extend(_write_file_if_changed(dest, agent_md, dry_run=dry_run))
    return actions


def _opencode_agent_md(name: str, description: str, body: str) -> str:
    """Generate an OpenCode agent markdown file with YAML frontmatter."""
    interactive = {"interviewer", "architect", "planner"}
    mode = "all-purpose" if name in interactive else "subagent"
    # Strip trailing whitespace from the combined description so that an empty
    # ``description`` argument does not produce a trailing space on the line
    # (CR L-01).  Pass the stripped value through _yaml_inline_scalar so that
    # colons, double-quotes, and newlines are safely quoted (CR H-01 / SR-V-001).
    full_desc = _yaml_inline_scalar(f"C3 {name} agent. {description}".strip())
    return (
        f"---\n"
        f"name: c3-{name}\n"
        f"mode: {mode}\n"
        f"description: {full_desc}\n"
        f"tools:\n"
        f"  - bash\n  - read\n  - edit\n  - write\n  - websearch\n"
        f"---\n\n"
        f"# C3 Agent: {name}\n\n"
        f"Generated from `.claude/agents/{name}.md`.\n\n"
        f"## Adapter Notes\n\n"
        f"- C3 state root: `.claude/`\n"
        f"- Reports go to `.claude/reports/`\n"
        f"- Session memory: `.claude/memory/`\n"
        f"- When done, write your output report to the appropriate path\n\n"
        f"## Original Agent Definition\n\n"
        f"{body.strip()}\n"
    )


def _write_opencode_skills(target_root: Path, *, dry_run: bool) -> list[AdapterAction]:
    """Convert .claude/skills/*/SKILL.md to .opencode/agents/c3-skill-*.md"""
    source_root = target_root / ".claude" / "skills"
    if not source_root.is_dir():
        return []
    actions: list[AdapterAction] = []
    source_root_resolved = source_root.resolve()
    for skill_dir in sorted(source_root.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue
        skill_name = skill_dir.name
        # CR M-04 / SR-NEW: mirror should_skip guard from _write_codex_skills.
        # rel is relative to .claude/ (e.g. "skills/worktree-tdd-workflow/SKILL.md").
        rel = skill_file.relative_to(target_root / ".claude")
        if should_skip(rel.as_posix()):
            continue
        # SR-V-002: symlink guard — skip files that resolve outside source_root.
        try:
            resolved = skill_file.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_relative_to(source_root_resolved):
            continue
        text = skill_file.read_text(encoding="utf-8")
        metadata, body = _split_frontmatter(text)
        description = str(metadata.get("description") or _first_heading(body) or skill_name)
        agent_md = _skill_to_opencode_agent_md(skill_name, description, body)
        dest = target_root / ".opencode" / "agents" / f"c3-skill-{skill_name}.md"
        actions.extend(_write_file_if_changed(dest, agent_md, dry_run=dry_run))
    return actions


def _skill_to_opencode_agent_md(skill_name: str, description: str, body: str) -> str:
    """Convert a C3 SKILL.md to an OpenCode agent definition."""
    # CR H-02 / L-06 / SR-V-001: use the shared YAML-safe scalar helper so that
    # double-quotes or special characters in description do not break the
    # frontmatter (previously used a raw double-quoted string literal).
    full_desc = _yaml_inline_scalar(f"C3 skill: {description}")
    return (
        f"---\n"
        f"name: c3-skill-{skill_name}\n"
        f"mode: all-purpose\n"
        f"description: {full_desc}\n"
        f"tools:\n"
        f"  - bash\n  - read\n  - edit\n  - write\n  - websearch\n"
        f"---\n\n"
        f"# C3 Skill: {skill_name}\n\n"
        f"Generated from `.claude/skills/{skill_name}/SKILL.md`.\n\n"
        f"## Adapter Notes\n\n"
        f"- This skill is the OpenCode equivalent of the C3 `/{skill_name}` command\n"
        f"- C3 state root: `.claude/`\n"
        f"- When the original references `AskUserQuestion`, ask the user directly\n"
        f"- When the original references the `Agent` tool, use `@mention`\n"
        f"- When the original references the `Skill` tool, read `.claude/skills/<name>/SKILL.md`\n\n"
        f"## Original Skill Definition\n\n"
        f"{body.strip()}\n"
    )


def _convert_skill(text: str, skill_name: str) -> str:
    metadata, body = _split_frontmatter(text)
    metadata["name"] = metadata.get("name") or skill_name
    metadata["description"] = metadata.get("description") or _first_heading(body) or skill_name
    header = yaml.safe_dump(
        metadata,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    adapter_note = f"""
## C3 Adapter Notes

This Codex skill is generated from `.claude/skills/{skill_name}/SKILL.md`.

On Windows, read C3 Markdown/JSON files with UTF-8 explicitly, for example
`Get-Content -Encoding UTF8`, to avoid mojibake in Japanese text.

When these instructions contain an `AskUserQuestion` JSON block, call the C3 MCP
tool `c3_ask_user_question` with the full JSON payload. If MCP elicitation is
unavailable, use `c3 ask --file <json-file>` as the fallback. Preserve
`multiSelect: true` as a multi-select answer.

When the source instructions mention the Claude Code `Agent` tool, use Codex
subagents and the generated custom agents under `.codex/agents/`. When they
mention the Claude Code `Skill` tool, read the matching generated skill under
`.agents/skills/<name>/SKILL.md`.
"""
    return f"---\n{header}\n---\n\n{adapter_note.strip()}\n\n{body.lstrip()}"


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    body = text[end + 5 :]
    loaded = yaml.safe_load(raw) or {}
    if not isinstance(loaded, dict):
        loaded = {}
    return loaded, body


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def _codex_agent_toml(name: str, description: str, source_text: str) -> str:
    instructions = f"""Generated from `.claude/agents/{name}.md`.

Use `.claude/` as the C3 state root. Preserve C3 report and memory paths so
Claude Code, Codex, and Cursor remain compatible.

{source_text.rstrip()}
"""
    return (
        f'name = "{_toml_escape(name)}"\n'
        f'description = "{_toml_escape(description)}"\n'
        f'developer_instructions = """{_toml_multiline_escape(instructions)}"""\n'
    )


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _toml_multiline_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


def _dev_source_pythonpath() -> Path | None:
    """Return ``<repo>/src`` when C3 is running from a source checkout.

    ``src/c3/`` ディレクトリ構造（``__file__`` の親が ``c3``、その親が ``src``）が
    前提。それ以外のディレクトリ構造の場合、または ``pyproject.toml`` が見つからない
    場合は ``None`` を返す。
    """
    here = Path(__file__).resolve()
    if here.parent.name != "c3" or here.parent.parent.name != "src":
        return None
    project_root = here.parent.parent.parent
    if not (project_root / "pyproject.toml").is_file():
        return None
    return here.parent.parent


if sys.version_info < (3, 10):  # pragma: no cover - package metadata enforces this
    raise RuntimeError("C3 requires Python 3.10+")
