"""C3 SQLite write/read helpers for parallel-orchestra and review hooks.

F-002: PO の結果を `.claude/state/c3.db` の `po_results` テーブルに記録する。
F-001: review_decisions の INSERT / SELECT ヘルパーを追加（review_hint_inject.py から利用）。

DB が見つからない場合・書き込みエラー時は静かにスキップし、呼び出し側の本体は
止めない（観測機能の失敗で全体を止めない方針）。

書き込みは Python 標準の `sqlite3` で行う（WAL モード）。
読み・分析は別途 DuckDB の sqlite_scanner で ATTACH する想定（F-009 と整合）。

注記: パッケージ配置は `src/parallel_orchestra/` だが、F-001 のレビュー hook
からも import される。将来的に `src/c3/c3_db.py` への移動も検討するが、当面は
ここに集約することで `c3.db` 関連ロジックの単一窓口とする。
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


# ---------------------------------------------------------------------------
# F-001: review_decisions ヘルパー
# ---------------------------------------------------------------------------


def fetch_review_decisions(
    checklist_id: str,
    *,
    db_path: Path | None = None,
    limit: int = 3,
    months_window: int = 6,
) -> list[dict]:
    """指定 checklist_id に対する過去判断を直近順で返す。

    Args:
        checklist_id: 'CR-Q-001' / 'SR-K-001' 等の ID。
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。
        limit: 返す最大件数（直近順）。デフォルト 3。
        months_window: 何ヶ月前までを対象にするか。デフォルト 6 ヶ月。
            これより古いレコードは表示時に「[要再評価]」フラグの判定に使う。

    Returns:
        各行を dict にしたリスト。キー:
        ``checklist_id`` / ``finding_text`` / ``decision`` / ``reason`` /
        ``context_summary`` / ``decided_at`` / ``reviewer``。
        DB 不在 / エラー / レコード無しの場合は空リスト。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return []

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT checklist_id, finding_text, decision, reason, "
                "       context_summary, decided_at, reviewer "
                "FROM review_decisions "
                "WHERE checklist_id = ? "
                "ORDER BY decided_at DESC "
                "LIMIT ?",
                (checklist_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to fetch review_decisions: %s", exc)
        return []


def insert_review_decision(
    *,
    checklist_id: str,
    finding_text: str,
    decision: str,
    reason: str | None = None,
    context_summary: str | None = None,
    reviewer: str,
    decided_at: datetime | None = None,
    db_path: Path | None = None,
) -> bool:
    """review_decisions に 1 行 INSERT する。

    Args:
        checklist_id: 'CR-Q-001' 等。
        finding_text: 指摘本文（参照表示用）。
        decision: 'fixed' | 'accepted' | 'deferred'。
        reason: 許容/保留時の理由（``decision='accepted'`` / ``'deferred'`` で必要）。
        context_summary: ファイル名・コミット等の補助情報。
        reviewer: 'code-reviewer' | 'security-reviewer'。
        decided_at: 判断日時（UTC 推奨）。省略時は ``datetime.now(timezone.utc)``。
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。

    Returns:
        INSERT 成功時 True、DB 不在 / エラー時 False。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return False

    if decided_at is None:
        from datetime import timezone as _tz  # noqa: PLC0415
        decided_at = datetime.now(_tz.utc)
    decided_iso = decided_at.isoformat(timespec="seconds")

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "INSERT INTO review_decisions "
                "(checklist_id, finding_text, decision, reason, "
                " context_summary, decided_at, reviewer) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    checklist_id,
                    finding_text,
                    decision,
                    reason,
                    context_summary,
                    decided_iso,
                    reviewer,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to insert review_decision: %s", exc)
        return False


def aggregate_decisions(rows: list[dict]) -> dict:
    """fetch_review_decisions の結果を要約する。

    Returns:
        ``{"total": int, "fixed": int, "accepted": int, "deferred": int}``。
        rows が空でも 0 埋めで返す。
    """
    summary = {"total": len(rows), "fixed": 0, "accepted": 0, "deferred": 0}
    for r in rows:
        d = r.get("decision")
        if d in summary:
            summary[d] += 1  # type: ignore[index]
    return summary
