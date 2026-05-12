"""Generate Codex and Cursor adapter files from the canonical ``.claude/`` tree."""

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

MANAGED_CODEX_BEGIN = "<!-- BEGIN C3 CODEX ADAPTER -->"
MANAGED_CODEX_END = "<!-- END C3 CODEX ADAPTER -->"
MANAGED_CODEX_TOML_BEGIN = "# BEGIN C3 CODEX ADAPTER"
MANAGED_CODEX_TOML_END = "# END C3 CODEX ADAPTER"


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
    return actions


def print_adapter_actions(actions: list[AdapterAction], *, dry_run: bool = False) -> None:
    if not actions:
        print("adapters up to date" if dry_run else "adapters unchanged")
        return
    suffix = "would change" if dry_run else "updated"
    print(f"{len(actions)} adapter file(s) {suffix}:")
    for action in actions:
        print(f"  {action.action}: {action.path}")


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
        "command": "python",
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
        'command = "python"',
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
    """Return ``<repo>/src`` when C3 is running from a source checkout."""
    here = Path(__file__).resolve()
    if here.parent.name != "c3" or here.parent.parent.name != "src":
        return None
    project_root = here.parent.parent.parent
    if not (project_root / "pyproject.toml").is_file():
        return None
    return here.parent.parent


if sys.version_info < (3, 10):  # pragma: no cover - package metadata enforces this
    raise RuntimeError("C3 requires Python 3.10+")
