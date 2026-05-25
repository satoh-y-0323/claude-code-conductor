"""C3 SQLite migration runner (v2.20.0 新規)。

公開 API:
  - apply_pending_migrations(db_path, migrations_dir=None) -> list[str]
  - MigrationError(RuntimeError)

命名規約:
  migrations/ 配下の NNN_xxx.sql ファイル（NNN は 3 桁ゼロパディング連番）を
  昇順で適用する。適用済みの version は schema_migrations テーブルで管理する。

落とし穴 (architecture §10.4):
  executescript() は内部で自動 COMMIT を発行するため、SQL ファイル内に BEGIN;
  を書かないとステートメント単位の autocommit になり ROLLBACK が効かない。
  各 migration ファイルは必ず BEGIN; で始まり COMMIT; で終わること。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import warnings
from pathlib import Path

from c3.db import BUSY_TIMEOUT_MS

logger = logging.getLogger(__name__)

# migration ファイル名の命名規約パターン: NNN_説明.sql (ASCII 小文字・数字・アンダースコア)
_MIGRATION_FILENAME_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")

# デフォルト migrations ディレクトリ: このファイルと同階層の migrations/
_DEFAULT_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class MigrationError(RuntimeError):
    """Migration の適用に失敗したことを示す例外。

    raise されるのは _run_migration() 内のみ。発生時:
    - 当該 migration の transaction は ROLLBACK 済み
    - schema_migrations への INSERT も未実行（version 列に当該 version は無い）
    - これより後ろの pending migration は適用されない
    """


def apply_pending_migrations(
    db_path: Path | str,
    migrations_dir: Path | None = None,
) -> list[str]:
    """未適用の migration を順次適用し、適用した version のリストを返す。

    Args:
        db_path: c3.db の絶対パス。文字列でも Path でも受け付ける。
        migrations_dir: migration SQL を置くディレクトリ。省略時は
            Path(__file__).parent / "migrations"（= src/c3/migrations/）。

    Returns:
        今回新たに適用した version のリスト（適用順）。
        全 migration が既に適用済みなら空リスト。

    Raises:
        MigrationError: いずれかの migration の実行に失敗した場合。
        FileNotFoundError: migrations_dir が存在しない場合。
        sqlite3.Error: db_path が SQLite として開けない等の場合。

    Note:
        本関数を C3 runtime の session_start.py 経由（apply_schema()）で呼んだ場合、
        MigrationError は session_start.py::main() の except Exception で warning 出力に
        変換され exit 0 が維持される（architecture §3.2）。Python から直接呼んだ場合は
        上記 Raises のとおり例外が伝播する。
    """
    if migrations_dir is None:
        migrations_dir = _DEFAULT_MIGRATIONS_DIR

    # migrations_dir 不在は早期 raise（wheel が壊れている兆候）
    if not migrations_dir.exists():
        raise FileNotFoundError(
            f"migrations_dir が存在しません: {migrations_dir}"
        )

    # migration ファイル一覧取得（FileNotFoundError は伝播させる）
    migrations = _list_migrations(migrations_dir)

    conn = sqlite3.connect(str(db_path))
    try:
        # PRAGMA はパラメータバインドできないため int() で型を強制する
        # （PRAGMA インジェクション防御 [SR-INJ-001] の踏襲）
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_MS)}")

        # schema_migrations テーブルを事前に作成する
        # （_get_applied_versions の SELECT が失敗しないよう先行して CREATE）
        _ensure_schema_migrations_table(conn)

        applied_versions = _get_applied_versions(conn)

        newly_applied: list[str] = []
        for version, path in migrations:
            if version in applied_versions:
                continue
            _run_migration(conn, version, path)
            newly_applied.append(version)

        return newly_applied
    finally:
        conn.close()


def _list_migrations(migrations_dir: Path) -> list[tuple[str, Path]]:
    """migrations_dir 内の NNN_xxx.sql ファイルを version 昇順で返す。

    Args:
        migrations_dir: migration SQL を置くディレクトリ。

    Returns:
        [(version, path), ...] を version の文字列昇順にソートしたリスト。
        命名規約に違反するファイルはスキップ（warning ログを出す）。
        version 重複の場合は最初の 1 件のみ採用（warning ログを出す）。

    Raises:
        FileNotFoundError: migrations_dir が存在しない場合。
            apply_pending_migrations() から呼ばれる場合は呼び出し元 (L70) で先に
            exists チェックされるため到達しない。本チェックは _list_migrations を
            単独で利用するケースのための防衛的チェック。
    """
    if not migrations_dir.exists():
        raise FileNotFoundError(
            f"migrations_dir が存在しません: {migrations_dir}"
        )

    result: list[tuple[str, Path]] = []
    seen_versions: dict[str, Path] = {}

    for path in sorted(migrations_dir.glob("*.sql")):
        if path.is_symlink():  # [SR-V-002] symlink は信頼境界外のため無視
            logger.warning("シンボリックリンクをスキップします: %s", path.name)
            continue
        m = _MIGRATION_FILENAME_RE.match(path.name)
        if not m:
            logger.warning(
                "命名規約違反のファイルをスキップします: %s "
                "(期待パターン: NNN_description.sql, 例: 001_initial.sql)",
                path.name,
            )
            continue

        version = m.group(1)
        if version in seen_versions:
            logger.warning(
                "version '%s' が重複しています: %s と %s。最初の 1 件のみ採用します。",
                version,
                seen_versions[version].name,
                path.name,
            )
            continue

        seen_versions[version] = path
        result.append((version, path))

    # version の文字列昇順でソート（= 連番昇順と一致）
    result.sort(key=lambda t: t[0])
    return result


def _ensure_schema_migrations_table(conn: sqlite3.Connection) -> None:
    """schema_migrations テーブルが存在しなければ作成する。

    _get_applied_versions() の SELECT が失敗しないよう、事前に呼び出す。
    テーブルが既に存在する場合は何もしない（IF NOT EXISTS）。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT PRIMARY KEY,
            applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()


def _get_applied_versions(conn: sqlite3.Connection) -> set[str]:
    """schema_migrations テーブルから適用済み version の集合を返す。

    Args:
        conn: SQLite 接続。

    Returns:
        適用済み version の集合。テーブルが存在しない場合は空集合（防御的挙動）。
        通常は apply_pending_migrations() 内で _ensure_schema_migrations_table()
        を先行呼び出しするため、テーブル不在には到達しない。
    """
    try:
        rows = conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
        return {row[0] for row in rows}
    except sqlite3.OperationalError:
        # schema_migrations テーブルが存在しない場合（防御的挙動）
        logger.debug(
            "_get_applied_versions: schema_migrations テーブルが見つかりません。"
            "空集合を返します。"
        )
        return set()


def _run_migration(
    conn: sqlite3.Connection,
    version: str,
    path: Path,
) -> None:
    """1 件の migration SQL ファイルを実行し、schema_migrations に記録する。

    Args:
        conn: SQLite 接続（PRAGMA 設定済みであること）。
        version: migration の version 文字列（例: '001'）。
        path: migration SQL ファイルのパス。

    Raises:
        MigrationError: SQL 実行失敗 / schema_migrations への INSERT 失敗。

    Notes:
        SQL ファイル内に BEGIN; / COMMIT; を明示記述すること（architecture §10.4）。
        executescript() は autocommit モードで動作するため、ファイル内 BEGIN;
        がないとステートメント単位の autocommit になり ROLLBACK が効かない。

        schema_migrations への INSERT は SQL ファイルの executescript() 後に
        Python 側の別 transaction として実行する。SQL ファイル内に
        INSERT OR IGNORE を書いている場合（001_initial.sql の bootstrap）と
        二重になるが OR IGNORE で吸収される。

        注意: executescript() 内の COMMIT; が発行された後に schema_migrations への
        INSERT が失敗した場合、続く except 節の conn.rollback() は SQL 本体には作用しない
        （SQL 本体は既に commit 済み）。この場合 schema_migrations への記録のみ未実行となり、
        次回 session_start で同 version が再適用される。001_initial.sql の IF NOT EXISTS /
        INSERT OR IGNORE による冪等設計で整合性を回復する（architecture §7.1 既知の落とし穴）。
    """
    sql = path.read_text(encoding="utf-8")

    try:
        # executescript() は内部で BEGIN を発行しないため、SQL ファイル内の
        # BEGIN; ... COMMIT; の transaction 境界をそのまま尊重する。
        conn.executescript(sql)

        # schema_migrations への記録（SQL 本体とは別 transaction）
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)",
            (version,),
        )
        conn.commit()

    except sqlite3.Error as e:
        # SQL 本体または INSERT が失敗した場合は ROLLBACK して MigrationError を raise
        try:
            conn.rollback()
        except sqlite3.Error:
            pass  # rollback 自体が失敗しても元の例外を優先する
        raise MigrationError(
            f"migration '{version}' ({path.name}) の適用に失敗しました: {e}"
        ) from e
