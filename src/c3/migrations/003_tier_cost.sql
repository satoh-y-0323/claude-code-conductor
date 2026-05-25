-- C3 SQLite migration 003: tier_recent_outcomes に session_id 列・INDEX を追加し、
--                           tier_bandit にコスト集計用列（v2.23.0 用）を確保する。
--
-- 変更内容:
--   - tier_recent_outcomes.session_id TEXT（既存行は NULL）
--   - idx_tier_recent_session ON tier_recent_outcomes(session_id)
--   - tier_bandit.total_cost_usd REAL NOT NULL DEFAULT 0.0  （v2.23.0 用確保のみ）
--   - tier_bandit.cost_samples   INTEGER NOT NULL DEFAULT 0  （v2.23.0 用確保のみ）
--
-- 後方互換: ADD COLUMN DEFAULT により既存行は NULL / 0.0 / 0 で埋まる。

BEGIN;

ALTER TABLE tier_recent_outcomes ADD COLUMN session_id TEXT;

CREATE INDEX IF NOT EXISTS idx_tier_recent_session
    ON tier_recent_outcomes(session_id);

ALTER TABLE tier_bandit ADD COLUMN total_cost_usd REAL NOT NULL DEFAULT 0.0;

ALTER TABLE tier_bandit ADD COLUMN cost_samples INTEGER NOT NULL DEFAULT 0;

INSERT OR IGNORE INTO schema_migrations (version) VALUES ('003');

COMMIT;
