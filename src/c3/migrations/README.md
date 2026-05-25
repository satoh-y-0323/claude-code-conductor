# C3 SQLite Migrations

このディレクトリには C3 の SQLite スキーマを管理する migration ファイルを置く。
v2.20.0 で導入。旧 `.claude/hooks/schema.sql` の「冪等 DDL 一発実行」から
連番 migration runner への移行基盤。

## 命名規約

```
NNN_description.sql
```

| 要素 | 形式 | 例 |
|---|---|---|
| NNN | 3 桁ゼロパディング連番 | `001`, `002`, `010` |
| description | ASCII 小文字・数字・アンダースコアのみ | `initial`, `add_tier_cost` |
| 拡張子 | `.sql` (小文字) | `.sql` |

**正しい例**: `001_initial.sql`, `002_add_tier_cost.sql`, `010_rename_column.sql`

**誤った例**: `1_init.sql`（桁数不足）, `001_AddTierCost.sql`（大文字含む）,
`001_initial.txt`（拡張子違反）

命名規約に違反するファイルは `apply_pending_migrations()` に warning を出して
スキップされる。

## ファイルの書き方

各 migration ファイルは **必ず** `BEGIN;` で始まり `COMMIT;` で終わること。

```sql
BEGIN;

-- ここに DDL / DML を書く
CREATE TABLE IF NOT EXISTS my_table (...);

COMMIT;
```

### 重要: BEGIN; / COMMIT; が必須な理由

Python の `sqlite3.executescript()` は autocommit モードで動作する。
`BEGIN;` を明示しないと各 SQL ステートメントが個別に commit されるため、
途中で失敗しても ROLLBACK が効かない。

`BEGIN;` を明示することで、ファイル内の全 SQL が 1 transaction としてまとまり、
失敗時に確実に ROLLBACK される。

## 失敗時の挙動

1. `executescript()` が失敗すると `conn.rollback()` が呼ばれる
2. 当該 migration の transaction は ROLLBACK される
3. `MigrationError` が raise される
4. `schema_migrations` テーブルに当該 version の行は記録されない
5. これより後ろの pending migration は適用されない

**per-migration commit**: 1 ファイル = 1 transaction。
002 が失敗しても、001 の適用記録は `schema_migrations` に残る。

## 文字エンコーディング

UTF-8 / BOM なしで保存すること。`_run_migration()` は `path.read_text(encoding="utf-8")`
で読み込むため、BOM 付きファイルは SQL 先頭に U+FEFF が混入し SQLite の構文エラーに
なる可能性がある（warning ではなくエラー）。

## 002 以降を追加する手順

1. `src/c3/migrations/` に `NNN_description.sql` を新規作成する
2. ファイルの先頭に `BEGIN;`、末尾に `COMMIT;` を必ず書く
3. `src/c3/migrations/` への変更は wheel に自動同梱される
   （`__init__.py` が置かれているため Hatchling のデフォルト packages 検出に乗る）
4. 次回 Claude Code セッション開始時に `session_start.py` 経由で自動適用される

### 例: 002_add_tier_cost.sql

```sql
BEGIN;

ALTER TABLE tier_bandit ADD COLUMN cost_avg REAL;

COMMIT;
```

## 適用済み migration の確認

`schema_migrations` テーブルを参照する:

```sql
SELECT version, applied_at FROM schema_migrations ORDER BY version;
```
