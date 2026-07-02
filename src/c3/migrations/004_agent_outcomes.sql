-- C3 SQLite migration 004: tier-routing 学習シグナルの role 対応再設計。
--
-- 変更内容:
--   - 旧 tier_bandit / tier_recent_outcomes を DROP（role なしスキーマは維持しない）
--   - agent_tier_bandit（role, task_complexity, tier）PK の bandit テーブルを新設
--   - agent_outcomes（イベントログ。escalation 判定・cost JOIN・将来 OTel 源泉）を新設
--   - agent_outcomes 用 INDEX 2 種（cell 検索用・session 突合用）
--
-- 後方互換: なし（ADR-1: 既存 10 試行はノイズ確定・移行データなしの単純 DROP）。
-- 混在バージョン対応は db.py 側の deprecated シム（ADR-5）で担う。

BEGIN;

DROP TABLE IF EXISTS tier_bandit;
DROP TABLE IF EXISTS tier_recent_outcomes;

CREATE TABLE IF NOT EXISTS agent_tier_bandit (
    role             TEXT NOT NULL,
    task_complexity  TEXT NOT NULL,
    tier             TEXT NOT NULL,
    alpha            REAL NOT NULL DEFAULT 1.0,
    beta             REAL NOT NULL DEFAULT 1.0,
    trials           INTEGER NOT NULL DEFAULT 0,
    last_updated     TEXT,
    PRIMARY KEY (role, task_complexity, tier)
);

CREATE TABLE IF NOT EXISTS agent_outcomes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    role             TEXT NOT NULL,
    task_complexity  TEXT NOT NULL,
    tier             TEXT NOT NULL,
    success          INTEGER NOT NULL,
    gate             TEXT,
    note             TEXT,
    session_id       TEXT,
    ts               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_outcomes_cell
    ON agent_outcomes(role, task_complexity, tier, ts DESC);
CREATE INDEX IF NOT EXISTS idx_agent_outcomes_session
    ON agent_outcomes(session_id);

INSERT OR IGNORE INTO schema_migrations (version) VALUES ('004');

COMMIT;
