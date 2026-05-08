"""Tests for .claude/hooks/init_c3_db.py

F-009: DuckDB ハイブリッド構成の基盤。
SessionStart で呼ばれる DB 初期化スクリプトの挙動を検証する。

テストケース:
 基本動作:
  1. DB ファイルが新規作成される
  2. 全テーブル（schema_version + 5 機能テーブル）が CREATE される
  3. WAL モードが有効化される
  4. schema_version に現行バージョン（1）が記録される

 冪等性:
  5. 既存 DB に対して再実行しても crash せず、データを破壊しない
  6. schema_version は重複 INSERT されない（INSERT OR IGNORE）

 失敗耐性:
  7. schema.sql が存在しなくても main() は exit 0 を返す（セッションを止めない）
  8. 不正な DB ファイル（書き込み権限なし等）でも exit 0 を返す

 DuckDB 連携:
  9. DuckDB から ATTACH して全テーブルを SELECT できる
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "init_c3_db.py"
SCHEMA_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "schema.sql"


def _load_hook_module() -> types.ModuleType:
    """Hook スクリプトを __main__ を実行せずにモジュールとしてロードする。"""
    spec = importlib.util.spec_from_file_location("init_c3_db", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def _list_tables(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 基本動作
# ---------------------------------------------------------------------------


class TestBasicInitialization:

    def test_db_file_is_created(self, tmp_path: Path) -> None:
        """DB ファイルが新規作成される。"""
        module = _load_hook_module()
        db_path = tmp_path / "c3.db"

        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))

        assert db_path.exists(), "DB ファイルが作成されていない"

    def test_all_tables_are_created(self, tmp_path: Path) -> None:
        """全テーブル（schema_version + 5 機能テーブル）が作られる。"""
        module = _load_hook_module()
        db_path = tmp_path / "c3.db"

        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))

        tables = _list_tables(db_path)
        expected = {
            'schema_version',
            'review_decisions',
            'po_results',
            'po_status',
            'tier_bandit',
            'agent_runs',
        }
        missing = expected - tables
        assert not missing, f"作られていないテーブル: {missing}"

    def test_wal_mode_is_enabled(self, tmp_path: Path) -> None:
        """WAL モードが有効化されている。"""
        module = _load_hook_module()
        db_path = tmp_path / "c3.db"

        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))

        conn = sqlite3.connect(str(db_path))
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        assert mode.lower() == 'wal', f"journal_mode={mode}, want 'wal'"

    def test_schema_version_is_recorded(self, tmp_path: Path) -> None:
        """schema_version に現行バージョン（1）が記録される。"""
        module = _load_hook_module()
        db_path = tmp_path / "c3.db"

        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))

        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT version FROM schema_version ORDER BY version"
            ).fetchall()
        finally:
            conn.close()
        assert rows == [(module.SCHEMA_VERSION,)], f"got {rows}"


# ---------------------------------------------------------------------------
# 冪等性
# ---------------------------------------------------------------------------


class TestIdempotency:

    def test_reapply_does_not_crash(self, tmp_path: Path) -> None:
        """既存 DB に対して再実行しても crash しない。"""
        module = _load_hook_module()
        db_path = tmp_path / "c3.db"

        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))
        # 2 回目
        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))

        # 全テーブルが残っていることを確認
        tables = _list_tables(db_path)
        assert 'schema_version' in tables
        assert 'agent_runs' in tables

    def test_existing_data_is_preserved(self, tmp_path: Path) -> None:
        """再実行で既存データを破壊しない。"""
        module = _load_hook_module()
        db_path = tmp_path / "c3.db"

        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))

        # ダミーデータを INSERT
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO review_decisions "
                "(checklist_id, finding_text, decision, decided_at, reviewer) "
                "VALUES (?, ?, ?, ?, ?)",
                ('CR-Q-001', 'foo', 'accepted', '2026-05-08T00:00:00+00:00', 'code-reviewer'),
            )
            conn.commit()
        finally:
            conn.close()

        # 再実行
        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))

        # データが残っていることを確認
        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM review_decisions"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1, f"既存データが消えた（count={count}）"

    def test_schema_version_not_duplicated(self, tmp_path: Path) -> None:
        """schema_version は再実行しても重複 INSERT されない。"""
        module = _load_hook_module()
        db_path = tmp_path / "c3.db"

        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))
        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))
        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))

        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        finally:
            conn.close()
        assert count == 1, f"schema_version が重複（count={count}）"


# ---------------------------------------------------------------------------
# 失敗耐性
# ---------------------------------------------------------------------------


class TestFailureTolerance:

    def test_main_returns_zero_when_schema_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """schema.sql が無くても main() は exit 0 を返す（セッション継続）。"""
        module = _load_hook_module()

        # 存在しないスキーマパスに差し替え
        monkeypatch.setattr(module, 'SCHEMA_PATH', str(tmp_path / "missing.sql"))
        monkeypatch.setattr(module, 'STATE_DIR', str(tmp_path / "state"))
        monkeypatch.setattr(module, 'DB_PATH', str(tmp_path / "state" / "c3.db"))

        result = module.main()
        assert result == 0


# ---------------------------------------------------------------------------
# DuckDB 連携
# ---------------------------------------------------------------------------


class TestDuckDBIntegration:

    def test_duckdb_can_attach_and_query(self, tmp_path: Path) -> None:
        """DuckDB から SQLite に ATTACH して全テーブルを SELECT できる。

        これが F-009 ハイブリッド構成のコア確認: 書き込みは sqlite3、
        読み・分析は DuckDB の sqlite_scanner 経由。
        """
        try:
            import duckdb  # type: ignore[import-not-found]
        except ImportError:
            pytest.skip("duckdb がインストールされていない")

        module = _load_hook_module()
        db_path = tmp_path / "c3.db"
        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))

        # 検証用にダミーデータを sqlite3 で INSERT
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO agent_runs "
                "(session_id, agent_id, agent_type, event, ts, total_tokens, status, model) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ('sess-1', 'agent-a', 'Explore', 'stop',
                 '2026-05-08T00:00:00+00:00', 12345, 'success', 'claude-sonnet-4-6'),
            )
            conn.commit()
        finally:
            conn.close()

        # DuckDB から ATTACH して読む
        con = duckdb.connect()
        try:
            con.execute("INSTALL sqlite")
            con.execute("LOAD sqlite")
            con.execute(f"ATTACH '{db_path}' AS c3 (TYPE SQLITE)")
            rows = con.execute(
                "SELECT session_id, total_tokens, model FROM c3.agent_runs"
            ).fetchall()
        finally:
            con.close()

        assert rows == [('sess-1', 12345, 'claude-sonnet-4-6')]
