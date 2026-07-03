-- C3 SQLite migration 005: bandit のイベントログ導出化に伴い agent_tier_bandit を撤去。
--
-- 変更内容:
--   - agent_tier_bandit（累積 α/β/trials テーブル）を DROP
--
-- 背景: read_agent_tier_params は agent_outcomes からの GROUP BY 集計へ移行した
--   （フェーズ2.5・ADR-25-3/25-4）。累積テーブルと導出集計の二重の真実源は
--   乖離しうるため累積テーブルを撤去する。イベントログ（agent_outcomes）は
--   全 gate で不変に保持され、シグナル定義変更が過去ログへ遡及再計算される。
-- 後方互換: なし（update_agent_tier_params 削除と同一 breaking・累積値は破棄）。

BEGIN;

DROP TABLE IF EXISTS agent_tier_bandit;

INSERT OR IGNORE INTO schema_migrations (version) VALUES ('005');

COMMIT;
