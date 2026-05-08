#!/usr/bin/env python3
"""SessionStart hook: initialize C3 SQLite database.

F-009: DuckDB ハイブリッド構成の基盤。
書き込みは Python 標準 sqlite3 経由（WAL モード）、読み・分析は DuckDB の
sqlite_scanner 拡張で ATTACH してアクセスする想定。

このスクリプトは SessionStart で 1 回呼ばれ、`.claude/state/c3.db` に対して:
- DB ファイルを作成（存在しなければ）
- WAL モードを有効化
- schema.sql に定義された全テーブル / インデックスを CREATE TABLE IF NOT EXISTS

冪等動作: 既存 DB に対して何度実行しても安全。
失敗してもセッションは止めない（exit 0）— C3 の他機能を停止させない方針。

スキーマバージョン: 1
スキーマ変更時は schema.sql の更新と本ファイルの SCHEMA_VERSION の bump、
必要なら手書きマイグレーションを追加すること。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone

try:
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass


# 現行スキーマバージョン。schema.sql に破壊的変更を入れたら +1 して
# マイグレーションロジックを apply_schema() に追加する。
SCHEMA_VERSION = 1

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
STATE_DIR = os.path.join(_CLAUDE_DIR, 'state')
DB_PATH = os.path.join(STATE_DIR, 'c3.db')
SCHEMA_PATH = os.path.join(_HOOKS_DIR, 'schema.sql')


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def _ensure_state_dir() -> None:
    """state/ ディレクトリを作成する（既存なら no-op）。"""
    os.makedirs(STATE_DIR, exist_ok=True)


def _read_schema() -> str:
    """schema.sql の内容を読み込む。"""
    with open(SCHEMA_PATH, 'r', encoding='utf-8') as f:
        return f.read()


def apply_schema(db_path: str = DB_PATH, schema_path: str = SCHEMA_PATH) -> None:
    """schema_path の DDL を db_path の SQLite に適用する。

    - WAL モードに切り替える
    - schema.sql の CREATE TABLE IF NOT EXISTS 等を実行
    - schema_version テーブルに現行バージョンを INSERT OR IGNORE

    冪等: 既存 DB に何度呼んでもエラーにならない。
    """
    with open(schema_path, 'r', encoding='utf-8') as f:
        ddl = f.read()

    conn = sqlite3.connect(db_path)
    try:
        # WAL モードを有効化（reader が writer をブロックしない）
        conn.execute('PRAGMA journal_mode=WAL')
        # 全 DDL を一括実行（CREATE TABLE IF NOT EXISTS なので冪等）
        conn.executescript(ddl)
        # スキーマバージョンを記録（既存なら無視）
        conn.execute(
            'INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)',
            (SCHEMA_VERSION, _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    """SessionStart で呼ばれるエントリポイント。

    失敗してもセッションを止めない（stderr に警告を出すだけで exit 0）。
    C3 の他機能の妨げにならないようにする。
    """
    try:
        _ensure_state_dir()
        apply_schema()
    except Exception as e:
        # 失敗しても他機能を止めないため、警告のみ
        print(f'[C3 init_c3_db] DB 初期化に失敗しました: {e}', file=sys.stderr)
        return 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
