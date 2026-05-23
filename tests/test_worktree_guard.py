"""Tests for .claude/hooks/worktree_guard.py

Tests are organised around the CWD-based activation scheme:
1. CWD outside .claude/worktrees/ → exits 0 (guard disabled, silent)
2. CWD inside .claude/worktrees/ + Write inside worktree → exits 0
3. CWD inside .claude/worktrees/ + Write outside worktree → exits 2 + stderr
4. Block message sanitizes ANSI escapes in file_path (sec-Low)
5. env gate: PO_WORKTREE_GUARD 未設定 → no-op (CWD が worktree 内でも exit 0)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Absolute path to the hook under test
_HOOK = Path(__file__).parent.parent / ".claude" / "hooks" / "worktree_guard.py"

# Absolute path to the template copy of the hook
_TEMPLATE_HOOK = (
    Path(__file__).parent.parent
    / "src"
    / "c3"
    / "_template"
    / ".claude"
    / "hooks"
    / "worktree_guard.py"
)


def _make_worktree_cwd(base: Path) -> Path:
    """`base/.claude/worktrees/agent-test/` を作って返す（worktree-shaped CWD）."""
    worktree = base / ".claude" / "worktrees" / "agent-test"
    worktree.mkdir(parents=True, exist_ok=True)
    return worktree


def _run_guard(
    payload: dict,
    *,
    cwd: str | None = None,
    hook: Path = _HOOK,
    enable_guard: bool = True,
) -> subprocess.CompletedProcess:
    """Run worktree_guard.py as a subprocess, feeding *payload* via stdin.

    worktree_guard.py は `PO_WORKTREE_GUARD=1` が設定されている場合のみ動作する。
    デフォルトでガード有効化（`enable_guard=True`）でテストする。
    """
    env: dict[str, str] = {}
    if enable_guard:
        env["PO_WORKTREE_GUARD"] = "1"
    # Windows では subprocess に SYSTEMROOT を継承させないと sys.executable 起動が失敗する
    for key in ("SYSTEMROOT", "PATH"):
        if key in os.environ:
            env[key] = os.environ[key]
    return subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=cwd,
    )


# ---------------------------------------------------------------------------
# 1. CWD outside .claude/worktrees/ → exits 0 silently
# ---------------------------------------------------------------------------


def test_guard_disabled_when_cwd_outside_worktrees(tmp_path: Path):
    """CWD が .claude/worktrees/ 外なら exit 0 で何もしない（main セッション扱い）."""
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "/some/outside/path/file.txt"},
    }
    result = _run_guard(payload, cwd=str(tmp_path))
    assert result.returncode == 0, (
        f"Expected exit 0 when CWD is outside .claude/worktrees/, "
        f"got {result.returncode}"
    )
    assert not result.stderr.strip(), (
        f"Expected NO stderr output when guard is inactive, but got: {result.stderr!r}"
    )


def test_template_guard_disabled_when_cwd_outside_worktrees(tmp_path: Path):
    """Template copy も同じ挙動: CWD が外なら静かに exit 0."""
    import pytest

    if not _TEMPLATE_HOOK.exists():
        pytest.skip(f"Template hook not found at {_TEMPLATE_HOOK}; skipping.")

    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "/some/outside/path/file.txt"},
    }
    result = _run_guard(payload, cwd=str(tmp_path), hook=_TEMPLATE_HOOK)
    assert result.returncode == 0
    assert not result.stderr.strip(), (
        f"Template hook should produce NO stderr when CWD outside worktrees, "
        f"got: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# 2. CWD inside .claude/worktrees/ + Write inside worktree → exits 0
# ---------------------------------------------------------------------------


def test_write_inside_worktree_is_allowed(tmp_path: Path):
    """CWD が worktree 配下で、書き込み先も worktree 内部 → exit 0."""
    worktree = _make_worktree_cwd(tmp_path)
    target = worktree / "subdir" / "file.txt"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target)},
    }
    result = _run_guard(payload, cwd=str(worktree))
    assert result.returncode == 0, (
        f"Write inside worktree should be allowed (exit 0), got {result.returncode}.\n"
        f"stderr: {result.stderr}"
    )


def test_write_relative_path_inside_worktree_is_allowed(tmp_path: Path):
    """相対パスでの書き込みも worktree 内部に解決されれば exit 0."""
    worktree = _make_worktree_cwd(tmp_path)
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "subdir/file.txt"},
    }
    result = _run_guard(payload, cwd=str(worktree))
    assert result.returncode == 0, (
        f"Relative path inside worktree should be allowed (exit 0), got {result.returncode}"
    )


# ---------------------------------------------------------------------------
# 3. CWD inside .claude/worktrees/ + Write outside worktree → exits 2
# ---------------------------------------------------------------------------


def test_write_outside_worktree_is_blocked(tmp_path: Path):
    """CWD が worktree 配下で、書き込み先が worktree 外 → exit 2 + stderr."""
    worktree = _make_worktree_cwd(tmp_path)
    # tmp_path は worktree の親より上 → worktree 外
    outside = tmp_path / "outside_file.txt"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(outside)},
    }
    result = _run_guard(payload, cwd=str(worktree))
    assert result.returncode == 2, (
        f"Write outside worktree should be blocked (exit 2), got {result.returncode}.\n"
        f"stderr: {result.stderr}"
    )
    assert result.stderr.strip(), (
        "Blocked operation must also emit a message to stderr."
    )
    assert "WorktreeGuard BLOCK" in result.stderr, (
        f"stderr should contain '[WorktreeGuard BLOCK]', got: {result.stderr!r}"
    )


def test_write_absolute_path_to_main_repo_is_blocked(tmp_path: Path):
    """worktree CWD から絶対パスで main repo に書こうとしても exit 2 でブロック."""
    worktree = _make_worktree_cwd(tmp_path)
    # 別のディレクトリへの絶対パス（worktree の親階層など）
    outside_abs = tmp_path / "main_repo_path" / "src" / "file.py"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(outside_abs)},
    }
    result = _run_guard(payload, cwd=str(worktree))
    assert result.returncode == 2, (
        f"Absolute path to outside should be blocked, got {result.returncode}"
    )


# ---------------------------------------------------------------------------
# 4. Block message sanitizes ANSI escapes in file_path (sec-Low)
# ---------------------------------------------------------------------------


def test_block_message_sanitizes_ansi_escapes(tmp_path: Path):
    """file_path に ANSI escape が含まれていても stderr にそのまま出力されない."""
    worktree = _make_worktree_cwd(tmp_path)
    ansi_injected_path = str(tmp_path / f"outside\x1b[31mINJECTED\x1b[0m.txt")
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": ansi_injected_path},
    }
    result = _run_guard(payload, cwd=str(worktree))

    assert result.returncode == 2, (
        f"Command with ANSI-injected path must still be blocked (exit 2), "
        f"got exit={result.returncode}.\nstderr: {result.stderr!r}"
    )

    assert "\x1b" not in result.stderr, (
        "[sec-Low] Block message must not contain raw ANSI escape sequences. "
        f"stderr preview: {result.stderr[:300]!r}"
    )


# ---------------------------------------------------------------------------
# 5. env gate: PO_WORKTREE_GUARD 未設定なら CWD が worktree 内でも no-op
# ---------------------------------------------------------------------------


def test_guard_disabled_when_env_not_set(tmp_path: Path):
    """env 未設定なら CWD が worktree 配下でも no-op (exit 0)。

    worktree_guard.py L40 の env gate 契約を固定する回帰テスト。
    将来 env gate を外す変更（auto activation のみ）に切り替える場合は
    このテストが落ちることで設計変更を検出する。

    Note (env gate 廃止移行時):
        env gate を外して CWD ベース自動有効化に切り替える場合は、
        先に本テスト自体を更新（または削除）してから worktree_guard.py 側を変更すること。
        順序を逆にすると本テストが先に落ちて hook の正常な改修と区別できなくなる。
    """
    worktree = _make_worktree_cwd(tmp_path)
    outside = tmp_path / "outside_file.txt"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(outside)},
    }
    result = _run_guard(payload, cwd=str(worktree), enable_guard=False)

    assert result.returncode == 0, (
        f"Guard must be inactive without PO_WORKTREE_GUARD=1 even with CWD "
        f"inside worktree, got exit={result.returncode}\nstderr: {result.stderr}"
    )
    assert not result.stderr.strip(), (
        f"Inactive guard must produce NO stderr, got: {result.stderr!r}"
    )
