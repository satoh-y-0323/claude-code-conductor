"""Tests for --resume dependency chain correctness."""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from parallel_orchestra.manifest import Task
from parallel_orchestra.runner import (
    TaskResult,
    _DependencyScheduler,  # noqa: PLC2701
)


def _make_read_only_task(
    task_id: str,
    *,
    depends_on: Sequence[str] = (),
) -> Task:
    return Task(
        id=task_id,
        agent="code-reviewer",
        read_only=True,
        prompt=f"Review {task_id}",
        env={},
        depends_on=tuple(depends_on),
        max_retries=0,
    )


def _make_ok_result(task: Task) -> TaskResult:
    return TaskResult(
        task_id=task.id,
        agent=task.agent,
        returncode=0,
        stdout="ok",
        stderr="",
        timed_out=False,
        duration_sec=0.1,
    )


def test_resume_dep_chain_A_not_resumed_B_resumed_C_not_before_A() -> None:
    """A -> B(resumed) -> C: C must wait for A to complete."""
    task_a = _make_read_only_task("task-a")
    task_b = _make_read_only_task("task-b", depends_on=["task-a"])
    task_c = _make_read_only_task("task-c", depends_on=["task-b"])

    tasks = [task_a, task_b, task_c]
    execution_order: list[str] = []
    lock = threading.Lock()
    a_done_event = threading.Event()

    def execute_fn(task: Task) -> TaskResult:
        with lock:
            execution_order.append(task.id)
        if task.id == "task-a":
            time.sleep(0.05)
            a_done_event.set()
        elif task.id == "task-c":
            assert a_done_event.is_set(), "task-c started before task-a finished"
        return _make_ok_result(task)

    resumed_ids = frozenset({"task-b"})

    with ThreadPoolExecutor(max_workers=4) as executor:
        scheduler = _DependencyScheduler(
            tasks, executor, execute_fn, resumed_task_ids=resumed_ids
        )
        results = scheduler.run()

    assert "task-a" in execution_order
    assert "task-c" in execution_order
    assert execution_order.index("task-a") < execution_order.index("task-c")

    results_by_id = {r.task_id: r for r in results}
    assert results_by_id["task-a"].ok is True
    assert results_by_id["task-b"].resumed is True
    assert results_by_id["task-c"].ok is True


def test_resume_dep_chain_all_resumed_no_execution() -> None:
    """A(resumed) -> B(resumed) -> C(resumed): no tasks are executed."""
    task_a = _make_read_only_task("task-a")
    task_b = _make_read_only_task("task-b", depends_on=["task-a"])
    task_c = _make_read_only_task("task-c", depends_on=["task-b"])

    tasks = [task_a, task_b, task_c]
    executed: list[str] = []

    def execute_fn(task: Task) -> TaskResult:
        executed.append(task.id)
        return _make_ok_result(task)

    resumed_ids = frozenset({"task-a", "task-b", "task-c"})

    with ThreadPoolExecutor(max_workers=4) as executor:
        scheduler = _DependencyScheduler(
            tasks, executor, execute_fn, resumed_task_ids=resumed_ids
        )
        results = scheduler.run()

    assert executed == [], f"No tasks should run when all resumed, got: {executed}"
    for r in results:
        assert r.resumed is True
        assert r.ok is True


def test_resume_dep_chain_A_resumed_B_not_C_not() -> None:
    """A(resumed) -> B -> C: B and C must run in order."""
    task_a = _make_read_only_task("task-a")
    task_b = _make_read_only_task("task-b", depends_on=["task-a"])
    task_c = _make_read_only_task("task-c", depends_on=["task-b"])

    tasks = [task_a, task_b, task_c]
    execution_order: list[str] = []
    lock = threading.Lock()

    def execute_fn(task: Task) -> TaskResult:
        with lock:
            execution_order.append(task.id)
        return _make_ok_result(task)

    resumed_ids = frozenset({"task-a"})

    with ThreadPoolExecutor(max_workers=4) as executor:
        scheduler = _DependencyScheduler(
            tasks, executor, execute_fn, resumed_task_ids=resumed_ids
        )
        results = scheduler.run()

    assert "task-a" not in execution_order
    assert "task-b" in execution_order
    assert "task-c" in execution_order
    assert execution_order.index("task-b") < execution_order.index("task-c")

    results_by_id = {r.task_id: r for r in results}
    assert results_by_id["task-a"].resumed is True
    assert results_by_id["task-b"].ok is True
    assert results_by_id["task-c"].ok is True


def test_resume_dep_chain_A_not_resumed_B_resumed_C_resumed_D_executes() -> None:
    """A → B(resumed) → C(resumed) → D: D is executed after A."""
    task_a = _make_read_only_task("task-a")
    task_b = _make_read_only_task("task-b", depends_on=["task-a"])
    task_c = _make_read_only_task("task-c", depends_on=["task-b"])
    task_d = _make_read_only_task("task-d", depends_on=["task-c"])

    tasks = [task_a, task_b, task_c, task_d]
    execution_order: list[str] = []
    lock = threading.Lock()
    a_done_event = threading.Event()

    def execute_fn(task: Task) -> TaskResult:
        with lock:
            execution_order.append(task.id)
        if task.id == "task-a":
            time.sleep(0.05)
            a_done_event.set()
        elif task.id == "task-d":
            assert a_done_event.is_set(), "task-d started before task-a finished"
        return _make_ok_result(task)

    resumed_ids = frozenset({"task-b", "task-c"})

    with ThreadPoolExecutor(max_workers=4) as executor:
        scheduler = _DependencyScheduler(
            tasks, executor, execute_fn, resumed_task_ids=resumed_ids
        )
        results = scheduler.run()

    assert "task-b" not in execution_order
    assert "task-c" not in execution_order
    assert "task-a" in execution_order
    assert "task-d" in execution_order
    assert execution_order.index("task-a") < execution_order.index("task-d")

    results_by_id = {r.task_id: r for r in results}
    assert results_by_id["task-a"].ok is True
    assert results_by_id["task-b"].resumed is True
    assert results_by_id["task-c"].resumed is True
    assert results_by_id["task-d"].ok is True


def test_resume_dep_chain_no_resumed_normal_order() -> None:
    """Without any resumed tasks, A -> B -> C must execute in order."""
    task_a = _make_read_only_task("task-a")
    task_b = _make_read_only_task("task-b", depends_on=["task-a"])
    task_c = _make_read_only_task("task-c", depends_on=["task-b"])

    tasks = [task_a, task_b, task_c]
    execution_order: list[str] = []
    lock = threading.Lock()

    def execute_fn(task: Task) -> TaskResult:
        with lock:
            execution_order.append(task.id)
        return _make_ok_result(task)

    with ThreadPoolExecutor(max_workers=4) as executor:
        scheduler = _DependencyScheduler(
            tasks, executor, execute_fn, resumed_task_ids=None
        )
        results = scheduler.run()

    assert execution_order == ["task-a", "task-b", "task-c"]
    for r in results:
        assert r.ok is True
