"""Tests for ``c3.gitutil`` — subprocess monkeypatched; no real git invocations."""

from __future__ import annotations

import subprocess
from pathlib import Path

from c3 import gitutil
from c3.gitutil import GitStatus


class _FakeResult:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


# ---------------------------------------------------------------------------
# detect_git_status
# ---------------------------------------------------------------------------


def test_detect_git_status_inside_repo(tmp_path: Path, monkeypatch) -> None:
    """returncode=0 with stdout 'true\\n' reports INSIDE_REPO; call uses check=False."""
    calls: list = []

    def fake_run(args, *, cwd=None, capture_output=False, text=False, check=False, **kw):
        calls.append({"args": list(args), "cwd": cwd, "check": check})
        return _FakeResult(returncode=0, stdout="true\n")

    monkeypatch.setattr("c3.gitutil.subprocess.run", fake_run)

    result = gitutil.detect_git_status(tmp_path)

    assert result is GitStatus.INSIDE_REPO
    assert calls[0]["args"] == ["git", "rev-parse", "--is-inside-work-tree"]
    assert calls[0]["cwd"] == str(tmp_path)
    assert calls[0]["check"] is False


def test_detect_git_status_not_a_repo(tmp_path: Path, monkeypatch) -> None:
    """returncode=128 (git 管理外の慣例値) from rev-parse reports NOT_A_REPO."""

    def fake_run(args, *, cwd=None, capture_output=False, text=False, check=False, **kw):
        return _FakeResult(returncode=128, stdout="")

    monkeypatch.setattr("c3.gitutil.subprocess.run", fake_run)

    result = gitutil.detect_git_status(tmp_path)

    assert result is GitStatus.NOT_A_REPO


def test_detect_git_status_unexpected_nonzero_falls_back_to_inside_repo(
    tmp_path: Path, monkeypatch
) -> None:
    """returncode=1 (128 以外の予期しない非ゼロ) → 安全側 INSIDE_REPO にフォールバックする。

    B-2: git init を誘発しない安全側フォールバック仕様。現実装は NOT_A_REPO を返すため Red。
    """

    def fake_run(args, *, cwd=None, capture_output=False, text=False, check=False, **kw):
        return _FakeResult(returncode=1, stdout="")

    monkeypatch.setattr("c3.gitutil.subprocess.run", fake_run)

    result = gitutil.detect_git_status(tmp_path)

    assert result is GitStatus.INSIDE_REPO


def test_detect_git_status_git_missing(tmp_path: Path, monkeypatch) -> None:
    """FileNotFoundError from subprocess (git absent from PATH) reports GIT_MISSING."""

    def fake_run(*_args, **_kw):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr("c3.gitutil.subprocess.run", fake_run)

    result = gitutil.detect_git_status(tmp_path)

    assert result is GitStatus.GIT_MISSING


def test_detect_git_status_timeout_falls_back_to_inside_repo(
    tmp_path: Path, monkeypatch
) -> None:
    """TimeoutExpired は捕捉し INSIDE_REPO を返す（例外を外に送出しない）。

    C-1: 現実装は TimeoutExpired を捕捉しないため Red（例外が漏れる）。
    """

    def fake_run(*_args, **_kw):
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=10)

    monkeypatch.setattr("c3.gitutil.subprocess.run", fake_run)

    result = gitutil.detect_git_status(tmp_path)

    assert result is GitStatus.INSIDE_REPO


def test_detect_git_status_passes_timeout_kwarg(tmp_path: Path, monkeypatch) -> None:
    """subprocess.run に timeout=10 が渡されること。

    C-3: 現実装は timeout 未指定のため Red。
    """
    captured_kw: dict = {}

    def fake_run(args, *, cwd=None, capture_output=False, text=False, check=False, **kw):
        captured_kw.update(kw)
        return _FakeResult(returncode=0, stdout="true\n")

    monkeypatch.setattr("c3.gitutil.subprocess.run", fake_run)

    gitutil.detect_git_status(tmp_path)

    assert captured_kw.get("timeout") == 10


def test_detect_git_status_passes_encoding_kwargs(tmp_path: Path, monkeypatch) -> None:
    """subprocess.run に encoding='utf-8' と errors='replace' が渡されること。

    D-1: 現実装は encoding/errors 未指定のため Red。
    """
    captured_kw: dict = {}

    def fake_run(args, *, cwd=None, capture_output=False, text=False, check=False, **kw):
        captured_kw.update(kw)
        return _FakeResult(returncode=0, stdout="true\n")

    monkeypatch.setattr("c3.gitutil.subprocess.run", fake_run)

    gitutil.detect_git_status(tmp_path)

    assert captured_kw.get("encoding") == "utf-8"
    assert captured_kw.get("errors") == "replace"


# ---------------------------------------------------------------------------
# git_init
# ---------------------------------------------------------------------------


def test_git_init_success(tmp_path: Path, monkeypatch) -> None:
    """returncode=0 returns True; call uses cwd=str(target_root) and check=False."""
    calls: list = []

    def fake_run(args, *, cwd=None, capture_output=False, text=False, check=False, **kw):
        calls.append({"args": list(args), "cwd": cwd, "check": check})
        return _FakeResult(returncode=0)

    monkeypatch.setattr("c3.gitutil.subprocess.run", fake_run)

    result = gitutil.git_init(tmp_path)

    assert result is True
    assert calls[0]["args"] == ["git", "init"]
    assert calls[0]["cwd"] == str(tmp_path)
    assert calls[0]["check"] is False


def test_git_init_failure_nonzero(tmp_path: Path, monkeypatch) -> None:
    """Non-zero returncode returns False."""

    def fake_run(*_args, **_kw):
        return _FakeResult(returncode=128)

    monkeypatch.setattr("c3.gitutil.subprocess.run", fake_run)

    result = gitutil.git_init(tmp_path)

    assert result is False


def test_git_init_git_missing(tmp_path: Path, monkeypatch) -> None:
    """FileNotFoundError is caught and returns False without propagating the exception."""

    def fake_run(*_args, **_kw):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr("c3.gitutil.subprocess.run", fake_run)

    result = gitutil.git_init(tmp_path)

    assert result is False


def test_git_init_timeout_returns_false(tmp_path: Path, monkeypatch) -> None:
    """TimeoutExpired は捕捉し False を返す（例外を外に送出しない）。

    C-2: 現実装は TimeoutExpired を捕捉しないため Red（例外が漏れる）。
    """

    def fake_run(*_args, **_kw):
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=10)

    monkeypatch.setattr("c3.gitutil.subprocess.run", fake_run)

    result = gitutil.git_init(tmp_path)

    assert result is False


def test_git_init_passes_timeout_kwarg(tmp_path: Path, monkeypatch) -> None:
    """subprocess.run に timeout=10 が渡されること。

    C-3: 現実装は timeout 未指定のため Red。
    """
    captured_kw: dict = {}

    def fake_run(args, *, cwd=None, capture_output=False, text=False, check=False, **kw):
        captured_kw.update(kw)
        return _FakeResult(returncode=0)

    monkeypatch.setattr("c3.gitutil.subprocess.run", fake_run)

    gitutil.git_init(tmp_path)

    assert captured_kw.get("timeout") == 10


def test_git_init_passes_encoding_kwargs(tmp_path: Path, monkeypatch) -> None:
    """subprocess.run に encoding='utf-8' と errors='replace' が渡されること。

    D-1: 現実装は encoding/errors 未指定のため Red。
    """
    captured_kw: dict = {}

    def fake_run(args, *, cwd=None, capture_output=False, text=False, check=False, **kw):
        captured_kw.update(kw)
        return _FakeResult(returncode=0)

    monkeypatch.setattr("c3.gitutil.subprocess.run", fake_run)

    gitutil.git_init(tmp_path)

    assert captured_kw.get("encoding") == "utf-8"
    assert captured_kw.get("errors") == "replace"
