"""Tests for parallel_orchestra.runner module — _DependencyScheduler and depends_on."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from parallel_orchestra.manifest import load_manifest
from parallel_orchestra.runner import (
    RunnerError,
    RunResult,
    TaskResult,
    run_manifest,
)

# ---------------------------------------------------------------------------
# Manifest content helpers
# ---------------------------------------------------------------------------

THREE_INDEPENDENT = """\
---
po_plan_version: "0.1"
name: three-independent
cwd: "."
tasks:
  - id: task-a
    agent: code-reviewer
    read_only: true
  - id: task-b
    agent: security-reviewer
    read_only: true
  - id: task-c
    agent: developer
    read_only: true
---
"""

LINEAR_AB = """\
---
po_plan_version: "0.1"
name: linear-ab
cwd: "."
tasks:
  - id: task-a
    agent: code-reviewer
    read_only: true
    max_retries: 0
  - id: task-b
    agent: security-reviewer
    read_only: true
    max_retries: 0
    depends_on:
      - task-a
---
"""

Y_SHAPE = """\
---
po_plan_version: "0.1"
name: y-shape
cwd: "."
tasks:
  - id: task-a
    agent: code-reviewer
    read_only: true
  - id: task-b
    agent: security-reviewer
    read_only: true
  - id: task-c
    agent: developer
    read_only: true
    depends_on:
      - task-a
      - task-b
---
"""

DIAMOND = """\
---
po_plan_version: "0.1"
name: diamond
cwd: "."
tasks:
  - id: task-a
    agent: code-reviewer
    read_only: true
  - id: task-b
    agent: security-reviewer
    read_only: true
    depends_on:
      - task-a
  - id: task-c
    agent: developer
    read_only: true
    depends_on:
      - task-a
  - id: task-d
    agent: interviewer
    read_only: true
    depends_on:
      - task-b
      - task-c
---
"""

FAIL_WITH_DOWNSTREAM_AND_INDEPENDENT = """\
---
po_plan_version: "0.1"
name: fail-propagation
cwd: "."
tasks:
  - id: task-a
    agent: code-reviewer
    read_only: true
  - id: task-b
    agent: security-reviewer
    read_only: true
    depends_on:
      - task-a
  - id: task-d
    agent: developer
    read_only: true
---
"""

TRANSITIVE_SKIP = """\
---
po_plan_version: "0.1"
name: transitive-skip
cwd: "."
tasks:
  - id: task-a
    agent: code-reviewer
    read_only: true
    max_retries: 0
  - id: task-b
    agent: security-reviewer
    read_only: true
    max_retries: 0
    depends_on:
      - task-a
  - id: task-c
    agent: developer
    read_only: true
    max_retries: 0
    depends_on:
      - task-b
---
"""

TWO_READONLY_TASKS = """\
---
po_plan_version: "0.1"
name: all-readonly
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


def _make_manifest(tmp_path: Path, content: str) -> Any:
    p = tmp_path / "manifest.md"
    p.write_text(content, encoding="utf-8")
    return load_manifest(p)


# ---------------------------------------------------------------------------
# _DependencyScheduler class exists
# ---------------------------------------------------------------------------


def test_DependencySchedulerクラスが存在する():
    import parallel_orchestra.runner as runner_module
    scheduler_cls = getattr(runner_module, "_DependencyScheduler")
    assert scheduler_cls is not None


# ---------------------------------------------------------------------------
# Parallel execution
# ---------------------------------------------------------------------------


def test_DependencyScheduler_依存なし3タスクが並列で走る(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    import parallel_orchestra.runner as runner_module

    scheduler_cls = getattr(runner_module, "_DependencyScheduler")
    manifest = _make_manifest(tmp_path, THREE_INDEPENDENT)
    tasks = manifest.tasks

    thread_ids: list[int] = []
    lock = threading.Lock()

    def fake_execute(task: Any) -> TaskResult:
        time.sleep(0.05)
        with lock:
            thread_ids.append(threading.get_ident())
        return TaskResult(
            task_id=task.id, agent=task.agent, returncode=0,
            stdout="", stderr="", timed_out=False, duration_sec=0.05,
        )

    with ThreadPoolExecutor(max_workers=3) as executor:
        scheduler = scheduler_cls(tasks, executor, fake_execute)
        scheduler.run()

    assert len(set(thread_ids)) >= 2


# ---------------------------------------------------------------------------
# Serial dependency
# ---------------------------------------------------------------------------


def test_DependencyScheduler_直列依存AからBでA完了後にBがsubmitされる(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    import parallel_orchestra.runner as runner_module

    scheduler_cls = getattr(runner_module, "_DependencyScheduler")
    manifest = _make_manifest(tmp_path, LINEAR_AB)
    tasks = manifest.tasks

    completion_order: list[str] = []
    lock = threading.Lock()

    def fake_execute(task: Any) -> TaskResult:
        if task.id == "task-a":
            time.sleep(0.05)
        with lock:
            completion_order.append(task.id)
        return TaskResult(
            task_id=task.id, agent=task.agent, returncode=0,
            stdout="", stderr="", timed_out=False, duration_sec=0.0,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        scheduler = scheduler_cls(tasks, executor, fake_execute)
        scheduler.run()

    assert completion_order == ["task-a", "task-b"]


# ---------------------------------------------------------------------------
# Y-shape dependency
# ---------------------------------------------------------------------------


def test_DependencyScheduler_Y字依存でCはAとB両方完了後に走る(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    import parallel_orchestra.runner as runner_module

    scheduler_cls = getattr(runner_module, "_DependencyScheduler")
    manifest = _make_manifest(tmp_path, Y_SHAPE)
    tasks = manifest.tasks

    completion_order: list[str] = []
    lock = threading.Lock()

    def fake_execute(task: Any) -> TaskResult:
        time.sleep(0.05)
        with lock:
            completion_order.append(task.id)
        return TaskResult(
            task_id=task.id, agent=task.agent, returncode=0,
            stdout="", stderr="", timed_out=False, duration_sec=0.05,
        )

    with ThreadPoolExecutor(max_workers=3) as executor:
        scheduler = scheduler_cls(tasks, executor, fake_execute)
        scheduler.run()

    idx_c = completion_order.index("task-c")
    assert idx_c > completion_order.index("task-a")
    assert idx_c > completion_order.index("task-b")


# ---------------------------------------------------------------------------
# Results order matches manifest declaration order
# ---------------------------------------------------------------------------


def test_DependencyScheduler_resultsのタプル順はマニフェスト記述順(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    import parallel_orchestra.runner as runner_module

    scheduler_cls = getattr(runner_module, "_DependencyScheduler")
    manifest = _make_manifest(tmp_path, LINEAR_AB)
    tasks = manifest.tasks

    def ordered_execute(task: Any) -> TaskResult:
        return TaskResult(
            task_id=task.id, agent=task.agent, returncode=0,
            stdout="", stderr="", timed_out=False, duration_sec=0.0,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        scheduler = scheduler_cls(tasks, executor, ordered_execute)
        results = scheduler.run()

    result_ids = [r.task_id for r in results]
    expected_ids = [t.id for t in tasks]
    assert result_ids == expected_ids


# ---------------------------------------------------------------------------
# Skip propagation
# ---------------------------------------------------------------------------


def test_run_manifest_A失敗でBがskippedになる(fake_claude_runner, tmp_path):
    outcomes = [
        {"returncode": 1, "stdout": "", "stderr": "A error"},
        {"returncode": 0, "stdout": "B ran", "stderr": ""},
    ]
    fake_claude_runner(outcomes)

    manifest = _make_manifest(tmp_path, LINEAR_AB)
    result = run_manifest(manifest)

    tr_b = next(r for r in result.results if r.task_id == "task-b")
    assert tr_b.skipped is True
    assert tr_b.returncode is None
    assert tr_b.ok is False


def test_run_manifest_A失敗でBとCが推移的にskippedになる(fake_claude_runner, tmp_path):
    outcomes = [
        {"returncode": 1, "stdout": "", "stderr": "A error"},
        {"returncode": 0, "stdout": "", "stderr": ""},  # should not be called
        {"returncode": 0, "stdout": "", "stderr": ""},  # should not be called
    ]
    fake_claude_runner(outcomes)

    manifest = _make_manifest(tmp_path, TRANSITIVE_SKIP)
    result = run_manifest(manifest)

    tr_b = next(r for r in result.results if r.task_id == "task-b")
    tr_c = next(r for r in result.results if r.task_id == "task-c")
    assert tr_b.skipped is True
    assert tr_c.skipped is True


def test_run_manifest_A失敗でも独立タスクDは実行される(fake_claude_runner, tmp_path):
    outcomes = [
        {"returncode": 1, "stdout": "", "stderr": "A error"},
        {"returncode": 0, "stdout": "D ok", "stderr": ""},
    ]
    fake_claude_runner(outcomes)

    manifest = _make_manifest(tmp_path, FAIL_WITH_DOWNSTREAM_AND_INDEPENDENT)
    result = run_manifest(manifest)

    tr_d = next(r for r in result.results if r.task_id == "task-d")
    assert tr_d.ok is True
    assert tr_d.skipped is False


# ---------------------------------------------------------------------------
# read_only-only manifest runs without git
# ---------------------------------------------------------------------------


def test_run_manifest_全read_onlyはgitリポジトリなしで動作する(fake_claude_runner, tmp_path):
    outcomes = [
        {"returncode": 0, "stdout": "ok", "stderr": ""},
        {"returncode": 0, "stdout": "ok", "stderr": ""},
    ]
    fake_claude_runner(outcomes)

    manifest = _make_manifest(tmp_path, TWO_READONLY_TASKS)
    # Should not raise RunnerError even outside a git repo
    result = run_manifest(manifest)

    assert all(r.ok for r in result.results)


# ---------------------------------------------------------------------------
# Multiple RunnerErrors — only first propagates
# ---------------------------------------------------------------------------


def test_run_manifest_複数RunnerError発生時は最初の1件のみ伝播する(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    import parallel_orchestra.runner as runner_module

    scheduler_cls = getattr(runner_module, "_DependencyScheduler")
    manifest = _make_manifest(tmp_path, THREE_INDEPENDENT)
    tasks = manifest.tasks

    error_count = [0]
    lock = threading.Lock()

    def always_raise(task: Any) -> TaskResult:
        with lock:
            error_count[0] += 1
        raise RunnerError(f"error from {task.id}")

    with pytest.raises(RunnerError):
        with ThreadPoolExecutor(max_workers=3) as executor:
            scheduler = scheduler_cls(tasks, executor, always_raise)
            scheduler.run()
