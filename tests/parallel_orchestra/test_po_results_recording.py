"""Tests for src/parallel_orchestra/c3_db.py

F-002: PO の結果を `.claude/state/c3.db` の po_results に記録する機能の検証。

テストケース:
 locate_c3_db:
  1. cwd 配下に .claude/state/c3.db があれば見つける
  2. 親ディレクトリにあれば遡って見つける
  3. どこにも無ければ None

 record_task_results:
  4. 通常の成功タスクを INSERT できる
  5. 失敗タスクの status は 'failure' にマッピング
  6. スキップタスクの status は 'cancelled' にマッピング
  7. read_only タスク（branch_name=None）の worktree_id は '(read-only)'
  8. 大きい stdout/stderr は 500 文字に切り詰められる
  9. UNIQUE 制約により同 session_id + worktree_id + task_id の重複は無視
 10. DB 不在時はエラーを出さず 0 を返す（runner を止めない）
 11. SQL エラー時もエラーを出さず 0 を返す
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# テスト対象モジュール
from parallel_orchestra import c3_db
from parallel_orchestra.runner import TaskResult

# F-009 で作成済みの schema.sql を使う（DB 初期化）
WORKTREE_ROOT = Path(__file__).parents[2]
SCHEMA_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "schema.sql"
INIT_HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "init_c3_db.py"


def _create_c3_db(db_path: Path) -> None:
    """schema.sql を適用して c3.db を初期化する。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("init_c3_db", INIT_HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))


def _make_task_result(
    *,
    task_id: str = "task-1",
    agent: str = "developer",
    returncode: int = 0,
    stdout: str = "ok",
    stderr: str = "",
    skipped: bool = False,
    branch_name: str | None = None,
    duration_sec: float = 1.0,
) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        agent=agent,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=False,
        duration_sec=duration_sec,
        skipped=skipped,
        branch_name=branch_name,
    )


def _read_po_results(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM po_results ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# locate_c3_db
# ---------------------------------------------------------------------------


class TestLocateC3Db:

    def test_finds_db_in_cwd(self, tmp_path: Path) -> None:
        """起点ディレクトリ配下に .claude/state/c3.db があれば見つける。"""
        db_dir = tmp_path / ".claude" / "state"
        db_dir.mkdir(parents=True)
        db_path = db_dir / "c3.db"
        db_path.write_bytes(b"")

        found = c3_db.locate_c3_db(start=tmp_path)
        assert found == db_path.resolve()

    def test_finds_db_in_ancestor(self, tmp_path: Path) -> None:
        """親ディレクトリに c3.db があれば遡って見つける。"""
        db_dir = tmp_path / ".claude" / "state"
        db_dir.mkdir(parents=True)
        db_path = db_dir / "c3.db"
        db_path.write_bytes(b"")

        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)

        found = c3_db.locate_c3_db(start=deep)
        assert found == db_path.resolve()

    def test_returns_none_when_not_found(self, tmp_path: Path) -> None:
        """どこにも無ければ None。"""
        # tmp_path 配下に .claude/state/c3.db を作らない
        found = c3_db.locate_c3_db(start=tmp_path)
        # 親ディレクトリの誤検出を避けるため、tmp_path 配下に c3.db がないことだけを確認
        if found is not None:
            # 親に既存の c3.db がある環境（C3 開発リポ等）はスキップ
            pytest.skip(f"親ディレクトリに c3.db が見つかった: {found}")
        assert found is None


# ---------------------------------------------------------------------------
# record_task_results
# ---------------------------------------------------------------------------


class TestRecordTaskResults:

    def test_inserts_success_task(self, tmp_path: Path) -> None:
        """成功タスクが INSERT される。status は 'success'。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        started = datetime(2026, 5, 8, 10, 0, 0, tzinfo=timezone.utc)
        finished = datetime(2026, 5, 8, 10, 1, 30, tzinfo=timezone.utc)
        results = [_make_task_result(task_id="t1", branch_name="po/t1")]

        n = c3_db.record_task_results(
            results,
            session_id="sess-1",
            started_at=started,
            finished_at=finished,
            db_path=db_path,
        )
        assert n == 1

        rows = _read_po_results(db_path)
        assert len(rows) == 1
        assert rows[0]["session_id"] == "sess-1"
        assert rows[0]["worktree_id"] == "po/t1"
        assert rows[0]["task_id"] == "t1"
        assert rows[0]["status"] == "success"
        assert rows[0]["started_at"] == started.isoformat(timespec="seconds")
        assert rows[0]["completed_at"] == finished.isoformat(timespec="seconds")

    def test_failed_task_maps_to_failure(self, tmp_path: Path) -> None:
        """失敗タスクの status は 'failure'。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        results = [_make_task_result(returncode=1, stderr="error message")]
        c3_db.record_task_results(
            results,
            session_id="sess-1",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            db_path=db_path,
        )

        rows = _read_po_results(db_path)
        assert rows[0]["status"] == "failure"
        assert "error message" in rows[0]["error_message"]

    def test_skipped_task_maps_to_cancelled(self, tmp_path: Path) -> None:
        """スキップタスクの status は 'cancelled'。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        results = [_make_task_result(skipped=True, returncode=None)]
        c3_db.record_task_results(
            results,
            session_id="sess-1",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            db_path=db_path,
        )

        rows = _read_po_results(db_path)
        assert rows[0]["status"] == "cancelled"

    def test_read_only_task_uses_placeholder_worktree_id(self, tmp_path: Path) -> None:
        """branch_name=None（read-only タスク）は worktree_id を '(read-only)' で記録。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        results = [_make_task_result(branch_name=None)]
        c3_db.record_task_results(
            results,
            session_id="sess-1",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            db_path=db_path,
        )

        rows = _read_po_results(db_path)
        assert rows[0]["worktree_id"] == "(read-only)"

    def test_large_stdout_is_truncated(self, tmp_path: Path) -> None:
        """大きい stdout は 500 文字 + '...' に切り詰められる。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        large_stdout = "x" * 1000
        results = [_make_task_result(stdout=large_stdout)]
        c3_db.record_task_results(
            results,
            session_id="sess-1",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            db_path=db_path,
        )

        rows = _read_po_results(db_path)
        assert len(rows[0]["output_summary"]) == 503  # 500 + "..."
        assert rows[0]["output_summary"].endswith("...")

    def test_unique_constraint_skips_duplicates(self, tmp_path: Path) -> None:
        """同 session_id + worktree_id + task_id は INSERT OR IGNORE で重複スキップ。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        results = [_make_task_result(task_id="t1", branch_name="po/t1")]
        # 同じデータを 2 回 INSERT
        c3_db.record_task_results(
            results,
            session_id="sess-1",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            db_path=db_path,
        )
        c3_db.record_task_results(
            results,
            session_id="sess-1",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            db_path=db_path,
        )

        rows = _read_po_results(db_path)
        # UNIQUE 制約で重複は 1 行のみ
        assert len(rows) == 1

    def test_db_not_found_returns_zero(self, tmp_path: Path) -> None:
        """DB が見つからない場合はエラーを出さず 0 を返す。"""
        # 存在しないパスを明示
        db_path = tmp_path / "nonexistent" / "c3.db"
        results = [_make_task_result()]

        # 内部 locate も走らせず明示パスで実行 → SQL 接続が試みられて失敗
        # → except で握りつぶして 0 を返すこと
        n = c3_db.record_task_results(
            results,
            session_id="sess-1",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            db_path=db_path,
        )
        assert n == 0

    def test_locate_failure_returns_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """db_path 省略時、locate_c3_db が None を返したら 0 を返す。"""
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: None)
        results = [_make_task_result()]
        n = c3_db.record_task_results(
            results,
            session_id="sess-1",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        assert n == 0

    def test_empty_iterable_returns_zero(self, tmp_path: Path) -> None:
        """空 iterable の場合は何もせず 0 を返す。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        n = c3_db.record_task_results(
            [],
            session_id="sess-1",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            db_path=db_path,
        )
        assert n == 0
