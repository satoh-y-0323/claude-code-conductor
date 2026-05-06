"""Tests for retry and failure classification in parallel_orchestra.

Covers:
- _classify_failure categorises rate-limit / quota-exceeded stderr as "rate_limited"
- rate_limited tasks are retried when max_retries > 0
- retry_delay_sec and retry_backoff_factor are internal constants (not configurable)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def _make_task(max_retries: int = 0) -> Any:
    """Build a minimal read-only Task for retry tests."""
    from parallel_orchestra.manifest import Task
    return Task(
        id="t1",
        agent="dev",
        read_only=True,
        prompt="p",
        env={},
        max_retries=max_retries,
    )


def _make_task_result(
    *,
    returncode: int | None = 0,
    timed_out: bool = False,
    stderr: str = "",
    stdout: str = "ok",
) -> Any:
    import parallel_orchestra.runner as runner_module
    return runner_module.TaskResult(
        task_id="t1",
        agent="dev",
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        duration_sec=1.0,
    )


# ===========================================================================
# Section 1: _classify_failure — rate_limited カテゴリ分類
# ===========================================================================


class TestClassifyFailureRateLimited:
    def test_rate_limit_stderr_returns_rate_limited(self):
        import parallel_orchestra.runner as runner_module
        classify = getattr(runner_module, "_classify_failure")
        result = classify(1, "Error: rate limit exceeded, try again later")
        assert result == "rate_limited"

    def test_rate_limit_case_insensitive(self):
        import parallel_orchestra.runner as runner_module
        classify = getattr(runner_module, "_classify_failure")
        result = classify(1, "RATE LIMIT exceeded")
        assert result == "rate_limited"

    def test_rate_limit_with_underscore(self):
        import parallel_orchestra.runner as runner_module
        classify = getattr(runner_module, "_classify_failure")
        result = classify(1, "rate_limit hit for this API key")
        assert result == "rate_limited"

    def test_quota_exceeded_returns_rate_limited(self):
        import parallel_orchestra.runner as runner_module
        classify = getattr(runner_module, "_classify_failure")
        result = classify(1, "quota exceeded for this billing period")
        assert result == "rate_limited"

    def test_quota_exhausted_returns_rate_limited(self):
        import parallel_orchestra.runner as runner_module
        classify = getattr(runner_module, "_classify_failure")
        result = classify(1, "quota exhausted, please wait")
        assert result == "rate_limited"

    def test_rate_limit_takes_precedence_over_transient(self):
        import parallel_orchestra.runner as runner_module
        classify = getattr(runner_module, "_classify_failure")
        result = classify(1, "You have exceeded the rate limit for this endpoint")
        assert result == "rate_limited"

    def test_permanent_takes_precedence_over_rate_limited(self):
        import parallel_orchestra.runner as runner_module
        classify = getattr(runner_module, "_classify_failure")
        result = classify(126, "rate limit exceeded")
        assert result == "permanent"

    def test_permanent_stderr_takes_precedence_over_rate_limited(self):
        import parallel_orchestra.runner as runner_module
        classify = getattr(runner_module, "_classify_failure")
        result = classify(1, "permission denied and rate limit exceeded")
        assert result == "permanent"

    def test_credit_balance_still_permanent_not_rate_limited(self):
        import parallel_orchestra.runner as runner_module
        classify = getattr(runner_module, "_classify_failure")
        result = classify(1, "credit balance too low to complete the request")
        assert result == "permanent"

    def test_empty_stderr_returncode_1_is_transient(self):
        import parallel_orchestra.runner as runner_module
        classify = getattr(runner_module, "_classify_failure")
        result = classify(1, "")
        assert result == "transient"


# ===========================================================================
# Section 2: rate_limited タスクがリトライされる
# ===========================================================================


class TestRateLimitedTaskIsRetried:
    def test_rate_limited_with_max_retries_1_is_retried(self, monkeypatch):
        import parallel_orchestra.runner as runner_module

        call_count = [0]

        def fake_execute_task(task: Any, claude_exe: str, **kwargs: Any) -> Any:
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_task_result(returncode=1, stderr="rate limit exceeded")
            return _make_task_result(returncode=0, stdout="success after retry")

        monkeypatch.setattr(runner_module, "_execute_task", fake_execute_task)

        execute_with_retry = getattr(runner_module, "_execute_with_retry")
        task = _make_task(max_retries=1)
        result = execute_with_retry(
            task, "claude", git_root=None, effective_cwd=Path("."), log_config=None
        )

        assert call_count[0] == 2
        assert result.ok is True
        assert result.retry_count == 1
        assert result.failure_category == "none"

    def test_rate_limited_max_retries_0_is_not_retried(self, monkeypatch):
        import parallel_orchestra.runner as runner_module

        call_count = [0]

        def fake_execute_task(task: Any, claude_exe: str, **kwargs: Any) -> Any:
            call_count[0] += 1
            return _make_task_result(returncode=1, stderr="rate limit exceeded")

        monkeypatch.setattr(runner_module, "_execute_task", fake_execute_task)

        execute_with_retry = getattr(runner_module, "_execute_with_retry")
        task = _make_task(max_retries=0)
        result = execute_with_retry(
            task, "claude", git_root=None, effective_cwd=Path("."), log_config=None
        )

        assert call_count[0] == 1
        assert result.ok is False
        assert result.retry_count == 0
        assert result.failure_category == "rate_limited"

    def test_rate_limited_exhausts_all_retries(self, monkeypatch):
        import parallel_orchestra.runner as runner_module

        call_count = [0]

        def fake_execute_task(task: Any, claude_exe: str, **kwargs: Any) -> Any:
            call_count[0] += 1
            return _make_task_result(returncode=1, stderr="rate limit exceeded")

        monkeypatch.setattr(runner_module, "_execute_task", fake_execute_task)

        execute_with_retry = getattr(runner_module, "_execute_with_retry")
        task = _make_task(max_retries=2)
        result = execute_with_retry(
            task, "claude", git_root=None, effective_cwd=Path("."), log_config=None
        )

        assert call_count[0] == 3
        assert result.retry_count == 2
        assert result.failure_category == "rate_limited"

    def test_quota_exceeded_is_retried(self, monkeypatch):
        import parallel_orchestra.runner as runner_module

        call_count = [0]

        def fake_execute_task(task: Any, claude_exe: str, **kwargs: Any) -> Any:
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_task_result(returncode=1, stderr="quota exceeded today")
            return _make_task_result(returncode=0)

        monkeypatch.setattr(runner_module, "_execute_task", fake_execute_task)

        execute_with_retry = getattr(runner_module, "_execute_with_retry")
        task = _make_task(max_retries=2)
        result = execute_with_retry(
            task, "claude", git_root=None, effective_cwd=Path("."), log_config=None
        )

        assert call_count[0] == 2
        assert result.ok is True


# ===========================================================================
# Section 3: 内部定数の確認
# ===========================================================================


def test_内部リトライ遅延が1秒のデフォルトである():
    """Internal retry delay constant defaults to 1.0 second."""
    import parallel_orchestra.runner as runner_module
    assert runner_module._INTERNAL_RETRY_DELAY_SEC == 1.0


def test_内部バックオフ係数が2_0のデフォルトである():
    """Internal retry backoff factor constant defaults to 2.0."""
    import parallel_orchestra.runner as runner_module
    assert runner_module._INTERNAL_RETRY_BACKOFF_FACTOR == 2.0


def test_retry_delay_secのデフォルトでsleepが呼ばれる(monkeypatch):
    """Default internal retry delay (1.0s) is applied before each retry."""
    import parallel_orchestra.runner as runner_module

    sleep_calls: list[float] = []
    call_count = [0]

    def fake_execute_task(task: Any, claude_exe: str, **kwargs: Any) -> Any:
        call_count[0] += 1
        if call_count[0] == 1:
            return _make_task_result(returncode=1, stderr="transient error")
        return _make_task_result(returncode=0)

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(runner_module, "_execute_task", fake_execute_task)
    monkeypatch.setattr(runner_module.time, "sleep", fake_sleep)

    execute_with_retry = getattr(runner_module, "_execute_with_retry")
    task = _make_task(max_retries=1)
    execute_with_retry(task, "claude", git_root=None, effective_cwd=Path("."), log_config=None)

    # 1 retry → 1 sleep call. delay = 1.0 * 2.0^0 = 1.0s.
    assert sleep_calls == [1.0], f"Expected single 1.0s sleep, got {sleep_calls}"
