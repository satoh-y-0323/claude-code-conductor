"""Tests for F-002 Phase 2: worktree 内からの直接 SQLite 書き込み配管。

このファイルは Phase 2-A / 2-B / 2-C を通じて段階的に拡張される。

Phase 2-A の対象:
  - runner._execute_task が subprocess.Popen に渡す env dict に
    C3_PO_DB_PATH / C3_PO_SESSION_ID / C3_PO_TASK_ID / C3_PO_WORKTREE_ID
    が注入されること（write task / read_only task の両方）。
  - locate_c3_db の env-aware 化は test_po_results_recording.py 側で検証。

Phase 2-B の対象（このコミット追加分）:
  - po_heartbeat.py CLI の動作（env 経由で po_status を UPSERT）
  - subagent_log.py の C3_PO_WORKTREE_ID ガード付き UPSERT
"""

from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from parallel_orchestra import runner
from parallel_orchestra.c3_db import READ_ONLY_WORKTREE_ID
from parallel_orchestra.manifest import Task

WORKTREE_ROOT = Path(__file__).parents[2]
SCHEMA_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "schema.sql"
INIT_HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "init_c3_db.py"
PO_HEARTBEAT_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "po_heartbeat.py"
SUBAGENT_LOG_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "subagent_log.py"


def _create_c3_db(db_path: Path) -> None:
    """schema.sql を適用して c3.db を初期化する。"""
    spec = importlib.util.spec_from_file_location("init_c3_db", INIT_HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))


def _read_po_status(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM po_status ORDER BY session_id, worktree_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


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


# ---------------------------------------------------------------------------
# F-002 Phase 2-B: po_heartbeat.py CLI
# ---------------------------------------------------------------------------


class TestPoHeartbeatCli:
    """`.claude/hooks/po_heartbeat.py` の CLI 動作テスト。"""

    def _run_cli(
        self,
        *,
        args: list[str],
        env: dict[str, str],
    ) -> subprocess.CompletedProcess:
        """サブプロセスとして po_heartbeat.py を実行する。

        env の "PATH": "" は最小限の env を渡す意図（sys.executable が
        絶対パスのため Python 起動には PATH が不要）。これにより親プロセスの
        環境変数による副作用を排除してテストが決定論的に動く。
        """
        return subprocess.run(
            [sys.executable, str(PO_HEARTBEAT_PATH), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_writes_running_state_to_db(self, tmp_path: Path) -> None:
        """env 完備で --state running を呼ぶと po_status に行が追加される。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        env = {
            "PATH": "",  # 最小限の env
            "C3_PO_DB_PATH": str(db_path),
            "C3_PO_SESSION_ID": "sess-cli-1",
            "C3_PO_WORKTREE_ID": "po/task-cli/abc",
            "PYTHONIOENCODING": "utf-8",
        }
        result = self._run_cli(
            args=["--state", "running", "--step", "wave 2"],
            env=env,
        )
        assert result.returncode == 0, result.stderr

        rows = _read_po_status(db_path)
        assert len(rows) == 1
        assert rows[0]["session_id"] == "sess-cli-1"
        assert rows[0]["worktree_id"] == "po/task-cli/abc"
        assert rows[0]["state"] == "running"
        assert rows[0]["current_step"] == "wave 2"

    def test_progress_pct_is_recorded(self, tmp_path: Path) -> None:
        """--progress 50 で progress_pct が DB に記録される。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        env = {
            "PATH": "",
            "C3_PO_DB_PATH": str(db_path),
            "C3_PO_SESSION_ID": "sess-cli-2",
            "C3_PO_WORKTREE_ID": "po/task-prog",
            "PYTHONIOENCODING": "utf-8",
        }
        result = self._run_cli(
            args=["--state", "running", "--step", "x", "--progress", "50"],
            env=env,
        )
        assert result.returncode == 0
        rows = _read_po_status(db_path)
        assert rows[0]["progress_pct"] == 50

    def test_missing_env_no_op(self, tmp_path: Path) -> None:
        """C3_PO_SESSION_ID 等の env が無い場合 exit 0 で何も書かない。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        env = {
            "PATH": "",
            "C3_PO_DB_PATH": str(db_path),
            # C3_PO_SESSION_ID / C3_PO_WORKTREE_ID 不在
            "PYTHONIOENCODING": "utf-8",
        }
        result = self._run_cli(args=["--state", "running"], env=env)
        # フェイルセーフ: exit 0 で何も起こさない
        assert result.returncode == 0
        rows = _read_po_status(db_path)
        assert rows == []

    def test_no_db_no_op(self, tmp_path: Path) -> None:
        """C3_PO_DB_PATH が無い場合も exit 0 で何もしない。

        env で C3_PO_DB_PATH を指定せず、cwd を一時ディレクトリにすることで
        locate_c3_db の親遡り探索が他のリポジトリの c3.db に当たることを防ぐ。
        書き込みが失敗してもフェイルセーフで exit 0 を保証することを確認。
        """
        env = {
            "PATH": "",
            "C3_PO_SESSION_ID": "sess-x",
            "C3_PO_WORKTREE_ID": "po/x",
            "PYTHONIOENCODING": "utf-8",
        }
        # cwd を tmp_path にして親遡り探索が tmp_path の祖先のみを見るようにする
        result = subprocess.run(
            [sys.executable, str(PO_HEARTBEAT_PATH), "--state", "running"],
            env=env,
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=15,
        )
        # locate_c3_db は親遡りでも見つからない or 見つかっても書き込み失敗 → exit 0
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# F-002 Phase 2-B: subagent_log.py の env ガード付き UPSERT
# ---------------------------------------------------------------------------


def _load_subagent_log_module():
    """subagent_log.py をテストから動的 import する。"""
    spec = importlib.util.spec_from_file_location(
        "subagent_log", SUBAGENT_LOG_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


class TestSubagentLogPoStatusFoldIn:
    """subagent_log.py が C3_PO_WORKTREE_ID 設定時のみ po_status を UPSERT する。"""

    def _invoke_subagent_stop(
        self,
        *,
        env: dict[str, str],
        agent_id: str = "agent-x",
        session_id: str = "claude-session-1",
        cwd: Path,
    ) -> subprocess.CompletedProcess:
        """subagent_log.py を SubagentStop イベントで叩く。"""
        payload = {
            "hook_event_name": "SubagentStop",
            "session_id": session_id,
            "agent_id": agent_id,
            "status": "success",
        }
        return subprocess.run(
            [sys.executable, str(SUBAGENT_LOG_PATH)],
            input=json.dumps(payload),
            env=env,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_upserts_when_env_set(self, tmp_path: Path) -> None:
        """C3_PO_WORKTREE_ID が設定されていれば po_status に completed が記録される。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        env = {
            "PATH": "",
            "C3_PO_DB_PATH": str(db_path),
            "C3_PO_SESSION_ID": "sess-log-1",
            "C3_PO_WORKTREE_ID": "po/task-log",
            "PYTHONIOENCODING": "utf-8",
        }
        # subagent_log は cwd 配下に .claude/logs/ を作るので一時ディレクトリで実行
        log_root = tmp_path / "log_root" / ".claude"
        log_root.mkdir(parents=True)
        result = self._invoke_subagent_stop(env=env, cwd=log_root.parent)
        assert result.returncode == 0, result.stderr

        rows = _read_po_status(db_path)
        assert len(rows) == 1
        assert rows[0]["session_id"] == "sess-log-1"
        assert rows[0]["worktree_id"] == "po/task-log"
        assert rows[0]["state"] == "completed"

    def test_failure_status_records_failed(self, tmp_path: Path) -> None:
        """status != 'success' なら state='failed' で UPSERT される。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        env = {
            "PATH": "",
            "C3_PO_DB_PATH": str(db_path),
            "C3_PO_SESSION_ID": "sess-log-2",
            "C3_PO_WORKTREE_ID": "po/task-fail",
            "PYTHONIOENCODING": "utf-8",
        }
        log_root = tmp_path / "log_root" / ".claude"
        log_root.mkdir(parents=True)

        payload = {
            "hook_event_name": "SubagentStop",
            "session_id": "claude-sess",
            "agent_id": "agent-fail",
            "status": "error",
        }
        result = subprocess.run(
            [sys.executable, str(SUBAGENT_LOG_PATH)],
            input=json.dumps(payload),
            env=env,
            cwd=str(log_root.parent),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr

        rows = _read_po_status(db_path)
        assert rows[0]["state"] == "failed"

    def test_no_op_without_env(self, tmp_path: Path) -> None:
        """C3_PO_WORKTREE_ID 不在では po_status に書かない（親 Claude セッション想定）。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        env = {
            "PATH": "",
            "C3_PO_DB_PATH": str(db_path),
            # C3_PO_WORKTREE_ID なし → no-op
            "PYTHONIOENCODING": "utf-8",
        }
        log_root = tmp_path / "log_root" / ".claude"
        log_root.mkdir(parents=True)
        result = self._invoke_subagent_stop(env=env, cwd=log_root.parent)
        assert result.returncode == 0

        rows = _read_po_status(db_path)
        assert rows == []

    def test_subagent_start_records_running(self, tmp_path: Path) -> None:
        """SubagentStart イベントで state='running' が UPSERT される。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        env = {
            "PATH": "",
            "C3_PO_DB_PATH": str(db_path),
            "C3_PO_SESSION_ID": "sess-start",
            "C3_PO_WORKTREE_ID": "po/task-start",
            "PYTHONIOENCODING": "utf-8",
        }
        log_root = tmp_path / "log_root" / ".claude"
        log_root.mkdir(parents=True)

        payload = {
            "hook_event_name": "SubagentStart",
            "session_id": "claude-sess",
            "agent_id": "agent-x",
            "agent_type": "tester",
        }
        result = subprocess.run(
            [sys.executable, str(SUBAGENT_LOG_PATH)],
            input=json.dumps(payload),
            env=env,
            cwd=str(log_root.parent),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr

        rows = _read_po_status(db_path)
        assert len(rows) == 1
        assert rows[0]["state"] == "running"
        # current_step は agent_type を採用する設計
        assert rows[0]["current_step"] == "tester"
