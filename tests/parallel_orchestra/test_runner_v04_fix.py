"""Tests for parallel_orchestra.runner merge and sanitize functions."""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import parallel_orchestra.runner as runner_module
from parallel_orchestra.runner import MergeResult, RunnerError


def _get_runner_source() -> str:
    return inspect.getsource(runner_module)


# ---------------------------------------------------------------------------
# _merge_single_branch: uses -m flag (not --no-edit)
# ---------------------------------------------------------------------------


def test_merge_single_branch_git_コマンドに_m_フラグが含まれる(tmp_path, monkeypatch):
    merge_single = getattr(runner_module, "_merge_single_branch", None)
    assert merge_single is not None

    captured_cmds: list[list[str]] = []
    fake_ok = MagicMock()
    fake_ok.returncode = 0
    fake_ok.stdout = ""
    fake_ok.stderr = ""

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list):
            captured_cmds.append(list(cmd))
        return fake_ok

    monkeypatch.setattr(runner_module, "_delete_branch", lambda *a: None)
    monkeypatch.setattr(runner_module, "_abort_merge", lambda *a: None)
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    task_id = "task-xyz"
    branch = "parallel-orchestra/task-xyz-aabbccdd"
    merge_single(tmp_path, "main", task_id, branch)

    merge_cmds = [cmd for cmd in captured_cmds if "merge" in cmd and "--abort" not in cmd]
    assert len(merge_cmds) >= 1
    merge_cmd = merge_cmds[0]

    assert "-m" in merge_cmd
    expected_msg = f"Merge parallel-orchestra task {task_id}"
    assert expected_msg in merge_cmd


def test_merge_single_branch_git_コマンドに_no_edit_が含まれない(tmp_path, monkeypatch):
    merge_single = getattr(runner_module, "_merge_single_branch", None)
    assert merge_single is not None

    captured_cmds: list[list[str]] = []
    fake_ok = MagicMock()
    fake_ok.returncode = 0
    fake_ok.stdout = ""
    fake_ok.stderr = ""

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list):
            captured_cmds.append(list(cmd))
        return fake_ok

    monkeypatch.setattr(runner_module, "_delete_branch", lambda *a: None)
    monkeypatch.setattr(runner_module, "_abort_merge", lambda *a: None)
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    merge_single(tmp_path, "main", "task-abc", "parallel-orchestra/task-abc-12345678")

    merge_cmds = [cmd for cmd in captured_cmds if "merge" in cmd and "--abort" not in cmd]
    assert len(merge_cmds) >= 1
    assert "--no-edit" not in merge_cmds[0]


# ---------------------------------------------------------------------------
# _sanitize_git_stderr
# ---------------------------------------------------------------------------


def test_sanitize_git_stderr_関数が存在する():
    sanitize = getattr(runner_module, "_sanitize_git_stderr", None)
    assert sanitize is not None and callable(sanitize)


def test_sanitize_git_stderr_ANSIエスケープシーケンスを除去する():
    sanitize = getattr(runner_module, "_sanitize_git_stderr")
    result = sanitize("\x1b[31mred text\x1b[0m")
    assert "\x1b" not in result
    assert "red text" in result


def test_sanitize_git_stderr_制御文字を除去する():
    sanitize = getattr(runner_module, "_sanitize_git_stderr")
    result = sanitize("normal\x00text\x01more\nkept")
    assert "\x00" not in result
    assert "\x01" not in result
    assert "normal" in result
    assert "text" in result
    assert "\n" in result  # newlines are kept


def test_sanitize_git_stderr_2000文字超の入力をトランケートする():
    sanitize = getattr(runner_module, "_sanitize_git_stderr")
    long_text = "a" * 3000
    result = sanitize(long_text)
    assert len(result) <= 2000


def test_sanitize_git_stderr_空文字列で例外が発生しない():
    sanitize = getattr(runner_module, "_sanitize_git_stderr")
    result = sanitize("")
    assert result == ""


# ---------------------------------------------------------------------------
# MergeResult.status is Literal type
# ---------------------------------------------------------------------------


def test_MergeResultのstatusにmerged_conflict_errorの値が使える():
    for status in ("merged", "conflict", "error"):
        mr = MergeResult(
            task_id="t",
            branch_name=f"parallel-orchestra/t-00000000",
            status=status,
            stderr="",
        )
        assert mr.status == status


# ---------------------------------------------------------------------------
# run_manifest uses git symbolic-ref in exactly 1 location
# ---------------------------------------------------------------------------


def test_run_manifest_symbolic_refの呼び出しが1箇所のみ():
    source = _get_runner_source()
    count = source.count("symbolic-ref")
    assert count == 1, f"Expected exactly 1 'symbolic-ref' in runner.py, found {count}"
