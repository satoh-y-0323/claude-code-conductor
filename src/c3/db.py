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
import math
import os
import sqlite3
import sys
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
# cost-aware tie-break の拮抗判定閾値。Beta サンプルは 0〜1 スケールで、
# 成功率 5pt（=0.05）以内を拮抗とみなす。本定数が SSOT。
# 過大にすると成功率を犠牲にするリスク、過小にすると無発動になる。
# C3_TIER_EPSILON 環境変数で上書き可（v2.25.0）。
EPSILON_TIEBREAK = 0.05
# cost-weighted Thompson の重み係数 λ の既定値。
# None = v2.25.0 互換モード（ε tie-break を維持し全 tier weighting を発動しない）。
# C3_TIER_COST_LAMBDA 環境変数で上書き可（v2.26.0）。
# λ>0 で全 tier の score=sample-λ*cost_norm weighting が発動、λ=0 明示で cost 無視（純 Thompson）。
# 本定数が SSOT。
COST_LAMBDA_DEFAULT = None
# failure rate がこの値以上で 1 段上位 tier へ escalation する閾値。
# C3_ESCALATION_THRESHOLD 環境変数で上書き可（v2.26.0）。
# 本定数が SSOT（select_tier.py はここから参照）。
ESCALATION_THRESHOLD_DEFAULT = 0.5

# cost-weighted Thompson の λ 有効範囲（v2.27.0: 上限を 1.0→5.0 に拡張）。
# cost を成功率より強く効かせる余地を確保するため上限を 5.0 に設定。
# select_tier.py の _resolve_cost_lambda はここを SSOT として参照する。
COST_LAMBDA_MIN = 0.0
COST_LAMBDA_MAX = 5.0


def resolve_cost_lambda() -> float | None:
    """``C3_TIER_COST_LAMBDA`` を安全に解決する（cli_tier 用 SSOT）。

    不正値（非数値 / 0 未満 / COST_LAMBDA_MAX 超 / NaN）は受け付けず、
    stderr 警告 + デフォルト（COST_LAMBDA_DEFAULT = None）に戻す。
    未設定 / 空文字は無警告でデフォルト（None）を返す。
    妥当域: [COST_LAMBDA_MIN, COST_LAMBDA_MAX]（x=0 許容の閉区間）。
    戻り値が None の場合は v2.25.0 互換の ε tie-break 経路を維持する（センチネル）。
    """
    raw = os.environ.get("C3_TIER_COST_LAMBDA")
    if raw is None or raw == "":
        return COST_LAMBDA_DEFAULT
    try:
        x = float(raw)
    except ValueError:
        print(
            f"[c3:cost_lambda] invalid C3_TIER_COST_LAMBDA={raw!r}, "
            f"using default {COST_LAMBDA_DEFAULT}",
            file=sys.stderr,
        )
        return COST_LAMBDA_DEFAULT
    if math.isnan(x):
        print(
            f"[c3:cost_lambda] C3_TIER_COST_LAMBDA={raw!r} is NaN, "
            f"using default {COST_LAMBDA_DEFAULT}",
            file=sys.stderr,
        )
        return COST_LAMBDA_DEFAULT
    if x < COST_LAMBDA_MIN or x > COST_LAMBDA_MAX:
        print(
            f"[c3:cost_lambda] C3_TIER_COST_LAMBDA={x!r} out of range "
            f"[{COST_LAMBDA_MIN}, {COST_LAMBDA_MAX}], "
            f"using default {COST_LAMBDA_DEFAULT}",
            file=sys.stderr,
        )
        return COST_LAMBDA_DEFAULT
    return x


def resolve_epsilon() -> float:
    """``C3_TIER_EPSILON`` を安全に解決する（cli_tier 用 SSOT）。

    不正値（非数値 / 0 以下 / 1 超 / NaN）は受け付けず、
    stderr 警告 + デフォルト（EPSILON_TIEBREAK）に戻す。
    未設定 / 空文字は無警告でデフォルトを返す。
    妥当域: (0, 1]（x=0 拒否の半開区間）。
    """
    raw = os.environ.get("C3_TIER_EPSILON")
    if raw is None or raw == "":
        return EPSILON_TIEBREAK
    try:
        x = float(raw)
    except ValueError:
        print(
            f"[c3:epsilon] invalid C3_TIER_EPSILON={raw!r}, "
            f"using default {EPSILON_TIEBREAK}",
            file=sys.stderr,
        )
        return EPSILON_TIEBREAK
    if math.isnan(x):
        print(
            f"[c3:epsilon] C3_TIER_EPSILON={raw!r} is NaN, "
            f"using default {EPSILON_TIEBREAK}",
            file=sys.stderr,
        )
        return EPSILON_TIEBREAK
    if x <= 0 or x > 1:
        print(
            f"[c3:epsilon] C3_TIER_EPSILON={x!r} out of range (0, 1], "
            f"using default {EPSILON_TIEBREAK}",
            file=sys.stderr,
        )
        return EPSILON_TIEBREAK
    return x


def resolve_escalation_threshold() -> float:
    """``C3_ESCALATION_THRESHOLD`` を安全に解決する（cli_tier 用 SSOT）。

    不正値（非数値 / 0 以下 / 1 超 / NaN）は受け付けず、
    stderr 警告 + デフォルト（ESCALATION_THRESHOLD_DEFAULT）に戻す。
    未設定 / 空文字は無警告でデフォルトを返す。
    妥当域: (0, 1]（x=0 拒否の半開区間）。
    """
    raw = os.environ.get("C3_ESCALATION_THRESHOLD")
    if raw is None or raw == "":
        return ESCALATION_THRESHOLD_DEFAULT
    try:
        x = float(raw)
    except ValueError:
        print(
            f"[c3:escalation] invalid C3_ESCALATION_THRESHOLD={raw!r}, "
            f"using default {ESCALATION_THRESHOLD_DEFAULT}",
            file=sys.stderr,
        )
        return ESCALATION_THRESHOLD_DEFAULT
    if math.isnan(x):
        print(
            f"[c3:escalation] C3_ESCALATION_THRESHOLD={raw!r} is NaN, "
            f"using default {ESCALATION_THRESHOLD_DEFAULT}",
            file=sys.stderr,
        )
        return ESCALATION_THRESHOLD_DEFAULT
    if x <= 0 or x > 1:
        print(
            f"[c3:escalation] C3_ESCALATION_THRESHOLD={x!r} out of range (0, 1], "
            f"using default {ESCALATION_THRESHOLD_DEFAULT}",
            file=sys.stderr,
        )
        return ESCALATION_THRESHOLD_DEFAULT
    return x


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
        logger.warning("failed to fetch review_decisions: %s", type(exc).__name__)
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
        logger.warning("failed to insert review_decision: %s", type(exc).__name__)
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
        logger.warning("failed to read tier_params: %s", type(exc).__name__)
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
        logger.warning("failed to update tier_params: %s", type(exc).__name__)
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
    session_id: str | None = None,
) -> bool:
    """``tier_recent_outcomes`` に 1 件 INSERT する。

    Phase 2-B のエスカレーション判定用。tier_bandit の累積 α/β とは別に、
    直近 N 件の event を時系列で保持する。

    Args:
        complexity: 'simple' | 'medium' | 'complex'。
        tier: 'haiku' | 'sonnet' | 'opus'。
        success: True なら成功、False なら失敗。
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。
        session_id: セッション UUID（v2.22.0+）。省略時は NULL で保存。
            cost 紐づけに使用。None の場合は既存行との後方互換を維持する。

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
                "(task_complexity, tier, success, ts, session_id) VALUES (?, ?, ?, ?, ?)",
                (complexity, tier, 1 if success else 0, now_iso, session_id),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to record tier_recent_outcome: %s", type(exc).__name__)
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
        logger.debug("read_recent_outcomes: table not found or inaccessible: %s", type(exc).__name__)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("read_recent_outcomes: unexpected error: %s", type(exc).__name__)
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
        logger.warning("failed to read tier_failure_rate: %s", type(exc).__name__)
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


def read_tier_cost_summary(
    *,
    db_path: Path | None = None,
) -> list[dict]:
    """tier_recent_outcomes × agent_cost_runs を session_id で JOIN し、
    complexity×tier 別コストを返す（粗い概算 / 精度向上は v2.23.0）。

    2 段 CTE で重複計上を防ぐ:
      - ``session_cost`` CTE: agent_cost_runs を mainline 除外しつつ
        session_id 単位で SUM（1 session = 1 行）→ cost の重複計上を構造的に防ぐ
      - ``outcome_sessions`` CTE: tier_recent_outcomes を
        (session_id, task_complexity, tier) で DISTINCT 化。
        session_id が NULL（v2.22.0 移行前行）は除外
      - JOIN して complexity×tier 別に sessions / total_cost_usd / avg_cost_usd を算出

    既知の不正確性（v2.22.0 許容・精度向上は v2.23.0）:
      - session が複数の (complexity, tier) outcome を持つ場合、同一 session の cost が
        複数の (complexity,tier) 行に重複加算されうる（1 session 内での cost 重複は防ぐが、
        複数 outcome を持つ session の cross-outcome 重複は未解決）
      - cost は session 全体の non-mainline 合計であり、特定 tier の model 行に限定しない
      - agent_id 単位の紐づけはしていない

    Args:
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。

    Returns:
        各行を dict にしたリスト。キー:
        ``complexity``(str) / ``tier``(str) / ``sessions``(int) /
        ``total_cost_usd``(float) / ``avg_cost_usd``(float)。
        ORDER BY total_cost_usd DESC。
        テーブル不在 / データ不在 / session_id 全 NULL / JOIN 0 行 /
        DB 不在 / エラー時は空リストを返す。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return []

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            # WAL は書き込みヘルパー呼び出し時または migrate 時に設定済みの前提
            # （read_agent_cost_summary 等の read ヘルパーと同方針 / CR-M-001）
            _apply_busy_timeout(conn)
            rows = conn.execute(
                "WITH session_cost AS ("
                "    SELECT session_id,"
                "           SUM(total_cost_usd) AS session_cost_usd"
                "    FROM agent_cost_runs"
                "    WHERE agent_type <> 'mainline'"
                "    GROUP BY session_id"
                "),"
                "outcome_sessions AS ("
                "    SELECT DISTINCT session_id, task_complexity, tier"
                "    FROM tier_recent_outcomes"
                "    WHERE session_id IS NOT NULL"
                ") "
                "SELECT o.task_complexity            AS complexity,"
                "       o.tier                       AS tier,"
                "       COUNT(DISTINCT o.session_id) AS sessions,"
                "       SUM(sc.session_cost_usd)     AS total_cost_usd,"
                "       SUM(sc.session_cost_usd) / COUNT(DISTINCT o.session_id)"
                "           AS avg_cost_usd "
                "FROM outcome_sessions o "
                "JOIN session_cost sc ON sc.session_id = o.session_id "
                "GROUP BY o.task_complexity, o.tier "
                "ORDER BY total_cost_usd DESC"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        # テーブル不在（no such table）は [] を返す（DB 未初期化でも止めない）
        logger.debug(
            "read_tier_cost_summary: table not found or inaccessible: %s",
            type(exc).__name__,
        )
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("read_tier_cost_summary: unexpected error: %s", type(exc).__name__)
        return []

    return [
        {
            "complexity": row[0],
            "tier": row[1],
            "sessions": int(row[2]),
            "total_cost_usd": float(row[3]) if row[3] is not None else 0.0,
            "avg_cost_usd": float(row[4]) if row[4] is not None else 0.0,
        }
        for row in rows
    ]


def read_tier_cost_for_complexity(
    complexity: str,
    *,
    db_path: Path | None = None,
) -> dict[str, float]:
    """complexity 別の tier 平均コストを {tier: avg_cost_usd} で返す。

    tie-break のハイブリッド cost 源（実測 avg_cost）。
    complexity 一致 & avg_cost_usd > 0 のみ。
    ``read_tier_cost_summary`` の薄いラッパー。

    DB アクセスは ``read_tier_cost_summary`` に委譲するため、
    DB 例外処理・busy_timeout・read 規約は同関数から継承される。
    データ/DB 不在で ``read_tier_cost_summary`` が ``[]`` を返す場合、
    本関数は ``{}`` を返す。

    Args:
        complexity: フィルタ対象の complexity 値（"simple" / "medium" / "complex"）。
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索
                 （``read_tier_cost_summary`` に委譲）。

    Returns:
        ``{tier: avg_cost_usd}`` の dict。
        該当データ不在・DB 不在・エラー時は ``{}``。
    """
    rows = read_tier_cost_summary(db_path=db_path)
    return {
        r["tier"]: r["avg_cost_usd"]
        for r in rows
        if r["complexity"] == complexity and r["avg_cost_usd"] > 0
    }


# ---------------------------------------------------------------------------
# v2.24.0: model 一致集計・USD/MTok レート化（精度向上版）
# ---------------------------------------------------------------------------


def _compute_tier_cost_rate_summary(
    cost_rows: list[tuple],
    outcome_rows: list[tuple],
) -> list[dict]:
    """agent_cost_runs 行リストと tier_recent_outcomes 行リストから
    (complexity, tier) 別 USD/MTok レートを集計して返す（DB 非依存の純関数）。

    DB に依存しないため、単体テストで任意のデータを直接渡せる。

    集計ステップ:
      1. cost_rows の各行を pricing.resolve_tier(model) で tier に振り分け。
         resolve_tier が None の未知モデル行はスキップ。
      2. (session_id, tier) 粒度で cost_sum と billable_tokens を集約。
         PK=(session_id, agent_id, model) のため行は元々一意 → 重複排除は構造的に成立。
      3. outcome_rows の各行 (session_id, task_complexity, tier) を
         (session_id, tier) バケットと突合し (complexity, tier) 別に集約。
      4. rate 化: rate_usd_per_mtok = total_cost_usd / (billable_tokens / 1_000_000)。
         billable_tokens == 0 の (complexity, tier) は除外。

    分母の定義:
        billable_tokens = input_tokens + output_tokens のみ（cache tokens 除外）。
        tier_reference_cost が input+output 単価和である次元に揃えるため。

    分子の注記:
        total_cost_usd は cache コストも含むが、cache 単価は input の約 1/10 と小さく
        tier 間順位（min-max が見る対象）の単調性は保たれる。
        厳密な cache 分離は過剰最適化のためスコープ外（v2.24.0 許容）。

    cross-outcome 重複の残存許容:
        1 session が同一 tier で複数の complexity outcome を持つレアケースでは、
        その session のコストが複数 (complexity, tier) に重複加算される。
        session_id JOIN では帰属先 complexity が原理的に不明なため按分は恣意的であり
        精度を逆に損なう。残存許容・docstring 明記（按分は将来検討）。

    Args:
        cost_rows: agent_cost_runs の行タプル。
            各要素: (session_id, model, total_cost_usd, input_tokens, output_tokens)。
        outcome_rows: tier_recent_outcomes の行タプル。
            各要素: (session_id, task_complexity, tier)。
            DISTINCT 化・session_id NOT NULL フィルタ済み想定。

    Returns:
        dict リスト。キー: complexity / tier / sessions / total_cost_usd /
        billable_tokens / rate_usd_per_mtok。
        billable_tokens == 0 の (complexity, tier) は除外。
        返却順は ``rate_usd_per_mtok`` 降順（既存 ``read_tier_cost_summary`` の
        ``ORDER BY total_cost_usd DESC`` と対称）。
    """
    from c3 import pricing  # noqa: PLC0415

    # Step 1-2: (session_id, tier) 粒度でコスト集約
    # bucket: {(session_id, tier): {"cost_sum": float, "billable": int}}
    bucket: dict[tuple[str, str], dict] = {}
    for row in cost_rows:
        sess_id, model, total_cost, input_tok, output_tok = row
        tier = pricing.resolve_tier(model)
        if tier is None:
            continue  # 未知モデルはスキップ（read 規約: ログなし）
        key = (sess_id, tier)
        if key not in bucket:
            bucket[key] = {"cost_sum": 0.0, "billable": 0}
        bucket[key]["cost_sum"] += float(total_cost) if total_cost is not None else 0.0
        bucket[key]["billable"] += (
            (int(input_tok) if input_tok else 0)
            + (int(output_tok) if output_tok else 0)
        )

    # Step 3: outcome を (complexity, tier) 別に集約
    # agg: {(complexity, tier): {"sessions": int, "cost_sum": float, "billable": int}}
    agg: dict[tuple[str, str], dict] = {}
    for row in outcome_rows:
        sess_id, complexity, tier = row
        key_bucket = (sess_id, tier)
        if key_bucket not in bucket:
            continue  # cost データがない session は集計対象外
        agg_key = (complexity, tier)
        if agg_key not in agg:
            agg[agg_key] = {"sessions": 0, "cost_sum": 0.0, "billable": 0}
        agg[agg_key]["sessions"] += 1
        agg[agg_key]["cost_sum"] += bucket[key_bucket]["cost_sum"]
        agg[agg_key]["billable"] += bucket[key_bucket]["billable"]

    # Step 4: rate 化・billable == 0 を除外
    result = []
    for (complexity, tier), vals in agg.items():
        billable = vals["billable"]
        if billable == 0:
            continue
        rate = vals["cost_sum"] / (billable / 1_000_000)
        result.append(
            {
                "complexity": complexity,
                "tier": tier,
                "sessions": vals["sessions"],
                "total_cost_usd": vals["cost_sum"],
                "billable_tokens": billable,
                "rate_usd_per_mtok": rate,
            }
        )
    result.sort(key=lambda r: r["rate_usd_per_mtok"], reverse=True)
    return result


def read_tier_cost_rate_summary(
    *,
    db_path: Path | None = None,
) -> list[dict]:
    """model 一致集計で (complexity, tier) 別 USD/MTok レートを返す（精度向上版）。

    v2.23.0 までの ``read_tier_cost_summary`` は session 全体の非 mainline コストを
    合算しており、outcome の tier の model に限定していなかった（H5 テストが確認）。
    本関数はこの不正確性を解消し、``pricing.resolve_tier(model)`` による model 一致
    集計と USD/MTok レート化（``tier_reference_cost`` と同次元）を行う。

    集計アルゴリズム（内部実装は :func:`_compute_tier_cost_rate_summary` 参照）:
      1. ``agent_cost_runs`` を ``agent_type <> 'mainline'`` で素読み。
      2. Python 側で各行を ``pricing.resolve_tier(model)`` で tier に振り分け。
         ``resolve_tier`` が ``None`` の未知モデル行はスキップ。
      3. ``(session_id, tier)`` 粒度で ``cost_sum`` と
         ``billable_tokens = input_tokens + output_tokens`` を集約。
         PK=(session_id, agent_id, model) のため行は元々一意 → 重複排除は構造的に成立。
      4. ``tier_recent_outcomes`` を
         ``DISTINCT (session_id, task_complexity, tier)``（``session_id IS NOT NULL``）で読み、
         ``(session_id, tier)`` バケットと突合して ``(complexity, tier)`` 別に集約。
      5. rate 化: ``rate_usd_per_mtok = total_cost_usd / (billable_tokens / 1_000_000)``。
         ``billable_tokens == 0`` の ``(complexity, tier)`` は除外。

    分母の定義:
        ``billable_tokens = input_tokens + output_tokens`` のみ（cache tokens 除外）。
        ``tier_reference_cost`` が input+output 単価和である次元に揃えるため。

    分子の注記:
        ``total_cost_usd`` は cache コストも含むが、cache 単価は input の約 1/10 と小さく
        tier 間順位（min-max が見る対象）の単調性は保たれる（v2.24.0 許容）。

    cross-outcome 重複の残存許容:
        1 session が同一 tier で複数の complexity outcome を持つレアケースでは、
        その session のコストが複数 ``(complexity, tier)`` に重複加算される。
        session_id JOIN では帰属先 complexity が原理的に不明なため按分は恣意的であり
        精度を逆に損なう。残存許容・docstring 明記（按分は将来検討）。

    Args:
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。

    Returns:
        各行を dict にしたリスト。キー:
        ``complexity``(str) / ``tier``(str) / ``sessions``(int) /
        ``total_cost_usd``(float) / ``billable_tokens``(int) /
        ``rate_usd_per_mtok``(float)。
        ``billable_tokens == 0`` の行は除外。
        テーブル不在 / データ不在 / DB 不在 / エラー時は空リストを返す。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return []

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            # WAL は書き込みヘルパー呼び出し時または migrate 時に設定済みの前提
            # （read_agent_cost_summary 等の read ヘルパーと同方針 / CR-M-001）
            _apply_busy_timeout(conn)
            cost_rows = conn.execute(
                "SELECT session_id, model, total_cost_usd, "
                "       input_tokens, output_tokens "
                "FROM agent_cost_runs "
                "WHERE agent_type <> 'mainline'"
            ).fetchall()
            outcome_rows = conn.execute(
                "SELECT DISTINCT session_id, task_complexity, tier "
                "FROM tier_recent_outcomes "
                "WHERE session_id IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        # テーブル不在（no such table）は [] を返す（DB 未初期化でも止めない）
        logger.debug(
            "read_tier_cost_rate_summary: table not found or inaccessible: %s",
            type(exc).__name__,
        )
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "read_tier_cost_rate_summary: unexpected error: %s", type(exc).__name__
        )
        return []

    return _compute_tier_cost_rate_summary(cost_rows, outcome_rows)


def read_tier_cost_rate_for_complexity(
    complexity: str,
    *,
    db_path: Path | None = None,
) -> dict[str, float]:
    """complexity 別の tier USD/MTok レートを {tier: rate_usd_per_mtok} で返す（精度向上版）。

    tie-break のハイブリッド cost 源（model 一致集計・rate 化）。
    complexity 一致 & rate_usd_per_mtok > 0 のみ。
    ``read_tier_cost_rate_summary`` の薄いラッパー。

    v2.23.0 の ``read_tier_cost_for_complexity``（session 合計 avg_cost_usd）と対称な構造を持ち、
    cost_map の値を「絶対 USD」から「USD/MTok レート」へ精度向上させた代替関数。
    これにより fallback の ``tier_reference_cost``（静的 per-MTok 単価）と同次元になり、
    tie-break の min-max 正規化が同スケールで比較可能になる。

    DB アクセスは ``read_tier_cost_rate_summary`` に委譲するため、
    DB 例外処理・busy_timeout・read 規約は同関数から継承される。
    データ/DB 不在で ``read_tier_cost_rate_summary`` が ``[]`` を返す場合、
    本関数は ``{}`` を返す。

    Args:
        complexity: フィルタ対象の complexity 値（"simple" / "medium" / "complex"）。
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索
                 （``read_tier_cost_rate_summary`` に委譲）。

    Returns:
        ``{tier: rate_usd_per_mtok}`` の dict。
        該当データ不在・DB 不在・エラー時は ``{}``。
    """
    rows = read_tier_cost_rate_summary(db_path=db_path)
    return {
        r["tier"]: r["rate_usd_per_mtok"]
        for r in rows
        if r["complexity"] == complexity and r["rate_usd_per_mtok"] > 0
    }


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


def read_tier_bandit_cost(
    *,
    db_path: Path | None = None,
) -> dict[tuple[str, str], tuple[float, int]]:
    """tier_bandit テーブルから cost 列を読む。

    cli_tier.py の _collect_snapshot が alpha/beta/trials（read_tier_params 由来）と
    別 SELECT で cost を取得するために使う。

    Args:
        db_path: c3.db のパス。省略時は locate_c3_db() で探索。

    Returns:
        ``{(task_complexity, tier): (total_cost_usd, cost_samples)}`` の dict。
        テーブル不在 / DB 不在 / エラー時は空 dict を返す。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return {}

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            _apply_busy_timeout(conn)
            rows = conn.execute(
                "SELECT task_complexity, tier, total_cost_usd, cost_samples "
                "FROM tier_bandit"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to read tier_bandit_cost: %s", type(exc).__name__)
        return {}

    return {
        (row[0], row[1]): (float(row[2]), int(row[3]))
        for row in rows
    }


def sync_tier_bandit_cost(
    *,
    db_path: Path | None = None,
) -> int:
    """tier_bandit テーブルの cost 列を rate_summary 集計値で同期する（冪等 SET 同期）。

    冪等性: 全行の cost 列を 0.0/0 にクリアしてから SET するため、
    何度呼び出しても結果が同じになる（全クリア→SET の SET 同期）。

    動作:
      1. ``read_tier_cost_rate_summary`` で (complexity, tier) 別集計を取得。
      2. 1 トランザクション内で:
         a. 全行 cost 列クリア: ``UPDATE tier_bandit SET total_cost_usd=0.0, cost_samples=0``
            （alpha/beta/trials/last_updated は一切変更しない）。
         b. 各集計行を ``UPDATE tier_bandit SET total_cost_usd=?, cost_samples=?
            WHERE task_complexity=? AND tier=?`` で SET。
            ``cost_samples`` は DISTINCT session 数（``read_tier_cost_rate_summary``
            の ``sessions`` フィールド）を格納する。
         c. ``commit()``。クリア後 SET 前に別プロセスが読む瞬間を作らないため
            途中で commit しない（R1 冪等性保証）。
      3. UPDATE-only: tier_bandit に行が存在しない (complexity, tier) は INSERT しない。
         alpha/beta のない半端な bandit 行の生成を防ぐ（R4 回避）。

    制限:
        USD/MTok レートの再現には billable_tokens が必要であり、本テーブル単独では
        再現不可。レートが必要な箇所は ``read_tier_cost_rate_summary`` /
        ``read_tier_cost_rate_for_complexity`` を使うこと。

    Args:
        db_path: c3.db のパス。省略時は locate_c3_db() で探索。
            None（locate_c3_db が None を返す）の場合は 0 を返す。

    Returns:
        cost を SET できた行数（rowcount > 0 の UPDATE 件数合計）。
        DB 不在 / エラー時は 0 を返す（例外を投げない）。
        例外発生時は ``finally: conn.close()`` 経由で rollback され、
        cost 列がクリアされた状態で残る可能性がある。
        戻り値 0 を受け取った呼び出し元は次回 session_stop での再試行に委ねる。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return 0

    rows = read_tier_cost_rate_summary(db_path=db_path)

    set_count = 0
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            _apply_busy_timeout(conn)
            # Step 1: 全行 cost 列クリア（alpha/beta/trials/last_updated は触らない）
            conn.execute("UPDATE tier_bandit SET total_cost_usd = 0.0, cost_samples = 0")
            # Step 2: 各集計行を SET（UPDATE-only: tier_bandit に行が無い場合は rowcount=0）
            for row in rows:
                cur = conn.execute(
                    "UPDATE tier_bandit "
                    "SET total_cost_usd = ?, cost_samples = ? "
                    "WHERE task_complexity = ? AND tier = ?",
                    (row["total_cost_usd"], row["sessions"], row["complexity"], row["tier"]),
                )
                set_count += cur.rowcount
            # Step 3: 全 SET が完了してから一括 commit（途中 commit しない）
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to sync_tier_bandit_cost: %s", type(exc).__name__)
        return 0

    return set_count
