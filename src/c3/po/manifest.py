"""Read and validate parallel-orchestra YAML frontmatter on plan-report files.

Final validation is delegated to ``parallel_orchestra.load_manifest``; this
module performs C3-side preflight only (agent file existence, wave decomposition).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

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
    frontmatter: dict, wave_index: int, waves=None, *, body: str = ""
) -> str:
    """Render an ephemeral PO manifest containing only the wave's tasks.

    All ``depends_on`` references are dropped (tasks within a single wave are
    independent by construction). Top-level fields ``po_plan_version`` /
    ``name`` / ``cwd`` / ``defaults`` / ``concurrency_limits`` are preserved;
    ``on_complete`` / ``on_failure`` webhooks are dropped because they are
    plan-level (not per-wave) lifecycle hooks.

    Args:
        frontmatter: Parsed frontmatter dict from the plan-report.
        wave_index: Zero-based index of the wave to render.
        waves: Pre-computed waves list. If ``None``, ``compute_waves`` is
            called automatically. Pass this to avoid redundant computation
            when the caller already has the waves available.
        body: Optional body text to append after the closing ``---``.

    Raises:
        IndexError: ``wave_index`` is out of range.
    """
    if waves is None:
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
                lines.append(f"      - {_yaml_quote(str(w))}")
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
    """Render *s* as a YAML double-quoted scalar.

    JSON's double-quoted string syntax is a strict subset of YAML's, so we use
    :func:`json.dumps` with ``ensure_ascii=False`` to keep non-ASCII characters
    intact while letting the standard library handle ``\\`` and ``"`` escaping.
    """
    return json.dumps(s, ensure_ascii=False)


def _yaml_scalar(v: Any) -> str:
    """Render a primitive Python value as a YAML scalar."""
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
        parsed = yaml.safe_load(body)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_manifest(plan_report_path: Path, claude_root: Path) -> list[str]:
    """Run preflight checks. Returns a list of error strings (empty = OK).

    Delegates structural validation (po_plan_version / name / cwd / task fields,
    cycle detection, write conflicts, …) to ``parallel_orchestra.load_manifest``,
    then adds C3-specific checks (agent file existence under ``.claude/agents/``).

    ``claude_root`` is the directory that contains the ``.claude/`` folder
    (i.e. the project root).
    """
    from parallel_orchestra import ManifestError, load_manifest

    try:
        manifest = load_manifest(plan_report_path)
    except ManifestError as exc:
        return [str(exc)]

    agents_dir = claude_root / ".claude" / "agents"
    errors: list[str] = []
    for task in manifest.tasks:
        agent_file = agents_dir / f"{task.agent}.md"
        if not agent_file.is_file():
            errors.append(
                f"task {task.id!r}: agent {task.agent!r} not found at {agent_file}"
            )
    return errors


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
