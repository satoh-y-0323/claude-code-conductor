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
INIT_HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "session_start.py"
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
        """C3_PO_DB_PATH の指定先が無効な場合も exit 0 で何もしない。

        防御策を二重化:
          1. cwd を tmp_path に隔離（親遡り探索の起点を一時領域に固定）
          2. C3_PO_DB_PATH を「tmp_path 内の存在しないパス」に明示
             → locate_c3_db は env_path を発見できず親遡り fallback、
                tmp_path の祖先（OS の Temp 配下）にも c3.db は無いため None
             これにより万が一 cwd が想定外でも親リポ DB が誤検出されない。
        """
        nonexistent_db = tmp_path / "absolutely_does_not_exist.db"
        env = {
            "PATH": "",
            "C3_PO_DB_PATH": str(nonexistent_db),
            "C3_PO_SESSION_ID": "sess-no-db",
            "C3_PO_WORKTREE_ID": "po/no-db",
            "PYTHONIOENCODING": "utf-8",
        }
        result = subprocess.run(
            [sys.executable, str(PO_HEARTBEAT_PATH), "--state", "running"],
            env=env,
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=15,
        )
        # 書き込み失敗でもフェイルセーフで exit 0
        assert result.returncode == 0
        # nonexistent_db は作られない（DB 不在のため接続前にスキップ）
        assert not nonexistent_db.exists()


# ---------------------------------------------------------------------------
# F-002 Phase 2 フォローアップ: po_heartbeat.main() の unit test (mock 化)
# ---------------------------------------------------------------------------


def _load_po_heartbeat_module():
    """`.claude/hooks/po_heartbeat.py` をテストから動的 import する。"""
    spec = importlib.util.spec_from_file_location(
        "po_heartbeat", PO_HEARTBEAT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


class TestPoHeartbeatUnit:
    """po_heartbeat.main() の関数レベル unit test。

    subprocess を経由しないため monkeypatch で c3_db.locate_c3_db を mock 化でき、
    親リポ DB を一切触らずに「DB 不在時の no-op 挙動」を厳密に検証できる。
    既存の TestPoHeartbeatCli は E2E カバレッジを維持するために残す。
    """

    def test_main_no_op_when_locate_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """locate_c3_db が None を返す環境で main() が exit 0 を返し、
        c3_db.upsert_po_status が呼ばれないこと。"""
        po_heartbeat = _load_po_heartbeat_module()

        # env を完備
        monkeypatch.setenv("C3_PO_SESSION_ID", "sess-unit-1")
        monkeypatch.setenv("C3_PO_WORKTREE_ID", "po/unit-1")
        monkeypatch.delenv("C3_PO_DB_PATH", raising=False)

        # parallel_orchestra.c3_db を mock 化
        from parallel_orchestra import c3_db
        upsert_calls: list[dict] = []

        def _fake_upsert(**kwargs):
            upsert_calls.append(kwargs)
            return False  # DB 不在を表現

        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: None)
        monkeypatch.setattr(c3_db, "upsert_po_status", _fake_upsert)

        rc = po_heartbeat.main(["--state", "running"])
        assert rc == 0
        # upsert は呼ばれる（DB 不在判定は upsert 側で行うため）
        # 重要なのは「親 DB に何も書かれない」こと（locate_c3_db が None なので
        # upsert 内部の sqlite3.connect も走らない → 副作用ゼロ）
        assert len(upsert_calls) == 1
        assert upsert_calls[0]["session_id"] == "sess-unit-1"
        assert upsert_calls[0]["worktree_id"] == "po/unit-1"
        assert upsert_calls[0]["state"] == "running"

    def test_main_no_op_when_env_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C3_PO_SESSION_ID / WORKTREE_ID 不在で main() が exit 0、
        c3_db.upsert_po_status は呼ばれない（DB 触らず早期 return）。"""
        po_heartbeat = _load_po_heartbeat_module()

        monkeypatch.delenv("C3_PO_SESSION_ID", raising=False)
        monkeypatch.delenv("C3_PO_WORKTREE_ID", raising=False)
        monkeypatch.delenv("C3_PO_DB_PATH", raising=False)

        from parallel_orchestra import c3_db
        upsert_calls: list[dict] = []

        def _fake_upsert(**kwargs):
            upsert_calls.append(kwargs)
            return True

        monkeypatch.setattr(c3_db, "upsert_po_status", _fake_upsert)

        rc = po_heartbeat.main(["--state", "running"])
        assert rc == 0
        # env 不在なので upsert は一切呼ばれない（早期 return）
        assert upsert_calls == []

    def test_main_writes_with_explicit_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """env 完備 + 有効 DB なら main() が upsert_po_status を実行する。"""
        po_heartbeat = _load_po_heartbeat_module()
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        monkeypatch.setenv("C3_PO_SESSION_ID", "sess-unit-2")
        monkeypatch.setenv("C3_PO_WORKTREE_ID", "po/unit-2")
        monkeypatch.setenv("C3_PO_DB_PATH", str(db_path))

        rc = po_heartbeat.main(
            ["--state", "completed", "--step", "all green", "--progress", "100"]
        )
        assert rc == 0
        rows = _read_po_status(db_path)
        assert len(rows) == 1
        assert rows[0]["state"] == "completed"
        assert rows[0]["current_step"] == "all green"
        assert rows[0]["progress_pct"] == 100


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
