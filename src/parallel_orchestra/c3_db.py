"""C3 SQLite write helpers for parallel-orchestra.

F-002: PO の結果を `.claude/state/c3.db` の `po_results` テーブルに記録する。
DB が見つからない場合・書き込みエラー時は静かにスキップし、runner.py 本体は
止めない（PO の主目的は記録ではないため、観測機能の失敗で全体を止めない方針）。

書き込みは Python 標準の `sqlite3` で行う（WAL モード）。
読み・分析は別途 DuckDB の sqlite_scanner で ATTACH する想定（F-009 と整合）。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # pragma: no cover - 実行時 import を避ける（循環防止）
    from .runner import TaskResult

logger = logging.getLogger(__name__)


# TaskResult のステータス文字列を `po_results.status` の語彙に変換するマッピング。
# schema.sql の status 制約: 'success' | 'failure' | 'cancelled'
_STATUS_MAPPING: dict[str, str] = {
    "succeeded": "success",
    "failed": "failure",
    "skipped": "cancelled",
}

# output_summary / error_message に保存する最大文字数。
# 巨大ログは agent-runs.jsonl 等の別管理に任せ、ここではサマリのみ保持する。
_MAX_TEXT_LEN = 500


def locate_c3_db(start: Path | None = None) -> Path | None:
    """`.claude/state/c3.db` を探索する。

    起点ディレクトリから親ディレクトリへ遡って候補を探す。
    見つからなければ ``None`` を返す（C3 利用先で `init_c3_db.py` がまだ
    走っていない、もしくは C3 環境ではないケースを想定）。

    Args:
        start: 探索の起点。省略時は ``Path.cwd()``。

    Returns:
        c3.db への絶対パス、または見つからなければ ``None``。
    """
    cwd = (start or Path.cwd()).resolve()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / ".claude" / "state" / "c3.db"
        if candidate.is_file():
            return candidate
    return None


def _task_status_str(result: TaskResult) -> str:
    """TaskResult から内部ステータス文字列を返す（report.py と整合）。"""
    if result.skipped:
        return "skipped"
    if result.ok:
        return "succeeded"
    return "failed"


def _truncate(text: str | None, limit: int = _MAX_TEXT_LEN) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def record_task_results(
    task_results: Iterable[TaskResult],
    *,
    session_id: str,
    started_at: datetime,
    finished_at: datetime,
    db_path: Path | None = None,
) -> int:
    """``task_results`` を ``po_results`` テーブルに INSERT する。

    Args:
        task_results: TaskResult の iterable。
        session_id: PO 実行の識別子（manifest 名 + 実行開始時刻 等）。
        started_at: PO 実行全体の開始時刻（タスク個別の値ではない）。
        finished_at: PO 実行全体の終了時刻。
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索する。

    Returns:
        INSERT を試みた行数。DB 不在 / エラー時は 0。
        実際の INSERT 件数（``rowcount``）ではなく、対象行数を返す
        （UNIQUE 制約による重複スキップは内部処理として吸収する）。

    Notes:
        - 失敗（DB 不在 / I/O エラー / SQL エラー）は警告ログのみで例外を出さない。
          PO の主目的を阻害しないため。
        - ``UNIQUE(session_id, worktree_id, task_id)`` 制約により、同じ
          session_id で再実行しても重複行は作られない。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            logger.debug("c3.db not found; skipping po_results recording")
            return 0

    started_iso = started_at.isoformat(timespec="seconds")
    finished_iso = finished_at.isoformat(timespec="seconds")

    rows: list[tuple] = []
    for r in task_results:
        status = _STATUS_MAPPING.get(_task_status_str(r), "failure")
        worktree_id = r.branch_name or "(read-only)"
        output_summary = _truncate(r.stdout) if not r.skipped else ""
        error_message = _truncate(r.stderr) if not r.ok else ""
        rows.append((
            session_id,
            worktree_id,
            r.task_id,
            status,
            started_iso,
            finished_iso,
            output_summary,
            error_message,
        ))

    if not rows:
        return 0

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            # WAL モードに揃える（init_c3_db.py で既に設定済みでも冪等）。
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executemany(
                "INSERT OR IGNORE INTO po_results "
                "(session_id, worktree_id, task_id, status, "
                " started_at, completed_at, output_summary, error_message) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()
        return len(rows)
    except Exception as exc:  # noqa: BLE001 - 観測機能なので広く捕捉して PO 本体を止めない
        logger.warning("failed to record po_results: %s", exc)
        return 0
