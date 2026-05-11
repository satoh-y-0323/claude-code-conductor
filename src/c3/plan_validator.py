"""plan-report YAML validation & topological wave decomposition.

Used by ``c3 plan validate`` / ``c3 plan waves`` CLI commands and the
parallel-agents skill. Introduced in v1.14.0 as the PO-independent replacement
for ``c3.po.manifest`` (which had been delegating structural validation to
``parallel_orchestra.load_manifest``). See plan: atomic-foraging-sprout.

The functions here perform only pure-Python YAML validation and topological
sorting; nothing touches ``parallel_orchestra`` or the c3.db DuckDB layer.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)


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
# Topological wave decomposition
# ---------------------------------------------------------------------------


def compute_waves(frontmatter: dict) -> list[list[dict]]:
    """Group ``frontmatter['tasks']`` into topological waves.

    Wave 0 contains tasks with no ``depends_on`` references. Wave N contains
    tasks whose ``depends_on`` are all in waves < N. Within each wave, tasks
    are independent and may execute in parallel.

    Wave order is deterministic: tasks within a wave are sorted by ``id``.

    Raises:
        ValueError: a cycle exists, a ``depends_on`` references an unknown
            task id, or a task lacks a valid string id.
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


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_plan_report(plan_report_path: Path, claude_root: Path) -> list[str]:
    """Validate plan-report YAML frontmatter. Returns a list of error messages
    (empty list means OK).

    Checks performed:
    - YAML frontmatter parses successfully
    - ``po_plan_version`` field present
    - ``tasks`` is a non-empty list
    - each task has a non-empty string ``id``
    - each task has a non-empty string ``agent``
    - each task has a non-empty string ``prompt``
    - agent file exists at ``{claude_root}/.claude/agents/{agent}.md``
    - no duplicate task ids, no unknown ``depends_on`` references, no cycles
      (delegated to :func:`compute_waves`)

    Args:
        plan_report_path: Path to the plan-report ``.md`` file.
        claude_root: Project root that contains the ``.claude/`` directory.
    """
    fm = extract_frontmatter(plan_report_path)
    if fm is None:
        return ["could not parse YAML frontmatter"]

    errors: list[str] = []
    if "po_plan_version" not in fm:
        errors.append("missing required field: po_plan_version")

    tasks = fm.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        errors.append("tasks must be a non-empty list")
        return errors  # 以下のチェックが無意味なので早期 return

    agents_dir = claude_root / ".claude" / "agents"
    seen_ids: set[str] = set()
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append(f"task[{i}]: must be a mapping")
            continue
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            errors.append(f"task[{i}]: id must be a non-empty string")
            continue
        if task_id in seen_ids:
            errors.append(f"task[{i}] id={task_id!r}: duplicate id")
        seen_ids.add(task_id)
        agent = task.get("agent")
        if not isinstance(agent, str) or not agent:
            errors.append(f"task {task_id!r}: agent must be a non-empty string")
            continue
        agent_file = agents_dir / f"{agent}.md"
        if not agent_file.is_file():
            errors.append(
                f"task {task_id!r}: agent {agent!r} not found at {agent_file}"
            )
        prompt = task.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            errors.append(f"task {task_id!r}: prompt must be a non-empty string")

    # cycle / unknown depends_on / duplicate id の追加チェック（compute_waves に委譲）
    try:
        compute_waves(fm)
    except ValueError as exc:
        errors.append(str(exc))

    return errors


# ---------------------------------------------------------------------------
# Wave decomposition for skill consumption
# ---------------------------------------------------------------------------


def split_waves(plan_report_path: Path) -> dict:
    """Compute waves and return a JSON-friendly dict for the parallel-agents skill.

    Returns:
        ``{"waves": [{"index": int, "tasks": [{...}, ...]}, ...]}``。
        Each task dict contains keys: ``id`` / ``agent`` / ``read_only`` /
        ``writes`` / ``prompt``。

    Raises:
        ValueError: YAML frontmatter is malformed, or a cycle / unknown
            depends_on reference is present.
    """
    fm = extract_frontmatter(plan_report_path)
    if fm is None:
        raise ValueError("could not parse YAML frontmatter")
    waves = compute_waves(fm)
    return {
        "waves": [
            {
                "index": index,
                "tasks": [
                    {
                        "id": task["id"],
                        "agent": task.get("agent"),
                        "read_only": task.get("read_only"),
                        "writes": task.get("writes") or [],
                        "prompt": task.get("prompt", ""),
                    }
                    for task in wave_tasks
                ],
            }
            for index, wave_tasks in enumerate(waves)
        ]
    }
