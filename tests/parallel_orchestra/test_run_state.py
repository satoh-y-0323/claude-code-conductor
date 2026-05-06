"""Tests for parallel_orchestra.run_state module.

State file name format: .po-run-state-<stem>.json
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import pytest

from parallel_orchestra.run_state import (
    RunState,
    create_run_state,
    delete_run_state,
    load_run_state,
    mark_task_completed,
    state_file_exists,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SINGLE_TASK_CONTENT = """\
---
po_plan_version: "0.1"
name: run-state-test
cwd: "."
tasks:
  - id: only-task
    agent: code-reviewer
    read_only: true
---
"""

TWO_TASKS_CONTENT = """\
---
po_plan_version: "0.1"
name: run-state-two-tasks
cwd: "."
tasks:
  - id: task-a
    agent: code-reviewer
    read_only: true
  - id: task-b
    agent: security-reviewer
    read_only: true
---
"""


def _state_path(manifest_path: Path) -> Path:
    """Return the expected state file path for *manifest_path*."""
    stem = manifest_path.stem
    return manifest_path.parent / f".po-run-state-{stem}.json"


# ---------------------------------------------------------------------------
# create_run_state
# ---------------------------------------------------------------------------


def test_create_run_state_creates_file(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(SINGLE_TASK_CONTENT, encoding="utf-8")

    create_run_state(manifest_path)

    expected = _state_path(manifest_path)
    assert expected.exists(), f"Expected state file at {expected}"


def test_create_run_state_manifest_hash_is_correct(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(SINGLE_TASK_CONTENT, encoding="utf-8")

    state = create_run_state(manifest_path)

    expected_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert state.manifest_hash == expected_hash


def test_create_run_state_returns_empty_completed_tasks(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(SINGLE_TASK_CONTENT, encoding="utf-8")

    state = create_run_state(manifest_path)

    assert state.completed_tasks == set()


def test_create_run_state_overwrites_existing(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(SINGLE_TASK_CONTENT, encoding="utf-8")

    state1 = create_run_state(manifest_path)
    mark_task_completed(state1, "only-task", manifest_path)

    raw = json.loads(_state_path(manifest_path).read_text(encoding="utf-8"))
    assert "only-task" in raw["completed_tasks"]

    state2 = create_run_state(manifest_path)
    assert state2.completed_tasks == set()

    raw2 = json.loads(_state_path(manifest_path).read_text(encoding="utf-8"))
    assert raw2["completed_tasks"] == []


# ---------------------------------------------------------------------------
# load_run_state
# ---------------------------------------------------------------------------


def test_load_run_state_returns_none_when_absent(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(SINGLE_TASK_CONTENT, encoding="utf-8")

    result = load_run_state(manifest_path)

    assert result is None


def test_load_run_state_returns_none_on_hash_mismatch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(SINGLE_TASK_CONTENT, encoding="utf-8")
    create_run_state(manifest_path)

    manifest_path.write_text(SINGLE_TASK_CONTENT + "\n# changed", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="parallel_orchestra.run_state"):
        result = load_run_state(manifest_path)

    assert result is None
    assert any("hash mismatch" in r.message for r in caplog.records)


def test_load_run_state_returns_none_on_malformed_json(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(SINGLE_TASK_CONTENT, encoding="utf-8")

    _state_path(manifest_path).write_text("{ not valid json }", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="parallel_orchestra.run_state"):
        result = load_run_state(manifest_path)

    assert result is None
    assert any("failed to parse" in r.message for r in caplog.records)


def test_load_run_state_returns_none_on_missing_field(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(SINGLE_TASK_CONTENT, encoding="utf-8")

    _state_path(manifest_path).write_text(
        json.dumps({"completed_tasks": []}), encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING, logger="parallel_orchestra.run_state"):
        result = load_run_state(manifest_path)

    assert result is None
    assert any(
        "malformed" in r.message or "Falling back" in r.message
        for r in caplog.records
    )


def test_load_run_state_returns_none_on_non_dict_json(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(SINGLE_TASK_CONTENT, encoding="utf-8")

    _state_path(manifest_path).write_text(
        json.dumps(["not", "a", "dict"]), encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING, logger="parallel_orchestra.run_state"):
        result = load_run_state(manifest_path)

    assert result is None
    assert any("malformed" in r.message for r in caplog.records)


def test_load_run_state_restores_completed_tasks(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(TWO_TASKS_CONTENT, encoding="utf-8")

    state = create_run_state(manifest_path)
    mark_task_completed(state, "task-a", manifest_path)
    mark_task_completed(state, "task-b", manifest_path)

    loaded = load_run_state(manifest_path)

    assert loaded is not None
    assert "task-a" in loaded.completed_tasks
    assert "task-b" in loaded.completed_tasks


# ---------------------------------------------------------------------------
# mark_task_completed
# ---------------------------------------------------------------------------


def test_mark_task_completed_adds_to_set(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(SINGLE_TASK_CONTENT, encoding="utf-8")
    state = create_run_state(manifest_path)

    mark_task_completed(state, "only-task", manifest_path)

    assert "only-task" in state.completed_tasks


def test_mark_task_completed_persists_to_file(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(SINGLE_TASK_CONTENT, encoding="utf-8")
    state = create_run_state(manifest_path)

    mark_task_completed(state, "only-task", manifest_path)

    raw = json.loads(_state_path(manifest_path).read_text(encoding="utf-8"))
    assert "only-task" in raw["completed_tasks"]


# ---------------------------------------------------------------------------
# delete_run_state
# ---------------------------------------------------------------------------


def test_delete_run_state_removes_file(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(SINGLE_TASK_CONTENT, encoding="utf-8")
    create_run_state(manifest_path)

    assert _state_path(manifest_path).exists()

    delete_run_state(manifest_path)

    assert not _state_path(manifest_path).exists()


def test_delete_run_state_noop_when_absent(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(SINGLE_TASK_CONTENT, encoding="utf-8")

    try:
        delete_run_state(manifest_path)
    except Exception as exc:
        pytest.fail(f"delete_run_state must not raise when file is absent: {exc!r}")


# ---------------------------------------------------------------------------
# state_file_exists
# ---------------------------------------------------------------------------


def test_state_file_exists_returns_false_when_absent(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(SINGLE_TASK_CONTENT, encoding="utf-8")

    assert state_file_exists(manifest_path) is False


def test_state_file_exists_returns_true_after_create(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(SINGLE_TASK_CONTENT, encoding="utf-8")
    create_run_state(manifest_path)

    assert state_file_exists(manifest_path) is True


# ---------------------------------------------------------------------------
# State file naming: .po-run-state-<stem>.json
# ---------------------------------------------------------------------------


def test_state_file_uses_po_prefix(tmp_path: Path) -> None:
    """State file must use .po-run-state- prefix (not .clade-run-state-)."""
    manifest_path = tmp_path / "my-plan.md"
    manifest_path.write_text(SINGLE_TASK_CONTENT, encoding="utf-8")

    create_run_state(manifest_path)

    expected = tmp_path / ".po-run-state-my-plan.json"
    assert expected.exists(), f"Expected .po-run-state-my-plan.json, not found"
    old_path = tmp_path / ".clade-run-state-my-plan.json"
    assert not old_path.exists(), "Must not use old .clade-run-state- prefix"


def test_different_manifests_use_different_state_files(tmp_path: Path) -> None:
    manifest_a = tmp_path / "plan-a.md"
    manifest_b = tmp_path / "plan-b.md"
    manifest_a.write_text(SINGLE_TASK_CONTENT, encoding="utf-8")
    manifest_b.write_text(SINGLE_TASK_CONTENT, encoding="utf-8")

    state_a = create_run_state(manifest_a)
    create_run_state(manifest_b)

    path_a = _state_path(manifest_a)
    path_b = _state_path(manifest_b)

    assert path_a != path_b
    assert path_a.exists()
    assert path_b.exists()

    mark_task_completed(state_a, "only-task", manifest_a)
    loaded_b = load_run_state(manifest_b)
    assert loaded_b is not None
    assert "only-task" not in loaded_b.completed_tasks
