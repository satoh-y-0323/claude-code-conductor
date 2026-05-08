"""Manifest loading and validation for parallel-orchestra plan files.

A manifest file is a Markdown file with a YAML frontmatter block delimited by
``---`` on the first line and a second ``---`` on a subsequent line.
"""

from __future__ import annotations

import os
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ._exceptions import ParallelOrchestraError

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

SUPPORTED_PLAN_VERSIONS: frozenset[str] = frozenset({"0.1"})

# Maximum allowed value for a single concurrency_limits entry.
MAX_CONCURRENCY_LIMIT: int = 256

# Default max_retries used when neither task nor defaults specify a value.
_DEFAULT_TASK_MAX_RETRIES: int = 1

# Known keys for the ``defaults:`` section.  Unrecognised keys are warned.
_KNOWN_DEFAULTS_KEYS: frozenset[str] = frozenset({"max_retries", "model"})

# Tier 自動ルーティング (F-005 Phase 2-A) で許容する model フロントマター値。
# Claude Code 公式仕様の短縮名のみ。
_ALLOWED_MODEL_VALUES: frozenset[str] = frozenset({"haiku", "sonnet", "opus"})

# Regular expression that defines the set of characters allowed in a task ID.
_TASK_ID_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_-]+$")

# Regular expression that defines the set of characters allowed in an agent name.
_AGENT_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_-]+$")

# Environment variable keys that are blocked for security reasons.
# PO_WORKTREE_GUARD is also blocked to prevent user override of PO internals.
_BLOCKED_ENV_KEYS: frozenset[str] = frozenset(
    {
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "PYTHONPATH",
        "PO_WORKTREE_GUARD",
    }
)

# Fields that are internally managed by PO and must not appear in task defs.
_FORBIDDEN_TASK_KEYS: frozenset[str] = frozenset(
    {
        "cwd",
        "timeout_sec",
        "idle_timeout_sec",
        "retry_delay_sec",
        "retry_backoff_factor",
    }
)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ManifestError(ParallelOrchestraError):
    """Raised when a manifest file is invalid or cannot be loaded."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    """A single agent task declared in a manifest.

    Attributes:
        id: Unique identifier for the task within the manifest.
        agent: Name of the agent that will execute this task.
        read_only: Whether the agent runs in read-only mode.
        prompt: Prompt string passed to the agent.
        env: Additional environment variables for the agent process.
        writes: Tuple of absolute, user-declared filesystem paths (as POSIX
            strings) that this task declares it will write. Used for static
            conflict detection.
        depends_on: Tuple of task IDs that must complete before this task starts.
        max_retries: Maximum number of additional attempts after the first try.
        concurrency_group: Optional group name for concurrency limiting.
    """

    id: str
    agent: str
    read_only: bool
    prompt: str
    env: dict[str, str]
    writes: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    max_retries: int = 0
    concurrency_group: str | None = None
    # F-005 Phase 2-A: model 動的切替。指定時は claude --agents JSON で
    # 起動時に上書きする。許容値は haiku / sonnet / opus（短縮名のみ）。
    model_override: str | None = None


@dataclass(frozen=True)
class Defaults:
    """Global default values applied to all tasks in a manifest.

    Attributes:
        max_retries: Default maximum number of retries for each task.
        model: Default model override applied to all tasks unless overridden
            per-task. F-005 Phase 2-A の優先順位は task.model_override >
            defaults.model > frontmatter（最後の砦）。
    """

    max_retries: int | None = None
    model: str | None = None


@dataclass(frozen=True)
class Manifest:
    """Parsed representation of a parallel-orchestra manifest file.

    Attributes:
        path: Path to the manifest file on disk.
        po_plan_version: Version string from the frontmatter.
        name: Human-readable name of the plan.
        cwd: Relative path from the manifest file's directory used to locate
            the working directory. Passed as-is to the runner.
        tasks: Ordered tuple of tasks declared in the manifest.
        defaults: Global default values for task fields. None if not specified.
        concurrency_limits: Mapping of concurrency group name to max concurrency.
    """

    path: Path
    po_plan_version: str
    name: str
    cwd: str
    tasks: tuple[Task, ...]
    defaults: Defaults | None = None
    concurrency_limits: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_non_negative_int(raw: object, task_id: str, field_name: str) -> int:
    """Parse *raw* as a non-negative integer for a task field."""
    try:
        value: int = int(raw)  # type: ignore[call-overload]
    except (TypeError, ValueError) as exc:
        raise ManifestError(
            f"Task '{task_id}': '{field_name}' must be an integer, got {raw!r}."
        ) from exc
    if value < 0:
        raise ManifestError(
            f"Task '{task_id}': '{field_name}' must be a non-negative integer,"
            f" got {value!r}."
        )
    return value


def _normalize_write_path(raw: object, task_id: str, cwd: Path) -> str:
    """Normalize a single 'writes' entry to an absolute POSIX string."""
    if not isinstance(raw, str):
        raise ManifestError(
            f"Task '{task_id}': each entry in 'writes' must be a string, "
            f"got {type(raw)!r}."
        )
    if raw == "":
        raise ManifestError(
            f"Task '{task_id}': 'writes' entry must be a non-empty string."
        )
    p = Path(raw)
    if not p.is_absolute():
        p = cwd / p
    return Path(os.path.normpath(p)).as_posix()


def _parse_defaults(raw: object) -> Defaults:
    """Parse the ``defaults:`` section of a manifest into a Defaults dataclass."""
    if not isinstance(raw, dict):
        raise ManifestError(f"'defaults' must be a YAML mapping, got {type(raw)!r}.")

    for key in raw:
        if key not in _KNOWN_DEFAULTS_KEYS:
            warnings.warn(
                f"Unknown key {key!r} in 'defaults' section will be ignored.",
                stacklevel=2,
            )

    _ctx = "defaults"
    max_retries: int | None = None
    if "max_retries" in raw:
        max_retries = _parse_non_negative_int(raw["max_retries"], _ctx, "max_retries")

    model: str | None = None
    if "model" in raw:
        model = _parse_model_value(raw["model"], _ctx, "defaults.model")

    return Defaults(max_retries=max_retries, model=model)


def _parse_model_value(raw: object, ctx: str, field_name: str) -> str:
    """Validate that a model frontmatter value is haiku/sonnet/opus.

    F-005 Phase 2-A 用。許容値以外は ManifestError。
    """
    if not isinstance(raw, str):
        raise ManifestError(
            f"{ctx}: '{field_name}' must be a string, got {type(raw)!r}."
        )
    if raw not in _ALLOWED_MODEL_VALUES:
        raise ManifestError(
            f"{ctx}: '{field_name}' must be one of "
            f"{sorted(_ALLOWED_MODEL_VALUES)}, got {raw!r}."
        )
    return raw


def _extract_frontmatter(text: str) -> str:
    """Extract the YAML frontmatter block from a manifest text."""
    lines = text.splitlines()

    if not lines or lines[0].rstrip() != "---":
        raise ManifestError(
            "Manifest frontmatter must start with '---' on the first line."
        )

    closing_index: int | None = None
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip() == "---":
            closing_index = i
            break

    if closing_index is None:
        raise ManifestError(
            "Manifest frontmatter is not closed with a second '---' delimiter."
        )

    return "\n".join(lines[1:closing_index])


def _parse_task(
    raw: object, default_cwd: Path, defaults: Defaults | None = None
) -> Task:
    """Parse a single raw task dict into a Task dataclass."""
    if not isinstance(raw, dict):
        raise ManifestError(f"Each task must be a YAML mapping, got {type(raw)!r}.")

    # Reject fields that are internally managed by PO.
    for forbidden in _FORBIDDEN_TASK_KEYS:
        if forbidden in raw:
            raise ManifestError(
                f"Task: '{forbidden}' is not allowed in parallel-orchestra"
                " (internally managed by PO)."
            )

    for key in ("id", "agent", "read_only"):
        if key not in raw:
            raise ManifestError(f"Task is missing required key: '{key}'.")

    task_id = raw["id"]
    agent = raw["agent"]
    read_only = raw["read_only"]

    if not isinstance(agent, str):
        raise ManifestError(
            f"Task '{task_id}': 'agent' must be a string, got {type(agent)!r}."
        )
    if agent and not _AGENT_PATTERN.match(agent):
        raise ManifestError(
            f"Task '{task_id}': 'agent' {agent!r} contains invalid characters. "
            "Only alphanumeric characters, hyphens, and underscores are allowed "
            "(pattern: [A-Za-z0-9_-]+)."
        )

    if not isinstance(task_id, str):
        raise ManifestError(f"Task ID must be a string, got {type(task_id)!r}.")
    if not task_id or not _TASK_ID_PATTERN.match(task_id):
        raise ManifestError(
            f"Task ID {task_id!r} contains invalid characters. "
            "Only alphanumeric characters, hyphens, and underscores are allowed "
            "(pattern: [A-Za-z0-9_-]+)."
        )

    if not isinstance(read_only, bool):
        raise ManifestError(
            f"Task '{task_id}': 'read_only' must be a boolean, got {type(read_only)!r}."
        )

    prompt: str = raw.get("prompt", f"/agent-{agent}")

    # Validate env keys against the blocklist.
    raw_env: dict[str, str] = raw.get("env", {}) or {}
    for key in raw_env:
        if key in _BLOCKED_ENV_KEYS:
            raise ManifestError(
                f"Task '{task_id}': env key '{key}' is not allowed"
                " for security reasons."
            )
    env: dict[str, str] = dict(raw_env)

    # Parse writes (relative paths resolved against default_cwd).
    raw_writes = raw.get("writes", []) or []
    if not isinstance(raw_writes, list):
        raise ManifestError(
            f"Task '{task_id}': 'writes' must be a list of strings, "
            f"got {type(raw_writes)!r}."
        )
    writes: tuple[str, ...] = tuple(
        _normalize_write_path(item, task_id, default_cwd) for item in raw_writes
    )

    raw_depends_on = raw.get("depends_on", []) or []
    if not isinstance(raw_depends_on, list):
        raise ManifestError(
            f"Task '{task_id}': 'depends_on' must be a list of strings, "
            f"got {type(raw_depends_on)!r}."
        )
    for item in raw_depends_on:
        if not isinstance(item, str):
            raise ManifestError(
                f"Task '{task_id}': each entry in 'depends_on' must be a string, "
                f"got {type(item)!r}."
            )
        if item == "":
            raise ManifestError(
                f"Task '{task_id}': 'depends_on' entry must be a non-empty string."
            )
    depends_on: tuple[str, ...] = tuple(dict.fromkeys(raw_depends_on))

    effective_max_retries = (
        raw["max_retries"]
        if "max_retries" in raw
        else (
            defaults.max_retries
            if defaults is not None and defaults.max_retries is not None
            else _DEFAULT_TASK_MAX_RETRIES
        )
    )
    max_retries: int = _parse_non_negative_int(
        effective_max_retries, task_id, "max_retries"
    )

    concurrency_group: str | None = None
    raw_concurrency_group = raw.get("concurrency_group")
    if raw_concurrency_group is not None:
        if not isinstance(raw_concurrency_group, str):
            raise ManifestError(
                f"Task '{task_id}': 'concurrency_group' must be a string,"
                f" got {type(raw_concurrency_group)!r}."
            )
        if raw_concurrency_group == "":
            raise ManifestError(
                f"Task '{task_id}': 'concurrency_group' must be a non-empty string."
            )
        concurrency_group = raw_concurrency_group

    # F-005 Phase 2-A: model 動的切替の per-task 指定を読む。
    # task に明示があれば優先、無ければ defaults.model を継承。
    model_override: str | None = None
    if "model" in raw:
        model_override = _parse_model_value(
            raw["model"], f"Task '{task_id}'", "model"
        )
    elif defaults is not None and defaults.model is not None:
        model_override = defaults.model

    return Task(
        id=task_id,
        agent=agent,
        read_only=read_only,
        prompt=prompt,
        env=env,
        writes=writes,
        depends_on=depends_on,
        max_retries=max_retries,
        concurrency_group=concurrency_group,
        model_override=model_override,
    )


def _check_duplicate_task_ids(tasks: tuple[Task, ...]) -> None:
    """Verify that every task has a unique ``id``."""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for task in tasks:
        if task.id in seen:
            duplicates.add(task.id)
        else:
            seen.add(task.id)
    if duplicates:
        sorted_ids = ", ".join(sorted(duplicates))
        raise ManifestError(f"duplicate task id(s): {sorted_ids}")


def _check_depends_on_refs(tasks: tuple[Task, ...]) -> None:
    """Verify that every depends_on reference points to an existing task ID."""
    known_ids: frozenset[str] = frozenset(t.id for t in tasks)
    undefined: set[str] = set()
    for task in tasks:
        for dep_id in task.depends_on:
            if dep_id not in known_ids:
                undefined.add(dep_id)

    if not undefined:
        return

    sorted_ids = ", ".join(sorted(undefined))
    raise ManifestError(f"depends_on references undefined task ID(s): {sorted_ids}")


def _check_cyclic_dependencies(tasks: tuple[Task, ...]) -> None:
    """Detect cyclic dependencies using DFS three-color marking."""
    adjacency: dict[str, list[str]] = {t.id: list(t.depends_on) for t in tasks}

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {task_id: WHITE for task_id in adjacency}

    for start_id in adjacency:
        if color[start_id] != WHITE:
            continue

        dfs_stack: list[tuple[str, list[str]]] = [(start_id, [start_id])]

        while dfs_stack:
            node_id, path = dfs_stack[-1]

            if color[node_id] == WHITE:
                color[node_id] = GRAY

            neighbors = adjacency.get(node_id, [])
            found_next = False
            for neighbor in neighbors:
                if color[neighbor] == GRAY:
                    cycle_start_idx = path.index(neighbor)
                    cycle_path = path[cycle_start_idx:] + [neighbor]
                    cycle_str = " -> ".join(cycle_path)
                    raise ManifestError(f"Cyclic dependency detected: {cycle_str}")
                if color[neighbor] == WHITE:
                    dfs_stack.append((neighbor, path + [neighbor]))
                    found_next = True
                    break

            if not found_next:
                color[node_id] = BLACK
                dfs_stack.pop()


def _check_writes_conflicts(tasks: tuple[Task, ...]) -> None:
    """Detect static write-conflicts across tasks."""
    claims: dict[str, list[tuple[str, str]]] = {}
    for task in tasks:
        seen_keys: set[str] = set()
        for declared in task.writes:
            try:
                key = Path(declared).resolve(strict=False).as_posix()
            except (OSError, RuntimeError) as e:
                raise ManifestError(
                    f"Task '{task.id}': symlink loop detected"
                    f" in writes path '{declared}'."
                ) from e
            if key in seen_keys:
                continue
            seen_keys.add(key)
            claims.setdefault(key, []).append((task.id, declared))

    conflicts = {k: v for k, v in claims.items() if len(v) >= 2}
    if not conflicts:
        return

    lines = ["Write-path conflict(s) detected in manifest:"]
    for key in sorted(conflicts):
        entries = sorted(conflicts[key])
        lines.append("  - tasks declaring the same write target:")
        for task_id, declared in entries:
            lines.append(f"    * {task_id}: '{declared}'")
    raise ManifestError("\n".join(lines))


def _parse_concurrency_limits(raw: object, tasks: tuple[Task, ...]) -> dict[str, int]:
    """Parse and validate the ``concurrency_limits:`` section of a manifest."""
    if raw is None:
        used_groups = {
            t.concurrency_group for t in tasks if t.concurrency_group is not None
        }
        if used_groups:
            sorted_groups = ", ".join(sorted(used_groups))
            raise ManifestError(
                f"Task(s) reference concurrency_group(s) {sorted_groups!r}"
                " but 'concurrency_limits' is not defined in the manifest."
            )
        return {}

    if not isinstance(raw, dict):
        raise ManifestError(
            f"'concurrency_limits' must be a YAML mapping, got {type(raw)!r}."
        )

    limits: dict[str, int] = {}
    for group, value in raw.items():
        if not isinstance(group, str) or group == "":
            raise ManifestError(
                f"'concurrency_limits' keys must be non-empty strings, got {group!r}."
            )
        try:
            limit = int(value)
        except (TypeError, ValueError) as exc:
            raise ManifestError(
                f"'concurrency_limits[{group!r}]' must be a positive integer,"
                f" got {type(value).__name__}: {value!r}."
            ) from exc
        if limit < 1:
            raise ManifestError(
                f"'concurrency_limits[{group!r}]' must be >= 1, got {limit!r}."
            )
        if limit > MAX_CONCURRENCY_LIMIT:
            raise ManifestError(
                f"'concurrency_limits[{group!r}]' must be <= {MAX_CONCURRENCY_LIMIT},"
                f" got {limit!r}."
            )
        limits[group] = limit

    used_groups = {
        t.concurrency_group for t in tasks if t.concurrency_group is not None
    }
    defined_groups = set(limits.keys())

    missing = used_groups - defined_groups
    if missing:
        sorted_missing = ", ".join(sorted(missing))
        raise ManifestError(
            f"Task(s) reference concurrency_group(s) {sorted_missing!r}"
            " that are not defined in 'concurrency_limits'."
        )

    unused = defined_groups - used_groups
    for group in sorted(unused):
        warnings.warn(
            f"'concurrency_limits' defines group {group!r} but no task uses it.",
            stacklevel=2,
        )

    return limits


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_manifest(path: str | Path) -> Manifest:
    """Load and validate a parallel-orchestra manifest file.

    Args:
        path: Filesystem path to the manifest (``.md``) file.

    Returns:
        A validated, frozen Manifest instance.

    Raises:
        FileNotFoundError: If the file does not exist.
        ManifestError: If the file content is structurally or semantically invalid.
    """
    resolved = Path(path).resolve()

    if not resolved.exists():
        raise FileNotFoundError(f"Manifest file not found: {resolved}")

    text = resolved.read_text(encoding="utf-8")
    frontmatter_text = _extract_frontmatter(text)

    try:
        data = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"Failed to parse YAML frontmatter: {exc}") from exc

    if not isinstance(data, dict):
        raise ManifestError("Frontmatter must be a YAML mapping.")

    # Validate po_plan_version.
    version = data.get("po_plan_version")
    if not isinstance(version, str):
        raise ManifestError(
            f"'po_plan_version' must be a string, got {type(version)!r}."
        )
    if version not in SUPPORTED_PLAN_VERSIONS:
        raise ManifestError(
            f"Unsupported po_plan_version: '{version}'. "
            f"Supported: {sorted(SUPPORTED_PLAN_VERSIONS)}."
        )

    name: str = data.get("name", "")

    # Parse required top-level cwd.
    raw_cwd = data.get("cwd")
    if raw_cwd is None:
        raise ManifestError("Manifest is missing required key: 'cwd'.")
    if not isinstance(raw_cwd, str):
        raise ManifestError(
            f"'cwd' must be a string, got {type(raw_cwd)!r}."
        )

    # Resolve default_cwd for writes path normalization (manifest directory as base).
    default_cwd = resolved.parent.resolve()

    # Parse optional ``defaults:`` section.
    defaults: Defaults | None = None
    raw_defaults = data.get("defaults")
    if raw_defaults is not None:
        defaults = _parse_defaults(raw_defaults)

    # Validate tasks.
    raw_tasks = data.get("tasks")
    if raw_tasks is None:
        raise ManifestError("Manifest is missing required key: 'tasks'.")
    if not isinstance(raw_tasks, list):
        raise ManifestError(
            f"'tasks' must be a YAML sequence (list), got {type(raw_tasks)!r}."
        )
    if len(raw_tasks) == 0:
        raise ManifestError("'tasks' must contain at least one task.")

    tasks = tuple(
        _parse_task(raw_task, default_cwd, defaults) for raw_task in raw_tasks
    )

    _check_duplicate_task_ids(tasks)
    _check_depends_on_refs(tasks)
    _check_cyclic_dependencies(tasks)
    _check_writes_conflicts(tasks)

    concurrency_limits: dict[str, int] = _parse_concurrency_limits(
        data.get("concurrency_limits"), tasks
    )

    return Manifest(
        path=resolved,
        po_plan_version=version,
        name=name,
        cwd=raw_cwd,
        tasks=tasks,
        defaults=defaults,
        concurrency_limits=concurrency_limits,
    )
