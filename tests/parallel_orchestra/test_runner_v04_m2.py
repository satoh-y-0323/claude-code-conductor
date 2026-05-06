"""Tests for parallel_orchestra.runner v0.4 M2 — merge functions."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from parallel_orchestra.manifest import load_manifest
from parallel_orchestra.runner import (
    MergeResult,
    RunnerError,
    RunResult,
    TaskResult,
    run_manifest,
)


def _make_manifest(tmp_path: Path, content: str):
    p = tmp_path / "manifest.md"
    p.write_text(content, encoding="utf-8")
    return load_manifest(p)


def _make_task_result(
    task_id: str,
    *,
    returncode: int = 0,
    timed_out: bool = False,
    skipped: bool = False,
    branch_name: str | None = None,
) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        agent="developer",
        returncode=returncode,
        stdout="",
        stderr="",
        timed_out=timed_out,
        duration_sec=0.1,
        skipped=skipped,
        branch_name=branch_name,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    import subprocess as sp

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", str(repo)], check=True, capture_output=True)
    sp.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo), check=True, capture_output=True,
    )
    sp.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo), check=True, capture_output=True,
    )
    (repo / "README.md").write_text("init", encoding="utf-8")
    sp.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    sp.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)
    return repo


# ---------------------------------------------------------------------------
# _resolve_merge_base_branch
# ---------------------------------------------------------------------------


def test_resolve_merge_base_branch_通常ブランチ名を返す(git_repo):
    import parallel_orchestra.runner as runner_module
    resolve = getattr(runner_module, "_resolve_merge_base_branch", None)
    assert resolve is not None

    branch = resolve(git_repo)
    assert isinstance(branch, str)
    assert len(branch) > 0


def test_resolve_merge_base_branch_monkeypatch_で特定ブランチ名を返す(tmp_path, monkeypatch):
    import parallel_orchestra.runner as runner_module
    resolve = getattr(runner_module, "_resolve_merge_base_branch", None)
    assert resolve is not None

    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = "feature/my-branch\n"

    def fake_run(cmd, **kwargs):
        if "symbolic-ref" in cmd:
            return fake_result
        return subprocess.run(cmd, **kwargs)

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    branch = resolve(tmp_path)
    assert branch == "feature/my-branch"


def test_resolve_merge_base_branch_detached_HEADでRunnerError(tmp_path, monkeypatch):
    import parallel_orchestra.runner as runner_module
    resolve = getattr(runner_module, "_resolve_merge_base_branch", None)
    assert resolve is not None

    fake_result = MagicMock()
    fake_result.returncode = 128
    fake_result.stdout = ""

    def fake_run(cmd, **kwargs):
        if "symbolic-ref" in cmd:
            return fake_result
        return subprocess.run(cmd, **kwargs)

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    with pytest.raises(RunnerError, match="detached"):
        resolve(tmp_path)


def test_resolve_merge_base_branch_エラーメッセージにparallel_orchestraが含まれる(tmp_path, monkeypatch):
    """Error message should reference parallel-orchestra, not clade-parallel."""
    import parallel_orchestra.runner as runner_module
    resolve = getattr(runner_module, "_resolve_merge_base_branch", None)
    assert resolve is not None

    fake_result = MagicMock()
    fake_result.returncode = 128
    fake_result.stdout = ""

    def fake_run(cmd, **kwargs):
        if "symbolic-ref" in cmd:
            return fake_result
        return subprocess.run(cmd, **kwargs)

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    with pytest.raises(RunnerError) as exc_info:
        resolve(tmp_path)

    assert "parallel-orchestra" in str(exc_info.value).lower() or "detached" in str(exc_info.value)


# ---------------------------------------------------------------------------
# _merge_single_branch
# ---------------------------------------------------------------------------


def test_merge_single_branch_成功時statusがmergedになる(tmp_path, monkeypatch):
    import parallel_orchestra.runner as runner_module
    merge_single = getattr(runner_module, "_merge_single_branch", None)
    assert merge_single is not None

    fake_ok = MagicMock()
    fake_ok.returncode = 0
    fake_ok.stdout = ""
    fake_ok.stderr = ""

    monkeypatch.setattr(runner_module.subprocess, "run", lambda *a, **kw: fake_ok)
    monkeypatch.setattr(runner_module, "_delete_branch", lambda *a: None)
    monkeypatch.setattr(runner_module, "_abort_merge", lambda *a: None)

    result = merge_single(tmp_path, "main", "task-a", "parallel-orchestra/task-a-abcd1234")
    assert result.status == "merged"


def test_merge_single_branch_コンフリクト時statusがconflictになりabortされる(
    tmp_path, monkeypatch
):
    import parallel_orchestra.runner as runner_module
    merge_single = getattr(runner_module, "_merge_single_branch", None)
    assert merge_single is not None

    fake_conflict = MagicMock()
    fake_conflict.returncode = 1
    fake_conflict.stdout = ""
    fake_conflict.stderr = "CONFLICT (content): Merge conflict in foo.py"

    abort_called = [False]

    monkeypatch.setattr(runner_module.subprocess, "run", lambda *a, **kw: fake_conflict)
    monkeypatch.setattr(
        runner_module, "_abort_merge",
        lambda *a: abort_called.__setitem__(0, True)
    )
    monkeypatch.setattr(runner_module, "_delete_branch", lambda *a: None)

    result = merge_single(tmp_path, "main", "task-a", "parallel-orchestra/task-a-abcd1234")
    assert result.status == "conflict"
    assert abort_called[0]


def test_merge_single_branch_TimeoutExpired時statusがerrorになる(tmp_path, monkeypatch):
    import parallel_orchestra.runner as runner_module
    merge_single = getattr(runner_module, "_merge_single_branch", None)
    assert merge_single is not None

    def raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired("git", 30)

    monkeypatch.setattr(runner_module.subprocess, "run", raise_timeout)
    monkeypatch.setattr(runner_module, "_abort_merge", lambda *a: None)
    monkeypatch.setattr(runner_module, "_delete_branch", lambda *a: None)

    result = merge_single(tmp_path, "main", "task-a", "parallel-orchestra/task-a-abcd1234")
    assert result.status == "error"


# ---------------------------------------------------------------------------
# _merge_write_branches
# ---------------------------------------------------------------------------


def test_merge_write_branches_成功タスクのみマージする(tmp_path, monkeypatch):
    import parallel_orchestra.runner as runner_module
    merge_write = getattr(runner_module, "_merge_write_branches", None)
    assert merge_write is not None

    merged: list[str] = []

    def fake_merge_single(cwd, base, task_id, branch, **kwargs):
        merged.append(branch)
        return MergeResult(task_id=task_id, branch_name=branch, status="merged", stderr="")

    monkeypatch.setattr(runner_module, "_merge_single_branch", fake_merge_single)

    results = (
        _make_task_result("task-a", branch_name="parallel-orchestra/task-a-00000001"),
        _make_task_result("task-b", returncode=1),  # failed, no branch
        _make_task_result("task-c", branch_name="parallel-orchestra/task-c-00000002"),
        _make_task_result("task-d", skipped=True),  # skipped, no branch
    )

    merge_results = merge_write(tmp_path, "main", results)

    assert len(merge_results) == 2
    assert "parallel-orchestra/task-a-00000001" in merged
    assert "parallel-orchestra/task-c-00000002" in merged


def test_merge_write_branches_コンフリクト時にRunnerErrorでfail_fast(tmp_path, monkeypatch):
    import parallel_orchestra.runner as runner_module
    merge_write = getattr(runner_module, "_merge_write_branches", None)
    assert merge_write is not None

    call_count = [0]

    def fake_merge_single(cwd, base, task_id, branch, **kwargs):
        call_count[0] += 1
        if task_id == "task-a":
            return MergeResult(task_id=task_id, branch_name=branch, status="conflict", stderr="conflict!")
        return MergeResult(task_id=task_id, branch_name=branch, status="merged", stderr="")

    monkeypatch.setattr(runner_module, "_merge_single_branch", fake_merge_single)

    results = (
        _make_task_result("task-a", branch_name="parallel-orchestra/task-a-00000001"),
        _make_task_result("task-b", branch_name="parallel-orchestra/task-b-00000002"),
    )

    with pytest.raises(RunnerError):
        merge_write(tmp_path, "main", results)

    # Only task-a should have been attempted (fail-fast)
    assert call_count[0] == 1


# ---------------------------------------------------------------------------
# _build_conflict_message
# ---------------------------------------------------------------------------


def test_build_conflict_message_タスクIDとブランチ名が含まれる(tmp_path):
    import parallel_orchestra.runner as runner_module
    build_msg = getattr(runner_module, "_build_conflict_message", None)
    assert build_msg is not None

    conflict = MergeResult(
        task_id="my-task",
        branch_name="parallel-orchestra/my-task-abcd1234",
        status="conflict",
        stderr="CONFLICT in foo.py",
    )
    msg = build_msg(conflict, ["parallel-orchestra/other-abcd5678"])

    assert "my-task" in msg
    assert "parallel-orchestra/my-task-abcd1234" in msg


# ---------------------------------------------------------------------------
# read_only-only manifest: no merge step
# ---------------------------------------------------------------------------


def test_run_manifest_全read_onlyマニフェストはマージステップをスキップする(
    fake_claude_runner, tmp_path
):
    content = """\
---
po_plan_version: "0.1"
name: readonly-only
cwd: "."
tasks:
  - id: review
    agent: code-reviewer
    read_only: true
---
"""
    outcomes = [{"returncode": 0, "stdout": "ok", "stderr": ""}]
    fake_claude_runner(outcomes)

    manifest = _make_manifest(tmp_path, content)
    result = run_manifest(manifest, log_enabled=False, dashboard_enabled=False)

    assert result.merge_results == ()
    assert all(r.ok for r in result.results)
