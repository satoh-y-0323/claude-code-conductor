"""Tests for F-002 Phase 2: worktree 内からの直接 SQLite 書き込み配管。

このファイルは Phase 2-A / 2-B / 2-C を通じて段階的に拡張される。

Phase 2-A の対象（このコミット）:
  - runner._execute_task が subprocess.Popen に渡す env dict に
    C3_PO_DB_PATH / C3_PO_SESSION_ID / C3_PO_TASK_ID / C3_PO_WORKTREE_ID
    が注入されること（write task / read_only task の両方）。
  - locate_c3_db の env-aware 化は test_po_results_recording.py 側で検証。

Phase 2-B 以降で追加予定:
  - po_heartbeat.py CLI の動作
  - subagent_log.py の C3_PO_WORKTREE_ID ガード付き UPSERT
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from parallel_orchestra import runner
from parallel_orchestra.c3_db import READ_ONLY_WORKTREE_ID
from parallel_orchestra.manifest import Task


def _make_task(
    *,
    task_id: str = "t1",
    agent: str = "developer",
    read_only: bool = True,
    env: dict[str, str] | None = None,
) -> Task:
    return Task(
        id=task_id,
        agent=agent,
        read_only=read_only,
        prompt="x",
        env=env or {},
        model_override=None,
    )


def _make_popen_stub(captured: list[dict]) -> type:
    """subprocess.Popen のスタブを生成する。captured に kwargs を詰める。

    既存 test_runner_model_override.py の `_stub_popen` パターンに合わせ、
    クラス変数共有のリスクを避けるためテストごとに新しい list を渡す。
    """

    class _StubProc:
        def __init__(self, cmd: list[str], **kwargs) -> None:
            captured.append({"cmd": cmd, "kwargs": kwargs})
            self.returncode = 0
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")

        def wait(self) -> None:
            return None

        def kill(self) -> None:
            return None

    return _StubProc


# ---------------------------------------------------------------------------
# F-002 Phase 2-A: subprocess 起動時の env 注入
# ---------------------------------------------------------------------------


class TestEnvVarInjectionReadOnly:
    """read_only=True タスクでも env 4 変数が注入されること。"""

    def test_injects_session_and_task_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[dict] = []
        monkeypatch.setattr(subprocess, "Popen", _make_popen_stub(captured))
        monkeypatch.chdir(tmp_path)

        task = _make_task(task_id="task-abc", read_only=True)
        runner._execute_task(
            task,
            claude_exe="claude",
            git_root=None,
            effective_cwd=tmp_path,
            dashboard=None,
            po_session_id="sess-123",
        )

        assert captured, "Popen should have been called"
        env = captured[0]["kwargs"]["env"]
        assert env["C3_PO_SESSION_ID"] == "sess-123"
        assert env["C3_PO_TASK_ID"] == "task-abc"
        # read_only タスクは worktree が無いので placeholder
        assert env["C3_PO_WORKTREE_ID"] == READ_ONLY_WORKTREE_ID

    def test_db_path_set_when_db_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """親リポに c3.db があれば C3_PO_DB_PATH に絶対パスがセットされる。"""
        db_dir = tmp_path / ".claude" / "state"
        db_dir.mkdir(parents=True)
        db_path = db_dir / "c3.db"
        db_path.write_bytes(b"")

        captured: list[dict] = []
        monkeypatch.setattr(subprocess, "Popen", _make_popen_stub(captured))
        monkeypatch.chdir(tmp_path)

        task = _make_task(read_only=True)
        runner._execute_task(
            task,
            claude_exe="claude",
            git_root=None,
            effective_cwd=tmp_path,
            dashboard=None,
            po_session_id="sess-1",
        )

        env = captured[0]["kwargs"]["env"]
        assert env["C3_PO_DB_PATH"] == str(db_path.resolve())

    def test_db_path_absent_when_no_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """c3.db が見つからない環境では C3_PO_DB_PATH は env に含まれない。"""
        # 親環境にある可能性を排除するため、locate_c3_db を直接 None に置換
        monkeypatch.setattr(runner, "locate_c3_db", lambda start=None: None)
        captured: list[dict] = []
        monkeypatch.setattr(subprocess, "Popen", _make_popen_stub(captured))
        monkeypatch.chdir(tmp_path)

        task = _make_task(read_only=True)
        runner._execute_task(
            task,
            claude_exe="claude",
            git_root=None,
            effective_cwd=tmp_path,
            dashboard=None,
            po_session_id="sess-1",
        )

        env = captured[0]["kwargs"]["env"]
        # DB が無くても他の 3 変数は入る
        assert env["C3_PO_SESSION_ID"] == "sess-1"
        assert env["C3_PO_TASK_ID"] == "t1"
        assert env["C3_PO_WORKTREE_ID"] == READ_ONLY_WORKTREE_ID
        # DB パスは未設定
        assert "C3_PO_DB_PATH" not in env


class TestEnvVarInjectionWriteTask:
    """read_only=False（write）タスクでは worktree 作成後に worktree_id が入る。"""

    def test_worktree_id_uses_branch_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """write task の C3_PO_WORKTREE_ID は worktree のブランチ名と一致する。"""
        captured: list[dict] = []
        monkeypatch.setattr(subprocess, "Popen", _make_popen_stub(captured))

        # _setup_worktree を mock して固定のブランチ名を返す
        fake_worktree = tmp_path / "fake_worktree"
        fake_worktree.mkdir()
        fake_branch = "po/task-write/abc123"

        def _fake_setup_worktree(git_root, task, *, claude_src_dir=None):
            return (fake_worktree, fake_branch)

        monkeypatch.setattr(runner, "_setup_worktree", _fake_setup_worktree)

        task = _make_task(task_id="task-write", read_only=False)
        runner._execute_task(
            task,
            claude_exe="claude",
            git_root=tmp_path,
            effective_cwd=tmp_path,
            dashboard=None,
            po_session_id="sess-write",
        )

        env = captured[0]["kwargs"]["env"]
        assert env["C3_PO_SESSION_ID"] == "sess-write"
        assert env["C3_PO_TASK_ID"] == "task-write"
        assert env["C3_PO_WORKTREE_ID"] == fake_branch
        # 既存の PO_WORKTREE_GUARD も併存していること（後方互換）
        assert env["PO_WORKTREE_GUARD"] == "1"


class TestPoSessionIdParameterDefault:
    """po_session_id 引数を省略した既存呼び出し互換（後方互換確認）。"""

    def test_default_session_id_when_omitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """po_session_id を省略しても _execute_task はクラッシュしない。"""
        captured: list[dict] = []
        monkeypatch.setattr(subprocess, "Popen", _make_popen_stub(captured))
        monkeypatch.chdir(tmp_path)

        task = _make_task(read_only=True)
        # 既存テスト互換: po_session_id 省略
        runner._execute_task(
            task,
            claude_exe="claude",
            git_root=None,
            effective_cwd=tmp_path,
            dashboard=None,
        )

        # クラッシュせず Popen が呼ばれることを確認
        assert captured, "Popen should have been called"
