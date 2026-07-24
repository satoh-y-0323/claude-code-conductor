"""C3 SQLite write/read helpers.

review-hint: review_decisions の INSERT / SELECT ヘルパー（review_hint_inject.py から利用）。
tier-routing: tier_bandit / tier_recent_outcomes ヘルパー（select_tier.py / record_tier_outcome.py から）。

DB が見つからない場合・書き込みエラー時は静かにスキップし、呼び出し側の本体は
止めない（観測機能の失敗で全体を止めない方針）。

書き込みは Python 標準の `sqlite3` で行う（WAL モード）。
読み・分析は別途 DuckDB の sqlite_scanner で ATTACH する想定（duckdb-hybrid と整合）。

tier-routing の tunable 定数・resolve_* は `_db_params` が SSOT。後方互換のため
本モジュールからも re-export される（既存の `from c3.db import ...` を壊さないため）。

履歴: v1.11.0 までは `src/parallel_orchestra/c3_db.py` に置かれていたが、
PO 廃止計画（plan: atomic-foraging-sprout）の Step 1 で本ファイルに物理移動し、
v2.0.0 で PO 専用ヘルパー（record_task_results / fetch_po_results /
upsert_po_status / fetch_po_status）も同時に削除した。
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

# tier-routing パラメータ（定数 + env 解決）は _db_params.py が SSOT。
# 後方互換のため c3.db からも参照可能にする（cli_tier.py / select_tier.py 等）。
from c3._db_params import (
    AGENT_ROLES as AGENT_ROLES,
    BANDIT_GATES as BANDIT_GATES,
    BANDIT_GATES_BY_ROLE as BANDIT_GATES_BY_ROLE,
    bandit_gates_for_role as bandit_gates_for_role,
    COST_LAMBDA_DEFAULT as COST_LAMBDA_DEFAULT,
    COST_LAMBDA_MAX as COST_LAMBDA_MAX,
    COST_LAMBDA_MIN as COST_LAMBDA_MIN,
    EPSILON_TIEBREAK as EPSILON_TIEBREAK,
    ESCALATION_THRESHOLD_DEFAULT as ESCALATION_THRESHOLD_DEFAULT,
    FAILURE_WINDOW_DAYS_DEFAULT as FAILURE_WINDOW_DAYS_DEFAULT,
    LEARNING_THRESHOLD as LEARNING_THRESHOLD,
    METRICS_DEV_GATES as METRICS_DEV_GATES,
    METRICS_REVIEW_GATES as METRICS_REVIEW_GATES,
    resolve_cost_lambda as resolve_cost_lambda,
    resolve_epsilon as resolve_epsilon,
    resolve_escalation_threshold as resolve_escalation_threshold,
    resolve_failure_window_days as resolve_failure_window_days,
)

logger = logging.getLogger(__name__)


# SQLite ロック衝突待機時間（ms）。並列書き込み増加に備えて 5 秒に設定する。
# 冪等に各書き込み関数で適用される。
# 公開定数として export し、cli_tier.py 等の呼び出し側から参照できるようにする（SSOT）。
BUSY_TIMEOUT_MS = 5000
_BUSY_TIMEOUT_MS = BUSY_TIMEOUT_MS  # 内部互換エイリアス（既存コードへの影響なし）


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
    except sqlite3.OperationalError as exc:
        logger.debug("fetch_review_decisions: table not found or inaccessible: %s", type(exc).__name__)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to fetch review_decisions: %s", type(exc).__name__)
        return []


# insert_review_decision の二層目フェイルセーフ検証用（SR-V-001 item4）。
# 正規（厳格）の検証はアプリ層 record_review_decision.py が担う
# （CHECKLIST_ID_PATTERN=^(CR|SR|DC)-[A-Z]+-\d{3,}$ / _SEVERITY_VOCAB / _truncate）。
# db 層は将来 record_review_decision.py を経由しない直接呼び出しに対する最終防御として
# 「軽量」な検証のみを行う（例外は投げない・呼び出し元の挙動を変えない）:
#   - checklist_id: 接頭辞（CR-/SR-/DC-）を持たない完全に無構造な値のみ False で弾く。
#     アプリ層の厳格な連番形式（-\d{3,}）までは課さない。これはアプリ層の緩い ID
#     （CR-NEW 等）や db 直接呼び出しの既存経路を壊さないための意図的な弱検証である。
#   - severity: 語彙（critical/high/medium/low）外は NULL 化（アプリ層と同じ正規化）。
#   - finding_text/reason/context_summary: 過長時に切り詰め（DB 肥大化防止）。
_CHECKLIST_ID_PREFIX_RE = re.compile(r"^(CR|SR|DC)-")
_REVIEW_SEVERITY_VOCAB = frozenset({"critical", "high", "medium", "low"})
# db 層はフィールド差を設けない単一の緩いバックストップ上限（アプリ層より意図的に緩い）。
# アプリ層 record_review_decision.py は MAX_FINDING_LEN=2000 / MAX_REASON_LEN=2000 /
# MAX_CONTEXT_LEN=1000 とフィールドごとに厳しい上限を設けるが、db 層はそれをバイパスした
# 直接呼び出しに対する最終防御のため、フィールド別ではなく単一の緩い上限で DB 肥大化のみを
# 防ぐ。正規経路ではアプリ層上限が先に効くため、この 4000 文字に直接到達することはない。
_MAX_REVIEW_TEXT_CHARS = 4000
_MAX_REVIEW_TEXT_BYTES = 8 * 1024


def _truncate_review_text(value: str | None) -> str | None:
    """None/空文字はそのまま、超過時は文字数・UTF-8 バイト数の両上限で切り詰めた。

    hooks 側 record_review_decision.py の ``_truncate`` と同型のアルゴリズム
    （文字数超過分を切り詰め → UTF-8 バイト長超過分をさらに 1 文字ずつ削る二段）だが、
    hooks 側は importlib 単体ロード前提の自己完結方針（import 非依存）のため共通化せず
    意図的に重複させている（severity 語彙の相互参照方式と同型）。相違点: 本関数は固定上限・
    無警告、record_review_decision.py の ``_truncate`` は limit/name 引数付き・超過時 stderr 警告。
    """
    # 非 str 型は切り詰めを通さず素通しした（呼び出し 3 箇所の isinstance 三項演算子を
    # ここへ集約。fix-cycle-4 で導入した「非 str → 素通し」の意味論を関数内部で等価保持）。
    if not isinstance(value, str):
        return value
    if not value:
        return value
    if len(value) > _MAX_REVIEW_TEXT_CHARS:
        value = value[:_MAX_REVIEW_TEXT_CHARS]
    while len(value.encode("utf-8")) > _MAX_REVIEW_TEXT_BYTES:
        value = value[: max(1, len(value) - 1)]
    return value


def insert_review_decision(
    *,
    checklist_id: str,
    finding_text: str,
    decision: str,
    reason: str | None = None,
    context_summary: str | None = None,
    reviewer: str,
    severity: str | None = None,
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
        reviewer: 'code-reviewer' | 'security-reviewer' | 'design-critic'。
        severity: 'critical' | 'high' | 'medium' | 'low' | None（severity 未記録）。
            migration 006 で追加された任意列。
        decided_at: 判断日時（UTC 推奨）。省略時は ``datetime.now(timezone.utc)``。
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。

    Returns:
        INSERT 成功時 True、DB 不在 / エラー時 False。

    Note:
        severity 列不在（migration 006 未適用）のレガシー DB では
        ``sqlite3.OperationalError``（"no such column: severity"）を捕捉し、
        severity を除いた旧 7 列 INSERT に 1 回だけリトライして True を返した
        （architecture-report §2-3(1) のレガシーフォールバック）。
    """
    # 二層目フェイルセーフ検証（SR-V-001 item4）。checklist_id が接頭辞すら持たない
    # 無構造値なら DB に蓄積させず False を返す（例外は投げない）。severity の語彙外
    # 正規化・過長文字列の切り詰めもここで防御的に行う。
    # 型ガードを前置し、非 str 型入力（record_review_decision.py を経由しない将来の
    # 直接呼び出しが型契約を破った場合）でも例外を呼び出し元へ伝播させずフェイルセーフに倒す:
    #   - checklist_id 非 str → False（無構造値と同じく DB に載せない）
    #   - severity 非 str → None 化（語彙外と同じく NULL 化）
    #   - finding_text/reason/context_summary 非 str → 切り詰めを通さず素通し
    try:
        if not isinstance(checklist_id, str) or not _CHECKLIST_ID_PREFIX_RE.match(checklist_id):
            logger.debug(
                "insert_review_decision: checklist_id lacks CR-/SR-/DC- prefix, skipped"
            )
            return False
        if severity is not None:
            if not isinstance(severity, str):
                severity = None
            else:
                normalized = severity.strip().lower()
                severity = normalized if normalized in _REVIEW_SEVERITY_VOCAB else None
        finding_text = _truncate_review_text(finding_text)
        reason = _truncate_review_text(reason)
        context_summary = _truncate_review_text(context_summary)
    except Exception as exc:  # noqa: BLE001
        logger.warning("insert_review_decision: type-guard failed: %s", type(exc).__name__)
        return False

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
            try:
                conn.execute(
                    "INSERT INTO review_decisions "
                    "(checklist_id, finding_text, decision, reason, "
                    " context_summary, decided_at, reviewer, severity) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        checklist_id,
                        finding_text,
                        decision,
                        reason,
                        context_summary,
                        decided_iso,
                        reviewer,
                        severity,
                    ),
                )
            except sqlite3.OperationalError as exc:
                exc_msg = str(exc)
                if "no such column" not in exc_msg and "has no column named" not in exc_msg:
                    raise
                # severity 列不在のレガシー DB: 旧 7 列 INSERT に 1 回だけリトライする。
                logger.debug(
                    "insert_review_decision: severity column not found, "
                    "retrying with legacy 7-column INSERT: %s",
                    type(exc).__name__,
                )
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
    except sqlite3.OperationalError as exc:
        logger.debug("insert_review_decision: table not found or inaccessible: %s", type(exc).__name__)
        return False
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
# metrics: 効果総括ヘルパー（P4 効果の総括メトリクス `c3 metrics`・architecture §2-3(2)）
#
# 全て read-only。agent_outcomes / agent_cost_runs へは書き込まない
# （bandit 学習シグナル不干渉の構造的担保・絶対制約・成功条件5）。
#
# 共通規約: db_path=None は locate_c3_db() で解決する。DB 不在・テーブル不在・
# その他エラー時は空値（[] または 0 埋め dict）を返し例外を投げない
# （既存 read ヘルパーの流儀を踏襲）。
#
# since: str | None は "YYYY-MM-DD" 文字列で、比較対象列（decided_at / ts /
# recorded_at。いずれも UTC ISO8601 秒精度で格納・DC-AS-001）に対する
# ">= since" の文字列（辞書順）比較で適用する。列名は表ごとに異なる点に注意:
# 差し戻し由来（review_decisions / agent_outcomes）は decided_at / ts、
# コスト由来（agent_cost_runs）は recorded_at（ts 列は存在しない）。
# ---------------------------------------------------------------------------

# fix-cycle / 手戻りコストの近似注記（人間向け ※ 行・--json 双方へ素通しする
# 単一共有文字列・architecture §2-6 確定版）。内部監査 finding ID・DB 内部名は
# 含めない（DC-AM-001 round 4/5・ADR-006-15）。
_FIX_CYCLE_NOTE = (
    "fix-cycle は session 単位の E-1/E-2 差し戻し件数による近似です。resume に"
    "よるセッション分割・1 セッション内の複数ワークフロー合算により、実際の"
    "差し戻しラウンド数とずれる場合があります。"
)

_REWORK_COST_NOTE = (
    "手戻りコストは session 粒度の近似です。gate/ラウンド単位のコスト帰属は"
    "コスト記録の構造上取得できないため、按分による精緻化は行っていません。"
    "なお `--since` 併用時は、差し戻しありセッションの件数とコスト合算とで"
    "集計の起点となる時刻の基準が異なり、対象期間の母集団が完全一致しない"
    "場合があるため、1 セッションあたり手戻りコストの暗算は目安としてください。"
)


def read_review_decision_matrix(
    db_path: Path | None = None,
    since: str | None = None,
) -> list[dict]:
    """review_decisions を reviewer×severity×decision で集計する。

    severity は ``COALESCE(severity, 'unknown')`` で NULL を "unknown" バケット
    にまとめる。headline の全カウント（fixed_medium_plus・critical/high/medium
    内訳・fixed_unknown）は本ヘルパーの ``(decision='fixed', severity=<各バケット>)``
    から CLI 層で導出する単一算出源であり、専用ヘルパーは設けない
    （two sources of truth を作らない・DC-AM-001）。

    Args:
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。
        since: "YYYY-MM-DD"。指定時 decided_at >= since の行のみ集計する。

    Returns:
        ``[{"reviewer": str, "severity": str, "decision": str, "count": int}, ...]``。
        DB 不在 / エラー / 行なしの場合は空リスト。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return []

    where = ""
    params: tuple = ()
    if since is not None:
        where = "WHERE decided_at >= ? "
        params = (since,)

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            _apply_busy_timeout(conn)
            rows = conn.execute(
                "SELECT reviewer, COALESCE(severity, 'unknown') AS severity, "
                "       decision, COUNT(*) AS count "
                "FROM review_decisions "
                f"{where}"
                "GROUP BY reviewer, COALESCE(severity, 'unknown'), decision",
                params,
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        logger.debug("read_review_decision_matrix: table not found or inaccessible: %s", type(exc).__name__)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to read_review_decision_matrix: %s", type(exc).__name__)
        return []

    return [
        {"reviewer": reviewer, "severity": severity, "decision": decision, "count": count}
        for reviewer, severity, decision, count in rows
    ]


def fetch_prevented_findings(
    db_path: Path | None = None,
    limit: int = 5,
    since: str | None = None,
) -> list[dict]:
    """事前検出できた指摘の実例（examples 表示専用）を直近順で返す。

    ``decision='fixed' AND severity IN ('critical','high','medium')`` のみ
    対象（severity 未記録の fixed 行はここに現れない）。本ヘルパーは実例
    リスト表示専用であり、``limit`` で打ち切られた行数を件数集計（headline）
    に流用してはならない（DC-AM-001・単一算出源は :func:`read_review_decision_matrix`）。

    Args:
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。
        limit: 返す最大件数（デフォルト 5）。
        since: "YYYY-MM-DD"。指定時 decided_at >= since の行のみ対象。

    Returns:
        ``[{"checklist_id", "reviewer", "severity", "finding_text",
        "decided_at", "context_summary"}, ...]``（decided_at DESC）。
        DB 不在 / エラー / 該当なしの場合は空リスト。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return []

    since_clause = ""
    since_params: tuple = ()
    if since is not None:
        since_clause = "AND decided_at >= ? "
        since_params = (since,)

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            _apply_busy_timeout(conn)
            conn.row_factory = sqlite3.Row
            # 相互参照: .claude/skills/dev-workflow/scripts/record_review_decision.py:_SEVERITY_VOCAB /
            # src/c3/cli_metrics.py:_derive_headline の severity リテラル（item2）。
            # 語彙変更時は 3 箇所同期が必要・import 共有は実行コンテキスト分離のため不可。
            rows = conn.execute(
                "SELECT checklist_id, reviewer, severity, finding_text, "
                "       decided_at, context_summary "
                "FROM review_decisions "
                "WHERE decision = 'fixed' AND severity IN ('critical', 'high', 'medium') "
                f"{since_clause}"
                "ORDER BY decided_at DESC LIMIT ?",
                (*since_params, limit),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        logger.debug("fetch_prevented_findings: table not found or inaccessible: %s", type(exc).__name__)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to fetch_prevented_findings: %s", type(exc).__name__)
        return []

    return [dict(r) for r in rows]


def _zero_filled_calendar_months(months: int, since: str | None) -> list[str]:
    """直近 ``months`` 個の暦月（"YYYY-MM"）を昇順（古い→新しい）で返す。

    月の起点は UTC 基準（:func:`datetime.now` の ``timezone.utc``）。``since``
    指定時は、since の暦月（先頭 7 文字）より前の月をリストから間引く
    （since 以降かつ直近 months の積集合・厳しい方が効く・architecture §2-6）。
    """
    from datetime import timezone as _tz  # noqa: PLC0415

    now = datetime.now(_tz.utc)
    year, month = now.year, now.month
    result = []
    for _ in range(months):
        result.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    result.reverse()
    if since is not None:
        since_month = since[:7]
        result = [m for m in result if m >= since_month]
    return result


def read_rework_trend(
    db_path: Path | None = None,
    months: int = 12,
    since: str | None = None,
) -> list[dict]:
    """月次の差し戻し傾向（暦月ゼロ埋め済み）を返す。

    ``success=0 AND gate IN METRICS_REVIEW_GATES`` の行を月次バケットに集計
    する。差し戻し 0 件の月も欠落させず ``rework_count=0`` / ``session_count=0``
    / ``per_session=0.0`` でゼロ埋めする（暦月ゼロ埋めはヘルパー層に一本化・
    DC-AM-002。CLI/JSON 層は本ヘルパーの返り値を素通しし追加ゼロ埋めしない）。

    Args:
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。
        months: 直近何ヶ月分を返すか（デフォルト 12）。
        since: "YYYY-MM-DD"。指定時 ts >= since の行のみ集計し、暦月リストも
            since の月以降に絞る（積集合）。

    Returns:
        ``[{"month": "YYYY-MM", "rework_count": int, "session_count": int,
        "per_session": float}, ...]``（月昇順）。per_session の 0 除算は 0.0。
        DB 不在時は共通規約どおり空リストを返した（ゼロ埋めは「schema はあるが
        行が無い」場合の業務ロジックであり、DB 不在時の共通失敗規約より
        優先されない）。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return []

    month_keys = _zero_filled_calendar_months(months, since)

    gate_placeholders = ",".join("?" * len(METRICS_REVIEW_GATES))  # nul-boundary: allow(SQL の IN プレースホルダ生成。区切りは SQL の文法で固定)
    where = f"WHERE success = 0 AND gate IN ({gate_placeholders}) "
    params: tuple = (*METRICS_REVIEW_GATES,)
    if since is not None:
        where += "AND ts >= ? "
        params = (*params, since)

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            _apply_busy_timeout(conn)
            rows = conn.execute(
                f"SELECT ts, session_id FROM agent_outcomes {where}",
                params,
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        logger.debug("read_rework_trend: table not found or inaccessible: %s", type(exc).__name__)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to read_rework_trend: %s", type(exc).__name__)
        return []

    from collections import defaultdict as _defaultdict  # noqa: PLC0415

    month_rework: dict[str, int] = _defaultdict(int)
    month_sessions: dict[str, set] = _defaultdict(set)
    for ts, session_id in rows:
        month = ts[:7]
        month_rework[month] += 1
        if session_id is not None:
            month_sessions[month].add(session_id)

    result = []
    for month in month_keys:
        rework_count = month_rework.get(month, 0)
        session_count = len(month_sessions.get(month, ()))
        per_session = (rework_count / session_count) if session_count else 0.0
        result.append({
            "month": month,
            "rework_count": rework_count,
            "session_count": session_count,
            "per_session": per_session,
        })
    return result


def read_rework_role_distribution(
    db_path: Path | None = None,
    since: str | None = None,
) -> list[dict]:
    """success=0 の行を role×gate で集計する（分類外 gate を含め全件返却）。

    ``METRICS_REVIEW_GATES`` / ``METRICS_DEV_GATES`` いずれにも属さない gate
    （別 gate のリトライ・``gate=NULL`` 等）も脱落させず全て返す。review /
    development / other への振り分けは CLI 層の責務（DC-AM-003）。

    Args:
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。
        since: "YYYY-MM-DD"。指定時 ts >= since の行のみ集計する。

    Returns:
        ``[{"role": str, "gate": str | None, "count": int}, ...]``。
        DB 不在 / エラー / 行なしの場合は空リスト。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return []

    where = "WHERE success = 0 "
    params: tuple = ()
    if since is not None:
        where += "AND ts >= ? "
        params = (since,)

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            _apply_busy_timeout(conn)
            rows = conn.execute(
                "SELECT role, gate, COUNT(*) AS count FROM agent_outcomes "
                f"{where}"
                "GROUP BY role, gate",
                params,
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        logger.debug("read_rework_role_distribution: table not found or inaccessible: %s", type(exc).__name__)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to read_rework_role_distribution: %s", type(exc).__name__)
        return []

    return [{"role": role, "gate": gate, "count": count} for role, gate, count in rows]


def read_session_fix_cycles(
    db_path: Path | None = None,
    since: str | None = None,
) -> dict:
    """session 単位の fix-cycle（E-1/E-2/C-3 差し戻し件数）分布を返す。

    母集団は ``session_id IS NOT NULL`` の agent_outcomes 全行（success の
    真偽を問わない）から得られる distinct session_id で、``since`` 指定時は
    それも含め ``ts >= since`` で絞る（--since 適用対象 6 経路の 1 つ・
    fix_cycles への適用漏れを作らない・DC-AM-003）。各 session について
    ``success=0 AND gate IN METRICS_REVIEW_GATES`` の件数を差し戻し回数として
    数え、0 回 / 1 回 / 2 回以上に分布化する。

    Args:
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。
        since: "YYYY-MM-DD"。指定時 ts >= since の行のみ集計する。

    Returns:
        ``{"distribution": {"0": int, "1": int, "2plus": int}, "mean": float,
        "max": int, "total_sessions": int, "granularity": "session-approximation",
        "note": str}``。空 DB / DB 不在時は 0 埋め。
    """
    zero_result = {
        "distribution": {"0": 0, "1": 0, "2plus": 0},
        "mean": 0.0,
        "max": 0,
        "total_sessions": 0,
        "granularity": "session-approximation",
        "note": _FIX_CYCLE_NOTE,
    }

    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return zero_result

    where = "WHERE session_id IS NOT NULL "
    params: tuple = ()
    if since is not None:
        where += "AND ts >= ? "
        params = (since,)

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            _apply_busy_timeout(conn)
            rows = conn.execute(
                f"SELECT session_id, success, gate FROM agent_outcomes {where}",
                params,
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        logger.debug("read_session_fix_cycles: table not found or inaccessible: %s", type(exc).__name__)
        return zero_result
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to read_session_fix_cycles: %s", type(exc).__name__)
        return zero_result

    sessions: dict[str, int] = {}
    for session_id, success, gate in rows:
        sessions.setdefault(session_id, 0)
        if not success and gate in METRICS_REVIEW_GATES:
            sessions[session_id] += 1

    total_sessions = len(sessions)
    if total_sessions == 0:
        return zero_result

    distribution = {"0": 0, "1": 0, "2plus": 0}
    for count in sessions.values():
        if count == 0:
            distribution["0"] += 1
        elif count == 1:
            distribution["1"] += 1
        else:
            distribution["2plus"] += 1

    counts = list(sessions.values())
    return {
        "distribution": distribution,
        "mean": sum(counts) / total_sessions,
        "max": max(counts),
        "total_sessions": total_sessions,
        "granularity": "session-approximation",
        "note": _FIX_CYCLE_NOTE,
    }


def read_rework_session_cost(
    db_path: Path | None = None,
    since: str | None = None,
) -> dict:
    """差し戻しありセッションの手戻りコストを session 粒度近似で集計する。

    rework 判定（session 抽出）は ``agent_outcomes.ts >= since`` を基準とする。
    コスト合算（分子・分母とも）は ``agent_cost_runs.recorded_at >= since``
    の同一フィルタで対称化する（分子 cost 行は分母 cost 行の部分集合となり
    ``overall_ratio <= 1.0`` が --since 併用時も常に成立・DC-GP-001）。

    overall（分母）は ``SELECT SUM(total_cost_usd) FROM agent_cost_runs``
    （LIMIT なしの専用集計。:func:`read_agent_cost_summary` の ``limit=50``
    agent_type 別 list 合算には依存しない・DC-GP-002）。突き当て列は
    ``recorded_at``（``agent_cost_runs`` に ``ts`` 列は存在しない・DC-AS-001）。

    ``--since`` 併用時、``rework_session_count``（ts 基準の rework 判定）と
    ``rework_total_usd``（recorded_at 基準のコスト合算）は別クロックのため
    母集団が完全一致しない場合がある。この近似は ``note`` に平易な文言で
    明記する（DC-AM-002 round 3・内部監査 ID / DB 内部名は note に含めない・
    DC-AM-001 round 4/5・ADR-006-15）。

    Args:
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。
        since: "YYYY-MM-DD"。

    Returns:
        ``{"rework_session_count": int, "rework_total_usd": float,
        "rework_total_tokens": int, "overall_total_usd": float,
        "overall_ratio": float, "granularity": "session-approximation",
        "has_cost_rows": bool, "note": str}``。overall_ratio の 0 除算は 0.0。
        has_cost_rows は agent_cost_runs に紐づく行が 1 件でも存在するかを示す。
        空 DB / DB 不在時は 0 埋め。
    """
    zero_result = {
        "rework_session_count": 0,
        "rework_total_usd": 0.0,
        "rework_total_tokens": 0,
        "overall_total_usd": 0.0,
        "overall_ratio": 0.0,
        "granularity": "session-approximation",
        "has_cost_rows": False,
        "note": _REWORK_COST_NOTE,
    }

    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return zero_result

    gate_placeholders = ",".join("?" * len(METRICS_REVIEW_GATES))  # nul-boundary: allow(SQL の IN プレースホルダ生成。区切りは SQL の文法で固定)
    outcome_where = (
        f"WHERE success = 0 AND gate IN ({gate_placeholders}) "
        "AND session_id IS NOT NULL "
    )
    outcome_params: tuple = (*METRICS_REVIEW_GATES,)
    if since is not None:
        outcome_where += "AND ts >= ? "
        outcome_params = (*outcome_params, since)

    cost_where = ""
    cost_params: tuple = ()
    if since is not None:
        cost_where = "WHERE recorded_at >= ? "
        cost_params = (since,)

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            _apply_busy_timeout(conn)
            rework_session_ids = [
                r[0] for r in conn.execute(
                    f"SELECT DISTINCT session_id FROM agent_outcomes {outcome_where}",
                    outcome_params,
                ).fetchall()
            ]

            overall_row = conn.execute(
                f"SELECT SUM(total_cost_usd) FROM agent_cost_runs {cost_where}",
                cost_params,
            ).fetchone()
            overall_total_usd = (
                float(overall_row[0]) if overall_row and overall_row[0] is not None else 0.0
            )

            rework_total_usd = 0.0
            rework_total_tokens = 0
            has_cost_rows = False
            if rework_session_ids:
                # SQLite バインド変数上限に依存しない設計へ変更（item6）。
                # TEMP テーブルへ session_id を INSERT した上で JOIN で絞り込む。
                # connection ローカルの TEMP テーブルは実 DB（c3.db）に永続的な変化を与えないため、
                # 読み取り専用契約と矛盾しない（SQLite のセッション終了・接続クローズ時に自動削除）。
                conn.execute("CREATE TEMP TABLE IF NOT EXISTS _rework_session_ids (session_id TEXT PRIMARY KEY)")
                conn.execute("DELETE FROM _rework_session_ids")
                conn.executemany(
                    "INSERT INTO _rework_session_ids VALUES (?)",
                    [(sid,) for sid in rework_session_ids],
                )

                # has_cost_rows を判定: agent_cost_runs に紐づく行が 1 件でも存在するか
                has_cost_rows_row = conn.execute(
                    "SELECT COUNT(*) > 0 FROM agent_cost_runs acr "
                    "WHERE EXISTS (SELECT 1 FROM _rework_session_ids WHERE session_id = acr.session_id)"
                    + (" AND acr.recorded_at >= ?" if since is not None else ""),
                    (since,) if since is not None else (),
                ).fetchone()
                has_cost_rows = bool(has_cost_rows_row[0]) if has_cost_rows_row else False

                # rework_total_usd / rework_total_tokens を集計
                cost_rework_where = (
                    "WHERE EXISTS (SELECT 1 FROM _rework_session_ids WHERE session_id = acr.session_id) "
                )
                cost_rework_params: tuple = ()
                if since is not None:
                    cost_rework_where += "AND acr.recorded_at >= ? "
                    cost_rework_params = (since,)
                cost_row = conn.execute(
                    "SELECT SUM(total_cost_usd), SUM(input_tokens + output_tokens) "
                    f"FROM agent_cost_runs acr {cost_rework_where}",
                    cost_rework_params,
                ).fetchone()
                if cost_row is not None:
                    rework_total_usd = float(cost_row[0]) if cost_row[0] is not None else 0.0
                    rework_total_tokens = int(cost_row[1]) if cost_row[1] is not None else 0
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        logger.debug("read_rework_session_cost: table not found or inaccessible: %s", type(exc).__name__)
        return zero_result
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to read_rework_session_cost: %s", type(exc).__name__)
        return zero_result

    overall_ratio = (rework_total_usd / overall_total_usd) if overall_total_usd else 0.0

    return {
        "rework_session_count": len(rework_session_ids),
        "rework_total_usd": rework_total_usd,
        "rework_total_tokens": rework_total_tokens,
        "overall_total_usd": overall_total_usd,
        "overall_ratio": overall_ratio,
        "granularity": "session-approximation",
        "has_cost_rows": has_cost_rows,
        "note": _REWORK_COST_NOTE,
    }


# ---------------------------------------------------------------------------
# tier-routing: tier_bandit ヘルパー（Tier 自動ルーティング Thompson Sampling）
# ---------------------------------------------------------------------------

# 学習対象の Tier 一覧（schema.sql のコメントと整合）
_TIER_BANDIT_TIERS: tuple[str, ...] = ("haiku", "sonnet", "opus")


# Phase 2-B 用: tier_recent_outcomes ヘルパー（直近 N 件の outcome 履歴）

# escalation 判定の最小サンプル数。これより少ないと escalation しない（統計的に弱い）。
_FAILURE_RATE_MIN_SAMPLES = 5


# ---------------------------------------------------------------------------
# agent-tier-routing 学習シグナル再設計（v2.41.0 db-foundation）
#
# migration 005（フェーズ2.5）で agent_outcomes 導出集計へ全面移行した
# agent-tier-routing ヘルパー群。旧 agent_tier_bandit テーブルは DROP され、
# 学習シグナルは agent_outcomes からの読み取り時導出に統一。
# ---------------------------------------------------------------------------


def read_agent_tier_params(
    role: str,
    complexity: str,
    *,
    db_path: Path | None = None,
) -> dict[str, tuple[float, float, int]]:
    """指定 role/complexity の各 Tier の (alpha, beta, trials) を返す。

    フェーズ2.5（ADR-25-3）で ``agent_tier_bandit`` 累積テーブル読みから
    ``agent_outcomes`` イベントログの GROUP BY 導出集計へ移行した。
    集計対象 gate は ``bandit_gates_for_role(role)`` で role 別に解決する（ADR-1）:
    既定は ``BANDIT_GATES``（D-2.5 / D-3 / D-5 / D-2.5-stuck）で developer 等の
    「動く実装」を測るが、``tester`` のみ Red 成果物の生存を測る ``("D-1",)`` に
    限定され、tester の D-3/D-5 記録は集計から外れる。いずれの role でも E-1/E-2
    （レビュー指摘由来）は success/failure とも対称に除外される（read-side
    フィルタ 1 点で完結・記録層は全 gate 継続）。シグナル定義（BANDIT_GATES /
    BANDIT_GATES_BY_ROLE）を変えても過去ログへ即遡及再計算される（累積テーブルに
    依存しない）のが導出化の眼目。

    集計規則: ``alpha = 1.0 + Σsuccess`` / ``beta = 1.0 + Σ(1 - success)`` /
    ``trials = COUNT(*)``（Beta(1,1) 事前分布 + 観測）。

    Args:
        role: '_db_params.AGENT_ROLES' のいずれか（例: 'developer'）。
        complexity: 'simple' | 'medium' | 'complex'。
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。

    Returns:
        ``{"haiku": (alpha, beta, trials), "sonnet": ..., "opus": ...}``。
        BANDIT_GATES 該当イベントが無い tier は ``(1.0, 1.0, 0)`` で初期化扱い
        （Beta(1,1)＝一様分布）。DB 不在 / エラー時も全 tier を初期値で返す。
        role が異なれば別セルとして分離される（tester の更新が developer に漏れない）。
        E-gate しか記録されない role（reviewer 系）は恒久的に全 tier uniform になる
        （BANDIT_GATES に該当 gate を持たないため・意図どおり・ADR-25-3 DC-AS-002）。
    """
    defaults: dict[str, tuple[float, float, int]] = {
        t: (1.0, 1.0, 0) for t in _TIER_BANDIT_TIERS
    }

    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return defaults

    # gate 集合は role 別（read-side フィルタ・ADR-1）: tester は D-1 のみ、他 role は
    # 既定 BANDIT_GATES。既定は本モジュールの BANDIT_GATES を second-arg に渡して解決する
    # （bandit_gates_for_role を直接呼ばず inline get する理由）。_db_params 側の helper
    # は _db_params.BANDIT_GATES を見るため、テストが db モジュールの BANDIT_GATES を
    # monkeypatch しても既定集合へ効かせられるよう、ここでは db 側の値を参照する。
    gates = BANDIT_GATES_BY_ROLE.get(role, BANDIT_GATES)
    # gate IN の placeholder は gate 集合長から動的生成する（DC-GP-001）。
    # literal '(?, ?, ?, ?)' 写経は将来 gate 集合の増減でバインド個数エラーを招く。
    gate_placeholders = ",".join("?" * len(gates))  # nul-boundary: allow(SQL の IN プレースホルダ生成。区切りは SQL の文法で固定)

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            _apply_busy_timeout(conn)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT tier, "
                "       SUM(success)     AS succ, "
                "       SUM(1 - success) AS fail, "
                "       COUNT(*)         AS trials "
                "FROM agent_outcomes "
                "WHERE role = ? AND task_complexity = ? "
                f"  AND gate IN ({gate_placeholders}) "
                "GROUP BY tier",
                (role, complexity, *gates),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        logger.debug("read_agent_tier_params: table not found or inaccessible: %s", type(exc).__name__)
        return defaults
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to read agent_tier_params: %s", type(exc).__name__)
        return defaults

    result = dict(defaults)
    for r in rows:
        tier = r["tier"]
        if tier in result:
            succ = int(r["succ"] or 0)
            fail = int(r["fail"] or 0)
            trials = int(r["trials"] or 0)
            result[tier] = (1.0 + succ, 1.0 + fail, trials)
    return result


def record_agent_outcome_event(
    *,
    role: str,
    complexity: str,
    tier: str,
    success: bool,
    gate: str | None = None,
    note: str | None = None,
    session_id: str | None = None,
    db_path: Path | None = None,
) -> bool:
    """``agent_outcomes`` に 1 件 INSERT する（履歴保持イベントログ）。

    agent_tier_bandit の累積 α/β とは別に、role/gate/note を含めた個々の
    outcome イベントを時系列で全件保持する（escalation 判定・cost JOIN・
    将来 OTel 源泉）。

    Args:
        role: '_db_params.AGENT_ROLES' のいずれか。
        complexity: 'simple' | 'medium' | 'complex'。
        tier: 'haiku' | 'sonnet' | 'opus'。
        success: True なら成功、False なら失敗。
        gate: ゲート ID（例: 'D-2.5'）。省略時 NULL。
        note: 帰属理由等の自由記述。省略時 NULL。
        session_id: セッション UUID。省略時 NULL。
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。

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
                "INSERT INTO agent_outcomes "
                "(role, task_complexity, tier, success, gate, note, session_id, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    role, complexity, tier, 1 if success else 0,
                    gate, note, session_id, now_iso,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except sqlite3.OperationalError as exc:
        logger.debug("record_agent_outcome_event: table not found or inaccessible: %s", type(exc).__name__)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to record agent_outcome_event: %s", type(exc).__name__)
        return False


def read_agent_failure_rate(
    role: str,
    complexity: str,
    tier: str,
    *,
    window_days: float | None = None,
    db_path: Path | None = None,
) -> tuple[float | None, int]:
    """時間窓内の agent_outcomes（role 別 bandit gate のみ）から failure rate を計算する。

    フェーズ2.5（ADR-25-2）で直近 ``last_n`` 件窓から時間窓（``window_days``）+
    ``gate IN`` フィルタへ全面移行した。集計対象 gate は
    ``bandit_gates_for_role(role)`` で role 別に解決する（ADR-1・read_agent_tier_params
    と同一方針）: 既定は BANDIT_GATES（D-2.5 / D-3 / D-5 / D-2.5-stuck）、``tester``
    のみ ``("D-1",)``。cutoff より新しく、かつ当該 gate 集合に該当するイベントのみを
    対象とする。E-1/E-2（レビュー指摘由来）の失敗は escalation シグナルを押し上げ
    なくなる（gate 除外を bandit だけでなく escalation にも一貫適用）。

    Args:
        role: '_db_params.AGENT_ROLES' のいずれか。
        complexity: 'simple' / 'medium' / 'complex'。
        tier: 'haiku' / 'sonnet' / 'opus'。
        window_days: 時間窓の日数。``None`` のとき ``resolve_failure_window_days()``
            で解決する（env ``C3_FAILURE_WINDOW_DAYS`` 未設定なら既定 14.0 日）。
        db_path: c3.db のパス。

    Returns:
        ``(failure_rate, sample_count)`` のタプル。

        - ``sample_count`` は窓内かつ role 別 bandit gate 該当の実イベント件数。
        - ``failure_rate`` は失敗件数 / sample_count。
        - サンプルが ``_FAILURE_RATE_MIN_SAMPLES`` 未満の場合は
          ``failure_rate = None`` を返し、escalation 判定を skip する目印にする。
        - DB 不在 / エラー時も ``(None, 0)`` を返す。
    """
    if db_path is None:
        db_path = locate_c3_db()
        if db_path is None:
            return None, 0

    if window_days is None:
        window_days = resolve_failure_window_days()

    # cutoff は record 側書式（UTC 秒精度 ISO 文字列）と一致させ文字列比較する。
    from datetime import timedelta, timezone as _tz  # noqa: PLC0415
    cutoff = (
        datetime.now(_tz.utc) - timedelta(days=window_days)
    ).isoformat(timespec="seconds")

    # gate 集合は role 別（read-side フィルタ・ADR-1）: tester は D-1 のみ、他 role は
    # 既定 BANDIT_GATES。read_agent_tier_params と同一方針で db 側の BANDIT_GATES を
    # second-arg に渡し、monkeypatch 追随を保つ。
    gates = BANDIT_GATES_BY_ROLE.get(role, BANDIT_GATES)
    # gate IN の placeholder は gate 集合長から動的生成する（DC-GP-001）。
    gate_placeholders = ",".join("?" * len(gates))  # nul-boundary: allow(SQL の IN プレースホルダ生成。区切りは SQL の文法で固定)

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            _apply_busy_timeout(conn)
            rows = conn.execute(
                "SELECT success FROM agent_outcomes "
                "WHERE role = ? AND task_complexity = ? AND tier = ? "
                f"  AND gate IN ({gate_placeholders}) "
                "  AND ts >= ? "
                "ORDER BY ts DESC",
                (role, complexity, tier, *gates, cutoff),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        logger.debug("read_agent_failure_rate: table not found or inaccessible: %s", type(exc).__name__)
        return None, 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to read agent_failure_rate: %s", type(exc).__name__)
        return None, 0

    sample_count = len(rows)
    if sample_count < _FAILURE_RATE_MIN_SAMPLES:
        return None, sample_count

    failures = sum(1 for r in rows if r[0] == 0)
    return failures / sample_count, sample_count


def read_recent_agent_outcomes(
    *,
    limit: int = 10,
    role: str | None = None,
    db_path: Path | None = None,
) -> list[dict]:
    """``agent_outcomes`` から直近 ``limit`` 件を時系列降順で返す（cli_tier 用）。

    Args:
        limit: 取得件数の上限（デフォルト 10）。
        role: 指定時は当該 role のみに絞り込む。省略時は全 role 対象。
        db_path: c3.db のパス。省略時は :func:`locate_c3_db` で探索。

    Returns:
        各行を ``{"role", "complexity", "tier", "success", "gate", "note",
        "session_id", "ts"}`` の dict にしたリスト。ts 降順。
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
            if role is None:
                rows = conn.execute(
                    "SELECT role, task_complexity, tier, success, gate, note, "
                    "session_id, ts "
                    "FROM agent_outcomes "
                    "ORDER BY ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT role, task_complexity, tier, success, gate, note, "
                    "session_id, ts "
                    "FROM agent_outcomes "
                    "WHERE role = ? "
                    "ORDER BY ts DESC LIMIT ?",
                    (role, limit),
                ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        logger.debug("read_recent_agent_outcomes: table not found or inaccessible: %s", type(exc).__name__)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("read_recent_agent_outcomes: unexpected error: %s", type(exc).__name__)
        return []

    return [
        {
            "role": r_role,
            "complexity": complexity,
            "tier": tier,
            "success": bool(success),
            "gate": gate,
            "note": note,
            "session_id": session_id,
            "ts": ts,
        }
        for r_role, complexity, tier, success, gate, note, session_id, ts in rows
    ]


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
    except sqlite3.OperationalError as exc:
        logger.debug("insert_agent_cost_run: table not found or inaccessible: %s", type(exc).__name__)
        return False
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
      4. ``agent_outcomes``（v2.41.0 で ``tier_recent_outcomes`` から差替。DC-GP-003）を
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
                "FROM agent_outcomes "
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
    except sqlite3.OperationalError as exc:
        logger.debug("set_ingest_offset: table not found or inaccessible: %s", type(exc).__name__)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to set_ingest_offset: %s", type(exc).__name__)
        return False
