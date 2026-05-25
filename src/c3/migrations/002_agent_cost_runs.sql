-- C3 SQLite migration 002: agent_cost_runs / usage_ingest_state テーブル追加
--
-- セッションログ（~/.claude/projects/<slug>/<session>.jsonl + subagents/）から
-- モデル別トークン消費を USD 換算して蓄積するコスト収集基盤テーブルを定義する。
--
-- PK=(session_id, agent_id, model) で「1 エージェント × 1 モデル = 1 行」。
-- jsonl 内の複数 requestId は ingester（usage_ingester.py）が合算 upsert する。
--
-- usage_ingest_state は jsonl ごとの処理済み行数 offset を管理する。
-- file_key = '<session>:mainline' / '<session>:agent-<id>'

BEGIN;

CREATE TABLE IF NOT EXISTS agent_cost_runs (
  session_id          TEXT NOT NULL,
  agent_id            TEXT NOT NULL,              -- 'agent-<id>' / mainline は 'mainline'
  agent_type          TEXT NOT NULL,              -- meta.json agentType / 'mainline'
  description         TEXT,
  model               TEXT NOT NULL,              -- message.model
  attribution_skill   TEXT,
  input_tokens        INTEGER NOT NULL DEFAULT 0,
  output_tokens       INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
  cache_create_tokens INTEGER NOT NULL DEFAULT 0,
  total_cost_usd      REAL NOT NULL,
  recorded_at         TEXT NOT NULL,              -- ISO8601 UTC
  PRIMARY KEY (session_id, agent_id, model)
);

CREATE INDEX IF NOT EXISTS idx_agent_cost_runs_agent_type
  ON agent_cost_runs(agent_type, recorded_at);

CREATE TABLE IF NOT EXISTS usage_ingest_state (
  file_key            TEXT PRIMARY KEY,           -- '<session>:mainline' / '<session>:agent-<id>'
  last_offset         INTEGER NOT NULL,           -- 処理済み行数
  last_processed_at   TEXT NOT NULL               -- ISO8601 UTC
);

INSERT OR IGNORE INTO schema_migrations (version) VALUES ('002');

COMMIT;
