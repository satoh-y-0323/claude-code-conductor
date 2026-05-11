"""C3 SQLite write/read helpers (formerly parallel_orchestra.c3_db).

F-001: review_decisions の INSERT / SELECT ヘルパー（review_hint_inject.py から利用）。
F-002: PO の結果を `.claude/state/c3.db` の `po_results` テーブルに記録する（PO 廃止予定）。
F-003: po_status の UPSERT / 取得（PO 廃止予定）。
F-005: tier_bandit / tier_recent_outcomes ヘルパー（PO 非依存、廃止後も継続）。

DB が見つからない場合・書き込みエラー時は静かにスキップし、呼び出し側の本体は
止めない（観測機能の失敗で全体を止めない方針）。

書き込みは Python 標準の `sqlite3` で行う（WAL モード）。
読み・分析は別途 DuckDB の sqlite_scanner で ATTACH する想定（F-009 と整合）。

履歴: 2026-05 までは `src/parallel_orchestra/c3_db.py` に置かれていたが、
PO 廃止計画（plan: atomic-foraging-sprout）で本ファイルに物理移動した。
parallel_orchestra.c3_db は当面 shim として `from c3.db import *` を再 export する。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # pragma: no cover - 実行時 import を避ける（循環防止）
    # PO 廃止時（v2.0.0）に削除し、record_task_results / _task_status_str も
    # 同時に廃止する。それまでは絶対 import で型ヒントのみ参照する。
    from parallel_orchestra.runner import TaskResult

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

# F-002 Phase 2-A: SQLite ロック衝突待機時間（ms）。worktree 内子プロセスからの
# 並列書き込み増加に備えて 5 秒に設定する。冪等に各書き込み関数で適用される。
_BUSY_TIMEOUT_MS = 5000

# read-only タスクの worktree_id プレースホルダ。runner.py の env 注入と
# record_task_results / po_status のレコードで一貫した値を使うため定数化する。
READ_ONLY_WORKTREE_ID = "(read-only)"


def locate_c3_db(start: Path | None = None) -> Path | None:
    """`.claude/state/c3.db` を探索する。

    解決順:
      1. 環境変数 ``C3_PO_DB_PATH`` が設定されており有効なファイルを指していれば、それを返す
         （F-002 Phase 2: worktree 内子プロセスから親リポの DB を直接参照する経路）。
      2. 起点ディレクトリから親ディレクトリへ遡って ``.claude/state/c3.db`` を探す。
      3. 見つからなければ ``None``。

    環境変数が設定されているが指すパスが無効な場合は警告ログを出して 2. に fall-through する
    （C3 利用先で `session_start.py` がまだ走っていない、もしくは C3 環境ではない
    ケースの後方互換維持）。

    Args:
        start: 探索の起点。省略時は ``Path.cwd()``。

    Returns:
        c3.db への絶対パス、または見つからなければ ``None``。
    """
    env_path = os.environ.get("C3_PO_DB_PATH")
    if env_path:
        candidate = Path(env_path)
        if candidate.is_file():
            return candidate.resolve()
        logger.warning(
            "C3_PO_DB_PATH set but file not found: %s (falling back to traversal)",
            env_path,
        )

    cwd = (start or Path.cwd()).resolve()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / ".claude" / "state" / "c3.db"
        if candidate.is_file():
            return candidate.resolve()
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
        worktree_id = r.branch_name or READ_ONLY_WORKTREE_ID
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
            # WAL モードに揃える（session_start.py で既に設定済みでも冪等）。
            conn.execute("PRAGMA journal_mode=WAL")
            # F-002 Phase 2-A: 並列書き込み増加に備えロック待機を伸ばす（冪等）。
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
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
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
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
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
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
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            # SQLite 3.24+ の UPSERT 構文を使う（Python 3.10 同梱版で利用可能）。
            # F-002 Phase 2-B: terminal state (completed / failed) は保護する。
            # 親 heartbeat と worktree 内子プロセスの heartbeat 競合で、
            # 子が completed を書いた後に親 heartbeat が running で逆行
            # 上書きする事故、および completed → failed の上書きも阻止する。
            # current_step / progress_pct / last_heartbeat は常に最新値で更新する。
            conn.execute(
                "INSERT INTO po_status "
                "(session_id, worktree_id, state, current_step, "
                " progress_pct, last_heartbeat) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, worktree_id) DO UPDATE SET "
                "  state = CASE "
                "    WHEN po_status.state IN ('completed', 'failed') "
                "      THEN po_status.state "
                "    ELSE excluded.state "
                "  END, "
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
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
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


def fetch_po_results(
    *,
    session_id: str | None = None,
    db_path: Path | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """``po_results`` から行を取得する（F-003 Phase 2: c3 status CLI 用）。

    Args:
        session_id: 指定時はその session のみ。省略時は全 session を対象。
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。
        status: 'success' / 'failure' / 'cancelled' のいずれか。指定時はその status のみ。
        limit: 返す最大件数(``completed_at`` 降順で先頭から)。

    Returns:
        各行を dict 化したリスト。キー:
        ``session_id`` / ``worktree_id`` / ``task_id`` / ``status`` /
        ``started_at`` / ``completed_at`` / ``output_summary`` / ``error_message``。
        DB 不在 / エラーは空リスト。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return []

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            conn.row_factory = sqlite3.Row
            sql = (
                "SELECT session_id, worktree_id, task_id, status, "
                "       started_at, completed_at, output_summary, error_message "
                "FROM po_results"
            )
            params: list = []
            where: list[str] = []
            if session_id is not None:
                where.append("session_id = ?")
                params.append(session_id)
            if status is not None:
                where.append("status = ?")
                params.append(status)
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY completed_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to fetch po_results: %s", exc)
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
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
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
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
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


# Phase 2-B 用: tier_recent_outcomes ヘルパー（直近 N 件の outcome 履歴）

# escalation 判定の最小サンプル数。これより少ないと escalation しない（統計的に弱い）。
_FAILURE_RATE_MIN_SAMPLES = 5


def record_tier_recent_outcome(
    *,
    complexity: str,
    tier: str,
    success: bool,
    db_path: Path | None = None,
) -> bool:
    """``tier_recent_outcomes`` に 1 件 INSERT する。

    Phase 2-B のエスカレーション判定用。tier_bandit の累積 α/β とは別に、
    直近 N 件の event を時系列で保持する。

    Returns:
        INSERT 成功時 True、DB 不在 / エラー時 False。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return False

    from datetime import timezone as _tz  # noqa: PLC0415
    now_iso = datetime.now(_tz.utc).isoformat(timespec="seconds")

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            conn.execute(
                "INSERT INTO tier_recent_outcomes "
                "(task_complexity, tier, success, ts) VALUES (?, ?, ?, ?)",
                (complexity, tier, 1 if success else 0, now_iso),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to record tier_recent_outcome: %s", exc)
        return False


def read_tier_failure_rate(
    complexity: str,
    tier: str,
    *,
    last_n: int = 10,
    db_path: Path | None = None,
) -> tuple[float | None, int]:
    """直近 ``last_n`` 件の outcome から failure rate を計算する。

    Args:
        complexity: 'simple' / 'medium' / 'complex'。
        tier: 'haiku' / 'sonnet' / 'opus'。
        last_n: 何件を対象にするか（デフォルト 10 件）。
        db_path: c3.db のパス。

    Returns:
        ``(failure_rate, sample_count)`` のタプル。

        - ``sample_count`` は実際に取得できた件数（最大 last_n）。
        - ``failure_rate`` は失敗件数 / sample_count。
        - サンプルが ``_FAILURE_RATE_MIN_SAMPLES`` 未満の場合は
          ``failure_rate = None`` を返し、escalation 判定を skip する目印にする。
        - DB 不在 / エラー時も ``(None, 0)`` を返す。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return None, 0

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            rows = conn.execute(
                "SELECT success FROM tier_recent_outcomes "
                "WHERE task_complexity = ? AND tier = ? "
                "ORDER BY ts DESC LIMIT ?",
                (complexity, tier, last_n),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to read tier_failure_rate: %s", exc)
        return None, 0

    sample_count = len(rows)
    if sample_count < _FAILURE_RATE_MIN_SAMPLES:
        return None, sample_count

    failures = sum(1 for r in rows if r[0] == 0)
    return failures / sample_count, sample_count
