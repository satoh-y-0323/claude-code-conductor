-- C3 SQLite migration 007: review_decisions に resolution 3 列を additive 追加。
--
-- 変更内容:
--   - review_decisions.resolution TEXT NULL を追加（NULL|'resolved'|'open'|'unverifiable'）
--   - review_decisions.resolution_note TEXT NULL を追加（判定の根拠テキスト）
--   - review_decisions.resolution_commit TEXT NULL を追加（判定時点の HEAD）
--
-- 後方互換: additive のみ。既存行の 3 列は NULL のまま保持される（= 未判定）。
--   CHECK 制約は設けない（severity/decision/reviewer と同じく検証はアプリ層＝既存規律）。
--   インデックス追加なし（ローカル SQLite・数百行オーダー）。

BEGIN;
ALTER TABLE review_decisions ADD COLUMN resolution TEXT;         -- NULL|'resolved'|'open'|'unverifiable'
ALTER TABLE review_decisions ADD COLUMN resolution_note TEXT;    -- 判定の根拠（何を見て閉じたか）
ALTER TABLE review_decisions ADD COLUMN resolution_commit TEXT;  -- 判定時点の HEAD（40 桁 SHA）
INSERT OR IGNORE INTO schema_migrations (version) VALUES ('007');
COMMIT;
