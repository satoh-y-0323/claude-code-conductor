"""
Tests for pre_compact.py basic runtime behavior.

These tests verify that:
  1. Normal execution produces no stdout output and appends a PreCompact
     checkpoint block to the session file (checkpoint-only responsibility;
     PreCompact does not support hookSpecificOutput.additionalContext per the
     official hooks spec).
  2. Worktree detection exits 0 with no stdout output.

Implementation notes:
  - TestPreCompactNormalExecution [T3, architecture-report-20260704-065052.md
    §8-1 案 A] runs pre_compact.py's main() in-process instead of via
    subprocess. The old subprocess approach wrote to the real
    .claude/memory/sessions/ directory (SESSIONS_DIR is derived from
    __file__ inside pre_compact.py, not from cwd, so subprocess execution
    cannot redirect it to a tmp dir). After debounce was introduced, two
    subprocess runs within the 10s window would interfere with each other
    (2nd run gets skipped -> empty stdout -> false failure). In-process
    execution overrides `mod.SESSIONS_DIR` per-test via monkeypatch, so each
    test gets a fresh tmp sessions dir with no prior checkpoint and no
    real-state writes.
  - TestPreCompactWorktreeDetection remains subprocess-based: the worktree
    guard runs before SESSIONS_DIR/session_file is touched, so no real state
    is read or written, and subprocess is still valuable here for verifying
    the actual `sys.exit(0)` process-exit behavior end-to-end.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests._pre_compact_helpers import (
    PRE_COMPACT_PY,
    _run_main_in_process,
)

# `_load_pre_compact_module` / `_run_main_in_process` は tests/_pre_compact_helpers.py
# （CR-M-001 対応の共通モジュール）から import する。tests/test_pre_compact.py と
# 重複定義しない。`_run_main_in_process` は `(module, sessions_dir, fake_stdout)` の
# 3-tuple を返す。本ファイルでは fake_stdout のみ使用する。

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_valid_input() -> bytes:
    """Return a minimal valid JSON payload for pre_compact.py stdin."""
    return json.dumps({"trigger": "manual", "context_items_before": 10}).encode()


def _run_pre_compact(cwd: Path, stdin: bytes) -> subprocess.CompletedProcess:
    """Run pre_compact.py as a subprocess with the given cwd and stdin bytes."""
    return subprocess.run(
        [sys.executable, str(PRE_COMPACT_PY)],
        input=stdin,
        capture_output=True,
        cwd=str(cwd),
    )


# ---------------------------------------------------------------------------
# Test Case 1: Normal execution — stdout is empty, checkpoint is appended
# ---------------------------------------------------------------------------

class TestPreCompactNormalExecution:
    """pre_compact.py in a non-worktree directory must not emit stdout output
    and must append a PreCompact checkpoint block to the session file.

    PreCompact hooks do not support `hookSpecificOutput.additionalContext`
    per the official hooks spec, so the hook's sole responsibility during
    normal execution is checkpoint-file bookkeeping (via
    `session_utils.append_checkpoint`), not stdout output.

    [T3] in-process 隔離方式に移行済み（旧: subprocess で実 sessions dir に
    連続書き込みしていたため、デバウンス導入後に 2 本目が干渉して赤化していた）。
    """

    def test_stdout_is_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """stdout must be empty when run outside a worktree (checkpoint-only)."""
        _, _, fake_stdout = _run_main_in_process(
            monkeypatch, tmp_path, {"trigger": "manual", "context_items_before": 10}
        )
        stdout = fake_stdout.getvalue().strip()
        assert stdout == "", (
            "stdout must be empty during normal (non-worktree) execution "
            f"(checkpoint-only hook). Got: {stdout!r}"
        )

    def test_checkpoint_is_appended_to_session_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A PreCompact checkpoint block must be appended to the session file."""
        module, sessions_dir, _ = _run_main_in_process(
            monkeypatch, tmp_path, {"trigger": "manual", "context_items_before": 10}
        )
        today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        session_file = sessions_dir / f"{today_str}.tmp"
        assert session_file.exists(), (
            f"session file must be created: {session_file}"
        )
        content = session_file.read_text(encoding="utf-8")
        assert "## [Checkpoint: PreCompact:" in content, (
            "session file must contain an appended PreCompact checkpoint "
            f"block. Actual content: {content!r}"
        )


# ---------------------------------------------------------------------------
# Test Case 2: Worktree detection — exits 0 with no stdout output
# ---------------------------------------------------------------------------

class TestPreCompactWorktreeDetection:
    """pre_compact.py must exit 0 silently when .git is a file (worktree)."""

    def test_exits_zero_in_worktree(self, tmp_path: Path) -> None:
        """When .git is a file (git worktree), exit code must be 0."""
        git_file = tmp_path / ".git"
        git_file.write_text(
            "gitdir: ../../.git/worktrees/some-worktree\n",
            encoding="utf-8",
        )

        result = _run_pre_compact(tmp_path, _make_valid_input())
        assert result.returncode == 0, (
            f"Expected exit code 0 in worktree, got {result.returncode}.\n"
            f"stderr: {result.stderr.decode(errors='replace')}"
        )

    def test_no_stdout_in_worktree(self, tmp_path: Path) -> None:
        """When .git is a file (git worktree), stdout must be empty."""
        git_file = tmp_path / ".git"
        git_file.write_text(
            "gitdir: ../../.git/worktrees/some-worktree\n",
            encoding="utf-8",
        )

        result = _run_pre_compact(tmp_path, _make_valid_input())
        assert result.stdout.strip() == b"", (
            f"Expected no stdout in worktree, got: {result.stdout!r}"
        )
