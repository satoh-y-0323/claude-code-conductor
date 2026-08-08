-- C3 SQLite migration 008: agent_runs テーブルの撤去。
--
-- 変更内容:
--   - agent_runs テーブルを DROP
--   - idx_agent_runs_session / idx_agent_runs_agent インデックスを DROP
--
-- 背景: agent_runs は production の書き手が歴史上存在せず、0 行のまま残された
--   死にテーブルである。したがって DROP によるデータ喪失リスクがない。
--   コスト集計は agent_cost_runs（書き込み元 usage_ingester.py）が担っており、
--   本 migration の影響を受けない。
-- 後方互換: 懸念なし（読み書きとも production 経路が存在しなかった）。

BEGIN;

DROP TABLE IF EXISTS agent_runs;
DROP INDEX IF EXISTS idx_agent_runs_session;
DROP INDEX IF EXISTS idx_agent_runs_agent;

INSERT OR IGNORE INTO schema_migrations (version) VALUES ('008');

COMMIT;
