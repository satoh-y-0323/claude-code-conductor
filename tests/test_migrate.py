"""tests/test_migrate.py

v2.20.0 SQLite migration runner のテスト。
A 群 (4 件): ユニットテスト（_list_migrations / _get_applied_versions / _run_migration）
B 群 (3 件): 統合テスト - 空 DB への適用
C 群 (2 件): 統合テスト - 既存 DB（v2.19.0 想定）からの upgrade
D 群 (3 件): 失敗系テスト（ROLLBACK / MigrationError / FileNotFoundError）
E 群 (1 件): _ensure_schema_migrations_table 冪等性単体テスト（Round 2 追加）
F 群 (1 件): 002 migration 適用テスト（v2.21.0 追加）
G 群 (4 件): 003 migration 適用テスト（v2.22.0 追加）
H 群 (7 件): 004 migration 適用テスト
I 群 (4 件): 005 migration 適用テスト
J 群 (4 件): 006 migration 適用テスト（P4 c3 metrics・review_decisions.severity 追加）
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
        """B2: 001 適用後、全 5 テーブル + 4 INDEX が作成される。

        NOTE(v2.41.0 db-foundation): 004 が tier_bandit/tier_recent_outcomes を
        DROP するため（ADR-1）、実 migrations ディレクトリ（004 含む）をそのまま
        使うと本テストが検証したい「001 適用直後」の状態を確認できなくなる。
        001 自体の DDL 検証という本テストの意図を保つため、001 のみを含む
        一時ディレクトリに限定する。
        """
        import shutil  # noqa: PLC0415

        from c3.migrate import _DEFAULT_MIGRATIONS_DIR  # noqa: PLC0415

        db_path = tmp_path / "c3.db"
        mdir_001_only = tmp_path / "migrations_001_only"
        mdir_001_only.mkdir()
        shutil.copy(
            _DEFAULT_MIGRATIONS_DIR / "001_initial.sql",
            mdir_001_only / "001_initial.sql",
        )
        apply_pending_migrations(db_path, migrations_dir=mdir_001_only)

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

        NOTE(v2.41.0 db-foundation): 004 が tier_bandit/tier_recent_outcomes を
        DROP するため（ADR-1）、実 migrations ディレクトリ（004 含む）をそのまま
        使うと 003 の効果を検証する前にテーブルが消えてしまう。003 自体の DDL 検証
        という本テストの意図を保つため、001+002+003 のみを含む一時ディレクトリに
        限定する（H6 と同じ手法）。
        """
        import shutil  # noqa: PLC0415

        from c3.migrate import _DEFAULT_MIGRATIONS_DIR  # noqa: PLC0415

        db_path = tmp_path / "c3.db"
        mdir_003_only = tmp_path / "migrations_003_only"
        mdir_003_only.mkdir()
        for name in ("001_initial.sql", "002_agent_cost_runs.sql", "003_tier_cost.sql"):
            shutil.copy(_DEFAULT_MIGRATIONS_DIR / name, mdir_003_only / name)
        apply_pending_migrations(db_path, migrations_dir=mdir_003_only)

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

        # 003 を適用。NOTE(v2.41.0 db-foundation): 実 migrations ディレクトリを使うと
        # 004（tier_bandit/tier_recent_outcomes を DROP・ADR-1）まで適用されてしまい
        # 本テストが検証したい「003 適用直後の DEFAULT 値」を確認できなくなるため、
        # mdir_tmp と同じ一時ディレクトリに 003 のみ追加コピーして限定適用する
        # （001+002 は schema_migrations に記録済みのためスキップされる）。
        shutil.copy(mdir_real / "003_tier_cost.sql", mdir_tmp / "003_tier_cost.sql")
        apply_pending_migrations(db_path, migrations_dir=mdir_tmp)

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


# ---------------------------------------------------------------------------
# H 群: 004 migration 適用テスト (v2.41.0 db-foundation・tier-routing 学習シグナル再設計)
# ---------------------------------------------------------------------------

class TestMigrate004AgentOutcomes:
    """H 群: 004_agent_outcomes.sql の適用テスト（Red 先行・未実装のため 004 は失敗する）。

    architecture-report-20260702-214748.md §3-1 に従い、旧 tier_bandit /
    tier_recent_outcomes を DROP し、agent_tier_bandit / agent_outcomes を新設する。
    """

    def test_apply_004_returns_version_in_applied(self, tmp_path: Path):
        """H1: apply_pending_migrations の戻り値に '004' が含まれ、003 より後に適用される。"""
        db_path = tmp_path / "c3.db"
        applied = apply_pending_migrations(db_path)

        assert "004" in applied, f"004 が applied に含まれない: {applied}"
        assert applied.index("003") < applied.index("004"), "003 が 004 より前に来るはず"

    def test_apply_004_creates_new_tables_and_drops_old(self, tmp_path: Path):
        """H2: 004 適用後、旧 tier_bandit / tier_recent_outcomes が消滅し、
        新 agent_tier_bandit / agent_outcomes が存在する。

        フェーズ2.5（ADR-25-4）で 005 が agent_tier_bandit を DROP するため、
        デフォルト migrations ディレクトリ（005 含む）を通しで適用すると
        「004 適用後」の状態を検証できない。004 時点の状態を確認する意図を
        保つため、H6/I4 と同型の 001〜004 限定ディレクトリで適用する。
        """
        import shutil  # noqa: PLC0415

        from c3.migrate import _DEFAULT_MIGRATIONS_DIR  # noqa: PLC0415

        db_path = tmp_path / "c3.db"
        mdir_004_only = tmp_path / "migrations_004_only"
        mdir_004_only.mkdir()
        for name in (
            "001_initial.sql", "002_agent_cost_runs.sql",
            "003_tier_cost.sql", "004_agent_outcomes.sql",
        ):
            shutil.copy(_DEFAULT_MIGRATIONS_DIR / name, mdir_004_only / name)
        apply_pending_migrations(db_path, migrations_dir=mdir_004_only)

        conn = sqlite3.connect(str(db_path))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()

        assert "agent_tier_bandit" in tables, (
            f"agent_tier_bandit テーブルが見つかりません: {tables}"
        )
        assert "agent_outcomes" in tables, (
            f"agent_outcomes テーブルが見つかりません: {tables}"
        )
        assert "tier_bandit" not in tables, "旧 tier_bandit が消えていません"
        assert "tier_recent_outcomes" not in tables, "旧 tier_recent_outcomes が消えていません"

    def test_apply_004_creates_expected_indexes(self, tmp_path: Path):
        """H3: agent_outcomes 用の 2 INDEX（cell / session）が作成される。"""
        db_path = tmp_path / "c3.db"
        apply_pending_migrations(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
        finally:
            conn.close()

        assert "idx_agent_outcomes_cell" in indexes, (
            f"idx_agent_outcomes_cell INDEX が見つかりません: {indexes}"
        )
        assert "idx_agent_outcomes_session" in indexes, (
            f"idx_agent_outcomes_session INDEX が見つかりません: {indexes}"
        )

    def test_agent_tier_bandit_columns(self, tmp_path: Path):
        """H4: agent_tier_bandit の列構成が仕様通り（role/task_complexity/tier/alpha/beta/trials/last_updated）。

        005（ADR-25-4）が agent_tier_bandit を DROP するため、H2 と同じ理由で
        001〜004 限定ディレクトリで適用し「004 時点」の列構成を検証する。
        """
        import shutil  # noqa: PLC0415

        from c3.migrate import _DEFAULT_MIGRATIONS_DIR  # noqa: PLC0415

        db_path = tmp_path / "c3.db"
        mdir_004_only = tmp_path / "migrations_004_only"
        mdir_004_only.mkdir()
        for name in (
            "001_initial.sql", "002_agent_cost_runs.sql",
            "003_tier_cost.sql", "004_agent_outcomes.sql",
        ):
            shutil.copy(_DEFAULT_MIGRATIONS_DIR / name, mdir_004_only / name)
        apply_pending_migrations(db_path, migrations_dir=mdir_004_only)

        conn = sqlite3.connect(str(db_path))
        try:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(agent_tier_bandit)").fetchall()
            }
        finally:
            conn.close()

        expected = {"role", "task_complexity", "tier", "alpha", "beta", "trials", "last_updated"}
        assert expected.issubset(columns), (
            f"agent_tier_bandit に不足している列: {expected - columns}"
        )

    def test_agent_outcomes_columns(self, tmp_path: Path):
        """H5: agent_outcomes の列構成が仕様通り（role/task_complexity/tier/success/gate/note/session_id/ts）。"""
        db_path = tmp_path / "c3.db"
        apply_pending_migrations(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(agent_outcomes)").fetchall()
            }
        finally:
            conn.close()

        expected = {
            "id", "role", "task_complexity", "tier", "success",
            "gate", "note", "session_id", "ts",
        }
        assert expected.issubset(columns), (
            f"agent_outcomes に不足している列: {expected - columns}"
        )

    def test_upgrade_from_003_drops_old_tables_and_data(self, tmp_path: Path):
        """H6: 003 まで適用済み・データありの DB から 004 を適用すると、
        旧テーブルと旧データが消え、新テーブルが（空で）存在する。

        003→004 upgrade 経路（plan-report db-foundation の主眼）を固定する。

        NOTE(フェーズ2.5・ADR-25-4): 005 が agent_tier_bandit を DROP する
        ため、デフォルト migrations ディレクトリ（005 含む）で「続きを適用」
        すると 004 時点の状態（agent_tier_bandit 存在）を検証できない。
        004 自体の upgrade 効果を検証する意図を保つため、継続適用も
        001〜004 限定ディレクトリで行う（005 は適用しない）。
        """
        import shutil  # noqa: PLC0415

        from c3.migrate import _DEFAULT_MIGRATIONS_DIR  # noqa: PLC0415

        db_path = tmp_path / "c3.db"

        # 001+002+003 のみを含む一時 migrations ディレクトリを作り、003 相当の DB を再現する
        mdir_003_only = tmp_path / "migrations_003_only"
        mdir_003_only.mkdir()
        for name in ("001_initial.sql", "002_agent_cost_runs.sql", "003_tier_cost.sql"):
            shutil.copy(_DEFAULT_MIGRATIONS_DIR / name, mdir_003_only / name)
        apply_pending_migrations(db_path, migrations_dir=mdir_003_only)

        # 003 段階の DB に旧テーブルへの既存データを投入する
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO tier_bandit (task_complexity, tier, alpha, beta, trials)"
                " VALUES ('medium', 'sonnet', 3.0, 2.0, 4)"
            )
            conn.execute(
                "INSERT INTO tier_recent_outcomes (task_complexity, tier, success, ts)"
                " VALUES ('medium', 'sonnet', 1, '2026-01-01T00:00:00')"
            )
            conn.commit()
        finally:
            conn.close()

        # 001〜004 限定ディレクトリ（005 非含有）から続きを適用する
        mdir_004_only = tmp_path / "migrations_004_only"
        mdir_004_only.mkdir()
        for name in (
            "001_initial.sql", "002_agent_cost_runs.sql",
            "003_tier_cost.sql", "004_agent_outcomes.sql",
        ):
            shutil.copy(_DEFAULT_MIGRATIONS_DIR / name, mdir_004_only / name)
        applied = apply_pending_migrations(db_path, migrations_dir=mdir_004_only)
        assert "004" in applied, f"004 が applied に含まれない: {applied}"

        conn = sqlite3.connect(str(db_path))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            # 新テーブルが存在すれば行数も確認（無ければ OperationalError で except に落ちる）
            agent_tier_bandit_count = None
            agent_outcomes_count = None
            if "agent_tier_bandit" in tables:
                agent_tier_bandit_count = conn.execute(
                    "SELECT COUNT(*) FROM agent_tier_bandit"
                ).fetchone()[0]
            if "agent_outcomes" in tables:
                agent_outcomes_count = conn.execute(
                    "SELECT COUNT(*) FROM agent_outcomes"
                ).fetchone()[0]
        finally:
            conn.close()

        assert "tier_bandit" not in tables, "旧 tier_bandit のデータごと消えているはず"
        assert "tier_recent_outcomes" not in tables, "旧 tier_recent_outcomes のデータごと消えているはず"
        assert "agent_tier_bandit" in tables
        assert "agent_outcomes" in tables
        assert agent_tier_bandit_count == 0, "新テーブルは空で作成されるはず（移行データなし・DROP+CREATE）"
        assert agent_outcomes_count == 0, "新テーブルは空で作成されるはず（移行データなし・DROP+CREATE）"

    def test_apply_004_schema_migrations_records_004(self, tmp_path: Path):
        """H7: 004 適用後 schema_migrations に '004' 行が記録されている。"""
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

        assert "004" in migration_versions, (
            f"schema_migrations に '004' が記録されていません: {migration_versions}"
        )


# ---------------------------------------------------------------------------
# I 群: 005 migration 適用テスト (tier-routing フェーズ2.5・ADR-25-4・Red 先行)
# ---------------------------------------------------------------------------

class TestMigrate005DropAgentTierBandit:
    """I 群: 005_drop_agent_tier_bandit.sql の適用テスト（Red 先行・未実装のため 005 は失敗する）。

    architecture-report-20260703-150507.md ADR-25-4 に従い、004 で導入された
    agent_tier_bandit を DROP する（read_agent_tier_params が agent_outcomes
    からの導出集計に置換されたため累積テーブルが不要になった）。
    agent_outcomes はイベントログとして不変で残す。H 群（004）と同型。
    """

    def test_apply_005_returns_version_in_applied(self, tmp_path: Path):
        """I1: apply_pending_migrations の戻り値に '005' が含まれ、004 より後に適用される。"""
        db_path = tmp_path / "c3.db"
        applied = apply_pending_migrations(db_path)

        assert "005" in applied, f"005 が applied に含まれない: {applied}"
        assert applied.index("004") < applied.index("005"), "004 が 005 より前に来るはず"

    def test_apply_005_drops_agent_tier_bandit_keeps_agent_outcomes(self, tmp_path: Path):
        """I2: 005 適用後、agent_tier_bandit が消滅し agent_outcomes は存置される。"""
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
        finally:
            conn.close()

        assert "agent_tier_bandit" not in tables, (
            f"agent_tier_bandit が消えていません（ADR-25-4）: {tables}"
        )
        assert "agent_outcomes" in tables, (
            f"agent_outcomes テーブルが見つかりません（イベントログは不変のはず）: {tables}"
        )

    def test_apply_005_schema_migrations_records_005(self, tmp_path: Path):
        """I3: 005 適用後 schema_migrations に '005' 行が記録されている。"""
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

        assert "005" in migration_versions, (
            f"schema_migrations に '005' が記録されていません: {migration_versions}"
        )

    def test_upgrade_from_004_drops_agent_tier_bandit_and_data(self, tmp_path: Path):
        """I4: 004 まで適用済み・agent_tier_bandit にデータありの DB から 005 を適用すると、
        agent_tier_bandit がデータごと消え、agent_outcomes の既存データは保持される。

        004→005 upgrade 経路（tier-routing フェーズ2.5 の主眼）を固定する。
        """
        import shutil  # noqa: PLC0415

        from c3.migrate import _DEFAULT_MIGRATIONS_DIR  # noqa: PLC0415

        db_path = tmp_path / "c3.db"

        # 001〜004 のみを含む一時 migrations ディレクトリを作り、004 相当の DB を再現する
        mdir_004_only = tmp_path / "migrations_004_only"
        mdir_004_only.mkdir()
        for name in (
            "001_initial.sql", "002_agent_cost_runs.sql",
            "003_tier_cost.sql", "004_agent_outcomes.sql",
        ):
            shutil.copy(_DEFAULT_MIGRATIONS_DIR / name, mdir_004_only / name)
        apply_pending_migrations(db_path, migrations_dir=mdir_004_only)

        # 004 段階の DB に agent_tier_bandit / agent_outcomes への既存データを投入する
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO agent_tier_bandit "
                "(role, task_complexity, tier, alpha, beta, trials, last_updated)"
                " VALUES ('developer', 'medium', 'sonnet', 3.0, 2.0, 4, '2026-01-01T00:00:00')"
            )
            conn.execute(
                "INSERT INTO agent_outcomes "
                "(role, task_complexity, tier, success, gate, ts)"
                " VALUES ('developer', 'medium', 'sonnet', 1, 'D-2.5', '2026-01-01T00:00:00')"
            )
            conn.commit()
        finally:
            conn.close()

        # 実 migrations ディレクトリ（005 を含む）から続きを適用する
        applied = apply_pending_migrations(db_path)
        assert "005" in applied, f"005 が applied に含まれない: {applied}"

        conn = sqlite3.connect(str(db_path))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            agent_outcomes_count = None
            if "agent_outcomes" in tables:
                agent_outcomes_count = conn.execute(
                    "SELECT COUNT(*) FROM agent_outcomes"
                ).fetchone()[0]
        finally:
            conn.close()

        assert "agent_tier_bandit" not in tables, "agent_tier_bandit はデータごと消えているはず"
        assert "agent_outcomes" in tables
        assert agent_outcomes_count == 1, (
            "agent_outcomes の既存データは 005（DROP 対象外）で保持されるはず"
        )


# ---------------------------------------------------------------------------
# J 群: 006 migration 適用テスト (P4 c3 metrics・review_decisions.severity 追加)
# ---------------------------------------------------------------------------

class TestMigrate006ReviewDecisionsSeverity:
    """J 群: 006_review_decisions_severity.sql の適用テスト。

    architecture-report-20260706-213701.md §2-1 に従い、review_decisions に
    severity TEXT（nullable・CHECK なし・additive）を追加する。
    """

    def test_apply_006_returns_version_in_applied(self, tmp_path: Path):
        """J1: 新規 DB への 001→006 連続適用で戻り値に '006' が含まれ、005 より後に適用された。"""
        db_path = tmp_path / "c3.db"
        applied = apply_pending_migrations(db_path)

        assert "006" in applied, f"006 が applied に含まれない: {applied}"
        assert applied.index("005") < applied.index("006"), "005 が 006 より前に来るはず"

    def test_apply_006_adds_severity_column(self, tmp_path: Path):
        """J2: 006 適用後、review_decisions に severity 列が存在した。"""
        db_path = tmp_path / "c3.db"
        apply_pending_migrations(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(review_decisions)").fetchall()
            }
        finally:
            conn.close()

        assert "severity" in columns, f"review_decisions に severity 列がない: {columns}"

    def test_apply_006_schema_migrations_records_006(self, tmp_path: Path):
        """J3: 006 適用後 schema_migrations に '006' 行が記録された。"""
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

        assert "006" in migration_versions, (
            f"schema_migrations に '006' が記録されていない: {migration_versions}"
        )

    def test_upgrade_from_005_adds_severity_column_preserves_existing_null(
        self, tmp_path: Path
    ):
        """J4: 005 適用済み・review_decisions に既存行ありの DB へ 006 を追適用すると、
        severity 列が追加され、既存行の severity は NULL のまま保持された。

        005→006 upgrade 経路（P4 c3 metrics の主眼）を固定する。
        """
        import shutil  # noqa: PLC0415

        from c3.migrate import _DEFAULT_MIGRATIONS_DIR  # noqa: PLC0415

        db_path = tmp_path / "c3.db"

        # 001〜005 のみを含む一時 migrations ディレクトリを作り、005 相当の DB を再現する
        mdir_005_only = tmp_path / "migrations_005_only"
        mdir_005_only.mkdir()
        for name in (
            "001_initial.sql", "002_agent_cost_runs.sql", "003_tier_cost.sql",
            "004_agent_outcomes.sql", "005_drop_agent_tier_bandit.sql",
        ):
            shutil.copy(_DEFAULT_MIGRATIONS_DIR / name, mdir_005_only / name)
        apply_pending_migrations(db_path, migrations_dir=mdir_005_only)

        # 005 段階の DB に review_decisions への既存データ（severity 列なしの旧 7 列 INSERT）を投入する
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO review_decisions"
                " (checklist_id, finding_text, decision, decided_at, reviewer)"
                " VALUES ('CR-Q-100', 'legacy finding', 'accepted',"
                " '2026-01-01T00:00:00+00:00', 'code-reviewer')"
            )
            conn.commit()
        finally:
            conn.close()

        # 実 migrations ディレクトリ（006 を含む）から続きを適用する
        applied = apply_pending_migrations(db_path)
        assert "006" in applied, f"006 が applied に含まれない: {applied}"

        conn = sqlite3.connect(str(db_path))
        try:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(review_decisions)").fetchall()
            }
            row = conn.execute(
                "SELECT severity FROM review_decisions WHERE checklist_id = 'CR-Q-100'"
            ).fetchone()
        finally:
            conn.close()

        assert "severity" in columns, f"review_decisions に severity 列がない: {columns}"
        assert row is not None, "既存の review_decisions 行が見つからない"
        assert row[0] is None, f"既存行の severity が NULL で保持されていない: {row[0]}"
