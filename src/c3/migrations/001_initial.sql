-- C3 SQLite migration 001: 初期スキーマ定義
--
-- .claude/hooks/schema.sql (v2.19.0 まで使用) の DDL を逐語移植し、
-- schema_migrations テーブル (v2.20.0 新規) を追加する。
-- 旧 schema_version テーブルは末尾で DROP する。
--
-- 書き込みは Python の sqlite3 経由、読み・分析は DuckDB の sqlite_scanner で
-- ATTACH してアクセスする想定（書き込みフローは sqlite3 に統一する）。

BEGIN;

-- ---------------------------------------------------------------------------
-- v2.0.0 マイグレーション: PO（Parallel Orchestra）廃止に伴うテーブル削除
-- ---------------------------------------------------------------------------
-- v1.x で作成された po_results / po_status テーブルは v2.0.0 で不要になった。
-- 利用先 DB から削除する。テーブル不在でもエラーにしない。
DROP TABLE IF EXISTS po_results;
DROP TABLE IF EXISTS po_status;

-- ---------------------------------------------------------------------------
-- スキーマバージョン管理 (v2.20.0 新規)
-- ---------------------------------------------------------------------------
-- 旧 schema_version (version INTEGER PK) の代わりに、適用済み migration の
-- 一覧を保持する schema_migrations テーブルを定義する。
-- version は NNN_xxx.sql のファイル名先頭 3 桁の文字列（例: '001'）。

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
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

-- ---------------------------------------------------------------------------
-- bootstrap: この migration 自体を schema_migrations に記録する
-- ---------------------------------------------------------------------------
-- apply_pending_migrations() の Python 側でも INSERT OR IGNORE を実行するが、
-- 既存 DB（schema_version あり）からの初回 upgrade 時に SQL 内で先行記録しておく。
-- Python 側の INSERT と二重になるが OR IGNORE で吸収される。
INSERT OR IGNORE INTO schema_migrations (version) VALUES ('001');

-- ---------------------------------------------------------------------------
-- 旧 schema_version テーブルの削除 (v2.19.0 以前の DB からの upgrade)
-- ---------------------------------------------------------------------------
-- v2.19.0 以前の c3.db には schema_version (version INTEGER PK) が存在する。
-- v2.20.0 では schema_migrations に一本化するため DROP する。
DROP TABLE IF EXISTS schema_version;

COMMIT;
