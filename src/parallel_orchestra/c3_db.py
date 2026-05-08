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


# ---------------------------------------------------------------------------
# F-003: po_status ヘルパー（PO 並列処理の状況可視化）
# ---------------------------------------------------------------------------

# po_status.state の許容語彙（schema.sql のコメントと一致）
_PO_STATUS_VALID_STATES: frozenset[str] = frozenset({
    "starting", "running", "completed", "failed",
})


def upsert_po_status(
    *,
    session_id: str,
    worktree_id: str,
    state: str,
    current_step: str | None = None,
    progress_pct: int | None = None,
    db_path: Path | None = None,
) -> bool:
    """``po_status`` テーブルに 1 行 UPSERT する。

    PRIMARY KEY ``(session_id, worktree_id)`` で重複を解消し、
    ``last_heartbeat`` は常に現在時刻（UTC ISO8601）に更新する。

    Args:
        session_id: PO 実行の識別子（``record_task_results`` と同じ ID 形式）。
        worktree_id: worktree のブランチ名。read-only タスクは
            ``"(read-only)"`` のようなプレースホルダで構わない。
        state: ``"starting" | "running" | "completed" | "failed"``。
            未知の値も受け付けるが警告ログのみ出して通過させる。
        current_step: 現在のサブステップ名（``"Wave 2 - tester"`` 等）。
        progress_pct: 進捗率 0-100。不明なら ``None``。
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。

    Returns:
        UPSERT 成功時 True、DB 不在 / エラー時 False。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return False

    if state not in _PO_STATUS_VALID_STATES:
        # 未知 state は警告のみで通過させる（呼び出し側の冪等性を優先）
        logger.warning("po_status: unknown state %r (continuing)", state)

    from datetime import timezone as _tz  # noqa: PLC0415
    now_iso = datetime.now(_tz.utc).isoformat(timespec="seconds")

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # SQLite 3.24+ の UPSERT 構文を使う（Python 3.10 同梱版で利用可能）
            conn.execute(
                "INSERT INTO po_status "
                "(session_id, worktree_id, state, current_step, "
                " progress_pct, last_heartbeat) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, worktree_id) DO UPDATE SET "
                "  state = excluded.state, "
                "  current_step = excluded.current_step, "
                "  progress_pct = excluded.progress_pct, "
                "  last_heartbeat = excluded.last_heartbeat",
                (session_id, worktree_id, state, current_step,
                 progress_pct, now_iso),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to upsert po_status: %s", exc)
        return False


def fetch_po_status(
    *,
    session_id: str | None = None,
    db_path: Path | None = None,
    limit: int = 100,
) -> list[dict]:
    """``po_status`` から行を取得する。

    Args:
        session_id: 指定時はその session のみ。省略時は全 session を対象。
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。
        limit: 返す最大件数（``last_heartbeat`` 降順で先頭から）。

    Returns:
        各行を dict 化したリスト。キー:
        ``session_id`` / ``worktree_id`` / ``state`` / ``current_step`` /
        ``progress_pct`` / ``last_heartbeat``。
        DB 不在 / エラーは空リスト。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return []

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.row_factory = sqlite3.Row
            if session_id is not None:
                rows = conn.execute(
                    "SELECT session_id, worktree_id, state, current_step, "
                    "       progress_pct, last_heartbeat "
                    "FROM po_status "
                    "WHERE session_id = ? "
                    "ORDER BY last_heartbeat DESC "
                    "LIMIT ?",
                    (session_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT session_id, worktree_id, state, current_step, "
                    "       progress_pct, last_heartbeat "
                    "FROM po_status "
                    "ORDER BY last_heartbeat DESC "
                    "LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to fetch po_status: %s", exc)
        return []


# ---------------------------------------------------------------------------
# F-005: tier_bandit ヘルパー（Tier 自動ルーティング Thompson Sampling）
# ---------------------------------------------------------------------------

# 学習対象の Tier 一覧（schema.sql のコメントと整合）
_TIER_BANDIT_TIERS: tuple[str, ...] = ("haiku", "sonnet", "opus")


def read_tier_params(
    complexity: str,
    *,
    db_path: Path | None = None,
) -> dict[str, tuple[float, float, int]]:
    """指定 complexity の各 Tier の (alpha, beta, trials) を返す。

    Args:
        complexity: 'simple' | 'medium' | 'complex'。
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。

    Returns:
        ``{"haiku": (alpha, beta, trials), "sonnet": ..., "opus": ...}``。
        行が無い tier は ``(1.0, 1.0, 0)`` で初期化扱い（Beta(1,1)＝一様分布）。
        DB 不在 / エラー時も全 tier を初期値で返す。
    """
    defaults: dict[str, tuple[float, float, int]] = {
        t: (1.0, 1.0, 0) for t in _TIER_BANDIT_TIERS
    }

    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return defaults

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT tier, alpha, beta, trials "
                "FROM tier_bandit "
                "WHERE task_complexity = ?",
                (complexity,),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to read tier_params: %s", exc)
        return defaults

    result = dict(defaults)
    for r in rows:
        tier = r["tier"]
        if tier in result:
            result[tier] = (float(r["alpha"]), float(r["beta"]), int(r["trials"]))
    return result


def update_tier_params(
    complexity: str,
    tier: str,
    *,
    success: bool,
    db_path: Path | None = None,
) -> bool:
    """tier_bandit の (alpha, beta, trials) を 1 試行分更新する。

    Args:
        complexity: 'simple' | 'medium' | 'complex'。
        tier: 'haiku' | 'sonnet' | 'opus'。
        success: True なら alpha+=1、False なら beta+=1。
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。

    Returns:
        UPDATE / INSERT 成功時 True、DB 不在 / エラー時 False。

    Notes:
        - 行が無ければ INSERT（初期 alpha=1.0, beta=1.0, trials=0 から開始）。
        - last_updated は現在時刻（UTC ISO8601）に更新。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return False

    if tier not in _TIER_BANDIT_TIERS:
        logger.warning("update_tier_params: unknown tier %r (continuing)", tier)

    from datetime import timezone as _tz  # noqa: PLC0415
    now_iso = datetime.now(_tz.utc).isoformat(timespec="seconds")
    alpha_delta = 1.0 if success else 0.0
    beta_delta = 0.0 if success else 1.0

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # SQLite 3.24+ の UPSERT。既存行があれば加算更新、無ければ初期値 + 1 試行分
            conn.execute(
                "INSERT INTO tier_bandit "
                "(task_complexity, tier, alpha, beta, trials, last_updated) "
                "VALUES (?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(task_complexity, tier) DO UPDATE SET "
                "  alpha = alpha + ?, "
                "  beta = beta + ?, "
                "  trials = trials + 1, "
                "  last_updated = excluded.last_updated",
                (
                    complexity, tier,
                    1.0 + alpha_delta, 1.0 + beta_delta, now_iso,
                    alpha_delta, beta_delta,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to update tier_params: %s", exc)
        return False
