"""Tests for src/parallel_orchestra/c3_db.py po_status helpers + heartbeat thread.

F-003: PO 並列処理の状況可視化の検証。

テストケース:
 c3_db ヘルパー（upsert_po_status / fetch_po_status）:
  1. 新規 INSERT: state / current_step / progress_pct が記録される
  2. UPDATE: 同 (session_id, worktree_id) なら state と last_heartbeat が更新される
  3. session_id 指定で fetch_po_status が該当行のみ返す
  4. session_id 省略で fetch_po_status が全行を last_heartbeat 降順で返す
  5. limit が効く
  6. DB 不在時は upsert_po_status False / fetch_po_status 空リスト
  7. 未知 state でも crash しない（警告のみで通過）

 _Dashboard.snapshot_states:
  8. 全 task の状態がコピーされて返される
  9. lock を保持せずに返り値を変更しても dashboard 内部に影響しない

 _heartbeat_po_status_loop:
 10. 1 サイクル動作: snapshot した状態が po_status に UPSERT される
 11. waiting タスクは記録されない
 12. stop_event.set() でループが抜ける
 13. dashboard.snapshot_states が例外を出してもループが落ちない
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from parallel_orchestra import c3_db
from parallel_orchestra.runner import (
    _Dashboard,
    _heartbeat_po_status_loop,
    _PO_STATUS_STATE_MAPPING,
    _TaskDisplayState,
)

WORKTREE_ROOT = Path(__file__).parents[2]
SCHEMA_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "schema.sql"
INIT_HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "session_start.py"


def _create_c3_db(db_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("init_c3_db_t", INIT_HOOK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    mod.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))


# ---------------------------------------------------------------------------
# c3_db ヘルパー
# ---------------------------------------------------------------------------


class TestUpsertFetchPoStatus:

    def test_insert_new_row(self, tmp_path: Path) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        ok = c3_db.upsert_po_status(
            session_id="sess-1",
            worktree_id="po/t1",
            state="running",
            current_step="impl phase",
            progress_pct=42,
            db_path=db_path,
        )
        assert ok is True

        rows = c3_db.fetch_po_status(session_id="sess-1", db_path=db_path)
        assert len(rows) == 1
        assert rows[0]["worktree_id"] == "po/t1"
        assert rows[0]["state"] == "running"
        assert rows[0]["current_step"] == "impl phase"
        assert rows[0]["progress_pct"] == 42

    def test_update_existing_row(self, tmp_path: Path) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        c3_db.upsert_po_status(
            session_id="sess-1", worktree_id="po/t1", state="starting",
            db_path=db_path,
        )
        # 同じ (session_id, worktree_id) で UPDATE
        c3_db.upsert_po_status(
            session_id="sess-1", worktree_id="po/t1", state="running",
            current_step="green phase", db_path=db_path,
        )

        rows = c3_db.fetch_po_status(session_id="sess-1", db_path=db_path)
        assert len(rows) == 1  # UPDATE なので 1 行のまま
        assert rows[0]["state"] == "running"
        assert rows[0]["current_step"] == "green phase"

    def test_fetch_filter_by_session(self, tmp_path: Path) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        c3_db.upsert_po_status(
            session_id="sess-1", worktree_id="po/a", state="running",
            db_path=db_path,
        )
        c3_db.upsert_po_status(
            session_id="sess-2", worktree_id="po/b", state="completed",
            db_path=db_path,
        )

        rows = c3_db.fetch_po_status(session_id="sess-1", db_path=db_path)
        assert len(rows) == 1
        assert rows[0]["worktree_id"] == "po/a"

    def test_fetch_all_sessions(self, tmp_path: Path) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        c3_db.upsert_po_status(
            session_id="sess-1", worktree_id="po/a", state="running",
            db_path=db_path,
        )
        c3_db.upsert_po_status(
            session_id="sess-2", worktree_id="po/b", state="completed",
            db_path=db_path,
        )

        rows = c3_db.fetch_po_status(db_path=db_path)
        assert len(rows) == 2

    def test_fetch_limit(self, tmp_path: Path) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        for i in range(5):
            c3_db.upsert_po_status(
                session_id="sess-1", worktree_id=f"po/{i}", state="running",
                db_path=db_path,
            )

        rows = c3_db.fetch_po_status(db_path=db_path, limit=2)
        assert len(rows) == 2

    def test_db_not_found_returns_false(self, tmp_path: Path) -> None:
        db_path = tmp_path / "nonexistent" / "c3.db"
        ok = c3_db.upsert_po_status(
            session_id="sess-1", worktree_id="po/t1", state="running",
            db_path=db_path,
        )
        assert ok is False

        rows = c3_db.fetch_po_status(db_path=db_path)
        assert rows == []

    def test_unknown_state_does_not_crash(self, tmp_path: Path) -> None:
        """未知 state でも警告のみで通過し、行は記録される。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        ok = c3_db.upsert_po_status(
            session_id="sess-1", worktree_id="po/t1", state="UNKNOWN_STATE",
            db_path=db_path,
        )
        assert ok is True

        rows = c3_db.fetch_po_status(session_id="sess-1", db_path=db_path)
        assert len(rows) == 1
        assert rows[0]["state"] == "UNKNOWN_STATE"

    def test_does_not_downgrade_completed_to_running(
        self, tmp_path: Path
    ) -> None:
        """F-002 Phase 2-B: completed の行に running を UPSERT しても完了が保たれる。

        親 heartbeat スレッドと worktree 内子プロセスからの heartbeat の競合で
        子が completed を書いた直後に親が running で逆行上書きしないこと。
        """
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        # まず completed を書く
        c3_db.upsert_po_status(
            session_id="sess-1", worktree_id="po/t1", state="completed",
            current_step="done", db_path=db_path,
        )
        # 後から running を書く（親 heartbeat の遅延発火を再現）
        c3_db.upsert_po_status(
            session_id="sess-1", worktree_id="po/t1", state="running",
            current_step="overwrite attempt", db_path=db_path,
        )

        rows = c3_db.fetch_po_status(session_id="sess-1", db_path=db_path)
        assert len(rows) == 1
        # 完了状態が保護されている（last_heartbeat / current_step は更新されてもよい）
        assert rows[0]["state"] == "completed"

    def test_does_not_downgrade_failed_to_running(
        self, tmp_path: Path
    ) -> None:
        """F-002 Phase 2-B: failed の行も running への逆行を阻止する。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        c3_db.upsert_po_status(
            session_id="sess-1", worktree_id="po/t1", state="failed",
            db_path=db_path,
        )
        c3_db.upsert_po_status(
            session_id="sess-1", worktree_id="po/t1", state="running",
            db_path=db_path,
        )

        rows = c3_db.fetch_po_status(session_id="sess-1", db_path=db_path)
        assert rows[0]["state"] == "failed"

    def test_running_to_completed_is_normal_progression(
        self, tmp_path: Path
    ) -> None:
        """F-002 Phase 2-B: 正常な running → completed の遷移は通る。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        c3_db.upsert_po_status(
            session_id="sess-1", worktree_id="po/t1", state="running",
            db_path=db_path,
        )
        c3_db.upsert_po_status(
            session_id="sess-1", worktree_id="po/t1", state="completed",
            db_path=db_path,
        )

        rows = c3_db.fetch_po_status(session_id="sess-1", db_path=db_path)
        assert rows[0]["state"] == "completed"

    def test_completed_to_failed_is_blocked(self, tmp_path: Path) -> None:
        """F-002 Phase 2-B: completed の行は failed にも上書きされない。

        どちらも terminal なので最初に書かれた方が保たれる（先勝ち）。
        """
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        c3_db.upsert_po_status(
            session_id="sess-1", worktree_id="po/t1", state="completed",
            db_path=db_path,
        )
        c3_db.upsert_po_status(
            session_id="sess-1", worktree_id="po/t1", state="failed",
            db_path=db_path,
        )

        rows = c3_db.fetch_po_status(session_id="sess-1", db_path=db_path)
        assert rows[0]["state"] == "completed"


# ---------------------------------------------------------------------------
# _Dashboard.snapshot_states
# ---------------------------------------------------------------------------


class TestDashboardSnapshot:

    def test_snapshot_returns_all_states(self) -> None:
        dashboard = _Dashboard(
            ["t1", "t2", "t3"], enabled=True, live_renders=False,
        )
        # update を呼んで状態を変える
        dashboard.update("t1", status="running", current_action="impl")
        dashboard.update("t2", status="failed")

        states = dashboard.snapshot_states()
        assert len(states) == 3
        by_id = {s.task_id: s for s in states}
        assert by_id["t1"].status == "running"
        assert by_id["t1"].current_action == "impl"
        assert by_id["t2"].status == "failed"
        assert by_id["t3"].status == "waiting"

        dashboard.stop()

    def test_snapshot_returns_copies(self) -> None:
        """返り値を変更しても dashboard 内部に影響しない（コピーが返る）。"""
        dashboard = _Dashboard(["t1"], enabled=True, live_renders=False)
        dashboard.update("t1", status="running")

        states = dashboard.snapshot_states()
        # 外側で書き換える
        states[0].status = "completed"

        # dashboard 内部の状態は変わっていない
        states2 = dashboard.snapshot_states()
        assert states2[0].status == "running"

        dashboard.stop()


# ---------------------------------------------------------------------------
# _heartbeat_po_status_loop
# ---------------------------------------------------------------------------


class TestHeartbeatLoop:

    def test_one_cycle_upserts_states(self, tmp_path: Path) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        # locate_c3_db を tmp_path 配下に向ける
        import parallel_orchestra.c3_db as c3_db_mod
        original_locate = c3_db_mod.locate_c3_db
        c3_db_mod.locate_c3_db = lambda start=None: db_path  # type: ignore[assignment]
        try:
            dashboard = _Dashboard(
                ["t1", "t2"], enabled=True, live_renders=False,
            )
            dashboard.update("t1", status="running", current_action="impl")
            dashboard.update("t2", status="complete")  # 内部語彙は "complete"

            stop_event = threading.Event()
            # interval を非常に短く設定して 1 サイクル動かしてすぐ止める
            t = threading.Thread(
                target=_heartbeat_po_status_loop,
                args=(dashboard, "sess-test", stop_event),
                kwargs={"interval": 0.05},
                daemon=True,
            )
            t.start()
            time.sleep(0.15)  # 数サイクル動かす
            stop_event.set()
            t.join(timeout=2.0)

            rows = c3_db.fetch_po_status(session_id="sess-test", db_path=db_path)
            assert len(rows) == 2
            by_id = {r["worktree_id"]: r for r in rows}
            assert by_id["t1"]["state"] == "running"
            assert by_id["t1"]["current_step"] == "impl"
            assert by_id["t2"]["state"] == "completed"

            dashboard.stop()
        finally:
            c3_db_mod.locate_c3_db = original_locate  # type: ignore[assignment]

    def test_waiting_tasks_are_excluded(self, tmp_path: Path) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        import parallel_orchestra.c3_db as c3_db_mod
        original_locate = c3_db_mod.locate_c3_db
        c3_db_mod.locate_c3_db = lambda start=None: db_path  # type: ignore[assignment]
        try:
            dashboard = _Dashboard(
                ["t1", "t2"], enabled=True, live_renders=False,
            )
            # t1 だけ実行中、t2 は waiting のまま
            dashboard.update("t1", status="running")

            stop_event = threading.Event()
            t = threading.Thread(
                target=_heartbeat_po_status_loop,
                args=(dashboard, "sess-test", stop_event),
                kwargs={"interval": 0.05},
                daemon=True,
            )
            t.start()
            time.sleep(0.15)
            stop_event.set()
            t.join(timeout=2.0)

            rows = c3_db.fetch_po_status(session_id="sess-test", db_path=db_path)
            assert len(rows) == 1
            assert rows[0]["worktree_id"] == "t1"

            dashboard.stop()
        finally:
            c3_db_mod.locate_c3_db = original_locate  # type: ignore[assignment]

    def test_stop_event_terminates_loop(self) -> None:
        """stop_event.set() でループが速やかに抜ける。"""
        dashboard = _Dashboard(["t1"], enabled=True, live_renders=False)
        stop_event = threading.Event()

        t = threading.Thread(
            target=_heartbeat_po_status_loop,
            args=(dashboard, "sess-x", stop_event),
            kwargs={"interval": 5.0},  # interval は長めに
            daemon=True,
        )
        t.start()
        time.sleep(0.05)  # 起動を待つ
        stop_event.set()
        t.join(timeout=1.0)
        assert not t.is_alive()  # 5.0s 待たずに終了している

        dashboard.stop()

    def test_snapshot_exception_does_not_kill_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """dashboard.snapshot_states が例外を出してもループが落ちない。"""
        dashboard = _Dashboard(["t1"], enabled=True, live_renders=False)

        call_count = [0]

        def boom() -> list:
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("boom")
            return []

        monkeypatch.setattr(dashboard, "snapshot_states", boom)

        stop_event = threading.Event()
        t = threading.Thread(
            target=_heartbeat_po_status_loop,
            args=(dashboard, "sess-x", stop_event),
            kwargs={"interval": 0.02},
            daemon=True,
        )
        t.start()
        time.sleep(0.1)
        stop_event.set()
        t.join(timeout=1.0)

        # 2 回以上呼ばれていること（1 回目で例外が出ても続行）
        assert call_count[0] >= 2
        dashboard.stop()


# ---------------------------------------------------------------------------
# 状態マッピング
# ---------------------------------------------------------------------------


class TestStateMapping:

    @pytest.mark.parametrize("internal,expected", [
        ("starting_up", "starting"),
        ("running", "running"),
        ("complete", "completed"),
        ("skipped", "completed"),
        ("failed", "failed"),
        ("resumed", "running"),
    ])
    def test_internal_to_schema_state(self, internal: str, expected: str) -> None:
        assert _PO_STATUS_STATE_MAPPING[internal] == expected


# ---------------------------------------------------------------------------
# F-003 Phase 2: fetch_po_results
# ---------------------------------------------------------------------------


class TestFetchPoResults:
    """fetch_po_results が session_id / status でフィルタ可能なこと。"""

    def _seed(self, db_path: Path) -> None:
        """po_results に 4 行 seed する（sess-1: success/failure, sess-2: success/cancelled）。"""
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(
                """
                INSERT INTO po_results
                  (session_id, worktree_id, task_id, status, started_at, completed_at, output_summary, error_message)
                VALUES
                  ('sess-1', 'wt-a', 'task-1', 'success', '2026-05-09T00:00:00', '2026-05-09T00:01:00', 'ok', NULL),
                  ('sess-1', 'wt-b', 'task-2', 'failure', '2026-05-09T00:02:00', '2026-05-09T00:03:00', NULL, 'pytest collect failed: ImportError no module named foo'),
                  ('sess-2', 'wt-c', 'task-3', 'success', '2026-05-09T01:00:00', '2026-05-09T01:01:00', 'done', NULL),
                  ('sess-2', 'wt-d', 'task-4', 'cancelled', '2026-05-09T01:02:00', '2026-05-09T01:03:00', NULL, NULL);
                """
            )
            conn.commit()
        finally:
            conn.close()

    def test_fetch_po_results_filters_by_session(self, tmp_path: Path) -> None:
        """session_id を指定すると該当 session のみ返る。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)
        self._seed(db_path)

        rows = c3_db.fetch_po_results(session_id="sess-1", db_path=db_path)

        assert len(rows) == 2
        assert all(r["session_id"] == "sess-1" for r in rows)
        # completed_at 降順
        assert rows[0]["task_id"] == "task-2"
        assert rows[1]["task_id"] == "task-1"

    def test_fetch_po_results_filters_by_status(self, tmp_path: Path) -> None:
        """status を指定すると該当 status のみ返る。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)
        self._seed(db_path)

        rows = c3_db.fetch_po_results(status="failure", db_path=db_path)

        assert len(rows) == 1
        assert rows[0]["status"] == "failure"
        assert rows[0]["error_message"].startswith("pytest collect failed")

    def test_fetch_po_results_db_not_found_returns_empty(self, tmp_path: Path) -> None:
        """DB が無ければ空リストを返す。"""
        db_path = tmp_path / "nonexistent" / "c3.db"
        rows = c3_db.fetch_po_results(db_path=db_path)
        assert rows == []
