#!/usr/bin/env python3
"""SessionStart hook: 初期化処理を一括実行する.

3 つの責務を順に実行する:
1. file-history のクリア（旧 clear_file_history.py）
2. sandbox 設定の有効化（旧 enable_sandbox.py、worktree ではスキップ）
3. C3 SQLite DB の初期化（旧 init_c3_db.py、F-009 基盤）

各処理は独立しており、1 つが失敗しても他を実行する（exit 0 を返す）。
失敗してもセッションは止めない（C3 の他機能を妨げない方針）。

設計判断:
- 旧 3 ファイルを統合することで SessionStart hook は本ファイル 1 本のみで完結
- init-session SKILL.md からの手動 2 回呼び出しが不要になる
- 旧 init_c3_db.py の `apply_schema()` / `SCHEMA_VERSION` は test 互換性のため
  module レベルでそのまま公開する
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

from session_utils import is_worktree

try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


# =============================================================================
# 1. file-history クリア（旧 clear_file_history.py）
# =============================================================================

FILE_HISTORY_DIR = os.path.join(os.path.expanduser('~'), '.claude', 'file-history')


def _run_clear_file_history() -> None:
    """`~/.claude/file-history/` を全削除する."""
    if not os.path.isdir(FILE_HISTORY_DIR):
        print('[clear-file-history] file-history フォルダが存在しません。スキップします。')
        return

    entries = os.listdir(FILE_HISTORY_DIR)
    deleted = 0

    for name in entries:
        full_path = os.path.join(FILE_HISTORY_DIR, name)
        try:
            if os.path.islink(full_path):
                # TOCTOU 対策: リンク先が FILE_HISTORY_DIR 配下に解決されることを確認してから削除する
                real = os.path.realpath(full_path)
                if not real.startswith(os.path.realpath(FILE_HISTORY_DIR)):
                    print(f'[clear-file-history] シンボリックリンクのリンク先が FILE_HISTORY_DIR 外のためスキップ: {name}',
                          file=sys.stderr)
                    continue
                os.unlink(full_path)
            elif os.path.isdir(full_path):
                shutil.rmtree(full_path)
            else:
                os.unlink(full_path)
            deleted += 1
        except FileNotFoundError:
            pass  # already deleted by another process between listdir and unlink/rmtree
        except Exception as e:
            print(f'[clear-file-history] 削除に失敗: {name} ({e})', file=sys.stderr)

    print(f'[clear-file-history] {deleted} 件削除しました。')


# =============================================================================
# 2. sandbox 設定有効化（旧 enable_sandbox.py）
# =============================================================================

FULL_SANDBOX_CONFIG = {
    "enabled": True,
    "autoAllowBashIfSandboxed": True,
    "allowUnsandboxedCommands": False,
    "excludedCommands": [],
    "network": {
        "allowUnixSockets": [],
        "allowAllUnixSockets": False,
        "allowLocalBinding": False,
        "allowedDomains": []
    },
    "enableWeakerNestedSandbox": True
}


def _run_enable_sandbox() -> None:
    """`.claude/settings.json` の sandbox 設定を有効化する。worktree ではスキップ."""
    cwd = os.getcwd()

    # git worktree 内では実行しない（settings.json は worktree 専用ではない）
    if is_worktree(cwd):
        print('[enable-sandbox] git worktree 内での実行のためスキップします。')
        return

    settings_path = os.path.join(cwd, '.claude', 'settings.json')
    if not os.path.exists(settings_path):
        print(f'[enable-sandbox] settings.json が見つかりません: {settings_path}')
        return

    with open(settings_path, 'r', encoding='utf-8') as f:
        try:
            settings = json.load(f)
        except json.JSONDecodeError as e:
            print(f'[enable-sandbox] settings.json の JSON 解析に失敗しました: {e}')
            return

    if settings.get('sandbox', {}).get('enabled') is True:
        print('[enable-sandbox] sandbox はすでに有効です。')
        return

    settings['sandbox'] = FULL_SANDBOX_CONFIG

    # アトミック書き込み: 一時ファイルに書き込んでから os.replace() で置換する
    tmp_path = None
    try:
        settings_dir = os.path.dirname(settings_path)
        fd, tmp_path = tempfile.mkstemp(dir=settings_dir, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as tmp_f:
                json.dump(settings, tmp_f, ensure_ascii=False, indent=2)
                tmp_f.write('\n')
        except Exception:
            os.close(fd)
            raise
        os.replace(tmp_path, settings_path)
        tmp_path = None  # os.replace が成功したので finally でのクリーンアップ不要
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    print('[enable-sandbox] sandbox を有効化しました。Claude Code 再起動後に反映されます。')


# =============================================================================
# 3. C3 SQLite DB 初期化（旧 init_c3_db.py）
# =============================================================================

# 現行スキーマバージョン。schema.sql に破壊的変更を入れたら +1 して
# マイグレーションロジックを apply_schema() に追加する。
SCHEMA_VERSION = 2  # F-005 Phase 2-B で tier_recent_outcomes を追加

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


def apply_schema(db_path: str = DB_PATH, schema_path: str = SCHEMA_PATH) -> None:
    """schema_path の DDL を db_path の SQLite に適用する。

    - WAL モードに切り替える
    - schema.sql の CREATE TABLE IF NOT EXISTS 等を実行
    - schema_version テーブルに現行バージョンを INSERT OR IGNORE

    冪等: 既存 DB に何度呼んでもエラーにならない。
    """
    with open(schema_path, 'r', encoding='utf-8') as f:
        ddl = f.read()

    conn = sqlite3.connect(db_path, timeout=30)
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


def _run_init_c3_db() -> None:
    """C3 SQLite DB を初期化する."""
    _ensure_state_dir()
    apply_schema()


# =============================================================================
# Orchestrator
# =============================================================================


def main() -> int:
    """SessionStart hook エントリポイント.

    各処理を独立して実行する。1 つが失敗しても他は実行される。
    失敗してもセッションは止めない（exit 0）。
    """
    handlers = (
        ('clear-file-history', _run_clear_file_history),
        ('enable-sandbox', _run_enable_sandbox),
        ('init-c3-db', _run_init_c3_db),
    )
    for label, handler in handlers:
        try:
            handler()
        except Exception as e:
            # 各ハンドラ失敗時は警告のみ（次のハンドラを継続）
            print(f'[session_start:{label}] failed: {e}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
