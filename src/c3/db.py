"""C3 SQLite write/read helpers.

review-hint: review_decisions の INSERT / SELECT ヘルパー（review_hint_inject.py から利用）。
tier-routing: tier_bandit / tier_recent_outcomes ヘルパー（select_tier.py / record_tier_outcome.py から）。

DB が見つからない場合・書き込みエラー時は静かにスキップし、呼び出し側の本体は
止めない（観測機能の失敗で全体を止めない方針）。

書き込みは Python 標準の `sqlite3` で行う（WAL モード）。
読み・分析は別途 DuckDB の sqlite_scanner で ATTACH する想定（duckdb-hybrid と整合）。

履歴: v1.11.0 までは `src/parallel_orchestra/c3_db.py` に置かれていたが、
PO 廃止計画（plan: atomic-foraging-sprout）の Step 1 で本ファイルに物理移動し、
v2.0.0 で PO 専用ヘルパー（record_task_results / fetch_po_results /
upsert_po_status / fetch_po_status）も同時に削除した。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


# SQLite ロック衝突待機時間（ms）。並列書き込み増加に備えて 5 秒に設定する。
# 冪等に各書き込み関数で適用される。
# 公開定数として export し、cli_tier.py 等の呼び出し側から参照できるようにする（SSOT）。
BUSY_TIMEOUT_MS = 5000
_BUSY_TIMEOUT_MS = BUSY_TIMEOUT_MS  # 内部互換エイリアス（既存コードへの影響なし）

# tier-routing: 学習データ収集期の閾値（合計試行数がこの値未満なら uniform 選択）。
# SSOT: cli_tier.py / select_tier.py はここから参照する（CR-M-002）。
LEARNING_THRESHOLD = 30


def _apply_busy_timeout(conn: sqlite3.Connection) -> None:
    # PRAGMA はパラメータバインドできないため値が整数であることを int() で強制する。
    # 現在 _BUSY_TIMEOUT_MS は定数だが、将来 env 等から読まれた場合の PRAGMA
    # インジェクション (`5000; ATTACH ...`) を未然に防ぐ防衛的キャスト [SR-INJ-001]。
    conn.execute(f"PRAGMA busy_timeout={int(_BUSY_TIMEOUT_MS)}")


def locate_c3_db(start: Path | None = None) -> Path | None:
    """`.claude/state/c3.db` を探索する。

    解決順:
      1. 環境変数 ``C3_DB_PATH`` が設定されており有効なファイルを指していれば、それを返す
         （v2.0.0 で導入。worktree 内子プロセスから親リポの DB を直接参照する経路）。
      2. 旧環境変数 ``C3_PO_DB_PATH`` が設定されており有効なファイルを指していれば、それを返す
         （v1.x からの互換維持。次回 major リリースで削除予定）。
      3. 起点ディレクトリから親ディレクトリへ遡って ``.claude/state/c3.db`` を探す。
      4. 見つからなければ ``None``。

    環境変数が設定されているが指すパスが無効な場合は警告ログを出して 3. に fall-through する
    （C3 利用先で `session_start.py` がまだ走っていない、もしくは C3 環境ではない
    ケースの後方互換維持）。

    Args:
        start: 探索の起点。省略時は ``Path.cwd()``。

    Returns:
        c3.db への絶対パス、または見つからなければ ``None``。
    """
    for env_name in ("C3_DB_PATH", "C3_PO_DB_PATH"):
        env_path = os.environ.get(env_name)
        if not env_path:
            continue
        candidate = Path(env_path)
        if candidate.is_file():
            if env_name == "C3_PO_DB_PATH":
                logger.warning(
                    "C3_PO_DB_PATH is deprecated; rename to C3_DB_PATH"
                )
            return candidate.resolve()
        logger.warning(
            "%s set but file not found: %s (falling back to traversal)",
            env_name, env_path,
        )

    cwd = (start or Path.cwd()).resolve()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / ".claude" / "state" / "c3.db"
        if candidate.is_file():
            return candidate.resolve()
    return None


# ---------------------------------------------------------------------------
# review-hint: review_decisions ヘルパー
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
            _apply_busy_timeout(conn)
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
            _apply_busy_timeout(conn)
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
# tier-routing: tier_bandit ヘルパー（Tier 自動ルーティング Thompson Sampling）
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
            _apply_busy_timeout(conn)
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
            _apply_busy_timeout(conn)
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
            _apply_busy_timeout(conn)
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


def read_recent_outcomes(
    *,
    limit: int = 10,
    db_path: Path | None = None,
) -> list[dict]:
    """``tier_recent_outcomes`` から直近 ``limit`` 件を時系列降順で返す。

    ``cli_tier._collect_snapshot`` の sqlite3 直接呼び出しを置き換えるヘルパー。
    busy_timeout は BUSY_TIMEOUT_MS を冪等に適用する。

    Args:
        limit: 取得件数の上限（デフォルト 10）。
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。

    Returns:
        各行を ``{"complexity", "tier", "success", "ts"}`` の dict にしたリスト。
        DB 不在 / テーブル不在 / エラー時は空リストを返す（呼び出し側を止めない）。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return []

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            _apply_busy_timeout(conn)
            rows = conn.execute(
                "SELECT task_complexity, tier, success, ts "
                "FROM tier_recent_outcomes "
                "ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        logger.debug("read_recent_outcomes: table not found or inaccessible: %s", exc)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("read_recent_outcomes: unexpected error: %s", exc)
        return []

    return [
        {"complexity": complexity, "tier": tier, "success": bool(success), "ts": ts}
        for complexity, tier, success, ts in rows
    ]


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
            _apply_busy_timeout(conn)
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


# ---------------------------------------------------------------------------
# usage-ingester: agent_cost_runs / usage_ingest_state ヘルパー（v2.21.0）
# ---------------------------------------------------------------------------


def insert_agent_cost_run(
    *,
    session_id: str,
    agent_id: str,
    agent_type: str,
    description: str | None,
    model: str,
    attribution_skill: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_create_tokens: int,
    total_cost_usd: float,
    db_path: Path | None = None,
) -> bool:
    """agent_cost_runs に 1 行 upsert する。

    PK=(session_id, agent_id, model) で「1 エージェント × 1 モデル = 1 行」。
    同一 PK が既に存在する場合は全数値列・total_cost_usd・recorded_at を最新合算値に上書き。

    Args:
        session_id: セッション UUID。
        agent_id: 'agent-<id>' または 'mainline'。
        agent_type: meta.json の agentType / 'mainline'。
        description: meta.json の description（任意）。
        model: message.model 文字列。
        attribution_skill: assistant レコードの attributionSkill（任意）。
        input_tokens: 入力トークン数（jsonl 内合算値）。
        output_tokens: 出力トークン数（jsonl 内合算値）。
        cache_read_tokens: キャッシュ読み込みトークン数（jsonl 内合算値）。
        cache_create_tokens: キャッシュ書き込みトークン数（jsonl 内合算値）。
        total_cost_usd: compute_cost_usd が返した USD コスト。
        db_path: c3.db のパス。省略時は locate_c3_db() で探索。

    Returns:
        upsert 成功時 True、DB 不在 / sqlite3.Error 時は静かに False。
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
            _apply_busy_timeout(conn)
            conn.execute(
                "INSERT INTO agent_cost_runs "
                "(session_id, agent_id, agent_type, description, model, "
                " attribution_skill, input_tokens, output_tokens, "
                " cache_read_tokens, cache_create_tokens, "
                " total_cost_usd, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, agent_id, model) DO UPDATE SET "
                "  agent_type = excluded.agent_type, "
                "  description = excluded.description, "
                "  attribution_skill = excluded.attribution_skill, "
                "  input_tokens = excluded.input_tokens, "
                "  output_tokens = excluded.output_tokens, "
                "  cache_read_tokens = excluded.cache_read_tokens, "
                "  cache_create_tokens = excluded.cache_create_tokens, "
                "  total_cost_usd = excluded.total_cost_usd, "
                "  recorded_at = excluded.recorded_at",
                (
                    session_id, agent_id, agent_type, description, model,
                    attribution_skill, input_tokens, output_tokens,
                    cache_read_tokens, cache_create_tokens,
                    total_cost_usd, now_iso,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to insert_agent_cost_run: %s", type(exc).__name__)
        return False


def read_agent_cost_summary(
    *,
    db_path: Path | None = None,
    limit: int = 50,
) -> list[dict]:
    """agent_cost_runs を agent_type 別に集計した結果を返す。

    SELECT agent_type, COUNT(*) runs, SUM(total_cost_usd), SUM(input_tokens),
    SUM(output_tokens), SUM(cache_read_tokens), SUM(cache_create_tokens)
    FROM agent_cost_runs GROUP BY agent_type ORDER BY total_cost_usd DESC LIMIT ?

    Args:
        db_path: c3.db のパス。省略時は locate_c3_db() で探索。
        limit: 返す最大件数（デフォルト 50）。

    Returns:
        各行を dict にしたリスト。キー:
        ``agent_type`` / ``runs`` / ``total_cost_usd`` /
        ``input_tokens`` / ``output_tokens`` /
        ``cache_read_tokens`` / ``cache_create_tokens``。
        DB 不在 / テーブル不在 / エラー時は空リスト。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return []

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            # WAL は書き込みヘルパー呼び出し時または migrate 時に設定済みの前提
            # （既存 read_recent_outcomes 等の read ヘルパーと同方針 / CR-M-001）
            _apply_busy_timeout(conn)
            rows = conn.execute(
                "SELECT agent_type, "
                "       COUNT(*) AS runs, "
                "       SUM(total_cost_usd) AS total_cost_usd, "
                "       SUM(input_tokens) AS input_tokens, "
                "       SUM(output_tokens) AS output_tokens, "
                "       SUM(cache_read_tokens) AS cache_read_tokens, "
                "       SUM(cache_create_tokens) AS cache_create_tokens "
                "FROM agent_cost_runs "
                "GROUP BY agent_type "
                "ORDER BY total_cost_usd DESC "
                "LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        # テーブル不在（no such table）は [] を返す（DB 未初期化でも止めない）
        logger.debug(
            "read_agent_cost_summary: table not found or inaccessible: %s",
            type(exc).__name__,
        )
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("read_agent_cost_summary: unexpected error: %s", type(exc).__name__)
        return []

    return [
        {
            "agent_type": row[0],
            "runs": row[1],
            "total_cost_usd": row[2],
            "input_tokens": row[3],
            "output_tokens": row[4],
            "cache_read_tokens": row[5],
            "cache_create_tokens": row[6],
        }
        for row in rows
    ]


def get_ingest_offset(
    file_key: str,
    *,
    db_path: Path | None = None,
) -> int:
    """usage_ingest_state から処理済み行数（offset）を返す。

    Args:
        file_key: '<session>:mainline' / '<session>:agent-<id>'。
        db_path: c3.db のパス。省略時は locate_c3_db() で探索。

    Returns:
        処理済み行数。行・テーブル不在・DB 不在は 0 を返す。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return 0

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            # WAL は書き込みヘルパー呼び出し時または migrate 時に設定済みの前提
            # （既存 read_recent_outcomes 等の read ヘルパーと同方針 / CR-M-001）
            _apply_busy_timeout(conn)
            row = conn.execute(
                "SELECT last_offset FROM usage_ingest_state WHERE file_key = ?",
                (file_key,),
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_ingest_offset: %s", type(exc).__name__)
        return 0

    return row[0] if row is not None else 0


def set_ingest_offset(
    file_key: str,
    offset: int,
    *,
    db_path: Path | None = None,
) -> bool:
    """usage_ingest_state に offset を upsert する。

    Args:
        file_key: '<session>:mainline' / '<session>:agent-<id>'。
        offset: 処理済み行数（= 次回開始行）。
        db_path: c3.db のパス。省略時は locate_c3_db() で探索。

    Returns:
        upsert 成功時 True、DB 不在 / エラー時は静かに False。
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
            _apply_busy_timeout(conn)
            conn.execute(
                "INSERT INTO usage_ingest_state "
                "(file_key, last_offset, last_processed_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(file_key) DO UPDATE SET "
                "  last_offset = excluded.last_offset, "
                "  last_processed_at = excluded.last_processed_at",
                (file_key, offset, now_iso),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to set_ingest_offset: %s", type(exc).__name__)
        return False
