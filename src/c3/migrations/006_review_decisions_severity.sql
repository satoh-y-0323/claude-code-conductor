-- C3 SQLite migration 006: review_decisions に severity 列を additive 追加。
--
-- 変更内容:
--   - review_decisions.severity TEXT NULL を追加（'critical'|'high'|'medium'|'low'|NULL）
--
-- 語彙拡張（DDL 変更を伴わないアプリ層のみの拡張・本 migration の背景として明記）:
--   - reviewer 語彙に 'design-critic' を追加（列は TEXT・CHECK なしのため DDL 変更不要）
--   - checklist_id に 'DC-XX-NNN' 形式を許容（同上・アプリ層の正規表現拡張のみ）
--
-- 後方互換: additive のみ。既存行の severity は NULL のまま保持される。
--   CHECK 制約は設けない（decision/reviewer と同じく検証はアプリ層＝既存規律）。
--   インデックス追加なし（ローカル SQLite・数百行オーダー）。

BEGIN;
ALTER TABLE review_decisions ADD COLUMN severity TEXT;  -- 'critical'|'high'|'medium'|'low'|NULL
INSERT OR IGNORE INTO schema_migrations (version) VALUES ('006');
COMMIT;
