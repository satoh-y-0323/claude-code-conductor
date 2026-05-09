"""Parallel task runner for parallel-orchestra manifests.

Executes agent tasks defined in a Manifest concurrently using a thread pool,
capturing stdout/stderr and timing for each task.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import uuid
import warnings
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Literal, TextIO

from ._exceptions import ParallelOrchestraError
from .c3_db import READ_ONLY_WORKTREE_ID, locate_c3_db
from .manifest import Manifest, Task, load_manifest

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

FailureCategory = Literal["transient", "permanent", "rate_limited", "timeout", "none"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_CLAUDE_EXECUTABLE = "claude"
_DEFAULT_MAX_WORKERS: int = 5
_CLAUDE_PROMPT_FLAG = "-p"
_WORKTREE_ROOT_NAME = ".po-worktrees"
_GIT_COMMAND_TIMEOUT_SEC = 30
_CONFLICT_STDERR_MAX_CHARS = 2000
_PROGRESS_INTERVAL_SEC = 5
_STARTUP_DISPLAY_SEC = 60
_LAST_LINES_ON_TIMEOUT = 20
_DASHBOARD_IDLE_RENDER_SEC = _PROGRESS_INTERVAL_SEC
_DASHBOARD_NONLIVE_RENDER_SEC = 30
_TOOL_ACTION_MAX_LEN = 45

# Internally managed constants — not configurable via manifest.
_INTERNAL_TIMEOUT_SEC: int = 1200
_INTERNAL_RETRY_DELAY_SEC: float = 1.0
_INTERNAL_RETRY_BACKOFF_FACTOR: float = 2.0

_PERMANENT_RETURNCODES: frozenset[int] = frozenset({2, 126, 127})

_PERMANENT_STDERR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bpermission[\s_-]?denied\b", re.IGNORECASE),
    re.compile(r"authentication[\s_-]?(failed|error)", re.IGNORECASE),
    re.compile(r"invalid[\s_-]?api[\s_-]?key", re.IGNORECASE),
    re.compile(r"credit[\s_-]?balance[\s_-]?(too[\s_-]?low|exceeded)", re.IGNORECASE),
)

_RATE_LIMITED_STDERR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"rate[\s_-]?limit", re.IGNORECASE),
    re.compile(r"quota[\s_-]?(exceeded|exhausted)", re.IGNORECASE),
)

# Environment variable keys whose values must be masked in log output.
_SENSITIVE_ENV_KEYS: frozenset[str] = frozenset(
    {"ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_HELPER"}
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RunnerError(ParallelOrchestraError):
    """Raised when the runner cannot proceed (e.g., claude binary not found)."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeResult:
    """Result of merging a task's worktree branch back to the base branch."""

    task_id: str
    branch_name: str
    status: Literal["merged", "conflict", "error"]
    stderr: str


@dataclass(frozen=True)
class TaskResult:
    """Result of executing a single agent task."""

    task_id: str
    agent: str
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_sec: float
    skipped: bool = False
    branch_name: str | None = None
    timeout_reason: Literal["total"] | None = None
    retry_count: int = 0
    failure_category: FailureCategory = "none"
    # Structured JSON output parsed from agent stdout (e.g. tdd-develop).
    agent_status: str | None = None    # "SUCCESS" or "FAILED"
    agent_cycles: int | None = None    # number of TDD/work cycles
    agent_reason: str | None = None    # failure reason
    agent_report: str | None = None    # relative path to agent-generated report

    @property
    def ok(self) -> bool:
        return not self.skipped and self.returncode == 0 and not self.timed_out


@dataclass(frozen=True)
class RunResult:
    """Aggregated result of running all tasks in a manifest."""

    results: tuple[TaskResult, ...]
    merge_results: tuple[MergeResult, ...] = ()

    @property
    def overall_ok(self) -> bool:
        return all(r.ok for r in self.results)


@dataclass(frozen=True)
class LogConfig:
    """Configuration for task-level log persistence."""

    base_dir: Path
    enabled: bool = True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@dataclass
class _RunState:
    """Shared mutable state for _run_with_progress helper threads."""

    last_output_ts: float
    has_received_output: bool
    lock: threading.Lock = field(default_factory=threading.Lock)
    done_event: threading.Event = field(default_factory=threading.Event)
    kill_reason: Literal["total"] | None = None


_TaskStatus = Literal[
    "waiting", "starting_up", "running", "complete", "failed", "skipped", "resumed"
]


# F-003: _TaskStatus → po_status.state の語彙マッピング。
# schema.sql の state は 'starting' | 'running' | 'completed' | 'failed' の 4 種。
# 'waiting'（まだ実行されていない）は heartbeat 側で除外する。
_PO_STATUS_STATE_MAPPING: dict[str, str] = {
    "waiting": "starting",       # 実際には waiting 行は heartbeat で除外
    "starting_up": "starting",
    "running": "running",
    "complete": "completed",
    "failed": "failed",
    "skipped": "completed",
    "resumed": "running",
}


@dataclass
class _TaskDisplayState:
    """Per-task mutable display state for _Dashboard."""

    task_id: str
    status: _TaskStatus = "waiting"
    current_action: str = ""
    tokens_out: int = 0
    start_ts: float = 0.0
    elapsed_sec: float = 0.0


class _Dashboard:
    """ANSI in-place progress dashboard for TTY terminals."""

    def __init__(
        self,
        task_ids: list[str],
        *,
        enabled: bool,
        live_renders: bool = True,
    ) -> None:
        self._enabled = enabled
        self._live_renders = live_renders
        self._task_ids: list[str] = list(task_ids)
        self._states: dict[str, _TaskDisplayState] = {
            tid: _TaskDisplayState(task_id=tid) for tid in task_ids
        }
        self._lock = threading.Lock()
        self._lines_rendered: int = 0
        self._stop_event = threading.Event()
        self._dirty_event = threading.Event()
        self._render_thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> None:
        if not self._enabled:
            return
        self._render_thread = threading.Thread(
            target=self._render_loop, daemon=True, name="po-dashboard"
        )
        self._render_thread.start()

    def stop(self) -> None:
        if not self._enabled:
            return
        self._stop_event.set()
        self._dirty_event.set()
        if self._render_thread is not None:
            self._render_thread.join(timeout=2.0)
        self._do_render(final=True)

    def update(self, task_id: str, *, important: bool = True, **kwargs: Any) -> None:
        # state は常に保持する（render が無効でも summary loop が snapshot_states を
        # 読むため、非 TTY 時に状態を捨ててはいけない）。
        with self._lock:
            state = self._states.get(task_id)
            if state is None:
                return
            if (
                "status" in kwargs
                and state.status == "waiting"
                and kwargs["status"] != "waiting"
                and "start_ts" not in kwargs
            ):
                kwargs["start_ts"] = time.perf_counter()
            for k, v in kwargs.items():
                setattr(state, k, v)
        # render の dirty 通知は enabled なときだけ意味がある
        if self._enabled and (important or self._live_renders):
            self._dirty_event.set()

    def snapshot_states(self) -> list[_TaskDisplayState]:
        """Return a thread-safe shallow copy of all task display states.

        F-003 heartbeat thread uses this to read PO progress without holding
        the dashboard lock during DB I/O. The returned dataclasses are copies
        so the caller can safely iterate after the lock is released.
        """
        from copy import copy as _copy  # noqa: PLC0415

        with self._lock:
            return [_copy(s) for s in self._states.values()]

    def _render_loop(self) -> None:
        interval = (
            _DASHBOARD_IDLE_RENDER_SEC
            if self._live_renders
            else _DASHBOARD_NONLIVE_RENDER_SEC
        )
        last_render_ts = 0.0
        while not self._stop_event.is_set():
            self._dirty_event.wait(timeout=interval)
            if self._stop_event.is_set():
                return
            self._dirty_event.clear()
            now = time.perf_counter()
            min_gap = 0.0 if self._live_renders else _PROGRESS_INTERVAL_SEC
            if now - last_render_ts >= min_gap:
                self._do_render()
                last_render_ts = now

    def _count_final_stats(self) -> tuple[int, int, int, int]:
        n_complete = sum(1 for s in self._states.values() if s.status == "complete")
        n_failed = sum(1 for s in self._states.values() if s.status == "failed")
        n_skipped_or_resumed = sum(
            1 for s in self._states.values() if s.status in ("skipped", "resumed")
        )
        n_total = len(self._task_ids)
        return n_complete, n_failed, n_skipped_or_resumed, n_total

    def _build_summary_line(self, *, final: bool) -> str:
        now = time.perf_counter()

        if final:
            (
                n_complete,
                n_failed,
                n_skipped_or_resumed,
                n_total,
            ) = self._count_final_stats()
            if n_failed == 0 and n_skipped_or_resumed == 0:
                return f"[done] all {n_total} tasks completed"
            final_parts: list[str] = [f"{n_complete}/{n_total} succeeded"]
            if n_failed > 0:
                final_parts.append(f"{n_failed} failed")
            if n_skipped_or_resumed > 0:
                final_parts.append(f"{n_skipped_or_resumed} skipped/resumed")
            return "[done] " + ", ".join(final_parts)

        start_times = [s.start_ts for s in self._states.values() if s.start_ts > 0]
        if start_times:
            overall_elapsed = now - min(start_times)
        else:
            overall_elapsed = 0.0

        running_parts: list[str] = []
        waiting_parts: list[str] = []
        done_parts: list[str] = []

        for tid in self._task_ids:
            state = self._states[tid]
            if state.status in ("running", "starting_up"):
                elapsed = now - state.start_ts if state.start_ts > 0 else 0.0
                running_parts.append(f"{tid} {elapsed:.0f}s")
            elif state.status == "waiting":
                waiting_parts.append(tid)
            elif state.status == "complete":
                done_parts.append(f"{tid} ✓")
            elif state.status == "failed":
                done_parts.append(f"{tid} ✗")
            elif state.status in ("skipped", "resumed"):
                done_parts.append(f"{tid} -")

        parts: list[str] = []
        if running_parts:
            parts.append("running: " + ", ".join(running_parts))
        if waiting_parts:
            parts.append("waiting: " + ", ".join(waiting_parts))
        if done_parts:
            parts.append("done: " + ", ".join(done_parts))

        summary = " | ".join(parts) if parts else "starting..."
        return f"[{overall_elapsed:.0f}s] {summary}"

    def _do_render(self, final: bool = False) -> None:
        buf = getattr(sys.stderr, "buffer", None)
        if self._live_renders:
            width = max(shutil.get_terminal_size(fallback=(80, 24)).columns, 20)
            with self._lock:
                lines = self._build_lines(final=final)
            chunks: list[str] = []
            if self._lines_rendered > 0:
                chunks.append(f"\033[{self._lines_rendered}A")
            for line in lines:
                chunks.append(f"\033[2K{line[:width]}\n")
            payload = "".join(chunks)
            self._lines_rendered = len(lines)
        else:
            with self._lock:
                line = self._build_summary_line(final=final)
            payload = line + "\n"
        if buf is not None:
            buf.write(payload.encode("utf-8"))
            buf.flush()
        else:
            sys.stderr.write(payload)
            sys.stderr.flush()

    def _build_lines(self, *, final: bool) -> list[str]:
        lines: list[str] = []
        now = time.perf_counter()

        if final:
            (
                n_complete,
                n_failed,
                _n_skipped_or_resumed,
                n_total,
            ) = self._count_final_stats()
            if n_failed > 0:
                header = (
                    f"parallel-orchestra done"
                    f" ({n_complete}/{n_total} succeeded, {n_failed} failed)"
                )
            else:
                header = f"parallel-orchestra done ({n_complete}/{n_total} succeeded)"
        else:
            header = "parallel-orchestra running"
        lines.append(header)

        for tid in self._task_ids:
            state = self._states[tid]

            if state.start_ts > 0:
                elapsed = (
                    state.elapsed_sec
                    if state.status in ("complete", "failed")
                    else now - state.start_ts
                )
                elapsed_str = f"  {elapsed:.0f}s"
            else:
                elapsed = 0.0
                elapsed_str = ""

            if state.status == "complete":
                indicator = " ✓"
            elif state.status == "failed":
                indicator = " ✗"
            elif state.status == "skipped":
                indicator = " -"
            elif state.status == "resumed":
                indicator = " »"
            else:
                indicator = ""

            lines.append(f"  [{tid}]{elapsed_str}{indicator}")

            if state.status == "complete":
                if state.tokens_out > 0:
                    action = (
                        f"complete!! {state.elapsed_sec:.0f}s"
                        f"  ({state.tokens_out:,} tokens)"
                    )
                else:
                    action = f"complete!! {state.elapsed_sec:.0f}s"
            elif state.status == "failed":
                action = "failed"
            elif state.status == "skipped":
                action = "skipped (dependency failed)"
            elif state.status == "resumed":
                action = "already done"
            elif state.status == "waiting":
                action = "waiting..."
            elif state.status == "starting_up":
                action = f"starting up... {elapsed:.0f}s"
            elif state.current_action:
                action = state.current_action
            else:
                action = "thinking..."

            lines.append(f"    └ {action}")

        return lines


# ---------------------------------------------------------------------------
# F-003: PO 並列処理の状況可視化（heartbeat to po_status table）
# ---------------------------------------------------------------------------


def _heartbeat_po_status_loop(
    dashboard: _Dashboard,
    session_id: str,
    stop_event: threading.Event,
    *,
    interval: float = 30.0,
) -> None:
    """30 秒ごとに dashboard の状態スナップショットを取って po_status に UPSERT する。

    F-003: PO 並列処理の状況可視化。`.claude/state/c3.db` の po_status テーブルに
    各 worktree の current state を定期的に書き込む。dashboard が無効化されている
    環境（非 TTY 等）では呼び出し側でこのスレッド自体を起動しない想定。

    実装上の注意:
    - dashboard._lock は ``snapshot_states()`` 内でしか取得しない（DB I/O は
      ロック解放後に行う）。
    - DB 不在 / SQL エラー時は upsert_po_status 側で握りつぶす（PO 本体を
      止めない）。
    - 初回は即座に発火、その後 ``interval`` 秒間隔で発火する。
    - ``stop_event.wait(interval)`` で停止可能（ブロックせず返る）。
    """
    from .c3_db import upsert_po_status  # noqa: PLC0415 - 遅延 import で循環回避

    while True:
        try:
            states = dashboard.snapshot_states()
            for s in states:
                # waiting（実行前）は記録対象外。schema の state は 4 種のみ
                # （'starting' / 'running' / 'completed' / 'failed'）。
                if s.status == "waiting":
                    continue
                state_label = _PO_STATUS_STATE_MAPPING.get(s.status, "running")
                upsert_po_status(
                    session_id=session_id,
                    worktree_id=s.task_id,
                    state=state_label,
                    current_step=s.current_action or None,
                )
        except Exception:  # noqa: BLE001 - 観測機能なので PO 本体を止めない
            pass

        if stop_event.wait(timeout=interval):
            break


# Default interval for non-TTY summary lines. Aligned with heartbeat (30s).
_DEFAULT_SUMMARY_INTERVAL_SEC = 30.0
# Max number of running tasks to enumerate in the summary line before showing
# "+N more". Keeps the line within ~1 terminal width even with 10+ workers.
_SUMMARY_DETAIL_TASK_LIMIT = 3


def _resolve_summary_interval(default: float = _DEFAULT_SUMMARY_INTERVAL_SEC) -> float:
    """Return summary interval in seconds (env C3_PO_SUMMARY_INTERVAL_SEC で上書き可)。

    無効値（非数値・0 以下）はデフォルトに戻し、ユーザーが --watch 相当の
    暴走 print を起こさないようガードする。
    """
    raw = os.environ.get("C3_PO_SUMMARY_INTERVAL_SEC")
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    if v <= 0:
        return default
    return v


def _format_summary_line(states: list[_TaskDisplayState], *, now: float) -> str:
    """非 TTY 用の wave 全体サマリを 1 行で組み立てる。

    Format: ``[summary] N tasks: R running (id1:Xs, id2:Ys), S starting, C completed, F failed``。
    runtime はモノトニックな ``time.perf_counter()`` 基準で算出する（dashboard.update が
    start_ts に同じ time base を使うため）。
    """
    total = len(states)
    starting = sum(1 for s in states if s.status == "starting_up")
    completed = sum(1 for s in states if s.status in ("complete", "skipped"))
    failed = sum(1 for s in states if s.status == "failed")
    running_states = [s for s in states if s.status in ("running", "resumed")]

    detail = []
    for s in running_states[:_SUMMARY_DETAIL_TASK_LIMIT]:
        elapsed = now - s.start_ts if s.start_ts > 0 else 0.0
        detail.append(f"{s.task_id}:{int(elapsed)}s")
    more = (
        f", +{len(running_states) - _SUMMARY_DETAIL_TASK_LIMIT} more"
        if len(running_states) > _SUMMARY_DETAIL_TASK_LIMIT
        else ""
    )
    detail_str = ", ".join(detail) + more if detail else ""

    parts = [f"{len(running_states)} running"]
    if detail_str:
        parts[-1] += f" ({detail_str})"
    if starting:
        parts.append(f"{starting} starting")
    if completed:
        parts.append(f"{completed} completed")
    if failed:
        parts.append(f"{failed} failed")

    return f"[summary] {total} tasks: " + ", ".join(parts)


def _summary_loop(
    dashboard: _Dashboard,
    stop_event: threading.Event,
    *,
    interval: float,
    out: TextIO | None = None,
) -> None:
    """非 TTY 環境用に wave 全体の進捗を 1 行ずつ ``out`` (default stderr) に出すスレッド。

    各 task の thinking / running 個別ログを廃止する代わりに、定期的にサマリ行を
    出すことで Claude Code 等のログ集約 UI で行が増えすぎないようにする。

    停止条件: ``stop_event.set()`` または全タスクが waiting / complete / skipped /
    failed のいずれかに収束した場合。
    """
    target = out if out is not None else sys.stderr
    while True:
        if stop_event.wait(timeout=interval):
            break
        try:
            states = dashboard.snapshot_states()
            # 動いているものが何も残っていなければサマリも出さない（最終行は
            # run_manifest 終了後に dashboard.stop() で出る）。
            active = [s for s in states if s.status in ("starting_up", "running", "resumed")]
            if not active:
                continue
            line = _format_summary_line(states, now=time.perf_counter())
            print(line, file=target, flush=True)
        except Exception:  # noqa: BLE001 - 観測機能なので PO 本体を止めない
            pass


def _sanitize_for_display(text: str, max_len: int = _TOOL_ACTION_MAX_LEN) -> str:
    """Remove ANSI escapes, control characters, and Unicode spoofing characters.

    This function is intended for **display purposes only** (e.g. terminal UI
    action strings).  As a side-effect of stripping Unicode ``Cc`` category
    characters, newline (``\\n``) and tab (``\\t``) characters are also removed.
    Do not use this function on text that must preserve whitespace semantics.
    """
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b.", "", text)
    text = text.replace("\x1b", "")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    # Strip Unicode direction-override and zero-width characters (Cf/Cc categories)
    # to prevent terminal UI spoofing via bi-directional control characters.
    text = "".join(
        ch for ch in text if unicodedata.category(ch) not in ("Cf", "Cc")
    )
    if len(text) > max_len:
        text = text[:max_len - 3] + "..."
    return text


def _format_tool_action(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Format a tool_use event into a short human-readable action string."""
    key_by_tool: dict[str, str] = {
        "Bash": "command",
        "Write": "file_path",
        "Read": "file_path",
        "Edit": "file_path",
        "Glob": "pattern",
        "Grep": "pattern",
    }
    key = key_by_tool.get(tool_name)
    if key and key in tool_input:
        arg = _sanitize_for_display(str(tool_input[key]))
        return f"{tool_name}({arg})"
    return tool_name


def _classify_failure(returncode: int | None, stderr: str) -> FailureCategory:
    """Classify a non-ok, non-timeout task outcome into retry buckets."""
    if returncode is not None and returncode in _PERMANENT_RETURNCODES:
        return "permanent"
    for pattern in _PERMANENT_STDERR_PATTERNS:
        if pattern.search(stderr):
            return "permanent"
    for pattern in _RATE_LIMITED_STDERR_PATTERNS:
        if pattern.search(stderr):
            return "rate_limited"
    return "transient"


def _with_retry_info(
    result: TaskResult, *, retry_count: int, category: FailureCategory
) -> TaskResult:
    return dataclasses.replace(
        result, retry_count=retry_count, failure_category=category
    )


def _mask_sensitive_env_values(
    text: str,
    extra_env: dict[str, str] | None = None,
) -> str:
    """Replace known sensitive environment variable values with a placeholder.

    Scans the process environment for keys listed in ``_SENSITIVE_ENV_KEYS`` and
    replaces any occurrence of their current value in *text* with ``[MASKED]``.
    ``extra_env`` に渡した dict の全値も同様にマスクする（task.env のシークレット対策）。

    Limitation: only the raw (plain-text) form of each value is matched via
    simple string substitution.  If a sensitive value appears in *text* as a
    URL-encoded (``%XX``) or Base64-encoded string, it will **not** be masked.
    This is an accepted limitation for the current use case where log content
    originates from Claude process output rather than encoded HTTP traffic.
    """
    for key in _SENSITIVE_ENV_KEYS:
        value = os.environ.get(key)
        if value:  # mask any non-empty value
            text = text.replace(value, "[MASKED]")
    if extra_env:
        for value in extra_env.values():
            if value:
                text = text.replace(value, "[MASKED]")
    return text


def _write_task_logs(
    task_id: str,
    stdout: str,
    stderr: str,
    *,
    attempt: int,
    log_config: LogConfig,
    task_env: dict[str, str] | None = None,
) -> None:
    """Persist a task's stdout/stderr to files on a best-effort basis."""
    if not log_config.enabled:
        return
    try:
        log_config.base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stdout_path = log_config.base_dir / f"{task_id}-stdout.log"
        stderr_path = log_config.base_dir / f"{task_id}-stderr.log"
        mode = "w" if attempt == 0 else "a"
        header = f"\n===== retry attempt {attempt} =====\n" if attempt > 0 else ""
        safe_stdout = _mask_sensitive_env_values(stdout, extra_env=task_env)
        safe_stderr = _mask_sensitive_env_values(stderr, extra_env=task_env)
        with stdout_path.open(mode, encoding="utf-8", errors="replace") as fp:
            fp.write(header)
            fp.write(safe_stdout)
        # NOTE: There is a small window between open() and chmod() during which
        # another process could read the file.  pathlib.Path.open() does not
        # expose an atomic open-with-mode API, so this two-step approach is the
        # best achievable with the standard library.
        stdout_path.chmod(0o600)
        with stderr_path.open(mode, encoding="utf-8", errors="replace") as fp:
            fp.write(header)
            fp.write(safe_stderr)
        # Same non-atomic window as above; accepted limitation.
        stderr_path.chmod(0o600)
    except OSError as exc:
        logger.warning(
            "_write_task_logs: failed to write logs for task %r: %s", task_id, exc
        )


def _execute_with_retry(
    task: Task,
    claude_exe: str,
    *,
    git_root: Path | None,
    effective_cwd: Path,
    log_config: LogConfig | None,
    dashboard: _Dashboard | None = None,
    po_session_id: str | None = None,
) -> TaskResult:
    """Execute *task* with automatic retry on transient failures.

    The ``po_session_id`` is passed through to ``_execute_task`` so the
    spawned subprocess can write to ``.claude/state/c3.db`` directly
    (F-002 Phase 2-A).
    """
    for attempt in range(task.max_retries + 1):
        result = _execute_task(
            task, claude_exe,
            git_root=git_root,
            effective_cwd=effective_cwd,
            dashboard=dashboard,
            po_session_id=po_session_id,
        )

        if log_config is not None:
            _write_task_logs(
                result.task_id,
                result.stdout,
                result.stderr,
                attempt=attempt,
                log_config=log_config,
                task_env=task.env or None,
            )

        if result.ok:
            return _with_retry_info(result, retry_count=attempt, category="none")

        if result.timed_out:
            return _with_retry_info(result, retry_count=attempt, category="timeout")

        category = _classify_failure(result.returncode, result.stderr)

        if category == "permanent":
            return _with_retry_info(result, retry_count=attempt, category="permanent")

        if attempt >= task.max_retries:
            return _with_retry_info(result, retry_count=attempt, category=category)

        # Exponential backoff: delay = base * factor^attempt
        # (defaults: 1.0s base, factor 2.0 → 1s, 2s, 4s, ...).
        delay: float = _INTERNAL_RETRY_DELAY_SEC * (
            _INTERNAL_RETRY_BACKOFF_FACTOR ** attempt
        )
        if delay > 0:
            time.sleep(delay)

    raise AssertionError("_execute_with_retry: loop exited without returning")


def _require_git_root(cwd: Path) -> Path:
    """Return the git repository root containing *cwd*."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_COMMAND_TIMEOUT_SEC,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise RunnerError(f"Not inside a git repository (cwd={cwd}): {exc}") from exc

    return Path(result.stdout.strip())


def _worktree_setup(
    git_root: Path,
    task: Task,
    claude_src_dir: Path | None = None,
) -> tuple[Path, str]:
    """Create an isolated git worktree for *task* and return its path and branch name.

    The worktree is rooted at ``git_root`` (the same git database is used for
    all worktrees, regardless of where the manifest lives). Agent assets such
    as plan-reports / skills / agent definitions are copied from
    ``claude_src_dir`` if provided, otherwise from ``git_root / ".claude"``.
    Pointing this at the manifest directory's ``.claude/`` lets users keep
    PO-specific assets under a subproject (e.g. ``example/.claude/``) while
    using a shared parent git repository.
    """
    worktree_root = git_root / _WORKTREE_ROOT_NAME
    worktree_root.mkdir(exist_ok=True)

    uuid8 = uuid.uuid4().hex[:8]
    worktree_name = f"{task.id}-{uuid8}"
    worktree_path = worktree_root / worktree_name
    branch_name = f"parallel-orchestra/{task.id}-{uuid8}"

    try:
        subprocess.run(
            ["git", "worktree", "add", "-b", branch_name, str(worktree_path)],
            cwd=str(git_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_COMMAND_TIMEOUT_SEC,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise RunnerError(
            f"Failed to create worktree for task {task.id!r} at {worktree_path}: {exc}"
        ) from exc

    dest_dir = worktree_path / ".claude"
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Copy .claude/ contents so agents in the worktree can find plan-reports,
    # skill definitions, agent definitions, hooks, and settings.
    # Excludes CLAUDE.md (replaced by empty below), settings.local.json
    # (handled separately), and runtime state dirs (logs/, memory/).
    # settings.json is intentionally excluded: Claude Code resolves it from the
    # main repo (via the git worktree pointer), so copying it would cause
    # permission patterns to be evaluated against the wrong base path.
    # settings.local.json is handled separately below.
    _CLAUDE_SKIP = frozenset(
        {"CLAUDE.md", "settings.json", "settings.local.json", "logs", "memory"}
    )
    claude_src = claude_src_dir if claude_src_dir is not None else git_root / ".claude"
    if claude_src.exists():
        for item in claude_src.iterdir():
            if item.name in _CLAUDE_SKIP:
                continue
            dest_item = dest_dir / item.name
            try:
                if item.is_dir() and item.name == "reports":
                    # Copy only plan-reports into the worktree; other report types
                    # are produced inside the worktree and must not be pre-seeded.
                    dest_item.mkdir(exist_ok=True)
                    for report in item.glob("plan-report-*.md"):
                        shutil.copy2(report, dest_item / report.name)
                elif item.is_dir():
                    shutil.copytree(item, dest_item, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest_item)
            except OSError:
                pass  # best-effort: never block worktree creation

    settings_local = claude_src / "settings.local.json"
    if settings_local.exists():
        shutil.copy2(settings_local, dest_dir / "settings.local.json")
    # Empty CLAUDE.md prevents startup protocols in worktree agents.
    (dest_dir / "CLAUDE.md").write_text("", encoding="utf-8")

    return worktree_path, branch_name


def _sanitize_git_stderr(text: str) -> str:
    """Sanitize git stderr output by removing ANSI escapes and control characters."""
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    if len(text) > _CONFLICT_STDERR_MAX_CHARS:
        text = text[:_CONFLICT_STDERR_MAX_CHARS]
    return text


def _setup_worktree(
    git_root: Path,
    task: Task,
    claude_src_dir: Path | None = None,
) -> tuple[Path, str]:
    """Create an isolated git worktree for *task*; thin wrapper for ``_worktree_setup``.

    This wrapper exists solely as a monkeypatch seam for tests.
    Do not call ``_worktree_setup`` directly from test code — patch this
    function instead so the injection point remains stable even if the
    underlying implementation is refactored.
    """
    return _worktree_setup(git_root, task, claude_src_dir=claude_src_dir)


def _resolve_merge_base_branch(
    cwd: Path, timeout: int = _GIT_COMMAND_TIMEOUT_SEC
) -> str:
    """Return the current branch name by querying git for the HEAD reference."""
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "-q", "HEAD"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RunnerError(
            "Cannot resolve merge base branch: HEAD is in detached state. "
            "Please check out a branch before running parallel-orchestra."
        )
    return result.stdout.strip()


def _abort_merge(cwd: Path, timeout: int = _GIT_COMMAND_TIMEOUT_SEC) -> None:
    try:
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except Exception:  # noqa: BLE001
        pass


def _delete_branch(
    cwd: Path, branch_name: str, timeout: int = _GIT_COMMAND_TIMEOUT_SEC
) -> None:
    try:
        subprocess.run(
            ["git", "branch", "-d", branch_name],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except Exception:  # noqa: BLE001
        pass


def _merge_single_branch(
    cwd: Path,
    base_branch: str,
    task_id: str,
    branch_name: str,
    timeout: int = _GIT_COMMAND_TIMEOUT_SEC,
) -> MergeResult:
    """Merge a single worktree branch into the current branch."""
    try:
        result = subprocess.run(
            [
                "git",
                "merge",
                "--no-ff",
                "-m",
                f"Merge parallel-orchestra task {task_id}",
                branch_name,
            ],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if result.returncode == 0:
            _delete_branch(cwd, branch_name)
            return MergeResult(
                task_id=task_id,
                branch_name=branch_name,
                status="merged",
                stderr=_sanitize_git_stderr(result.stderr or ""),
            )
        else:
            _abort_merge(cwd)
            return MergeResult(
                task_id=task_id,
                branch_name=branch_name,
                status="conflict",
                stderr=_sanitize_git_stderr(result.stderr or ""),
            )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _abort_merge(cwd)
        return MergeResult(
            task_id=task_id,
            branch_name=branch_name,
            status="error",
            stderr=str(exc),
        )


def _build_conflict_message(
    conflict: MergeResult,
    pending: list[str],
) -> str:
    lines = [
        f"Merge conflict detected in task '{conflict.task_id}' "
        f"on branch '{conflict.branch_name}'.",
    ]
    if conflict.stderr:
        lines.append(f"\nGit output:\n{conflict.stderr}")
    if pending:
        lines.append("\nThe following branches were NOT merged (pending):")
        for b in pending:
            lines.append(f"  - {b}")
    lines.append(
        "\nTo resolve manually:\n"
        f"  1. Inspect the conflict: git merge {conflict.branch_name}\n"
        "  2. Resolve the conflicting files.\n"
        "  3. Stage the resolved files: git add <files>\n"
        "  4. Complete the merge: git commit\n"
        "  5. Repeat for each pending branch above."
    )
    return "\n".join(lines)


def _merge_write_branches(
    cwd: Path,
    base_branch: str,
    results: tuple[TaskResult, ...],
    timeout: int = _GIT_COMMAND_TIMEOUT_SEC,
) -> tuple[MergeResult, ...]:
    """Merge all successful write-task branches into *base_branch* in manifest order."""
    eligible: list[TaskResult] = [
        tr for tr in results if tr.ok and tr.branch_name is not None
    ]

    merge_results: list[MergeResult] = []
    for i, tr in enumerate(eligible):
        merge_result = _merge_single_branch(
            cwd, base_branch, tr.task_id, tr.branch_name  # type: ignore[arg-type]
        )
        merge_results.append(merge_result)
        if merge_result.status == "conflict":
            pending_branches = [
                t.branch_name for t in eligible[i + 1 :] if t.branch_name is not None
            ]
            raise RunnerError(_build_conflict_message(merge_result, pending_branches))

    return tuple(merge_results)


def _parse_agent_json(stdout: str) -> dict[str, Any] | None:
    """Try to parse the last non-empty line of agent stdout as a JSON object."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        data = json.loads(lines[-1])
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return None


def _auto_commit_worktree(worktree_path: Path, task_id: str) -> None:
    """Commit any changes left uncommitted by the agent (best-effort).

    Sets core.autocrlf=false first to prevent Windows CRLF conversion from
    accidentally staging pre-existing tracked files as modified.
    """
    try:
        # Disable autocrlf to avoid Windows LF→CRLF issues causing false
        # "modified" state on tracked files when running git add -A.
        subprocess.run(
            ["git", "config", "core.autocrlf", "false"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_COMMAND_TIMEOUT_SEC,
            check=False,
        )
        # Restore the empty CLAUDE.md that PO wrote to suppress agent startup
        # protocols, so it is not committed and does not overwrite the original
        # in the main branch after merge.
        subprocess.run(
            ["git", "restore", ".claude/CLAUDE.md"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_COMMAND_TIMEOUT_SEC,
            check=False,
        )
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_COMMAND_TIMEOUT_SEC,
            check=False,
        )
        commit_message = f"parallel-orchestra: task {task_id} [auto-commit]"
        subprocess.run(
            ["git", "commit", "-m", commit_message],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_COMMAND_TIMEOUT_SEC,
            check=False,
        )
    except Exception:  # noqa: BLE001
        pass  # best-effort: never block task result


def _worktree_cleanup(git_root: Path, worktree_path: Path) -> None:
    """Remove a git worktree on a best-effort basis."""
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=str(git_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_COMMAND_TIMEOUT_SEC,
            check=True,
        )
    except Exception:  # noqa: BLE001
        pass


def _stream_reader(stream: IO[str], buf: list[str], state: _RunState) -> None:
    for line in stream:
        buf.append(line)
        with state.lock:
            state.last_output_ts = time.perf_counter()
            state.has_received_output = True


def _stream_json_reader(
    stream: IO[str],
    result_buf: list[str],
    state: _RunState,
    task_id: str,
    dashboard: _Dashboard,
) -> None:
    # NOTE: 現在 `_run_with_progress` は常に `_stream_reader` を使用する。
    # `--output-format stream-json` を cmd に追加した際にこの関数に切り替える。
    # tool_use イベントからの current_action 更新・tokens_out 集計はその時点で有効化される。
    for line in stream:
        with state.lock:
            state.last_output_ts = time.perf_counter()
            state.has_received_output = True

        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type", "")

        if event_type == "assistant":
            content = event.get("message", {}).get("content", [])
            for block in content:
                if block.get("type") == "tool_use":
                    action = _format_tool_action(
                        block.get("name", ""), block.get("input", {})
                    )
                    dashboard.update(task_id, current_action=action, status="running")
                    break
            else:
                dashboard.update(task_id, current_action="", status="running")

        elif event_type == "user":
            dashboard.update(task_id, current_action="", status="running")

        elif event_type == "result":
            result_text = event.get("result", "")
            result_buf.append(result_text)
            tokens_out = event.get("usage", {}).get("output_tokens", 0)
            if tokens_out:
                dashboard.update(task_id, tokens_out=tokens_out)


def _watchdog_loop(
    proc: subprocess.Popen[str],
    task: Task,
    start: float,
    state: _RunState,
    dashboard: _Dashboard | None = None,
) -> None:
    """Watch *proc*, print progress, and kill it when the total timeout is exceeded."""
    while True:
        now = time.perf_counter()
        total_remaining = _INTERNAL_TIMEOUT_SEC - (now - start)
        sleep_sec = min(
            _PROGRESS_INTERVAL_SEC,
            max(0.05, total_remaining),
        )
        if state.done_event.wait(timeout=sleep_sec):
            return

        now = time.perf_counter()
        with state.lock:
            last_ts = state.last_output_ts
            received = state.has_received_output
        idle = now - last_ts
        total = now - start

        # state 更新は dashboard.enabled に関係なく行う（summary loop が snapshot
        # を読んで状況を集計するため）。render は dashboard 内で gate される。
        if dashboard is not None:
            if not received and total < _STARTUP_DISPLAY_SEC:
                dashboard.update(task.id, status="starting_up", important=False)
            elif idle >= _PROGRESS_INTERVAL_SEC:
                dashboard.update(task.id, current_action="", important=False)
        # 非 TTY 時の per-task thinking / running / starting up の print は
        # 出力が膨らむため抑制する。代わりに run_manifest が起動する
        # _summary_loop が 30 秒間隔で wave 全体のサマリを 1 行で出す。

        if total >= _INTERNAL_TIMEOUT_SEC:
            with state.lock:
                state.kill_reason = "total"
            proc.kill()
            return


def _run_with_progress(
    proc: subprocess.Popen[str],
    task: Task,
    start: float,
    dashboard: _Dashboard | None = None,
) -> tuple[str, str, bool, Literal["total"] | None]:
    """Run *proc* to completion with progress reporting and total timeout.

    Returns:
        (stdout, stderr, timed_out, timeout_reason)
    """
    lines_stdout: list[str] = []
    lines_stderr: list[str] = []
    state = _RunState(last_output_ts=start, has_received_output=False)

    stdout_thread = threading.Thread(
        target=_stream_reader, args=(proc.stdout, lines_stdout, state), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_stream_reader, args=(proc.stderr, lines_stderr, state), daemon=True
    )
    watchdog_thread = threading.Thread(
        target=_watchdog_loop,
        args=(proc, task, start, state, dashboard),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    watchdog_thread.start()

    proc.wait()
    state.done_event.set()
    watchdog_thread.join()
    stdout_thread.join()
    stderr_thread.join()

    reason: Literal["total"] | None = state.kill_reason
    timed_out = reason is not None
    return "".join(lines_stdout), "".join(lines_stderr), timed_out, reason


def _resolve_effective_model(
    task: Task, tier_selection: dict | None
) -> tuple[str | None, str]:
    """F-005 Phase 2-A: タスクの最終 effective_model と override_source を解決する。

    優先順位（高い順）:
      1. ``task.model_override``（manifest 側で明示指定）
      2. ``tier_selection.json`` の ``suggested_model``（F-005 動的推奨）
      3. None（agent frontmatter に任せる、最後の砦）

    Returns:
        ``(effective_model, override_source)``。
        ``effective_model`` が None なら ``--agent`` 単独で起動、それ以外は
        ``--agents`` JSON で model を上書き起動する。
        ``override_source`` は ``"manifest" | "tier_selection" | "frontmatter"``。
    """
    if task.model_override:
        return task.model_override, "manifest"
    if tier_selection:
        suggested = tier_selection.get("suggested_model")
        if isinstance(suggested, str) and suggested:
            return suggested, "tier_selection"
    return None, "frontmatter"


def _build_claude_cmd(task: Task, claude_exe: str, effective_model: str | None) -> list[str]:
    """claude CLI のコマンドライン引数リストを組み立てる。"""
    cmd = [claude_exe, "--dangerously-skip-permissions"]
    if task.agent:
        if effective_model is not None:
            agents_obj = {task.agent: {"model": effective_model}}
            cmd.extend(["--agents", json.dumps(agents_obj, separators=(",", ":"))])
        else:
            cmd.extend(["--agent", task.agent])
    cmd.extend([_CLAUDE_PROMPT_FLAG, task.prompt])
    return cmd


def _inject_session_env(
    env: dict,
    *,
    task_id: str,
    po_session_id: str,
    branch_name: str | None,
    effective_cwd: Path,
) -> None:
    """F-002 Phase 2-A: 子プロセスの env に PO セッション情報を注入する。"""
    env["C3_PO_SESSION_ID"] = po_session_id
    env["C3_PO_TASK_ID"] = task_id
    env["C3_PO_WORKTREE_ID"] = branch_name if branch_name is not None else READ_ONLY_WORKTREE_ID
    _db_path = locate_c3_db(start=effective_cwd)
    if _db_path is not None:
        env["C3_PO_DB_PATH"] = str(_db_path)


def _read_tier_selection() -> dict | None:
    """``.claude/state/tier_selection.json`` を読んで dict を返す。

    存在しない / 壊れている場合は None。runner.py が PO 起動時に各タスクの
    model 上書き推奨を確認するために使う。F-005 select_tier.py が UserPromptSubmit
    hook で書き込む json を共有読みする形。
    """
    # cwd から見て .claude/state/tier_selection.json を探す。
    # PO は通常リポジトリルートで起動されるためこの相対パスで十分。
    selection_path = Path.cwd() / ".claude" / "state" / "tier_selection.json"
    if not selection_path.is_file():
        return None
    try:
        data = json.loads(selection_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _execute_task(
    task: Task,
    claude_exe: str,
    *,
    git_root: Path | None = None,
    effective_cwd: Path,
    dashboard: _Dashboard | None = None,
    po_session_id: str | None = None,
) -> TaskResult:
    """Execute a single agent task as a subprocess and return its result.

    For read_only=False tasks, a dedicated git worktree is created and
    PO_WORKTREE_GUARD=1 is set in the environment automatically.
    For read_only=True tasks, effective_cwd (passed by the caller) is used.

    F-002 Phase 2-A:
        ``po_session_id`` が指定されると、子プロセスの env に以下 4 変数を注入する:
          - ``C3_PO_DB_PATH`` (見つかった場合のみ): 親リポの c3.db への絶対パス
          - ``C3_PO_SESSION_ID``: PO 実行 ID
          - ``C3_PO_TASK_ID``: タスク ID
          - ``C3_PO_WORKTREE_ID``: ブランチ名 (write task) / ``"(read-only)"`` (read-only)
        子プロセスはこれらを使って `.claude/state/c3.db` に直接書き込める。
    """
    # F-005 Phase 2-A: model 動的切替の解決。shell=False + list 引数 + json.dumps で
    # Windows でもエスケープ問題なし（後方互換: model なし → --agent 単独起動）。
    tier_selection = _read_tier_selection()
    effective_model, _override_source = _resolve_effective_model(task, tier_selection)
    cmd = _build_claude_cmd(task, claude_exe, effective_model)
    env = {**os.environ, **task.env}

    branch_name: str | None = None
    task_cwd: Path
    worktree_path: Path | None = None

    if not task.read_only:
        if git_root is None:
            raise RunnerError(
                f"git_root must be provided for non-read-only task {task.id!r}"
            )
        # Source the worktree's .claude/ from the manifest's effective_cwd
        # when one exists there; otherwise fall back to the git-root's
        # .claude/. This lets a manifest under example/ pick up
        # example/.claude/ even though the git database lives at the parent.
        manifest_claude_dir = effective_cwd / ".claude"
        claude_src_dir = manifest_claude_dir if manifest_claude_dir.exists() else None
        worktree_path, branch_name = _setup_worktree(
            git_root, task, claude_src_dir=claude_src_dir
        )
        task_cwd = worktree_path
        env["PO_WORKTREE_GUARD"] = "1"
    else:
        task_cwd = effective_cwd

    # F-002 Phase 2-A: 子プロセスから c3.db を直接引けるように env を注入。
    # session_id を呼び出し側（run_manifest）が指定した場合のみ注入し、
    # 単体実行（既存テスト互換）では注入をスキップする。
    if po_session_id is not None:
        _inject_session_env(
            env,
            task_id=task.id,
            po_session_id=po_session_id,
            branch_name=branch_name,
            effective_cwd=effective_cwd,
        )

    start = time.perf_counter()
    if dashboard is not None and dashboard.enabled:
        dashboard.update(task.id, status="starting_up", start_ts=start)
    try:
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=task_cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise RunnerError(f"claude executable not found: {claude_exe!r}") from exc

        try:
            stdout, stderr, timed_out, timeout_reason = _run_with_progress(
                proc, task, start, dashboard
            )
            returncode: int | None = proc.returncode if not timed_out else None
        except Exception:
            duration_sec = time.perf_counter() - start
            if dashboard is not None and dashboard.enabled:
                dashboard.update(task.id, status="failed", elapsed_sec=duration_sec)
            return TaskResult(
                task_id=task.id,
                agent=task.agent,
                returncode=None,
                stdout="",
                stderr=traceback.format_exc(),
                timed_out=False,
                duration_sec=duration_sec,
                branch_name=branch_name,
            )

        # Parse structured JSON output from agent (e.g. tdd-develop status/report).
        agent_json = _parse_agent_json(stdout)
        agent_status: str | None = None
        agent_cycles: int | None = None
        agent_reason: str | None = None
        agent_report: str | None = None
        if agent_json:
            agent_status = str(agent_json["status"]) if "status" in agent_json else None
            raw_cycles = agent_json.get("cycles")
            agent_cycles = int(raw_cycles) if raw_cycles is not None else None
            agent_reason = str(agent_json["reason"]) if "reason" in agent_json else None
            agent_report = str(agent_json["report"]) if "report" in agent_json else None
            # If agent explicitly reports FAILED, treat as task failure.
            if agent_status == "FAILED" and returncode == 0:
                returncode = 1

        # Auto-commit any uncommitted agent changes before worktree cleanup.
        # On success, the worktree branch is later merged into main.
        # On failure / timeout, set PO_KEEP_WORKTREE=1 to retain the worktree.
        if worktree_path is not None and returncode == 0 and not timed_out:
            _auto_commit_worktree(worktree_path, task.id)

    finally:
        if worktree_path is not None and git_root is not None:
            if os.environ.get("PO_KEEP_WORKTREE") != "1":
                _worktree_cleanup(git_root, worktree_path)
            else:
                logger.info("PO_KEEP_WORKTREE=1: worktree kept at: %s", worktree_path)

    duration_sec = time.perf_counter() - start
    if dashboard is not None and dashboard.enabled:
        ok = returncode == 0 and not timed_out
        dashboard.update(
            task.id,
            status="complete" if ok else "failed",
            elapsed_sec=duration_sec,
        )
    return TaskResult(
        task_id=task.id,
        agent=task.agent,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        duration_sec=duration_sec,
        branch_name=branch_name,
        timeout_reason=timeout_reason,
        agent_status=agent_status,
        agent_cycles=agent_cycles,
        agent_reason=agent_reason,
        agent_report=agent_report,
    )


# ---------------------------------------------------------------------------
# Dependency Scheduler
# ---------------------------------------------------------------------------


class _DependencyScheduler:
    """Schedules tasks respecting ``depends_on`` DAG constraints."""

    def __init__(
        self,
        tasks: Sequence[Task],
        executor: ThreadPoolExecutor,
        execute_fn: Callable[[Task], TaskResult],
    ) -> None:
        self._tasks: Sequence[Task] = tasks
        self._executor: ThreadPoolExecutor = executor
        self._execute_fn: Callable[[Task], TaskResult] = execute_fn

        self._tasks_by_id: dict[str, Task] = {t.id: t for t in tasks}
        self._indegree: dict[str, int] = {t.id: len(t.depends_on) for t in tasks}
        self._reverse_deps: dict[str, list[str]] = {t.id: [] for t in tasks}
        for task in tasks:
            for dep_id in task.depends_on:
                self._reverse_deps[dep_id].append(task.id)

    def _should_skip(self, task: Task, results: dict[str, TaskResult]) -> bool:
        return any(not results[dep_id].ok for dep_id in task.depends_on)

    def _make_skipped(self, task: Task) -> TaskResult:
        return TaskResult(
            task_id=task.id,
            agent=task.agent,
            returncode=None,
            stdout="",
            stderr="",
            timed_out=False,
            duration_sec=0.0,
            skipped=True,
            branch_name=None,
        )

    def _unlock_task(
        self,
        task_id: str,
        results: dict[str, TaskResult],
        future_to_task: dict[Future[TaskResult], Task],
        pending: set[Future[TaskResult]],
    ) -> None:
        task = self._tasks_by_id[task_id]

        if self._should_skip(task, results):
            results[task_id] = self._make_skipped(task)
            self._propagate_skip(task, results)
        else:
            new_future: Future[TaskResult] = self._executor.submit(
                self._execute_fn, task
            )
            future_to_task[new_future] = task
            pending.add(new_future)

    def run(self) -> tuple[TaskResult, ...]:
        results: dict[str, TaskResult] = {}
        future_to_task: dict[Future[TaskResult], Task] = {}
        runner_error: RunnerError | None = None

        pending: set[Future[TaskResult]] = set()
        for task in self._tasks:
            if task.id in results:
                continue
            if self._indegree[task.id] == 0:
                future: Future[TaskResult] = self._executor.submit(
                    self._execute_fn, task
                )
                future_to_task[future] = task
                pending.add(future)

        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                task = future_to_task[future]
                exc = future.exception()

                if exc is not None:
                    if isinstance(exc, RunnerError):
                        if runner_error is None:
                            runner_error = exc
                        task_result = TaskResult(
                            task_id=task.id,
                            agent=task.agent,
                            returncode=None,
                            stdout="",
                            stderr=str(exc),
                            timed_out=False,
                            duration_sec=0.0,
                        )
                    else:
                        raise exc
                else:
                    task_result = future.result()

                results[task.id] = task_result

                for downstream_id in self._reverse_deps[task.id]:
                    self._indegree[downstream_id] -= 1
                    if self._indegree[downstream_id] == 0:
                        self._unlock_task(
                            downstream_id, results, future_to_task, pending
                        )

        if runner_error is not None:
            raise runner_error

        return tuple(results[t.id] for t in self._tasks)

    def _propagate_skip(
        self, skipped_task: Task, results: dict[str, TaskResult]
    ) -> None:
        for downstream_id in self._reverse_deps[skipped_task.id]:
            if downstream_id in results:
                continue
            self._indegree[downstream_id] -= 1
            downstream_task = self._tasks_by_id[downstream_id]
            results[downstream_id] = self._make_skipped(downstream_task)
            self._propagate_skip(downstream_task, results)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_manifest(
    manifest: Manifest | Path | str,
    *,
    max_workers: int | None = None,
    claude_executable: str = _DEFAULT_CLAUDE_EXECUTABLE,
    log_dir: Path | None = None,
    log_enabled: bool = True,
    report_path: Path | None = None,
    dashboard_enabled: bool | None = None,
) -> RunResult:
    """Run all tasks in a manifest concurrently using a thread pool.

    Args:
        manifest: A Manifest instance, or a Path/str pointing to a manifest file.
        max_workers: Maximum number of worker threads (default: 5).
        claude_executable: Name or path of the claude binary.
        log_dir: Directory for task stdout/stderr log files.
        log_enabled: When False, log writing is skipped entirely.
        report_path: When provided, write a JSON or Markdown run summary.
        dashboard_enabled: Override ANSI dashboard visibility.

    Returns:
        A RunResult containing a TaskResult for each task in the manifest.
    """
    if not isinstance(manifest, Manifest):
        manifest = load_manifest(manifest)

    _run_start_time = time.perf_counter()
    _run_started_at: datetime = datetime.now(tz=timezone.utc)
    tasks: Sequence[Task] = manifest.tasks
    workers = max_workers if max_workers is not None else _DEFAULT_MAX_WORKERS

    default_cwd = Path.cwd()

    has_write_tasks = any(not t.read_only for t in tasks)
    git_root: Path | None = None
    base_branch: str | None = None
    if has_write_tasks:
        git_root = _require_git_root(default_cwd)
        base_branch = _resolve_merge_base_branch(default_cwd)

    # Resolve the effective_cwd for read_only tasks from the manifest's cwd field.
    manifest_cwd = (manifest.path.parent / manifest.cwd).resolve()

    log_config: LogConfig | None
    if log_enabled:
        resolved_log_dir = (
            log_dir
            if log_dir is not None
            else (git_root or default_cwd) / ".claude" / "logs"
        )
        log_config = LogConfig(base_dir=resolved_log_dir)
    else:
        log_config = None

    group_semaphores: dict[str, threading.Semaphore] = {
        group: threading.Semaphore(limit)
        for group, limit in manifest.concurrency_limits.items()
    }

    task_count_by_group: dict[str, int] = {}
    for t in tasks:
        if t.concurrency_group is not None:
            task_count_by_group[t.concurrency_group] = (
                task_count_by_group.get(t.concurrency_group, 0) + 1
            )
    for group, limit in manifest.concurrency_limits.items():
        task_count = task_count_by_group.get(group, 0)
        if limit < workers and task_count >= workers:
            warnings.warn(
                f"Concurrency group '{group}' has limit {limit} but"
                f" {task_count} tasks and --max-workers={workers}."
                " If all worker slots are occupied waiting for this"
                " group's semaphore, throughput may degrade significantly."
                f" Consider setting --max-workers <= {limit} or"
                " splitting tasks across groups.",
                stacklevel=2,
            )

    _tty = sys.stderr.isatty()
    if dashboard_enabled is None:
        _dash_enabled = _tty  # Default: enabled only when stderr is a TTY
    else:
        _dash_enabled = dashboard_enabled
    dashboard = _Dashboard(
        [t.id for t in tasks],
        enabled=_dash_enabled,
        live_renders=_tty,
    )
    if _dash_enabled:
        dashboard.start()

    # F-002 / F-003 共通: session_id を上部で算出して両機能で共有する
    _po_session_id = (
        f"{manifest.name}_{_run_started_at.strftime('%Y%m%dT%H%M%SZ')}"
    )

    # F-003: dashboard が有効な場合のみ heartbeat スレッドを起動（30 秒間隔で
    # po_status テーブルに UPSERT）。非 TTY 環境では起動しない。
    _hb_stop_event: threading.Event | None = None
    _hb_thread: threading.Thread | None = None
    if _dash_enabled:
        _hb_stop_event = threading.Event()
        _hb_thread = threading.Thread(
            target=_heartbeat_po_status_loop,
            args=(dashboard, _po_session_id, _hb_stop_event),
            kwargs={"interval": 30.0},
            daemon=True,
            name="po-status-heartbeat",
        )
        _hb_thread.start()

    # 非 TTY 環境では per-task の thinking / running ログを抑制する代わりに、
    # 30 秒間隔で wave 全体のサマリ行を 1 行 stderr に出すスレッドを起動する。
    # env C3_PO_SUMMARY_INTERVAL_SEC で間隔を上書き可能。
    _sum_stop_event: threading.Event | None = None
    _sum_thread: threading.Thread | None = None
    if not _dash_enabled:
        _sum_stop_event = threading.Event()
        _sum_thread = threading.Thread(
            target=_summary_loop,
            args=(dashboard, _sum_stop_event),
            kwargs={"interval": _resolve_summary_interval()},
            daemon=True,
            name="po-summary",
        )
        _sum_thread.start()

    def execute_fn(task: Task) -> TaskResult:
        sem: threading.Semaphore | None = (
            group_semaphores.get(task.concurrency_group)
            if task.concurrency_group is not None
            else None
        )
        if sem is not None:
            sem.acquire()
        try:
            result = _execute_with_retry(
                task, claude_executable,
                git_root=git_root,
                effective_cwd=manifest_cwd,
                log_config=log_config,
                dashboard=dashboard,
                po_session_id=_po_session_id,
            )
        finally:
            if sem is not None:
                sem.release()
        return result

    with ThreadPoolExecutor(max_workers=workers) as executor:
        scheduler = _DependencyScheduler(tasks, executor, execute_fn)
        task_results: tuple[TaskResult, ...] = scheduler.run()

    for tr in task_results:
        if tr.skipped:
            dashboard.update(tr.task_id, status="skipped")

    # F-003: heartbeat 終了 → 最終状態をもう一度 UPSERT してから停止
    if _hb_thread is not None and _hb_stop_event is not None:
        # 最終状態を即座に反映するため、heartbeat スレッドが寝ている可能性に
        # 関わらず明示的に upsert を 1 回実行する
        try:
            from .c3_db import upsert_po_status  # noqa: PLC0415

            for s in dashboard.snapshot_states():
                if s.status == "waiting":
                    continue
                state_label = _PO_STATUS_STATE_MAPPING.get(s.status, "running")
                upsert_po_status(
                    session_id=_po_session_id,
                    worktree_id=s.task_id,
                    state=state_label,
                    current_step=s.current_action or None,
                )
        except Exception:  # noqa: BLE001
            pass
        _hb_stop_event.set()
        _hb_thread.join(timeout=2.0)

    # サマリ行スレッドの停止（非 TTY モード）
    if _sum_thread is not None and _sum_stop_event is not None:
        _sum_stop_event.set()
        _sum_thread.join(timeout=2.0)

    dashboard.stop()

    merge_results: tuple[MergeResult, ...] = ()
    if has_write_tasks and base_branch is not None:
        merge_results = _merge_write_branches(default_cwd, base_branch, task_results)

    run_result = RunResult(results=task_results, merge_results=merge_results)

    _run_finished_at = datetime.now(tz=timezone.utc)

    # F-002: 実行結果を `.claude/state/c3.db` の po_results に記録する。
    # DB が無い環境（C3 利用先で init_c3_db.py が走っていない等）や記録エラー時は
    # 静かにスキップする（PO 本体を止めない）。session_id は F-003 と共通。
    try:
        from .c3_db import record_task_results  # noqa: PLC0415

        record_task_results(
            task_results,
            session_id=_po_session_id,
            started_at=_run_started_at,
            finished_at=_run_finished_at,
        )
    except Exception:  # noqa: BLE001 - 観測機能なので PO 本体を止めない
        pass

    if report_path is not None:
        from .report import generate_report  # noqa: PLC0415

        try:
            generate_report(
                run_result,
                report_path,
                manifest_name=manifest.name,
                started_at=_run_started_at,
                finished_at=_run_finished_at,
            )
        except RunnerError:
            raise
        except Exception as exc:
            raise RunnerError(f"Report generation failed: {exc}") from exc

    return run_result
