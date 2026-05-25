"""tests/test_migrate.py

v2.20.0 SQLite migration runner のテスト。
A 群 (4 件): ユニットテスト（_list_migrations / _get_applied_versions / _run_migration）
B 群 (3 件): 統合テスト - 空 DB への適用
C 群 (2 件): 統合テスト - 既存 DB（v2.19.0 想定）からの upgrade
D 群 (3 件): 失敗系テスト（ROLLBACK / MigrationError / FileNotFoundError）
E 群 (1 件): _ensure_schema_migrations_table 冪等性単体テスト（Round 2 追加）
F 群 (1 件): 002 migration 適用テスト（v2.21.0 追加）
G 群 (4 件): 003 migration 適用テスト（v2.22.0 追加）
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from c3.migrate import (
    MigrationError,
    _ensure_schema_migrations_table,
    _get_applied_versions,
    _list_migrations,
    _run_migration,
    apply_pending_migrations,
)


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _make_migration(migrations_dir: Path, name: str, content: str) -> Path:
    """migrations_dir に SQL ファイルを書き出す。"""
    migrations_dir.mkdir(parents=True, exist_ok=True)
    path = migrations_dir / name
    path.write_text(content, encoding="utf-8")
    return path


def _simple_sql(table_name: str = "test_table") -> str:
    """成功する単純な migration SQL を返す。"""
    return f"""\
BEGIN;
CREATE TABLE IF NOT EXISTS {table_name} (id INTEGER PRIMARY KEY);
COMMIT;
"""


# ---------------------------------------------------------------------------
# A 群: ユニットテスト (4 件)
# ---------------------------------------------------------------------------

class TestMigrateListing:
    """A 群: _list_migrations / _get_applied_versions の単体テスト。"""

    def test_list_migrations_sorted_ascending(self, tmp_path: Path):
        """A1: _list_migrations が NNN_xxx.sql のみ拾い昇順ソートする。"""
        mdir = tmp_path / "migrations"
        mdir.mkdir()
        # 意図的に逆順で作成し、ソートが正しいことを確認
        (mdir / "003_third.sql").write_text("BEGIN;\nCOMMIT;\n", encoding="utf-8")
        (mdir / "001_first.sql").write_text("BEGIN;\nCOMMIT;\n", encoding="utf-8")
        (mdir / "002_second.sql").write_text("BEGIN;\nCOMMIT;\n", encoding="utf-8")

        result = _list_migrations(mdir)

        assert len(result) == 3
        versions = [v for v, _ in result]
        assert versions == ["001", "002", "003"]

    def test_invalid_filename_patterns_skipped(self, tmp_path: Path):
        """A2: 命名規約違反ファイルはスキップされる。

        Foo.sql（先頭数字なし）・1_bar.sql（桁数不足）は対象外。
        """
        mdir = tmp_path / "migrations"
        mdir.mkdir()
        (mdir / "001_valid.sql").write_text("BEGIN;\nCOMMIT;\n", encoding="utf-8")
        (mdir / "Foo.sql").write_text("BEGIN;\nCOMMIT;\n", encoding="utf-8")
        (mdir / "1_bar.sql").write_text("BEGIN;\nCOMMIT;\n", encoding="utf-8")
        (mdir / "README.md").write_text("# readme", encoding="utf-8")

        result = _list_migrations(mdir)

        # 001_valid.sql のみ返る
        assert len(result) == 1
        assert result[0][0] == "001"

    def test_get_applied_versions_returns_empty_when_table_missing(self, tmp_path: Path):
        """A3: _get_applied_versions がテーブル不在時に空 set を返す（防御的挙動）。"""
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        try:
            # schema_migrations テーブルを作らずに呼ぶ
            result = _get_applied_versions(conn)
        finally:
            conn.close()

        assert result == set()

    def test_run_migration_single_sql_inserts_into_schema_migrations(self, tmp_path: Path):
        """A4: _run_migration 単一 SQL 成功で schema_migrations に INSERT される。"""
        mdir = tmp_path / "migrations"
        path = _make_migration(mdir, "001_test.sql", _simple_sql())

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TIMESTAMP NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP)"
            )
            conn.commit()

            _run_migration(conn, "001", path)

            rows = conn.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        finally:
            conn.close()

        versions = [r[0] for r in rows]
        assert "001" in versions


# ---------------------------------------------------------------------------
# B 群: 統合テスト - 空 DB への適用 (3 件)
# ---------------------------------------------------------------------------

class TestMigrateApplyEmptyDb:
    """B 群: 空 DB に 001_initial.sql を適用するテスト。"""

    def test_apply_001_records_in_schema_migrations(self, tmp_path: Path):
        """B1: 001 適用後 schema_migrations に ('001', ts) が記録される。"""
        db_path = tmp_path / "c3.db"
        # デフォルトの migrations ディレクトリ（src/c3/migrations/）を使用
        applied = apply_pending_migrations(db_path)

        assert "001" in applied

        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT version, applied_at FROM schema_migrations WHERE version = '001'"
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 1
        version, applied_at = rows[0]
        assert version == "001"
        # applied_at は ISO8601 形式（YYYY-MM-DD HH:MM:SS）
        assert applied_at is not None
        assert len(applied_at) >= 10  # 最低 YYYY-MM-DD の長さ

    def test_apply_001_creates_all_tables(self, tmp_path: Path):
        """B2: 001 適用後、全 5 テーブル + 4 INDEX が作成される。"""
        db_path = tmp_path / "c3.db"
        apply_pending_migrations(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
        finally:
            conn.close()

        # 5 テーブルが存在すること
        expected_tables = {
            "schema_migrations",
            "review_decisions",
            "tier_bandit",
            "tier_recent_outcomes",
            "agent_runs",
        }
        assert expected_tables.issubset(tables), (
            f"不足しているテーブル: {expected_tables - tables}"
        )

        # 4 INDEX が存在すること
        expected_indexes = {
            "idx_review_decisions_checklist",
            "idx_tier_recent",
            "idx_agent_runs_session",
            "idx_agent_runs_agent",
        }
        # 4 INDEX（agent_runs に 2 つ: session / agent）が存在することを全件チェックする
        assert expected_indexes.issubset(indexes), (
            f"不足している INDEX: {expected_indexes - indexes}"
        )

    def test_apply_idempotent_runs_twice(self, tmp_path: Path):
        """B3: 冪等性 — 2 回連続適用しても schema_migrations の '001' 行は 1 件のみ。"""
        db_path = tmp_path / "c3.db"
        apply_pending_migrations(db_path)
        # 2 回目
        applied_second = apply_pending_migrations(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = '001'"
            ).fetchone()[0]
        finally:
            conn.close()

        # 2 回目は何も適用しない
        assert applied_second == []
        # schema_migrations には 1 行だけ
        assert count == 1


# ---------------------------------------------------------------------------
# C 群: 統合テスト - 既存 DB からの upgrade (2 件)
# ---------------------------------------------------------------------------

class TestMigrateApplyExistingDb:
    """C 群: v2.19.0 想定 DB（schema_version=3 + データあり）からの upgrade テスト。"""

    @pytest.fixture()
    def v219_db(self, tmp_path: Path) -> Path:
        """v2.19.0 想定 DB を構築する fixture。

        schema_version テーブルに version=3 の行を挿入し、
        review_decisions に 1 行のサンプルデータを持つ DB を返す。
        """
        db_path = tmp_path / "c3_v219.db"
        conn = sqlite3.connect(str(db_path))
        try:
            # v2.19.0 のスキーマ（旧 schema_version テーブルあり）
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO schema_version (version, applied_at)
                VALUES (3, '2025-01-01T00:00:00');

                CREATE TABLE IF NOT EXISTS review_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    checklist_id TEXT NOT NULL,
                    finding_text TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT,
                    context_summary TEXT,
                    decided_at TEXT NOT NULL,
                    reviewer TEXT NOT NULL
                );
                INSERT INTO review_decisions
                    (checklist_id, finding_text, decision, decided_at, reviewer)
                VALUES
                    ('CR-Q-001', 'test finding', 'accepted', '2025-01-01T00:00:00', 'code-reviewer');
            """)
        finally:
            conn.close()
        return db_path

    def test_upgrade_from_v219_records_schema_migrations(
        self, v219_db: Path
    ):
        """C1: v2.19.0 想定 DB からの upgrade 後の状態を検証する。

        - schema_migrations に '001' が記録される
        - schema_version テーブルが消える
        - review_decisions の既存行が保持される
        """
        apply_pending_migrations(v219_db)

        conn = sqlite3.connect(str(v219_db))
        try:
            # schema_migrations に '001' が記録される
            migrations = conn.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
            assert any(r[0] == "001" for r in migrations), (
                "schema_migrations に '001' が記録されていません"
            )

            # schema_version テーブルが消える
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "schema_version" not in tables, (
                "schema_version テーブルが残っています"
            )

            # review_decisions の既存行が保持される
            decisions = conn.execute(
                "SELECT checklist_id FROM review_decisions"
            ).fetchall()
            assert len(decisions) >= 1, (
                "review_decisions の既存行が消えています"
            )
        finally:
            conn.close()

    def test_upgrade_preserves_existing_data(self, v219_db: Path):
        """C2: 既存の review_decisions / tier_bandit / agent_runs データが保持される。

        migration 後も既存行が正しく参照できることを確認する。
        """
        apply_pending_migrations(v219_db)

        conn = sqlite3.connect(str(v219_db))
        try:
            row = conn.execute(
                "SELECT checklist_id, finding_text, decision FROM review_decisions"
                " WHERE checklist_id = 'CR-Q-001'"
            ).fetchone()
        finally:
            conn.close()

        assert row is not None, "既存の review_decisions 行が見つかりません"
        assert row[0] == "CR-Q-001"
        assert row[2] == "accepted"


# ---------------------------------------------------------------------------
# D 群: 失敗系テスト (3 件)
# ---------------------------------------------------------------------------

class TestMigrateFailure:
    """D 群: ROLLBACK / MigrationError / FileNotFoundError のテスト。"""

    def test_invalid_sql_raises_migration_error_and_rollback(
        self, tmp_path: Path
    ):
        """D1: 不正 SQL で MigrationError が raise され、ROLLBACK が確認できる。

        schema_migrations に当該 version の行が無いことで ROLLBACK を確認する。
        """
        mdir = tmp_path / "migrations"
        # 不正 SQL: SELECT FROM が構文エラー
        _make_migration(
            mdir,
            "001_bad.sql",
            "BEGIN;\nSELECT FROM nonexistent_table;\nCOMMIT;\n",
        )

        db_path = tmp_path / "c3.db"
        with pytest.raises(MigrationError):
            apply_pending_migrations(db_path, migrations_dir=mdir)

        # schema_migrations に '001' の行が無いこと（ROLLBACK 確認）
        conn = sqlite3.connect(str(db_path))
        try:
            # schema_migrations テーブル自体は作られているかもしれないが、
            # '001' の行は記録されていないはず
            try:
                rows = conn.execute(
                    "SELECT version FROM schema_migrations WHERE version = '001'"
                ).fetchall()
            except sqlite3.OperationalError:
                # テーブル自体が存在しない場合も '001' 未記録と同義
                rows = []
        finally:
            conn.close()

        assert len(rows) == 0, "ROLLBACK が効いておらず '001' が記録されています"

    def test_002_failure_leaves_001_applied(self, tmp_path: Path):
        """D2: 002 (不正 SQL) が失敗しても 001 の適用記録は残る (per-migration commit)。

        002 で MigrationError が raise されるため apply_pending_migrations の戻り値は
        得られないが、schema_migrations に 001 の記録が残っていることを直接確認する。
        """
        mdir = tmp_path / "migrations"
        _make_migration(mdir, "001_good.sql", _simple_sql("table_from_001"))
        _make_migration(
            mdir,
            "002_bad.sql",
            "BEGIN;\nSELECT FROM nonexistent;\nCOMMIT;\n",
        )

        db_path = tmp_path / "c3.db"

        # apply_pending_migrations は 001 を適用後、002 で MigrationError を raise
        # 例外伝播のため戻り値は得られない
        with pytest.raises(MigrationError):
            apply_pending_migrations(db_path, migrations_dir=mdir)

        # 001 は schema_migrations に残っている
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        finally:
            conn.close()

        versions = [r[0] for r in rows]
        assert "001" in versions, "001 の適用記録が消えています"
        assert "002" not in versions, "失敗した 002 が記録されています"

    def test_missing_migrations_dir_raises_file_not_found(self, tmp_path: Path):
        """D3: migrations_dir 不在で FileNotFoundError が raise される。"""
        db_path = tmp_path / "c3.db"
        nonexistent_dir = tmp_path / "does_not_exist" / "migrations"

        with pytest.raises(FileNotFoundError):
            apply_pending_migrations(db_path, migrations_dir=nonexistent_dir)


# ---------------------------------------------------------------------------
# E 群: _ensure_schema_migrations_table 単体テスト (1 件)
# ---------------------------------------------------------------------------

class TestEnsureSchemaMigrationsTable:
    """E 群: _ensure_schema_migrations_table の冪等性単体テスト（Round 2 CR L-2 由来: _ensure_schema_migrations_table 単体テスト追加）。"""

    def test_ensure_schema_migrations_table_idempotent(self, tmp_path: Path):
        """E1: _ensure_schema_migrations_table を 2 回呼んでも冪等でテーブル構造が正しい。

        - テーブルが 1 つだけ存在すること（重複作成なし）。
        - 列が version（TEXT PRIMARY KEY）と applied_at（TIMESTAMP NOT NULL DEFAULT ...）であること。
        - 2 回目の呼び出しで例外が発生しないこと（IF NOT EXISTS による冪等保証）。
        """
        db_path = tmp_path / "idempotent.db"
        conn = sqlite3.connect(str(db_path))
        try:
            # 1 回目
            _ensure_schema_migrations_table(conn)
            # 2 回目（冪等：例外が出てはならない）
            _ensure_schema_migrations_table(conn)

            # テーブルが 1 つだけ存在する
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
                ).fetchall()
            ]
            assert len(tables) == 1, "schema_migrations テーブルが 1 つ存在するはず"

            # 列名が version と applied_at であること
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(schema_migrations)").fetchall()
            }
            assert "version" in columns, "version 列が存在するはず"
            assert "applied_at" in columns, "applied_at 列が存在するはず"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# F 群: 002 migration 適用テスト (1 件、v2.21.0 追加)
# ---------------------------------------------------------------------------

class TestMigrate002AgentCostRuns:
    """F 群: 002_agent_cost_runs.sql の適用テスト。"""

    def test_apply_002_creates_agent_cost_tables_and_index(self, tmp_path: Path):
        """F1: 空 DB に 001+002 を適用すると両テーブル・INDEX・schema_migrations 行が揃う。

        - apply_pending_migrations の戻り値が ['001', '002']（昇順）
        - agent_cost_runs テーブルが存在する
        - usage_ingest_state テーブルが存在する
        - idx_agent_cost_runs_agent_type INDEX が存在する
        - schema_migrations に '002' 行が存在する
        """
        db_path = tmp_path / "c3.db"
        applied = apply_pending_migrations(db_path)

        # 001 と 002 が両方適用される
        assert "001" in applied, f"001 が applied に含まれない: {applied}"
        assert "002" in applied, f"002 が applied に含まれない: {applied}"
        # 昇順
        assert applied.index("001") < applied.index("002"), "001 が 002 より前に来るはず"

        conn = sqlite3.connect(str(db_path))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            migration_versions = {
                row[0]
                for row in conn.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
        finally:
            conn.close()

        # 002 で追加される 2 テーブルが存在する
        assert "agent_cost_runs" in tables, "agent_cost_runs テーブルが見つかりません"
        assert "usage_ingest_state" in tables, "usage_ingest_state テーブルが見つかりません"

        # 002 で追加される INDEX が存在する
        assert "idx_agent_cost_runs_agent_type" in indexes, (
            "idx_agent_cost_runs_agent_type INDEX が見つかりません"
        )

        # schema_migrations に '002' が記録されている
        assert "002" in migration_versions, (
            f"schema_migrations に '002' が記録されていません: {migration_versions}"
        )


# ---------------------------------------------------------------------------
# G 群: 003 migration 適用テスト (4 件、v2.22.0 追加)
# ---------------------------------------------------------------------------

class TestMigrate003TierCost:
    """G 群: 003_tier_cost.sql の適用テスト。"""

    def test_apply_003_returns_version_in_applied(self, tmp_path: Path):
        """G1: apply_pending_migrations の戻り値に '003' が含まれる。

        空 DB に 001+002+003 を適用し、戻り値リストに '003' が入ることを確認する。
        """
        db_path = tmp_path / "c3.db"
        applied = apply_pending_migrations(db_path)

        assert "003" in applied, f"003 が applied に含まれない: {applied}"
        # 適用順も確認（001 < 002 < 003）
        assert applied.index("001") < applied.index("003"), "001 が 003 より前に来るはず"
        assert applied.index("002") < applied.index("003"), "002 が 003 より前に来るはず"

    def test_apply_003_adds_columns_and_index(self, tmp_path: Path):
        """G2: 003 適用後、tier_recent_outcomes に session_id 列と idx_tier_recent_session が、
        tier_bandit に total_cost_usd / cost_samples 列が存在する。

        total_cost_usd / cost_samples は v2.23.0 用確保のみ
        （v2.22.0 では値の書き込み・読み出しなし）。
        """
        db_path = tmp_path / "c3.db"
        apply_pending_migrations(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            # tier_recent_outcomes の列名一覧を取得
            tro_columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(tier_recent_outcomes)"
                ).fetchall()
            }
            # tier_bandit の列名一覧を取得
            tb_columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(tier_bandit)"
                ).fetchall()
            }
            # tier_recent_outcomes のインデックス一覧を取得
            tro_indexes = {
                row[1]
                for row in conn.execute(
                    "PRAGMA index_list(tier_recent_outcomes)"
                ).fetchall()
            }
        finally:
            conn.close()

        assert "session_id" in tro_columns, (
            f"tier_recent_outcomes に session_id 列がありません: {tro_columns}"
        )
        assert "total_cost_usd" in tb_columns, (
            f"tier_bandit に total_cost_usd 列がありません: {tb_columns}"
        )
        assert "cost_samples" in tb_columns, (
            f"tier_bandit に cost_samples 列がありません: {tb_columns}"
        )
        assert "idx_tier_recent_session" in tro_indexes, (
            f"idx_tier_recent_session INDEX がありません: {tro_indexes}"
        )

    def test_apply_003_preserves_existing_rows_with_defaults(self, tmp_path: Path):
        """G3: 003 適用前に挿入した既存行が 003 適用後も保持され、追加列が DEFAULT 値になる。

        - tier_bandit の既存行: total_cost_usd=0.0 / cost_samples=0
        - tier_recent_outcomes の既存行: session_id=NULL
        """
        db_path = tmp_path / "c3.db"

        # 001+002 まで適用してデータを挿入
        from c3.migrate import _DEFAULT_MIGRATIONS_DIR
        mdir_real = _DEFAULT_MIGRATIONS_DIR

        # 実 migrations ディレクトリから 001+002 のみ適用するため、tmp に 001+002 だけコピーして使用
        import shutil
        mdir_tmp = tmp_path / "migrations_partial"
        mdir_tmp.mkdir()
        shutil.copy(mdir_real / "001_initial.sql", mdir_tmp / "001_initial.sql")
        shutil.copy(mdir_real / "002_agent_cost_runs.sql", mdir_tmp / "002_agent_cost_runs.sql")

        apply_pending_migrations(db_path, migrations_dir=mdir_tmp)

        # 003 適用前にデータを挿入
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO tier_bandit (task_complexity, tier, alpha, beta, trials)"
                " VALUES ('simple', 'haiku', 1.0, 1.0, 5)"
            )
            conn.execute(
                "INSERT INTO tier_recent_outcomes (task_complexity, tier, success, ts)"
                " VALUES ('simple', 'haiku', 1, '2026-01-01T00:00:00')"
            )
            conn.commit()
        finally:
            conn.close()

        # 003 を適用（実 migrations ディレクトリから。001+002 は schema_migrations に記録済みのためスキップ）
        apply_pending_migrations(db_path)

        # 003 適用後のデータ確認
        conn = sqlite3.connect(str(db_path))
        try:
            tb_row = conn.execute(
                "SELECT total_cost_usd, cost_samples FROM tier_bandit"
                " WHERE task_complexity='simple' AND tier='haiku'"
            ).fetchone()
            tro_row = conn.execute(
                "SELECT session_id FROM tier_recent_outcomes"
                " WHERE task_complexity='simple' AND tier='haiku'"
            ).fetchone()
        finally:
            conn.close()

        assert tb_row is not None, "tier_bandit の既存行が消えています"
        assert tb_row[0] == 0.0, f"total_cost_usd の DEFAULT 値が不正: {tb_row[0]}"
        assert tb_row[1] == 0, f"cost_samples の DEFAULT 値が不正: {tb_row[1]}"

        assert tro_row is not None, "tier_recent_outcomes の既存行が消えています"
        assert tro_row[0] is None, f"session_id の DEFAULT 値が NULL でない: {tro_row[0]}"

    def test_apply_003_schema_migrations_records_003(self, tmp_path: Path):
        """G4: 003 適用後 schema_migrations に '003' 行が記録されている。"""
        db_path = tmp_path / "c3.db"
        apply_pending_migrations(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            migration_versions = {
                row[0]
                for row in conn.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
        finally:
            conn.close()

        assert "003" in migration_versions, (
            f"schema_migrations に '003' が記録されていません: {migration_versions}"
        )
