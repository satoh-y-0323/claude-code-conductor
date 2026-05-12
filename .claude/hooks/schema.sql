-- C3 SQLite schema (duckdb-hybrid: DuckDB ハイブリッド構成の基盤)
--
-- このファイルは session_start.py の _run_init_c3_db ハンドラから読まれ、
-- `.claude/state/c3.db` に対して冪等に CREATE TABLE IF NOT EXISTS で適用される。
-- WAL モードへの切り替えは session_start.py 側で PRAGMA journal_mode=WAL を実行する。
--
-- 書き込みは Python の sqlite3 経由、読み・分析は DuckDB の sqlite_scanner で
-- ATTACH してアクセスする想定（書き込みフローは sqlite3 に統一する）。
--
-- スキーマ変更時は schema_version を上げて、session_start.py の apply_schema()
-- にマイグレーション処理を追加すること（CREATE TABLE IF NOT EXISTS だけで
-- 表現できない変更が必要になった場合の備え）。

-- ---------------------------------------------------------------------------
-- v2.0.0 マイグレーション: PO（Parallel Orchestra）廃止に伴うテーブル削除
-- ---------------------------------------------------------------------------
-- v1.x で作成された po_results / po_status テーブルは v2.0.0 で不要になった。
-- 利用先 DB から削除する。テーブル不在でもエラーにしない。
DROP TABLE IF EXISTS po_results;
DROP TABLE IF EXISTS po_status;

-- ---------------------------------------------------------------------------
-- スキーマバージョン管理
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- review-hint: レビュー判断ヒント機能
-- ---------------------------------------------------------------------------
-- code-reviewer / security-reviewer の指摘に対して、人間が下した判断
-- （対応 / 許容 / 保留）と理由を蓄積する。次回以降のレビュー時に過去判断を
-- ヒントとしてレポートに追記するために使う。

CREATE TABLE IF NOT EXISTS review_decisions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    checklist_id     TEXT NOT NULL,        -- 例: 'CR-Q-001' / 'SR-A-002'
    finding_text     TEXT NOT NULL,        -- 指摘内容（参考表示用）
    decision         TEXT NOT NULL,        -- 'fixed' | 'accepted' | 'deferred'
    reason           TEXT,                 -- 許容・保留時の理由
    context_summary  TEXT,                 -- ファイル名・コミット等
    decided_at       TEXT NOT NULL,        -- ISO8601
    reviewer         TEXT NOT NULL         -- 'code-reviewer' | 'security-reviewer'
);
CREATE INDEX IF NOT EXISTS idx_review_decisions_checklist
    ON review_decisions(checklist_id, decided_at DESC);

-- ---------------------------------------------------------------------------
-- tier-routing: Tier 自動ルーティング（Thompson Sampling 学習データ）
-- ---------------------------------------------------------------------------
-- タスク複雑度ごとに各 Tier の Beta(α, β) 事前分布を保持する。
-- α / β は完了ごとに更新され、サンプリングで次の Tier を選ぶ。

CREATE TABLE IF NOT EXISTS tier_bandit (
    task_complexity  TEXT NOT NULL,        -- 'simple' | 'medium' | 'complex'
    tier             TEXT NOT NULL,        -- 'haiku' | 'sonnet' | 'opus'
    alpha            REAL NOT NULL DEFAULT 1.0,
    beta             REAL NOT NULL DEFAULT 1.0,
    trials           INTEGER NOT NULL DEFAULT 0,
    last_updated     TEXT,                 -- ISO8601
    PRIMARY KEY (task_complexity, tier)
);

-- tier-routing Phase 2-B: 直近 N 件の outcome を保持して failure rate を計算する。
-- tier_bandit が「累積 α/β」の集約を持つのに対し、こちらは個別 event の履歴。
-- select_tier.py が直近 5 件以上で failure rate ≥ 0.5 を検出したら 1 段昇格する。
CREATE TABLE IF NOT EXISTS tier_recent_outcomes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_complexity  TEXT NOT NULL,
    tier             TEXT NOT NULL,
    success          INTEGER NOT NULL,     -- 0=failure / 1=success
    ts               TEXT NOT NULL         -- ISO8601
);
CREATE INDEX IF NOT EXISTS idx_tier_recent
    ON tier_recent_outcomes(task_complexity, tier, ts DESC);

-- ---------------------------------------------------------------------------
-- subagent-metrics: SubagentStop メトリクス（既存 JSONL と並行運用）
-- ---------------------------------------------------------------------------
-- subagent_log.py が JSONL に追記している記録を SQLite にも保存する。
-- tier-routing の学習データ収集の前提。
-- 既存 .claude/logs/agent-runs.jsonl は移行までの間並行運用する。

CREATE TABLE IF NOT EXISTS agent_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id         TEXT,
    agent_id           TEXT,
    agent_type         TEXT,
    event              TEXT NOT NULL,      -- 'start' | 'stop'
    ts                 TEXT NOT NULL,      -- ISO8601
    duration_seconds   REAL,               -- stop 時のみ
    total_tokens       INTEGER,
    status             TEXT,
    model              TEXT,
    payload_json       TEXT                -- 元 payload を JSON 文字列で保持（拡張用）
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_session
    ON agent_runs(session_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent
    ON agent_runs(agent_id, ts DESC);
