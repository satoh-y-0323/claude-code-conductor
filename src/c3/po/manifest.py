"""Read and validate parallel-orchestra YAML frontmatter on plan-report files.

This module deliberately avoids PyYAML so that C3's runtime has no third-party
dependency. The parser implements the subset of YAML used by the PO 0.1 schema:

- block mappings (``key: value``)
- block sequences (``- item``)
- scalars: strings (with optional quotes), ints, floats, booleans, null
- multi-line literal scalars (``key: |``)
- nested mappings and sequences

Final validation is delegated to ``parallel-orchestra run --dry-run``; this
module performs C3-side preflight only.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)


# ---------------------------------------------------------------------------
# Topological wave decomposition
# ---------------------------------------------------------------------------


def compute_waves(frontmatter: dict) -> list[list[dict]]:
    """Group ``frontmatter['tasks']`` into topological waves.

    Wave 0 contains tasks with no ``depends_on`` references that are still
    pending. Wave N contains tasks whose ``depends_on`` are all in waves
    < N. Within each wave, tasks are independent and may execute in
    parallel.

    Wave order is deterministic: tasks within a wave are sorted by ``id``.

    Raises:
        ValueError: a cycle exists or a ``depends_on`` references an
            unknown task id.
    """
    tasks = frontmatter.get("tasks") or []
    if not isinstance(tasks, list) or not tasks:
        return []

    by_id: dict[str, dict] = {}
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("each task must be a mapping")
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("each task must have a string `id`")
        if task_id in by_id:
            raise ValueError(f"duplicate task id: {task_id!r}")
        by_id[task_id] = task

    for task in by_id.values():
        for dep in task.get("depends_on", []) or []:
            if dep not in by_id:
                raise ValueError(
                    f"task {task['id']!r} depends_on unknown id {dep!r}"
                )

    remaining = dict(by_id)
    waves: list[list[dict]] = []
    while remaining:
        ready = [
            t for t in remaining.values()
            if all(d not in remaining for d in t.get("depends_on", []) or [])
        ]
        if not ready:
            raise ValueError(
                f"cycle detected among tasks: {sorted(remaining)}"
            )
        waves.append(sorted(ready, key=lambda t: t["id"]))
        for t in ready:
            del remaining[t["id"]]
    return waves


def build_wave_manifest_text(
    frontmatter: dict, wave_index: int, *, body: str = ""
) -> str:
    """Render an ephemeral PO manifest containing only the wave's tasks.

    All ``depends_on`` references are dropped (tasks within a single wave are
    independent by construction). Top-level fields ``po_plan_version`` /
    ``name`` / ``cwd`` / ``defaults`` / ``concurrency_limits`` are preserved;
    ``on_complete`` / ``on_failure`` webhooks are dropped because they are
    plan-level (not per-wave) lifecycle hooks.

    Raises:
        IndexError: ``wave_index`` is out of range.
    """
    waves = compute_waves(frontmatter)
    if wave_index < 0 or wave_index >= len(waves):
        raise IndexError(
            f"wave_index {wave_index} out of range (have {len(waves)} waves)"
        )
    wave_tasks = waves[wave_index]

    lines: list[str] = ["---"]
    lines.append(f'po_plan_version: "{frontmatter.get("po_plan_version", "0.1")}"')
    base_name = frontmatter.get("name", "wave")
    lines.append(f"name: {_yaml_quote(f'{base_name} - wave {wave_index}')}")
    lines.append(f"cwd: {_yaml_quote(frontmatter['cwd'])}")
    defaults = frontmatter.get("defaults")
    if isinstance(defaults, dict) and defaults:
        lines.append("defaults:")
        for k, v in defaults.items():
            lines.append(f"  {k}: {_yaml_scalar(v)}")
    cgroups = frontmatter.get("concurrency_limits")
    if isinstance(cgroups, dict) and cgroups:
        lines.append("concurrency_limits:")
        for k, v in cgroups.items():
            lines.append(f"  {k}: {_yaml_scalar(v)}")
    lines.append("")
    lines.append("tasks:")
    for task in wave_tasks:
        lines.append(f"  - id: {task['id']}")
        lines.append(f"    agent: {task['agent']}")
        lines.append(f"    read_only: {'true' if task['read_only'] else 'false'}")
        prompt = task.get("prompt", "")
        if "\n" in prompt or len(prompt) > 80:
            lines.append("    prompt: |")
            for pline in prompt.rstrip("\n").splitlines():
                lines.append(f"      {pline}")
        else:
            lines.append(f"    prompt: {_yaml_quote(prompt)}")
        writes = task.get("writes")
        if isinstance(writes, list) and writes:
            lines.append("    writes:")
            for w in writes:
                lines.append(f"      - {w}")
        max_retries = task.get("max_retries")
        if isinstance(max_retries, int):
            lines.append(f"    max_retries: {max_retries}")
        cgroup = task.get("concurrency_group")
        if isinstance(cgroup, str) and cgroup:
            lines.append(f"    concurrency_group: {cgroup}")
        # Intentionally no depends_on: wave is internally independent.
    lines.append("---")
    lines.append("")
    if body:
        lines.append(body.rstrip("\n"))
    else:
        lines.append(f"# Wave {wave_index} manifest (generated by c3 po run-wave)")
    return "\n".join(lines) + "\n"


def _yaml_quote(s: str) -> str:
    if s == "":
        return '""'
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _yaml_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return _yaml_quote(v)
    raise TypeError(f"unsupported scalar type: {type(v)}")


# ---------------------------------------------------------------------------
# Frontmatter extraction
# ---------------------------------------------------------------------------


def extract_frontmatter(plan_report_path: Path) -> dict | None:
    """Return the parsed YAML frontmatter dict, or ``None`` if absent/malformed."""
    try:
        text = plan_report_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None
    body = match.group(1)
    try:
        return _parse_yaml(body)
    except _ParseError:
        return None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_manifest(plan_report_path: Path, claude_root: Path) -> list[str]:
    """Run C3-side preflight checks. Returns a list of error strings (empty = OK).

    ``claude_root`` is the directory that contains the ``.claude/`` folder
    (i.e. the project root).
    """
    errors: list[str] = []
    fm = extract_frontmatter(plan_report_path)
    if fm is None:
        return [
            f"frontmatter missing or malformed in {plan_report_path}. "
            "Re-run /start Phase C to regenerate the plan-report."
        ]

    version = fm.get("po_plan_version")
    if version != "0.1":
        errors.append(
            f"unsupported po_plan_version: {version!r} (expected '0.1')"
        )

    if not isinstance(fm.get("name"), str) or not fm["name"]:
        errors.append("`name` is required and must be a non-empty string")

    cwd = fm.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        errors.append("`cwd` is required and must be a non-empty string")

    tasks = fm.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        errors.append("`tasks` is required and must contain at least one entry")
        return errors

    seen_ids: set[str] = set()
    agents_dir = claude_root / ".claude" / "agents"
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append(f"tasks[{index}] must be a mapping")
            continue
        prefix = f"tasks[{index}]"
        task_id = task.get("id")
        if not isinstance(task_id, str) or not _ID_RE.match(task_id):
            errors.append(
                f"{prefix}.id must match [A-Za-z0-9_-]+ (got {task_id!r})"
            )
        elif task_id in seen_ids:
            errors.append(f"duplicate task id: {task_id!r}")
        else:
            seen_ids.add(task_id)

        agent = task.get("agent")
        if not isinstance(agent, str) or not agent:
            errors.append(f"{prefix}.agent is required and must be a string")
        elif not (agents_dir / f"{agent}.md").is_file():
            errors.append(
                f"{prefix}.agent {agent!r} not found at {agents_dir / f'{agent}.md'}"
            )

        if "read_only" not in task or not isinstance(task["read_only"], bool):
            errors.append(f"{prefix}.read_only is required and must be a boolean")

        prompt = task.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            errors.append(f"{prefix}.prompt is required and must be non-empty")

        depends_on = task.get("depends_on")
        if depends_on is not None and not (
            isinstance(depends_on, list)
            and all(isinstance(d, str) for d in depends_on)
        ):
            errors.append(f"{prefix}.depends_on must be a list of strings")

    return errors


_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# ---------------------------------------------------------------------------
# Minimal YAML subset parser
# ---------------------------------------------------------------------------


class _ParseError(Exception):
    pass


def _parse_yaml(text: str) -> dict:
    lines = _preprocess(text)
    value, consumed = _parse_block(lines, 0, 0)
    if consumed != len(lines):
        # Trailing content - tolerate (it could be whitespace).
        pass
    if not isinstance(value, dict):
        raise _ParseError("top-level YAML must be a mapping")
    return value


def _preprocess(text: str) -> list[tuple[int, str]]:
    """Strip blank/comment lines, return [(indent, content)]."""
    out: list[tuple[int, str]] = []
    for raw in text.splitlines():
        stripped = raw.lstrip(" ")
        indent = len(raw) - len(stripped)
        if not stripped or stripped.startswith("#"):
            continue
        out.append((indent, stripped))
    return out


def _parse_block(
    lines: list[tuple[int, str]], start: int, base_indent: int
) -> tuple[Any, int]:
    if start >= len(lines):
        return {}, start
    first_indent, first = lines[start]
    if first_indent < base_indent:
        return {}, start
    if first.startswith("- "):
        return _parse_sequence(lines, start, first_indent)
    return _parse_mapping(lines, start, first_indent)


def _parse_mapping(
    lines: list[tuple[int, str]], start: int, indent: int
) -> tuple[dict, int]:
    result: dict[str, Any] = {}
    idx = start
    while idx < len(lines):
        cur_indent, content = lines[idx]
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise _ParseError(f"unexpected indent at line {idx}: {content!r}")
        if content.startswith("- "):
            raise _ParseError(f"sequence item where mapping expected: {content!r}")
        key, sep, rest = content.partition(":")
        if not sep:
            raise _ParseError(f"missing ':' in mapping line: {content!r}")
        key = key.strip()
        rest = rest.lstrip()
        idx += 1
        if rest == "" or rest is None:
            value, idx = _parse_block(lines, idx, indent + 1)
            result[key] = value
        elif rest == "|":
            value, idx = _parse_literal(lines, idx, indent + 1)
            result[key] = value
        else:
            result[key] = _scalar(rest)
    return result, idx


def _parse_sequence(
    lines: list[tuple[int, str]], start: int, indent: int
) -> tuple[list, int]:
    result: list[Any] = []
    idx = start
    while idx < len(lines):
        cur_indent, content = lines[idx]
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise _ParseError(f"unexpected indent at line {idx}: {content!r}")
        if not content.startswith("- "):
            break
        item_body = content[2:]
        idx += 1
        if not item_body:
            value, idx = _parse_block(lines, idx, indent + 1)
            result.append(value)
            continue
        if ":" in item_body and not item_body.startswith(("'", '"')):
            # Inline first key of a mapping item, e.g. "- id: foo"
            inline_key, _, inline_rest = item_body.partition(":")
            inline_key = inline_key.strip()
            inline_rest = inline_rest.lstrip()
            mapping: dict[str, Any] = {}
            if inline_rest == "":
                # Continue parsing the mapping with deeper indent.
                deeper, idx = _parse_block(lines, idx, indent + 2)
                mapping[inline_key] = deeper if deeper != {} else None
            elif inline_rest == "|":
                value, idx = _parse_literal(lines, idx, indent + 2)
                mapping[inline_key] = value
            else:
                mapping[inline_key] = _scalar(inline_rest)
            # Parse any further mapping fields that follow at indent+2.
            while idx < len(lines):
                next_indent, next_content = lines[idx]
                if next_indent <= indent:
                    break
                if next_content.startswith("- "):
                    break
                k2, sep2, r2 = next_content.partition(":")
                if not sep2:
                    raise _ParseError(
                        f"missing ':' in mapping continuation: {next_content!r}"
                    )
                k2 = k2.strip()
                r2 = r2.lstrip()
                idx += 1
                if r2 == "":
                    val, idx = _parse_block(lines, idx, next_indent + 1)
                    mapping[k2] = val
                elif r2 == "|":
                    val, idx = _parse_literal(lines, idx, next_indent + 1)
                    mapping[k2] = val
                else:
                    mapping[k2] = _scalar(r2)
            result.append(mapping)
        else:
            result.append(_scalar(item_body))
    return result, idx


def _parse_literal(
    lines: list[tuple[int, str]], start: int, base_indent: int
) -> tuple[str, int]:
    """Parse a multi-line literal scalar (``|``)."""
    body: list[str] = []
    idx = start
    while idx < len(lines):
        cur_indent, content = lines[idx]
        if cur_indent < base_indent:
            break
        body.append(" " * (cur_indent - base_indent) + content)
        idx += 1
    return "\n".join(body), idx


def _scalar(text: str) -> Any:
    text = text.strip()
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return text[1:-1]
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1]
    lower = text.lower()
    if lower in ("true", "yes"):
        return True
    if lower in ("false", "no"):
        return False
    if lower in ("null", "~", ""):
        return None
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return text


# ---------------------------------------------------------------------------
# CLI helper
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """``python -m c3.po.manifest validate <plan-report>``."""
    args = list(argv) if argv is not None else sys.argv[1:]
    if len(args) != 2 or args[0] != "validate":
        print("usage: python -m c3.po.manifest validate <plan-report-path>", file=sys.stderr)
        return 2
    plan_report = Path(args[1]).resolve()
    if not plan_report.is_file():
        print(f"plan-report not found: {plan_report}", file=sys.stderr)
        return 2

    # Walk up to find .claude/ to locate agents/.
    from c3.paths import claude_root_for

    root = claude_root_for(plan_report.parent) or claude_root_for(Path.cwd())
    if root is None:
        print("could not locate .claude/ directory", file=sys.stderr)
        return 2

    errors = validate_manifest(plan_report, root)
    if not errors:
        return 0
    for err in errors:
        print(err, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
