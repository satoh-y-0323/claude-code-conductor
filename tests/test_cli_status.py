"""Tests for src/c3/cli_status.py (F-003 Phase 2)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from c3 import cli_status
from c3 import db as c3_db


WORKTREE_ROOT = Path(__file__).parents[1]
SCHEMA_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "schema.sql"
INIT_HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "session_start.py"


def _create_c3_db(db_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("init_c3_db_clistatus", INIT_HOOK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    mod.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))


def _seed_status(
    db_path: Path,
    *,
    session_id: str,
    worktree_id: str,
    state: str,
    current_step: str | None = None,
    progress_pct: int | None = None,
    heartbeat_offset_sec: int = 0,
) -> None:
    """直接 INSERT で po_status に行を seed。"""
    ts = (datetime.now(timezone.utc) - timedelta(seconds=heartbeat_offset_sec)).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO po_status "
            "(session_id, worktree_id, state, current_step, progress_pct, last_heartbeat) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, worktree_id, state, current_step, progress_pct, ts),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_result(
    db_path: Path,
    *,
    session_id: str,
    worktree_id: str,
    task_id: str,
    status: str,
    error_message: str | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO po_results "
            "(session_id, worktree_id, task_id, status, started_at, completed_at, "
            " output_summary, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, worktree_id, task_id, status,
                "2026-05-09T00:00:00", "2026-05-09T00:01:00",
                None, error_message,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _make_args(**overrides) -> argparse.Namespace:
    """cli_status.handle に渡す Namespace を組み立てる（デフォルト値完備）。"""
    defaults = dict(
        session=None,
        all=False,
        state=None,
        worktree=None,
        watch=False,
        interval=30,
        stale_threshold=90,
        no_stale=False,
        limit=50,
        json=False,
        verbose=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _run(args: argparse.Namespace, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """locate_c3_db を mock して cli_status.handle を呼ぶ。"""
    monkeypatch.setattr(c3_db, "locate_c3_db", lambda: db_path)
    return cli_status.handle(args)


# ---------------------------------------------------------------------------
# テストケース
# ---------------------------------------------------------------------------


class TestStatusCli:

    def test_default_shows_latest_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """引数なしで最新 session のみが表示される。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        # 古い session（heartbeat が古い）と新しい session の両方を seed
        _seed_status(db, session_id="sess-old", worktree_id="wt-old", state="completed", heartbeat_offset_sec=3600)
        _seed_status(db, session_id="sess-new", worktree_id="wt-new", state="running", heartbeat_offset_sec=10)

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "wt-new" in out
        assert "wt-old" not in out

    def test_all_flag_shows_multiple_sessions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """--all で複数 session が横断表示される。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_status(db, session_id="sess-a", worktree_id="wt-a", state="completed", heartbeat_offset_sec=120)
        _seed_status(db, session_id="sess-b", worktree_id="wt-b", state="running", heartbeat_offset_sec=10)

        rc = _run(_make_args(all=True), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "wt-a" in out
        assert "wt-b" in out

    def test_session_filter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """--session SESSION_ID で指定 session のみ表示。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_status(db, session_id="sess-A", worktree_id="wt-A", state="running")
        _seed_status(db, session_id="sess-B", worktree_id="wt-B", state="running")

        rc = _run(_make_args(session="sess-A"), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "wt-A" in out
        assert "wt-B" not in out

    def test_state_filter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """--state failed で running 行が除外される。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_status(db, session_id="sess-1", worktree_id="wt-running", state="running")
        _seed_status(db, session_id="sess-1", worktree_id="wt-failed", state="failed")

        rc = _run(_make_args(state="failed"), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "wt-failed" in out
        assert "wt-running" not in out

    def test_json_output_is_machine_readable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """--json で json.loads 可能、必須キーが揃う。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_status(
            db, session_id="sess-1", worktree_id="wt-1", state="running",
            current_step="step", progress_pct=50,
        )

        rc = _run(_make_args(json=True), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["session_id"] == "sess-1"
        assert data[0]["state"] == "running"
        assert "stale" in data[0]

    def test_stale_marker_when_heartbeat_old(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """heartbeat が threshold 超の running 行は stale としてハイライト。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_status(
            db, session_id="sess-1", worktree_id="wt-stuck", state="running",
            heartbeat_offset_sec=200,  # threshold 90 を超える
        )

        rc = _run(_make_args(json=True), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data[0]["stale"] is True

    def test_failed_row_includes_error_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """failed 行に po_results.error_message が結合され、verbose で全文表示。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_status(db, session_id="sess-1", worktree_id="wt-1", state="failed")
        long_error = "X" * 200
        _seed_result(
            db, session_id="sess-1", worktree_id="wt-1", task_id="task-1",
            status="failure", error_message=long_error,
        )

        # デフォルト: 80 文字に切り詰め
        rc = _run(_make_args(json=True), db, monkeypatch)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 1
        assert data[0]["error_message"].startswith("X")
        assert len(data[0]["error_message"]) <= 81  # 80 + "…"

        # --verbose: 200 文字全文
        rc = _run(_make_args(json=True, verbose=True), db, monkeypatch)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data[0]["error_message"] == long_error  # 200 文字 < 500 なので truncate されない

    def test_db_not_found_returns_zero_with_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """c3.db 不在時は exit 0 + 案内メッセージ。"""
        nonexistent = tmp_path / "nonexistent" / "c3.db"
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda: nonexistent)

        rc = cli_status.handle(_make_args())

        assert rc == 0
        out = capsys.readouterr().out
        assert "no po_status records found" in out
