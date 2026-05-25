#!/usr/bin/env python3
"""SessionStart hook: 初期化処理を一括実行する.

3 つの責務を順に実行する:
1. file-history のクリア（旧 clear_file_history.py）
2. sandbox 設定の有効化（旧 enable_sandbox.py、worktree ではスキップ）
3. C3 SQLite DB の初期化（旧 init_c3_db.py、duckdb-hybrid 基盤）

各処理は独立しており、1 つが失敗しても他を実行する（exit 0 を返す）。
失敗してもセッションは止めない（C3 の他機能を妨げない方針）。

設計判断:
- 旧 3 ファイルを統合することで SessionStart hook は本ファイル 1 本のみで完結
- init-session SKILL.md からの手動 2 回呼び出しが不要になる
- v2.20.0 で apply_schema() を c3.migrate.apply_pending_migrations() に委譲。
  SCHEMA_VERSION / SCHEMA_PATH 定数および schema.sql は廃止。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile

from session_utils import is_worktree

logger = logging.getLogger(__name__)

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
            # [SR-R-001] shutil.rmtree / os.unlink のエラーメッセージには失敗したパス
            # （FILE_HISTORY_DIR はホームディレクトリ配下のためユーザー名を含む）が
            # 含まれる可能性がある。main() の SR M-1 修正と一貫して例外型名のみ出力する。
            logger.debug('[clear-file-history] delete failed: %s', name, exc_info=True)
            print(f'[clear-file-history] 削除に失敗: {name} ({type(e).__name__})', file=sys.stderr)

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
            # [SR-R-001] 一貫性のため例外型名のみ出力（JSONDecodeError 自体はパスを含まない
            # が、SR M-1 / _run_clear_file_history と方針を統一する）。
            print(f'[enable-sandbox] settings.json の JSON 解析に失敗しました: {type(e).__name__}')
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
            # [CR-E-001] os.fdopen 成功後の書き込み例外では with __exit__ が既に
            # fd を閉じているため、ここでの os.close(fd) は OSError(Bad file
            # descriptor) を raise し元の例外を上書きしてしまう。os.fdopen 自体が
            # 失敗して with に入らなかった場合のみ fd 解放が必要なので OSError を無視する。
            try:
                os.close(fd)
            except OSError:
                pass
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

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
STATE_DIR = os.path.join(_CLAUDE_DIR, 'state')
DB_PATH = os.path.join(STATE_DIR, 'c3.db')


def _ensure_state_dir() -> None:
    """state/ ディレクトリを作成する（既存なら no-op）。"""
    os.makedirs(STATE_DIR, exist_ok=True)


def apply_schema(db_path: str = DB_PATH) -> list[str]:
    """SQLite DB にスキーマ migration を適用する。

    v2.20.0+: 実体は c3.migrate.apply_pending_migrations() に委譲。
    schema.sql は廃止され、src/c3/migrations/ の連番 SQL ファイルで管理される。

    戻り値: 今回新たに適用した migration version のリスト（例: ['001']）
    """
    from c3.migrate import apply_pending_migrations
    try:
        return apply_pending_migrations(db_path)
    except FileNotFoundError:
        # migrations ディレクトリ不在（wheel が壊れている等）でもセッションは続行。
        # [SR-R-001] 例外 e にはインストールパス（ユーザー名含む）が含まれるため
        # stderr には固定文言のみ出力する。詳細は内部 logger（DEBUG）に残す。
        logger.debug('apply_schema: migrations directory not found', exc_info=True)
        print(
            'warning: c3 migrations directory not found (wheel may be corrupted)',
            file=sys.stderr,
        )
        return []


def _run_init_c3_db() -> None:
    """C3 SQLite DB を初期化する."""
    _ensure_state_dir()
    # [CR-M-003] apply_schema() の戻り値（適用した migration version の list[str]）は
    # 現状ログ・利用しない。セッション開始を妨げないことを最優先し、適用結果の通知は
    # 行わない設計（将来 welcome メッセージ等で利用する場合はここで受ける）。
    _ = apply_schema()


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
            # [SR-R-001] 例外メッセージには db_path 等のプロジェクトパスが含まれる
            # 可能性がある（例: sqlite3.OperationalError）。stderr には例外型名のみ
            # 出力し、詳細は内部 logger（DEBUG）に残す。
            # NOTE(SR Info-2): exc_info=True は現状ハンドラ未設定で出力されないが、
            # 将来 logging ハンドラを追加する場合はスタックトレースにインストールパスが
            # 含まれる点に留意する。
            logger.debug('[session_start:%s] handler failed', label, exc_info=True)
            print(f'[session_start:{label}] failed: {type(e).__name__}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
