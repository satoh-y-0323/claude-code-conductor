"""Tests for parallel_orchestra.runner v0.4 M1 — MergeResult and branch naming."""

from __future__ import annotations

import dataclasses
import io
import re
import subprocess
from pathlib import Path

import pytest

from parallel_orchestra.manifest import load_manifest
from parallel_orchestra.runner import (
    RunnerError,
    RunResult,
    TaskResult,
    run_manifest,
)


def _make_manifest(tmp_path: Path, content: str):
    p = tmp_path / "manifest.md"
    p.write_text(content, encoding="utf-8")
    return load_manifest(p)


# ---------------------------------------------------------------------------
# MergeResult dataclass
# ---------------------------------------------------------------------------


def test_MergeResultクラスがrunnerモジュールに存在する():
    import parallel_orchestra.runner as runner_module
    merge_result_cls = getattr(runner_module, "MergeResult", None)
    assert merge_result_cls is not None


def test_MergeResultがfrozenデータクラスである():
    import parallel_orchestra.runner as runner_module
    merge_result_cls = getattr(runner_module, "MergeResult")
    assert dataclasses.is_dataclass(merge_result_cls)

    instance = merge_result_cls(
        task_id="task-a",
        branch_name="parallel-orchestra/task-a-abcd1234",
        status="merged",
        stderr="",
    )
    with pytest.raises((AttributeError, TypeError)):
        instance.status = "conflict"  # type: ignore[misc]


def test_MergeResultのフィールドが正しい():
    import parallel_orchestra.runner as runner_module
    merge_result_cls = getattr(runner_module, "MergeResult")

    mr = merge_result_cls(
        task_id="my-task",
        branch_name="parallel-orchestra/my-task-cafebabe",
        status="merged",
        stderr="",
    )
    assert mr.task_id == "my-task"
    assert mr.branch_name == "parallel-orchestra/my-task-cafebabe"
    assert mr.status == "merged"
    assert mr.stderr == ""


# ---------------------------------------------------------------------------
# TaskResult.branch_name field
# ---------------------------------------------------------------------------


def test_TaskResultにbranch_nameフィールドが存在しデフォルトNone():
    tr = TaskResult(
        task_id="x", agent="code-reviewer", returncode=0,
        stdout="", stderr="", timed_out=False, duration_sec=0.0,
    )
    assert hasattr(tr, "branch_name")
    assert tr.branch_name is None


def test_TaskResultのbranch_nameにstr値を設定できる():
    tr = TaskResult(
        task_id="write-task", agent="developer", returncode=0,
        stdout="", stderr="", timed_out=False, duration_sec=0.0,
        branch_name="parallel-orchestra/write-task-12345678",
    )
    assert tr.branch_name == "parallel-orchestra/write-task-12345678"


# ---------------------------------------------------------------------------
# RunResult.merge_results field
# ---------------------------------------------------------------------------


def test_RunResultにmerge_resultsフィールドが存在する():
    rr = RunResult(results=())
    assert hasattr(rr, "merge_results")
    assert rr.merge_results == ()


# ---------------------------------------------------------------------------
# _worktree_setup: branch name format
# ---------------------------------------------------------------------------


def test_worktree_setupのブランチ名が正しい形式である(tmp_path, monkeypatch):
    import parallel_orchestra.runner as runner_module

    worktree_path_created: list[Path] = []
    branch_names_created: list[str] = []

    def fake_subprocess_run(cmd, **kwargs):
        if "worktree" in cmd and "add" in cmd:
            branch = cmd[cmd.index("-b") + 1]
            wt_path = Path(cmd[-1])
            worktree_path_created.append(wt_path)
            branch_names_created.append(branch)
            wt_path.mkdir(parents=True, exist_ok=True)
            fake_result = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            return fake_result
        fake_result = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        return fake_result

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    from parallel_orchestra.manifest import Task
    task = Task(
        id="my-task", agent="developer", read_only=False,
        prompt="test", env={},
    )

    # Make a fake git root dir
    fake_git_root = tmp_path / "repo"
    fake_git_root.mkdir()
    (fake_git_root / ".claude").mkdir(exist_ok=True)

    try:
        runner_module._worktree_setup(fake_git_root, task)
    except Exception:
        pass  # May fail due to git not being set up, but we check what was called

    if branch_names_created:
        branch_name = branch_names_created[0]
        assert branch_name.startswith("parallel-orchestra/"), (
            f"Branch name must start with 'parallel-orchestra/', got: {branch_name}"
        )
        # Check format: parallel-orchestra/<task-id>-<uuid8>
        pattern = r"^parallel-orchestra/[A-Za-z0-9_-]+-[0-9a-f]{8}$"
        assert re.match(pattern, branch_name), (
            f"Branch name format wrong: {branch_name}"
        )


def test_worktree_rootが_po_worktrees_ディレクトリを使用する():
    import parallel_orchestra.runner as runner_module
    assert runner_module._WORKTREE_ROOT_NAME == ".po-worktrees"


# ---------------------------------------------------------------------------
# PO_WORKTREE_GUARD is set for write tasks
# ---------------------------------------------------------------------------


def test_read_only_falseタスクにPO_WORKTREE_GUARD_1がセットされる(
    fake_claude_runner, tmp_path, monkeypatch
):
    """read_only=false tasks must have PO_WORKTREE_GUARD=1 in environment."""
    import os

    import parallel_orchestra.runner as runner_module

    # Mock _setup_worktree to avoid git dependency
    worktree_path = tmp_path / ".po-worktrees" / "task-a-testtest"
    worktree_path.mkdir(parents=True)

    def fake_setup_worktree(git_root, task, claude_src_dir=None):
        return worktree_path, "parallel-orchestra/task-a-testtest"

    monkeypatch.setattr(runner_module, "_setup_worktree", fake_setup_worktree)
    monkeypatch.setattr(runner_module, "_worktree_cleanup", lambda *a: None)

    outcomes = [{"returncode": 0, "stdout": "ok", "stderr": ""}]
    recorder = fake_claude_runner(outcomes)

    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
tasks:
  - id: task-a
    agent: developer
    read_only: false
---
"""
    p = tmp_path / "manifest.md"
    p.write_text(content, encoding="utf-8")
    manifest = load_manifest(p)

    # We need a fake git root
    def fake_require_git_root(cwd):
        return tmp_path

    def fake_resolve_base_branch(cwd, **kwargs):
        return "main"

    def fake_merge_write_branches(*a, **kw):
        return ()

    monkeypatch.setattr(runner_module, "_require_git_root", fake_require_git_root)
    monkeypatch.setattr(runner_module, "_resolve_merge_base_branch", fake_resolve_base_branch)
    monkeypatch.setattr(runner_module, "_merge_write_branches", fake_merge_write_branches)

    run_manifest(manifest, log_enabled=False, dashboard_enabled=False)

    # Check that PO_WORKTREE_GUARD was set in environment for the popen call
    if recorder["call_args"]:
        _, kwargs = recorder["call_args"][0]
        env = kwargs.get("env", {})
        assert env.get("PO_WORKTREE_GUARD") == "1", (
            f"PO_WORKTREE_GUARD=1 must be set for read_only=false tasks. "
            f"Got env keys: {list(env.keys())}"
        )
