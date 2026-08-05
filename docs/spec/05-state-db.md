# 05 — 永続状態 / DB 層 仕様目次

> **⛔ 凍結: 2026-08-05 時点のスナップショット。**
> 実装を変更しても更新しない（2026-08-05 裁定）。**現行仕様として読まないこと。**
> 理由と、現在の姿を知る方法は [`00-index.md`](00-index.md) の冒頭を参照。

C3 の永続状態（`c3.db` / `.claude/state/` / `.claude/memory/` / `.claude/agent-memory/`）を全列挙し、
各項目の「書き込む主体」「読み込む主体」を grep で実測した棚卸し目次。

- 実測日: 2026-08-05（JST）
- 実測環境: `C:\Users\shoma\github_project\claude-code-conductor`（配布元リポジトリ・branch `main` / `d9037a2`）
- 網羅性の基準: `ls` 実測値と本文の項目数を突き合わせ済み（§1 参照）
- 読みやすさより **網羅性と根拠の正確さ** を優先している。全項目に `file:line` の根拠を付す

---

## 0. サマリ

### 0-1. 特記フラグ件数

| フラグ | 件数 | 対象 |
|---|---|---|
| `[書き手なし]` | **5** | `agent_runs` / `sqlite_sequence`(*) / `state/security_audit_exceptions.json` / `state/stop_exit2_test.flag` / `agent-memory/wt_developer/`・`agent-memory/wt_tester/`(**) |
| `[読み手なし]` | **8** | `agent_runs` / `sqlite_sequence`(*) / `state/security_audit_exceptions.json` / `state/stop_exit2_test.flag` / `state/recall.hnsw.bak` / `state/recall_meta.json.bak` / `memory/archive/*.tmp` / `memory/consolidated_summary.md` / `memory/promotion-candidates.md` |
| `[未文書化]` | **6** | `.claude/state/` ディレクトリ自体（`taxonomy.md` に節が無い） / `state/stop_exit2_test.flag` / `memory/consolidated_summary.md` / `memory/promotion-candidates.md` / `memory/archive/` / `agent-memory/` の空ディレクトリ運用 |

(*) `sqlite_sequence` は SQLite エンジンが自動管理するテーブルで、C3 コードからの読み書きは無い。
「棚卸しで削れる死んだ状態」ではないため、上記の件数には数えているが是正対象ではない。
(**) `wt_developer` / `wt_tester` はディレクトリを 2 件まとめて 1 件と数えた。

> `[読み手なし]` の合計は 9 項目（`sqlite_sequence` を含む）だが、`recall.hnsw.bak` と
> `recall_meta.json.bak` は同一機構（`_atomic_replace` の rollover）による対の生成物なので
> 集計上は 8 グループとした。個別の内訳は各節を参照。

### 0-2. 最重要の発見

1. **`agent_runs` は完全な死にテーブル**（`[書き手なし]` + `[読み手なし]` + 実測 0 行）。
   `src/c3/migrations/001_initial.sql:88` の DDL 以外に production コードの参照が 1 件も無い。
   にもかかわらず `ARCHITECTURE.md:159` は書き込み元を `usage_ingester.py` と記載しており、**誤り**。
2. **`memory/consolidated_summary.md`（27,256 B）と `memory/promotion-candidates.md` は毎 Stop で
   書かれるが誰も読まない**。skills / agents / hooks / CLI / recall index のいずれにも読み取り経路が無い。
   「集めているだけのデータ」の典型。
3. **`memory/archive/*.tmp`（73 ファイル・959,059 B）も読み手なし**。
   `recall_index.py:429-433` が索引化するのは `sessions/` のみで `archive/` は対象外。
   TTL で archive へ移した瞬間に検索対象から外れる。
4. **`.claude/state/` は `taxonomy.md` にディレクトリの節が無い**（`agents/` `rules/` `skills/` `hooks/`
   `docs/` `memory/` `reports/` `tmp/` `output-styles/` `plugins/` の 10 節のみ）。
   永続状態の中心である state 層が規約文書の分類体系に存在しない。
5. **`ARCHITECTURE.md` §4-1 のテーブル表は 3 箇所が現行スキーマと不一致**（詳細は §5-1）。

### 0-3. c3.db の非改変検証（完了条件 5）

**結果: 検査前後で size / mtime は一致しなかった（3 時点すべてで異なる）。
ただし変更はいずれも本監査によるものではなく、監査を実行している Claude Code セッション自身の
Stop hook（`session_stop.py` Phase 3 = `usage_ingester`）によるものと実測で特定した。**

| 時点 | size | mtime | md5 |
|---|---|---|---|
| ① 検査前（baseline） | 1,269,760 | `1785903028` = 2026-08-05 13:10:28 | `50fdf43f8177fb9093a6cde6ee556dcc` |
| ② 検査後（本文執筆前） | 1,273,856 | `1785904820` = 2026-08-05 13:40:20 | `b042ca53f06abaa00de6334d358785f3` |
| ③ 検査後（本文執筆後・最終） | 1,273,856 | `1785905420` = 2026-08-05 13:50:20 | `8c4890ba0df3164273ac087ce6a1ff66` |

**実行したクエリ（すべて read-only）**

接続文字列は 2 通りのみ。いずれも書き込み不可の URI で開いている。

```
sqlite3.connect("file:.claude/state/c3.db?mode=ro&immutable=1", uri=True)   # 1 回目・3 回目
sqlite3.connect("file:.claude/state/c3.db?mode=ro", uri=True)               # 2 回目（件数再取得・最大値確認）
```

発行した SQL は以下がすべて。`INSERT` / `UPDATE` / `DELETE` / DDL は 1 件も発行していない。

```sql
SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name;
SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' ORDER BY tbl_name, name;
SELECT sql FROM sqlite_master WHERE type='table' AND name=?;
SELECT COUNT(*) FROM <各テーブル>;
SELECT * FROM schema_migrations;
SELECT * FROM sqlite_sequence;
SELECT * FROM usage_ingest_state;
PRAGMA page_size; PRAGMA journal_mode; PRAGMA user_version;
SELECT MAX(recorded_at) FROM agent_cost_runs;
SELECT MAX(ts) FROM agent_outcomes;
SELECT MAX(decided_at) FROM review_decisions;
SELECT MAX(last_processed_at) FROM usage_ingest_state;
```

**変更の帰属（実測による）**

| テーブル | ① 検査前 | ② 執筆前 | ③ 最終 | ①→③ 差分 |
|---|---|---|---|---|
| `agent_cost_runs` | 1329 | 1335 | 1335 | **+6** |
| `usage_ingest_state` | 1290 | 1296 | 1296 | **+6** |
| `agent_outcomes` | 828 | 828 | 828 | 0 |
| `review_decisions` | 1342 | 1342 | 1342 | 0 |
| `agent_runs` | 0 | 0 | 0 | 0 |
| `schema_migrations` | 7 | 7 | 7 | 0 |

増加した 2 テーブルの最新タイムスタンプは双方 `2026-08-05T04:40:20+00:00`（= JST 13:40:20）で、
**変更後の c3.db の mtime と秒まで一致する**。この 2 テーブルの唯一の書き込み経路は
`src/c3/usage_ingester.py:72` の `ingest_session()` であり、これを呼ぶのは
`.claude/hooks/session_stop.py:103`（Stop hook Phase 3）だけである。
すなわち **本監査を実行中の Claude Code セッション自身の Stop hook が書いた**もので、監査クエリ由来ではない。
同時刻に `state/recall.hnsw` / `state/recall_meta.json` も更新されており（同じ Stop hook の
`recall_autorebuild.py`）、Stop hook 一式が走ったことと整合する。

②→③ の変化（13:40:20 → 13:50:20・**ちょうど 600 秒後**）も同型で、**行数はどのテーブルも増えていない**
（1335 / 1296 のまま）一方で `MAX(recorded_at)` と `MAX(last_processed_at)` が
`2026-08-05T04:50:20+00:00`（= JST 13:50:20）へ進み、③ の mtime と秒まで一致する。
これは `insert_agent_cost_run()` / `set_ingest_offset()` の **upsert が既存行を UPDATE した**
結果で（`src/c3/db.py:1358` / `:1770`）、やはり Stop hook の周期実行によるもの。
本監査のクエリは `SELECT` と `PRAGMA` のみで UPDATE 経路を持たない。

**監査が作った副産物（申告）**: 2 回目の接続（`immutable=1` なし）で SQLite が WAL 用サイドカー
`.claude/state/c3.db-shm`（32,768 B）と `.claude/state/c3.db-wal`（0 B）を生成した。
`c3.db` 本体の mtime / size はこの読み取りで変化していない（13:40 のまま）。
両ファイルは `.claude/.gitignore:50-51`（`state/*-wal` / `state/*-shm`）で除外済み。削除はしていない。

---

## 1. 実測ファイル一覧（`ls` との突き合わせ）

### 1-1. `.claude/state/`（実測 17 エントリ）

| # | エントリ | 本文の節 |
|---|---|---|
| 1 | `.gitkeep` | §6-14 |
| 2 | `c3.db` | §3 |
| 3 | `c3.db-shm` | §0-3（監査中に生成） |
| 4 | `c3.db-wal` | §0-3（監査中に生成） |
| 5 | `e0-targets-1785880616-1008-29047.txt` | §6-1 |
| 6 | `init_session.flag` | §6-2 |
| 7 | `recall.hnsw` | §6-3 |
| 8 | `recall.hnsw.bak` | §6-4 |
| 9 | `recall_meta.json` | §6-5 |
| 10 | `recall_meta.json.bak` | §6-6 |
| 11 | `security_audit_exceptions.json` | §6-7 |
| 12 | `setup_done.flag` | §6-8 |
| 13 | `stop_exit2_test.flag` | §6-9 |
| 14 | `tier_autoapply.jsonl` | §6-10 |
| 15 | `tier_autoapply.jsonl.lock` | §6-11 |
| 16 | `tier_selection.json` | §6-12 |
| 17 | （不在）`recall_rebuild.lock` | §6-13（コード上は存在・実測時は未生成） |

### 1-2. `.claude/memory/`（実測 4 ファイル + 2 ディレクトリ）

| # | エントリ | 実測 | 本文の節 |
|---|---|---|---|
| 1 | `.gitkeep` | 0 B | §7-6 |
| 2 | `consolidated_summary.md` | 27,256 B | §7-3 |
| 3 | `patterns.json` | 6,195 B / 9 entries | §7-2 |
| 4 | `promotion-candidates.md` | 271 B | §7-4 |
| 5 | `sessions/` | 21 files / 620,567 B | §7-1 |
| 6 | `archive/` | 73 `.tmp` + `.gitkeep` / 959,059 B | §7-5 |

### 1-3. `.claude/agent-memory/`（実測 7 ディレクトリ / 263 ファイル / 1,070,550 B）

| # | ディレクトリ | ファイル数 | サイズ | 本文の節 |
|---|---|---|---|---|
| 1 | `code-reviewer/` | 21 | 223,712 B | §8 |
| 2 | `design-critic/` | 5 | 145,810 B | §8 |
| 3 | `developer/` | 31 | 61,247 B | §8 |
| 4 | `security-reviewer/` | 172 | 414,693 B | §8 |
| 5 | `tester/` | 34 | 225,088 B | §8 |
| 6 | `wt_developer/` | **0** | 0 B | §8-2 `[書き手なし]` |
| 7 | `wt_tester/` | **0** | 0 B | §8-2 `[書き手なし]` |

### 1-4. `src/c3/migrations/`（実測 9 エントリ）

`__init__.py` / `README.md` / `001`〜`007` の 7 本の `.sql`。詳細は §4。

---

## 2. DuckDB の実態

| 項目 | 内容 |
|---|---|
| 用途 | **実装されていない。** 設計意図としての言及のみ |
| 実測 | `import duckdb` / `duckdb.` の呼び出しはリポジトリ全体で **0 件**（`src/` `.claude/` `.dev/` を `--include="*.py"` で grep） |
| 残存する言及 | `src/c3/migrations/001_initial.sql:7`「読み・分析は DuckDB の sqlite_scanner で ATTACH してアクセスする想定」／ `src/c3/db.py:10` 同旨／ `src/c3/plan_validator.py:9`「the c3.db DuckDB layer」／ `.claude/hooks/session_start.py:7`「duckdb-hybrid 基盤」／ `ARCHITECTURE.md:149`「SQLite + DuckDB ハイブリッド」 |
| 判定 | 現行 C3 の永続層は **SQLite 単独**。「SQLite+DuckDB ハイブリッド」は設計上の将来構想であり稼働コードは無い |
| 根拠 | `src/c3/migrations/001_initial.sql:7` / `src/c3/db.py:10` / `src/c3/plan_validator.py:9` / `.claude/hooks/session_start.py:7` / `ARCHITECTURE.md:149` |

> 注: `src/c3/__pycache__/cli_status.cpython-311.pyc` にも `duckdb` 文字列がヒットするが、
> 対応する `src/c3/cli_status.py` は既に存在しない（削除済みモジュールの stale bytecode）。

---

## 3. `c3.db`（SQLite）

### 3-0. データベース全体

| 項目 | 内容 |
|---|---|
| 用途 | tier-routing の学習イベントログ・review-hint の判断記録・エージェントコスト集計を保持する単一 SQLite DB |
| 物理パス | `.claude/state/c3.db`（`.claude/hooks/session_start.py:167` の `DB_PATH`） |
| パス解決 | 環境変数 `C3_DB_PATH` → （deprecated）`C3_PO_DB_PATH` → 上位ディレクトリ探索。`src/c3/db.py:133` `locate_c3_db()` / `src/c3/db.py:154` |
| journal mode | WAL（`src/c3/db.py:125` / `:353` / `:1126` / `:1355` で `PRAGMA journal_mode=WAL`） |
| スキーマ適用 | `src/c3/migrate.py:46` `apply_pending_migrations()`。呼び出し元は `.claude/hooks/session_start.py:175` `apply_schema()`（SessionStart hook） |
| テーブル数 | 7（実測・`SELECT name FROM sqlite_master WHERE type='table'`） |
| 現在のサイズ | 1,273,856 B（検査後） |
| git 管理 | **tracked**（`.claude/.gitignore` で意図的に除外していない。同ファイル §「意図的に tracked にしているもの」） |
| 根拠 | `.claude/hooks/session_start.py:167`, `src/c3/db.py:133`, `src/c3/migrate.py:46`, `.claude/.gitignore:65-70` |

### 3-1. `agent_cost_runs`

| 項目 | 内容 |
|---|---|
| 用途 | セッションログ（jsonl）から集計した agent 単位のトークン数・USD コストを保持する |
| スキーマ | `session_id TEXT NOT NULL` / `agent_id TEXT NOT NULL` / `agent_type TEXT NOT NULL` / `description TEXT` / `model TEXT NOT NULL` / `attribution_skill TEXT` / `input_tokens INTEGER NOT NULL DEFAULT 0` / `output_tokens INTEGER NOT NULL DEFAULT 0` / `cache_read_tokens INTEGER NOT NULL DEFAULT 0` / `cache_create_tokens INTEGER NOT NULL DEFAULT 0` / `total_cost_usd REAL NOT NULL` / `recorded_at TEXT NOT NULL`。PK = `(session_id, agent_id, model)` |
| INDEX | `idx_agent_cost_runs_agent_type ON agent_cost_runs(agent_type, recorded_at)` |
| 書き込む主体 | `src/c3/usage_ingester.py:72` `ingest_session()` → `src/c3/db.py:1307` `insert_agent_cost_run()`（`src/c3/db.py:1358` の `INSERT`・upsert）。呼び出し元は `.claude/hooks/session_stop.py:103`（Stop hook Phase 3） |
| 読み込む主体 | `src/c3/cli_tier.py:227`（`c3 tier stats` の Agent 別コスト集計）／ `src/c3/db.py:1393` `read_agent_cost_summary()`（`:1434` の SELECT）／ `src/c3/db.py:1575` `read_tier_cost_rate_summary()`（`:1638`）／ `src/c3/db.py:1664` `read_tier_cost_rate_for_complexity()`（`.claude/hooks/select_tier.py:838` から cost-weighted 選択に使用）／ `src/c3/db.py:824` `read_rework_session_cost()`（`:902` `:926` `:943`・`src/c3/cli_metrics.py:204` 経由の `c3 metrics`） |
| 現在の行数 | **1,335 行**（検査後・検査前 1,329） |
| 根拠 | `src/c3/migrations/002_agent_cost_runs.sql:14-31`, `src/c3/db.py:1307`, `src/c3/db.py:1358`, `.claude/hooks/session_stop.py:103`, `src/c3/cli_tier.py:227`, `.claude/hooks/select_tier.py:838` |

> 注意点: **`ts` 列は存在しない**（時刻列は `recorded_at` のみ）。`src/c3/db.py:838` にこの旨が明記され、
> `tests/test_db.py:3827` が `"ts" not in cols` を機械強制している。

### 3-2. `agent_outcomes`

| 項目 | 内容 |
|---|---|
| 用途 | tier-routing の学習シグナル本体。role × complexity × tier ごとの成否イベントログ（累積テーブルは migration 005 で廃止され、読み取り時に本ログから導出集計する） |
| スキーマ | `id INTEGER PRIMARY KEY AUTOINCREMENT` / `role TEXT NOT NULL` / `task_complexity TEXT NOT NULL` / `tier TEXT NOT NULL` / `success INTEGER NOT NULL` / `gate TEXT` / `note TEXT` / `session_id TEXT` / `ts TEXT NOT NULL` |
| INDEX | `idx_agent_outcomes_cell ON agent_outcomes(role, task_complexity, tier, ts DESC)` / `idx_agent_outcomes_session ON agent_outcomes(session_id)` |
| 書き込む主体 | `.claude/skills/dev-workflow/scripts/record_agent_outcome.py`（`:905` で `c3_db.locate_c3_db()` を引き、`src/c3/db.py:1085` `record_agent_outcome_event()` → `src/c3/db.py:1129` の `INSERT`）。dev-workflow の各フェーズで LLM が Bash 経由で起動する |
| 読み込む主体 | `.claude/hooks/select_tier.py:828` `:869`（`src/c3/db.py:995` `read_agent_tier_params()` → `:1059` SELECT）／ `.claude/hooks/select_tier.py:475`（`src/c3/db.py:1149` `read_agent_failure_rate()` → `:1210`）／ `src/c3/cli_tier.py:149`（`src/c3/db.py:1234` `read_recent_agent_outcomes()` → `:1265` `:1273`）／ `.claude/hooks/tier_gap_check.py:288` `:309`（session 突合の COUNT）／ `src/c3/db.py:604` `read_rework_trend()`（`:648`）・`:684`（`:718`）・`:735`（`:782`）・`:824`（`:896`）・`:1575`（`:1643`）＝`c3 metrics` / `c3 tier stats` |
| 現在の行数 | **828 行**（`sqlite_sequence` の next = 829） |
| 根拠 | `src/c3/migrations/004_agent_outcomes.sql:28-42`, `src/c3/db.py:1085`, `src/c3/db.py:1129`, `.claude/skills/dev-workflow/scripts/record_agent_outcome.py:905`, `.claude/hooks/select_tier.py:828`, `.claude/hooks/tier_gap_check.py:288` |

### 3-3. `agent_runs` — `[書き手なし]` `[読み手なし]`

| 項目 | 内容 |
|---|---|
| 用途 | （設計意図）agent の start / stop イベントと所要時間・トークン・payload の生ログ。**実際には一度も使われていない** |
| スキーマ | `id INTEGER PRIMARY KEY AUTOINCREMENT` / `session_id TEXT` / `agent_id TEXT` / `agent_type TEXT` / `event TEXT NOT NULL`（`'start'` \| `'stop'`） / `ts TEXT NOT NULL` / `duration_seconds REAL` / `total_tokens INTEGER` / `status TEXT` / `model TEXT` / `payload_json TEXT` |
| INDEX | `idx_agent_runs_session ON agent_runs(session_id, ts DESC)` / `idx_agent_runs_agent ON agent_runs(agent_id, ts DESC)` |
| 書き込む主体 | **`[書き手なし]`**。`src/c3/` `.claude/hooks/` `.claude/skills/` `scripts/` `.dev/` を横断 grep しても `INSERT INTO agent_runs` は 1 件も無い。ヒットするのは DDL（`src/c3/migrations/001_initial.sql:88`）とテスト（`tests/hooks/test_session_start.py:604` の seed）と文書のみ |
| 読み込む主体 | **`[読み手なし]`**。`SELECT ... FROM agent_runs` は production コードに存在しない。ヒットはテスト（`tests/hooks/test_session_start.py:620`）のみ |
| 現在の行数 | **0 行**（実測） |
| 根拠 | `src/c3/migrations/001_initial.sql:88-104`, `tests/hooks/test_session_start.py:604`, `tests/hooks/test_session_start.py:620`, （誤記載）`ARCHITECTURE.md:159` |

> **`ARCHITECTURE.md:159` は `agent_runs` の書き込み元を `usage_ingester.py` と記載しているが誤り。**
> `src/c3/usage_ingester.py` に `agent_runs` の文字列は 1 度も現れない。
> 作り直しの際、このテーブルは移行対象から外してよい（データ 0 件・参照 0 件）。

### 3-4. `review_decisions`

| 項目 | 内容 |
|---|---|
| 用途 | code-reviewer / security-reviewer の指摘に対する人間の判断（対応 / 許容 / 保留）と、その後の是正判定を蓄積する。次回レビュー時のヒント注入に使う |
| スキーマ | `id INTEGER PRIMARY KEY AUTOINCREMENT` / `checklist_id TEXT NOT NULL` / `finding_text TEXT NOT NULL` / `decision TEXT NOT NULL`（`'fixed'`\|`'accepted'`\|`'deferred'`） / `reason TEXT` / `context_summary TEXT` / `decided_at TEXT NOT NULL` / `reviewer TEXT NOT NULL` ＋ 後付け 4 列: `severity TEXT`（006）/ `resolution TEXT` / `resolution_note TEXT` / `resolution_commit TEXT`（007） |
| INDEX | `idx_review_decisions_checklist ON review_decisions(checklist_id, decided_at DESC)` |
| 書き込む主体 | (1) 新規 INSERT: `.claude/skills/dev-workflow/scripts/record_review_decision.py:147-153` → `src/c3/db.py:278` `insert_review_decision()`（`:357` / `:383` の `INSERT`。`severity` 列の有無で 2 分岐）。(2) 既存行の UPDATE: `scripts/audit_review_decisions.py:325`（`resolution` 3 列の書き戻し・配布元専用 dev ツール） |
| 読み込む主体 | `.claude/skills/dev-workflow/scripts/review_hint_inject.py:240` → `src/c3/db.py:183` `fetch_review_decisions()`（`:218` SELECT）／ `src/c3/db.py:459` `read_review_decision_matrix()`（`:497`）・`:517` `fetch_prevented_findings()`（`:561`）＝`c3 metrics`／ `scripts/audit_review_decisions.py:188`（`list`）・`:305`（`resolve` 前チェック）・`:356`（`summary`） |
| 現在の行数 | **1,342 行**（`sqlite_sequence` の next = 1345 → 3 件は ID 欠番） |
| 根拠 | `src/c3/migrations/001_initial.sql:39-50`, `src/c3/migrations/006_review_decisions_severity.sql:15`, `src/c3/migrations/007_review_decisions_resolution.sql:13-15`, `src/c3/db.py:278`, `.claude/skills/dev-workflow/scripts/record_review_decision.py:153`, `scripts/audit_review_decisions.py:325`, `.claude/skills/dev-workflow/scripts/review_hint_inject.py:240` |

### 3-5. `schema_migrations`

| 項目 | 内容 |
|---|---|
| 用途 | 適用済み migration の version 一覧。冪等適用の基準 |
| スキーマ | `version TEXT PRIMARY KEY` / `applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP` |
| INDEX | `sqlite_autoindex_schema_migrations_1`（PK 由来の暗黙 index） |
| 書き込む主体 | (1) `src/c3/migrate.py:248` `INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)`（SQL 本体とは別トランザクション）。(2) 各 `.sql` 末尾の自己記録（例 `src/c3/migrations/003_tier_cost.sql:23`・`005_drop_agent_tier_bandit.sql:13`）。テーブル自体の作成は `src/c3/migrate.py:166` `_ensure_schema_migrations_table()`（`:174` DDL）と `src/c3/migrations/001_initial.sql:27` |
| 読み込む主体 | `src/c3/migrate.py:183` `_get_applied_versions()`（`:196` `SELECT version FROM schema_migrations`）。呼び出し元は `src/c3/migrate.py:46` `apply_pending_migrations()` ← `.claude/hooks/session_start.py:175`（SessionStart hook） |
| 現在の行数 | **7 行**: `001`(2026-05-24 17:16:37) / `002`(05-25 05:50:03) / `003`(05-25 08:35:50) / `004`(07-02 13:43:11) / `005`(07-03 07:06:34) / `006`(07-06 15:46:00) / `007`(08-04 12:39:02) |
| 根拠 | `src/c3/migrate.py:166`, `src/c3/migrate.py:196`, `src/c3/migrate.py:248`, `.claude/hooks/session_start.py:175`, `src/c3/migrations/001_initial.sql:27` |

> 二重記録の設計: SQL ファイル内の `INSERT OR IGNORE` と `migrate.py:248` の `INSERT OR IGNORE` が
> 両方走る。`src/c3/migrate.py:233` に「`executescript()` 内の `COMMIT;` 後に例外が出ると
> `schema_migrations` への記録のみ未実行になる」旨の注記がある。

### 3-6. `sqlite_sequence` — `[書き手なし]` `[読み手なし]`（SQLite エンジン管理）

| 項目 | 内容 |
|---|---|
| 用途 | `AUTOINCREMENT` 列を持つテーブルの次採番値。SQLite が自動生成・自動更新する |
| スキーマ | `sqlite_sequence(name, seq)`（SQLite 内部定義。C3 の migration には現れない） |
| 書き込む主体 | **C3 コードには無い**（SQLite エンジンが `AUTOINCREMENT` 列への INSERT 時に更新）。C3 側の契機は `agent_outcomes` / `review_decisions` の `id INTEGER PRIMARY KEY AUTOINCREMENT` 宣言（`src/c3/migrations/004_agent_outcomes.sql:29`, `src/c3/migrations/001_initial.sql:40`） |
| 読み込む主体 | **C3 コードには無い**。`sqlite_sequence` の文字列はリポジトリ全体で本監査のクエリ以外にヒットしない |
| 現在の行数 | **2 行**: `('review_decisions', 1345)` / `('agent_outcomes', 829)` |
| 根拠 | `src/c3/migrations/001_initial.sql:40`, `src/c3/migrations/004_agent_outcomes.sql:29` |

> 棚卸し上の扱い: 削除も移行も不要。`AUTOINCREMENT` を使う限り自動的に再生成される。
> 作り直しの際に `AUTOINCREMENT` をやめれば消える。

### 3-7. `usage_ingest_state`

| 項目 | 内容 |
|---|---|
| 用途 | セッションログ jsonl ごとの「どこまで読んだか」の offset。再取り込みの重複計上を防ぐ |
| スキーマ | `file_key TEXT PRIMARY KEY`（`'<session>:mainline'` / `'<session>:agent-<id>'`） / `last_offset INTEGER NOT NULL`（処理済み行数） / `last_processed_at TEXT NOT NULL`（ISO8601 UTC） |
| INDEX | `sqlite_autoindex_usage_ingest_state_1`（PK 由来） |
| 書き込む主体 | `src/c3/db.py:1740` `set_ingest_offset()`（`:1770` の `INSERT`・upsert）。呼び出し元は `src/c3/usage_ingester.py:72` `ingest_session()` ← `.claude/hooks/session_stop.py:103` |
| 読み込む主体 | `src/c3/db.py:1702` `get_ingest_offset()`（`:1728` の SELECT）。呼び出し元は同じく `src/c3/usage_ingester.py`。**読み手は ingester 自身のみ**（自己参照的な進捗管理で、外部の消費者はいない） |
| 現在の行数 | **1,296 行**（検査後・検査前 1,290） |
| 根拠 | `src/c3/migrations/002_agent_cost_runs.sql:33`, `src/c3/db.py:1702`, `src/c3/db.py:1740`, `src/c3/usage_ingester.py:72`, `.claude/hooks/session_stop.py:103` |

---

## 4. `src/c3/migrations/*.sql`

### 4-0. 適用機構

| 項目 | 内容 |
|---|---|
| 用途 | `c3.db` のスキーマを冪等適用する |
| 適用順 | `src/c3/migrate.py:135` `sorted(migrations_dir.glob("*.sql"))` によるファイル名昇順。version は先頭 3 桁の文字列（`'001'` 等） |
| 適用主体 | `src/c3/migrate.py:46` `apply_pending_migrations()` ← `.claude/hooks/session_start.py:175` `apply_schema()`（SessionStart hook で毎セッション起動時に走る） |
| 適用済み判定 | `schema_migrations` の version 集合（`src/c3/migrate.py:183`） |
| 実測 | ディスク上 7 本 / DB 適用済み 7 件（一致） |
| 根拠 | `src/c3/migrate.py:110`, `src/c3/migrate.py:135`, `src/c3/migrate.py:208`, `.claude/hooks/session_start.py:175` |

### 4-1. 一覧

| version | ファイル | サイズ | 内容 | 適用日時（DB 実測） |
|---|---|---|---|---|
| — | `__init__.py` | 72 B | パッケージマーカー | — |
| — | `README.md` | 3,106 B | migration 運用の説明（人間向け） | — |
| `001` | `001_initial.sql` | 6,613 B | 旧 `.claude/hooks/schema.sql` の逐語移植。`po_results` / `po_status` を DROP（v2.0.0 PO 廃止）、`schema_migrations` / `review_decisions` / `tier_bandit` / `tier_recent_outcomes` / `agent_runs` を CREATE、旧 `schema_version` を DROP | 2026-05-24 17:16:37 |
| `002` | `002_agent_cost_runs.sql` | 1,844 B | `agent_cost_runs` / `usage_ingest_state` と `idx_agent_cost_runs_agent_type` を追加 | 2026-05-25 05:50:03 |
| `003` | `003_tier_cost.sql` | 1,016 B | `tier_recent_outcomes.session_id` 追加 + INDEX、`tier_bandit.total_cost_usd` / `cost_samples` 追加（v2.23.0 用の先行確保） | 2026-05-25 08:35:50 |
| `004` | `004_agent_outcomes.sql` | 1,798 B | **`tier_bandit` / `tier_recent_outcomes` を DROP**。`agent_tier_bandit` と `agent_outcomes` + INDEX 2 種を新設（role 次元導入） | 2026-07-02 13:43:11 |
| `005` | `005_drop_agent_tier_bandit.sql` | 840 B | **`agent_tier_bandit` を DROP**。bandit params は `agent_outcomes` からの読み取り時導出へ全面移行 | 2026-07-03 07:06:34 |
| `006` | `006_review_decisions_severity.sql` | 1,007 B | `review_decisions.severity TEXT` を additive 追加 | 2026-07-06 15:46:00 |
| `007` | `007_review_decisions_resolution.sql` | 1,109 B | `review_decisions` に `resolution` / `resolution_note` / `resolution_commit` を additive 追加 | 2026-08-04 12:39:02 |

### 4-2. 実装と文書の乖離（migration 由来）

`001` → `003` で作られたテーブルのうち、`tier_bandit` / `tier_recent_outcomes` / `agent_tier_bandit` は
`004` / `005` で DROP 済みだが、以下に記述が残っている:

| 場所 | 記述 | 状態 |
|---|---|---|
| `ARCHITECTURE.md:157` | `tier_bandit` テーブルの行（書き込み元 `select_tier.py` / `record_tier_outcome.py`） | **テーブルは 004 で DROP 済み。`record_tier_outcome.py` も `.claude/skills/dev-workflow/scripts/` に存在しない**（実測: `detect_execution_verification.py` / `record_agent_outcome.py` / `record_review_decision.py` / `review_hint_inject.py` の 4 本のみ） |
| `ARCHITECTURE.md:158` | `tier_recent_outcomes` テーブルの行 | 004 で DROP 済み |
| `src/c3/db.py:4` / `:980` / `:1476` / `:1509` | docstring 内の `tier_recent_outcomes` 参照 | コメントのみ残存（実クエリは `agent_outcomes` に差し替え済み・`src/c3/db.py:1593` に明記） |

---

## 5. 規約文書のカバレッジ

### 5-1. `ARCHITECTURE.md` §4-1 テーブル表の実測突き合わせ

| 表の記載 | 実測 | 判定 |
|---|---|---|
| `schema_migrations` ← `migrate.py` | 一致 | OK |
| `review_decisions` ← `record_review_decision.py` | 一致（ただし `audit_review_decisions.py` の UPDATE 経路が未記載） | 部分的 |
| `tier_bandit` ← `select_tier.py` / `record_tier_outcome.py` | **テーブル不在・スクリプト不在** | 誤り |
| `tier_recent_outcomes` ← 同上 | **テーブル不在** | 誤り |
| `agent_runs` / `agent_cost_runs` ← `usage_ingester.py` | `agent_cost_runs` は一致。**`agent_runs` は誤り**（書き手なし） | 誤り |
| `usage_ingest_state` ← `usage_ingester.py` | 一致 | OK |
| （未記載） `agent_outcomes` | **現行の学習シグナル本体が表に無い** | 欠落 |

### 5-2. `taxonomy.md` のカバレッジ — `[未文書化]`

`taxonomy.md` の「フォルダ一覧」は `agents/` / `rules/` / `skills/` / `hooks/` / `docs/` / `memory/` /
`reports/` / `tmp/` / `output-styles/` / `plugins/` の 10 節。

- **`state/` の節が存在しない**（`.claude/state/` は `taxonomy.md` 中に 1 度も現れない）→ `[未文書化]`
- `memory/` の節（`taxonomy.md:289-296`）が挙げるのは `sessions/*.tmp` と `patterns.json` の 2 つのみ。
  `consolidated_summary.md` / `promotion-candidates.md` / `archive/` は **未記載** → `[未文書化]`
- `agent-memory/` は `taxonomy.md:60-62` `:77` `:80-81` `:100` で扱われている → 文書化済み

補完的に state ファイルを列挙している文書は `.claude/.gitignore`（分類コメント）と
`.claude/docs/config-policy.md:62` `:93` の 2 箇所のみで、いずれも **git 管理方針の観点**であり
「誰が書き誰が読むか」の記述は無い。

---

## 6. `.claude/state/`

### 6-1. `e0-targets-*.txt`

| 項目 | 内容 |
|---|---|
| 用途 | dev-workflow フェーズ E-0（実行検証判定）の検出器が tester へ渡す対象ファイル一覧。1 周回限りの受け渡し |
| 書式 | TSV。`<判定>\t<件数>\t<スペース区切りパス列>`。実測 1 行: `NEEDS_VERIFY\t6\tscripts/audit_review_decisions.py tests/...` |
| 書き込む主体 | `.claude/skills/dev-workflow/SKILL.md:871-872` の Bash ブロック（`rm -f` 後に `E0_OUT=".claude/state/e0-targets-$(date +%s)-$$-$RANDOM.txt"`）に `.claude/skills/dev-workflow/scripts/detect_execution_verification.py --print0` の stdout をリダイレクト |
| 読み込む主体 | 同周回の tester（`.claude/skills/dev-workflow/SKILL.md:914` で `path:` として LLM に渡る）。**検出器自身は自分の出力を明示的に除外する**（`.claude/skills/dev-workflow/scripts/detect_execution_verification.py:108` `_SELF_OUTPUT_PATTERNS = (".claude/state/e0-targets-*.txt",)`） |
| 現在の行数・サイズ | 1 ファイル / 225 B（`e0-targets-1785880616-1008-29047.txt`・2026-08-05 06:56 の残骸） |
| 根拠 | `.claude/skills/dev-workflow/SKILL.md:871`, `.claude/skills/dev-workflow/scripts/detect_execution_verification.py:11`, `:108`, `.claude/.gitignore:44` |

> 寿命: 次回 E-0 実行時の `rm -f .claude/state/e0-targets-*.txt` で消える。現存分は前回周回の残骸。

### 6-2. `init_session.flag`

| 項目 | 内容 |
|---|---|
| 用途 | `/init-session` がこのセッションで実行済みかのマーカー（`/start` ↔ `/init-session` の再帰呼び出し回避） |
| 書式 | プレーンテキスト 1 行 = `CLAUDE_CODE_SESSION_ID`。実測値 `94b226b1-1616-4ac5-82eb-5093e69c35a0` |
| 書き込む主体 | `.claude/skills/init-session/scripts/session_guard.py`（`mark` サブコマンド。パス定数 `:43` `FLAG_REL`） |
| 読み込む主体 | 同 `session_guard.py` の `check` サブコマンド（`:11`「読み strip して `CLAUDE_CODE_SESSION_ID` と比較」）。呼び出し元は `.claude/skills/start/SKILL.md:29` `:32` のガード |
| 現在の行数・サイズ | 36 B / 1 行 |
| 根拠 | `.claude/skills/init-session/scripts/session_guard.py:9`, `:11`, `:43`, `.claude/skills/start/SKILL.md:32` |

### 6-3. `recall.hnsw`

| 項目 | 内容 |
|---|---|
| 用途 | recall（意味検索）のベクトル索引本体 |
| 書式 | バイナリ（拡張子 `.hnsw` は歴史的経緯。実体は numpy ベースのブルートフォース索引。`src/c3/recall_index.py:119` に明記） |
| 書き込む主体 | `src/c3/recall_index.py:261` の保存処理（`:589` `_atomic_replace` 経由）。パス決定は `src/c3/recall_index.py:616`。起動経路は `c3 recall rebuild`（`src/c3/cli_recall.py`）と Stop hook の `.claude/hooks/recall_autorebuild.py:131` |
| 読み込む主体 | `.claude/hooks/recall_inject.py:164` `:185`（UserPromptSubmit hook で索引を引き additionalContext に注入）／ `.claude/hooks/recall_autorebuild.py:114`（存在チェック）／ `c3 recall search`（`src/c3/cli_recall.py`） |
| 現在の行数・サイズ | 25,775,744 B（≒24.6 MB） |
| 根拠 | `src/c3/recall_index.py:119`, `:261`, `:616`, `.claude/hooks/recall_inject.py:164`, `.claude/hooks/recall_autorebuild.py:114`, `.claude/.gitignore:28` |

### 6-4. `recall.hnsw.bak` — `[読み手なし]`

| 項目 | 内容 |
|---|---|
| 用途 | `recall.hnsw` の直前世代。原子的置換の副産物 |
| 書式 | `recall.hnsw` と同じバイナリ |
| 書き込む主体 | `src/c3/recall_index.py:589` `_atomic_replace()`（`:592` で `dst.with_suffix(dst.suffix + ".bak")` へ `os.replace`） |
| 読み込む主体 | **`[読み手なし]`**。`.bak` を読み戻すコードはリポジトリに無い（`--include="*.py"` で `src/c3` `.claude/hooks` を grep したヒットは `recall_index.py:266` `:590` `:592` `:593` の生成側のみ）。ロールバック手段としては人間が手動 rename する以外に経路が無い |
| 現在の行数・サイズ | 25,774,208 B（≒24.6 MB） |
| 根拠 | `src/c3/recall_index.py:589-599`, `.claude/.gitignore:30`（`state/*.bak`） |

### 6-5. `recall_meta.json`

| 項目 | 内容 |
|---|---|
| 用途 | recall 索引のチャンクメタデータ（本文スニペット・出典パス・見出し等） |
| 書式 | JSON。トップレベルキー実測: `model`(str, 59 文字) / `dim`(int, 384) / `created_at` / `rebuilt_at` / `next_id`(int) / `chunks`(dict) |
| 書き込む主体 | `src/c3/recall_index.py:261`（`recall.hnsw` と同時にアトミック書き込み）。パス決定 `:616` |
| 読み込む主体 | `.claude/hooks/recall_inject.py:163`／ `src/c3/recall_index.py:320`（読み込み時の破損検知メッセージ）／ `c3 recall search`（`src/c3/cli_recall.py`） |
| 現在の行数・サイズ | 13,438,346 B（≒12.8 MB）/ `chunks` 16,781 件・`next_id` 16781 |
| 根拠 | `src/c3/recall_index.py:61`, `:261`, `:320`, `:616`, `.claude/hooks/recall_inject.py:163` |

### 6-6. `recall_meta.json.bak` — `[読み手なし]`

| 項目 | 内容 |
|---|---|
| 用途 | `recall_meta.json` の直前世代 |
| 書式 | `recall_meta.json` と同じ JSON |
| 書き込む主体 | `src/c3/recall_index.py:589` `_atomic_replace()` |
| 読み込む主体 | **`[読み手なし]`**（§6-4 と同一機構・同一根拠） |
| 現在の行数・サイズ | 13,437,203 B（≒12.8 MB） |
| 根拠 | `src/c3/recall_index.py:589-599`, `.claude/.gitignore:30` |

> §6-3〜6-6 の合計は約 74.6 MB で、`.claude/state/` 全体（実測 `ls` 合計 ≒ 79.5 MB）の 94% を占める。

### 6-7. `security_audit_exceptions.json` — `[書き手なし]` `[読み手なし]`

| 項目 | 内容 |
|---|---|
| 用途 | `pip-audit` 等で検出された残存 CVE と設計判断のうち、C3 として受容したものの記録 |
| 書式 | JSON。`_readme`(str) と `exceptions`(array)。要素は CVE 型（`package` / `version` / `cve` / `reason` / `first_observed` / `review_cycle`）と design 型（`type: "design"` / `target` / `issue` / `reason` / …）の 2 種混在。実測 3 件（markdown PYSEC-2026-89 / pyjwt PYSEC-2025-183 / settings.json auto_allow の設計判断） |
| 書き込む主体 | **`[書き手なし]`**。コードからの書き込みは無い。security-reviewer フェーズで LLM / 人間が Write ツールで直接編集する運用（honor system） |
| 読み込む主体 | **`[読み手なし]`**。`.claude/hooks/` `.claude/skills/` `.claude/agents/` `src/c3/` `scripts/` を横断 grep してもヒットは `.claude/docs/config-policy.md:93`（git 管理方針の表）と `.claude/.gitignore:70`（コメント）のみ。**CI にも hook にも読み込み経路が無く、記録しても何も自動的には効かない** |
| 現在の行数・サイズ | 1,571 B / exceptions 3 件（最終更新 2026-05-21） |
| 根拠 | `.claude/docs/config-policy.md:93`, `.claude/.gitignore:70`, `.dev/loop/README.md:337` |

> 棚卸し所見: 「許容例外の設定」と `.gitignore` のコメントは呼ぶが、実態は**人間向けの台帳**。
> 最終更新が 2026-05-21 で 2 か月半停止しており、実運用では参照されていない可能性が高い。

### 6-8. `setup_done.flag`

| 項目 | 内容 |
|---|---|
| 用途 | `/setup` 実行済みマーカー（プロジェクト単位の状態） |
| 書式 | **空ファイル**（存在自体がマーカー・中身不要。`.claude/skills/init-session/scripts/session_guard.py:129` に明記） |
| 書き込む主体 | `.claude/skills/init-session/scripts/session_guard.py:129` `cmd_setup_mark()`（`setup-mark` サブコマンド・`/setup` Phase 4 で実行）。パス定数 `:47` `SETUP_DONE_FLAG_REL` |
| 読み込む主体 | 同 `session_guard.py` の `mark` サブコマンド（`:10` `SETUP_MARKERS_REL` 経由で `coding-standards.md` または本フラグの存在から `SETUP_DONE` / `SETUP_NEEDED` を出力）。判定結果の消費者は `.claude/skills/init-session/SKILL.md:33-34` |
| 現在の行数・サイズ | 0 B（2026-06-16 生成） |
| 根拠 | `.claude/skills/init-session/scripts/session_guard.py:10`, `:47`, `:129`, `.claude/skills/init-session/SKILL.md:33`, `.claude/.gitignore:36`（`!state/setup_done.flag` で唯一 tracked） |

### 6-9. `stop_exit2_test.flag` — `[書き手なし]` `[読み手なし]` `[未文書化]`

| 項目 | 内容 |
|---|---|
| 用途 | **不明**。名称から Stop hook の `exit 2` 挙動を試験するための手動フラグと推測されるが、コードにも文書にも定義が無い |
| 書式 | プレーンテキスト。実測内容は 4 バイトの `test`（`od -c` で確認・末尾改行なし） |
| 書き込む主体 | **`[書き手なし]`**。リポジトリ全体を grep してもヒットは `.dev/loop/README.md:337`（「静的設定フラグ」の例示）と `.dev/tests/test_run_loop_pure.py:5113-5114`（loop ハーネスの benign 判定テストで「意図的に benign にしない対象」として名前だけ参照）の 2 箇所のみ |
| 読み込む主体 | **`[読み手なし]`**。読み取りコードは存在しない |
| 現在の行数・サイズ | 4 B（2026-05-15 23:58 生成・以後 2 か月半更新なし） |
| 根拠 | `.dev/loop/README.md:337`, `.dev/tests/test_run_loop_pure.py:5113`, `.dev/tests/test_run_loop_pure.py:5114` |

> 棚卸し所見: **死んだ状態ファイルの典型**。かつての手動デバッグの残骸と考えられる。
> ただし `.dev/tests/test_run_loop_pure.py:5113` がファイル名を文字列リテラルで参照しているため、
> 削除時はそのテストへの影響確認が要る（テストはパス文字列の分類を検査するだけでファイル実体は不要と読めるが、未実行のため断定しない）。

### 6-10. `tier_autoapply.jsonl`

| 項目 | 内容 |
|---|---|
| 用途 | tier-routing の **applied-state**（実際に注入した model の記録）。「適用者＝記録の SSOT」を成立させる |
| 書式 | JSON Lines（1 行 1 適用イベント）。`session_id` / `role_recorded` / `task_id` / `ts` 等を含む（`.claude/hooks/tier_autoapply.py:327` `json.dumps(row, ensure_ascii=False)`）。1 MB でローテーション（`:115` `_MAX_JSONL_BYTES = 1 * 1024 * 1024` / `:275` `_rotate_if_needed`） |
| 書き込む主体 | `.claude/hooks/tier_autoapply.py:315` `_append_applied_state()`（`:358` の追記・`:328` の `.lock` で直列化）。PreToolUse(Agent) hook として `.claude/settings.json` に登録 |
| 読み込む主体 | `.claude/skills/dev-workflow/scripts/record_agent_outcome.py:452` `_resolve_applied_tier()`（`(session_id, role, task_id)` 突合で実適用 tier を機械解決・優先 2）／ `.claude/hooks/tier_gap_check.py:143` `:245`（起動ログ vs `agent_outcomes` の欠落検針） |
| 現在の行数・サイズ | **308 行** / 171,252 B（最終更新 2026-08-05 08:12） |
| 根拠 | `.claude/hooks/tier_autoapply.py:85`, `:115`, `:315`, `:358`, `.claude/skills/dev-workflow/scripts/record_agent_outcome.py:125`, `:452`, `.claude/hooks/tier_gap_check.py:42`, `:143` |

### 6-11. `tier_autoapply.jsonl.lock`

| 項目 | 内容 |
|---|---|
| 用途 | 並行 append の直列化用ロックファイル（Windows は `msvcrt.locking` を使用） |
| 書式 | **空ファイル**（OS ロックの対象としてのみ存在。中身は使わない） |
| 書き込む主体 | `.claude/hooks/tier_autoapply.py:328-338`（`lock_path = APPLIED_STATE_PATH + ".lock"` を open し `:298` `msvcrt.locking(..., LK_LOCK, 1)`） |
| 読み込む主体 | 同 `tier_autoapply.py`（ロック取得のみ。内容は読まない）。`:335` で symlink 化を検出したら中断する |
| 現在の行数・サイズ | 0 B（2026-07-07 生成・以後 mtime 不変＝ロック取得は内容を変えないため） |
| 根拠 | `.claude/hooks/tier_autoapply.py:298`, `:318`, `:322`, `:328`, `:335`, `.claude/.gitignore:38`（`state/*.lock`） |

> `.claude/hooks/tier_autoapply.py:322` に「ロックファイルは stale-lock 回避のため永続化する（削除しない）」旨の設計注記（CR F-6）がある。

### 6-12. `tier_selection.json`

| 項目 | 内容 |
|---|---|
| 用途 | 直近 1 件の tier 選択結果（推奨）。プロンプト単位で上書きされる |
| 書式 | JSON 単一オブジェクト。実測キー: `complexity` / `tier` / `mode` / `suggested_model` / `prompt_prefix`（最大 200 文字・秘密マスク済み） / `prompt_hash`（SHA256 先頭 16） / `session_id` / `roles`（role 別の `{tier, mode}`） |
| 書き込む主体 | `.claude/hooks/select_tier.py:605-606`（UserPromptSubmit hook・`json.dump`）。パス定数 `:96`。**直近 1 件のみ保持**（`:27`） |
| 読み込む主体 | `.claude/hooks/tier_autoapply.py:215` `_read_selection()` → `:229` / `:248`（PreToolUse(Agent) で `updatedInput` の `model:` に注入）／ `.claude/skills/dev-workflow/scripts/record_agent_outcome.py:396`（優先 3 のフォールバック tier 解決・`:810` `:816` で session_id 取得）／ `.claude/hooks/tier_gap_check.py:121`（session_id の fallback）／ `.claude/skills/parallel-agents/SKILL.md:158`（wt_developer の tier SSOT として参照） |
| 削除する主体 | `.claude/skills/dev-workflow/scripts/record_agent_outcome.py:612`（`--final` 指定時＝E-2 完了時） |
| 現在の行数・サイズ | 799 B / 1 オブジェクト（最終更新 2026-08-05 13:25） |
| 根拠 | `.claude/hooks/select_tier.py:96`, `:542`, `:605`, `.claude/hooks/tier_autoapply.py:84`, `:215`, `.claude/skills/dev-workflow/scripts/record_agent_outcome.py:120`, `:396`, `:612`, `.claude/hooks/tier_gap_check.py:43`, `.claude/.gitignore:37` |

> セキュリティ注記: `prompt_prefix` にユーザープロンプト冒頭 200 文字が平文で入る（マスク→切り詰めの順で
> 秘密情報を除去。`CHANGELOG.md:390` に順序是正の経緯）。実測ファイルにも当セッションのプロンプト冒頭が入っている。

### 6-13. `recall_rebuild.lock`（コード上定義・実測時は不在）

| 項目 | 内容 |
|---|---|
| 用途 | recall 索引の自動再構築の多重起動防止ロック |
| 書式 | ロックファイル（内容不使用） |
| 書き込む主体 | `.claude/hooks/recall_autorebuild.py:184` `_lock_path()` が返すパスを同 hook が使用（Stop hook） |
| 読み込む主体 | 同 hook のみ |
| 現在の行数・サイズ | **実測時点で不在**（再構築が走っていない時は存在しない。`.claude/.gitignore:38` の `state/*.lock` で除外） |
| 根拠 | `.claude/hooks/recall_autorebuild.py:184`, `.claude/.gitignore:38` |

### 6-14. `.gitkeep`

| 項目 | 内容 |
|---|---|
| 用途 | 空ディレクトリを git 上に保持するためのプレースホルダ |
| 書式 | 空ファイル |
| 書き込む主体 | `c3 init` の配布処理（`src/c3/_excludes.py:73` の `KEEP_PATTERNS` に `memory/archive/.gitkeep` 等が列挙されている） |
| 読み込む主体 | なし（git のみが意味を持つ） |
| 現在の行数・サイズ | 0 B（`.claude/state/.gitkeep` / `.claude/memory/.gitkeep` / `.claude/memory/archive/.gitkeep` の 3 箇所） |
| 根拠 | `src/c3/_excludes.py:73` |

---

## 7. `.claude/memory/`

### 7-1. `sessions/YYYYMMDD.tmp`

| 項目 | 内容 |
|---|---|
| 用途 | 日単位のセッション記録。現在地・運転モード・残タスク・うまくいった/失敗したアプローチ・パターン観測 JSON を保持する。C3 のセッション間引き継ぎの中核 |
| 書式 | Markdown。`現在地:` 行 / `モード:` 行 / `## 残タスク` / `## うまくいったアプローチ` / `## 試みたが失敗したアプローチ` / 末尾に `<!-- C3:SESSION:JSON ... -->` ブロック（`.claude/hooks/stop.py:288` の正規表現でパースされる） |
| 書き込む主体 | (1) 骨格生成・タイムスタンプ・最終応答追記: `.claude/hooks/stop.py:177-187`（`create_session_template` + `_inherit_backlog_from_latest_session`）・`:245`（アトミック書き換え）。パスは `.claude/hooks/stop.py:80` / ディレクトリ定数は `.claude/hooks/session_utils.py:49` `SESSIONS_DIR`。(2) 本文: LLM が skills の指示に従い Write/Edit（`.claude/skills/dev-workflow/SKILL.md:282`・`.claude/skills/extract-lib/SKILL.md:211` `:268`・`.claude/skills/parallel-agents/SKILL.md:95`） |
| 読み込む主体 | `.claude/hooks/restore_session.py:73-78`（SessionStart(compact) hook が最新 `.tmp` を読み復元）／ `.claude/hooks/consolidate_memory.py:4`（過去 7 日分を集約）／ `.claude/hooks/stop.py:116`（前日ファイルからの残タスク引き継ぎ）／ `.claude/hooks/session_mode_watch.py:2`（モード行の監視）／ `.claude/skills/init-session/SKILL.md:54`（Glob で最大日付を Read）／ `.claude/skills/dev-workflow/SKILL.md:357`（自律モード判定）／ `src/c3/recall_index.py:429-433`（recall 索引化・`source_type="session"`）／ `.claude/skills/autonomous-mode/SKILL.md:338`（`grep -m1 '^モード: '`） |
| 現在の行数・サイズ | **21 ファイル / 620,567 B**（`20260716.tmp`〜`20260805.tmp`。最大は `20260804.tmp` 84,712 B） |
| 根拠 | `.claude/hooks/session_utils.py:49`, `.claude/hooks/stop.py:80`, `:177`, `:245`, `:288`, `.claude/hooks/restore_session.py:20`, `:73`, `.claude/hooks/consolidate_memory.py:4`, `src/c3/recall_index.py:433`, `.claude/skills/init-session/SKILL.md:54` |

### 7-2. `patterns.json`

| 項目 | 内容 |
|---|---|
| 用途 | 学習パターンストア。session.tmp の観測 JSON を取り込み、信頼スコアと昇格候補フラグを機械管理する |
| 書式 | JSON。トップレベルキーは `patterns` のみ（実測）。要素キー: `id` / `description` / `registered_date` / `trust_score` / `promotion_candidate` / `observations` / `last_updated` |
| 書き込む主体 | **`.claude/hooks/stop.py` のみが正規経路**（`:25` `PATTERNS_FILE` / `:357` `tempfile.mkstemp` によるアトミック書き換え）。Stop hook から `.claude/hooks/session_stop.py:80` 経由で起動。**LLM の Write/Edit は `.claude/hooks/patterns_guard.py:73`（PreToolUse）が exit 2 でブロック**（`.claude/state/patterns_guard_allow.flag`・TTL 600 秒で一時許可） |
| 読み込む主体 | `.claude/hooks/stop.py:345`（取り込み・trust_score 更新）／ `.claude/hooks/consolidate_memory.py:264` `_load_patterns_readonly()`（`:73` `PATTERNS_PATH`）／ `src/c3/recall_index.py:450` `_collect_patterns_json()`（recall 索引化・`source_type="pattern"`）／ `.claude/skills/promote-pattern/SKILL.md:13`（Read）／ `.claude/skills/init-session/SKILL.md:91`（Read） |
| 現在の行数・サイズ | 6,195 B / **patterns 9 件** |
| 根拠 | `.claude/hooks/stop.py:25`, `:345`, `:357`, `.claude/hooks/patterns_guard.py:67`, `:73`, `.claude/hooks/consolidate_memory.py:73`, `:264`, `src/c3/recall_index.py:450`, `.claude/docs/taxonomy.md:293`, `/CLAUDE.md §10` |
| ガード | 肥大検知は `.claude/hooks/stop.py:33` `DESCRIPTION_WARN_LENGTH`（`:391` で stderr 警告のみ・削除しない） |

### 7-3. `consolidated_summary.md` — `[読み手なし]` `[未文書化]`

| 項目 | 内容 |
|---|---|
| 用途 | 過去 7 日の session.tmp から `## うまくいったアプローチ` / `## 試みたが失敗したアプローチ` を抽出・重複除去して集約したもの。末尾に昇格候補サマリを追記 |
| 書式 | Markdown（人間可読。`.claude/hooks/consolidate_memory.py:364` `:379` に「人間が読む Markdown で機械パースしない」と明記） |
| 書き込む主体 | `.claude/hooks/consolidate_memory.py:53` `OUTPUT_FILE_NAME` / `:338` `build_summary_section()` / `:644`。Stop hook から `.claude/hooks/session_stop.py:87-88` `run_sync(today=today)` で毎回起動 |
| 読み込む主体 | **`[読み手なし]`**。`.claude/hooks/` `.claude/skills/` `.claude/agents/` `.claude/rules/` `src/c3/` `CLAUDE.md` を横断 grep してヒットするのは `src/c3/_excludes.py:35`（**配布除外リスト**）と生成側の `consolidate_memory.py` のみ。`recall_index.py:429-451` の索引対象（`sessions/` / `agent-memory/` / `reports/archive/` / `patterns.json`）にも**含まれない**ため recall からも引けない |
| 現在の行数・サイズ | **27,256 B**（毎 Stop で更新・最終 2026-08-05 13:10） |
| 根拠 | `.claude/hooks/consolidate_memory.py:7`, `:53`, `:338`, `:644`, `.claude/hooks/session_stop.py:88`, `src/c3/_excludes.py:35`, `src/c3/recall_index.py:429-451` |

> **棚卸しの本命**: 毎セッション終了時に 27 KB を再生成しているが、消費者が 1 つも無い。
> `taxonomy.md` の `memory/` 節にも記載が無い（`[未文書化]`）。

### 7-4. `promotion-candidates.md` — `[読み手なし]` `[未文書化]`

| 項目 | 内容 |
|---|---|
| 用途 | `patterns.json` のうち `promotion_candidate=true` かつ `promoted!=true` のパターン一覧（Markdown 表 + 詳細セクション） |
| 書式 | Markdown。**毎回上書き**（`.claude/hooks/consolidate_memory.py:416` `:482`）。`|` / backtick をエスケープ |
| 書き込む主体 | `.claude/hooks/consolidate_memory.py:56` `PROMOTION_CANDIDATES_FILE_NAME` / `:416` `write_promotion_candidates_log()` / `:645`。Stop hook 経由 |
| 読み込む主体 | **`[読み手なし]`**。grep ヒットは `src/c3/_excludes.py:36`（配布除外）と生成側のみ。昇格を実際に行う `.claude/skills/promote-pattern/SKILL.md:13` は **`patterns.json` を直接 Read** しており、本ファイルは経由しない |
| 現在の行数・サイズ | 271 B（最終 2026-08-05 13:10） |
| 根拠 | `.claude/hooks/consolidate_memory.py:56`, `:416`, `:482`, `:645`, `src/c3/_excludes.py:36`, `.claude/skills/promote-pattern/SKILL.md:13` |

### 7-5. `archive/YYYYMMDD.tmp` — `[読み手なし]` `[未文書化]`

| 項目 | 内容 |
|---|---|
| 用途 | TTL（既定 21 日）を過ぎた session.tmp の退避先 |
| 書式 | `sessions/*.tmp` と同じ Markdown。同名衝突時は `YYYYMMDD-{N}.tmp`（N=1..1000） |
| 書き込む主体 | `.claude/hooks/consolidate_memory.py:529` `archive_old_sessions()`（`shutil.move`）／ TTL は `:50` `DEFAULT_ARCHIVE_TTL_DAYS = DEFAULT_WINDOW_DAYS * 3` = 21 日、`:597` の環境変数 `C3_CONSOLIDATE_ARCHIVE_TTL_DAYS` で上書き可 |
| 読み込む主体 | **`[読み手なし]`**。`src/c3/recall_index.py:429-433` が索引化するのは `.claude/memory/sessions` のみで `archive/` は対象外。`.claude/skills/dev-workflow/SKILL.md:357` も「過去ファイル・archive は遡らない」と明示。grep ヒットは `src/c3/_excludes.py:32` `:73`（配布除外）のみ |
| 現在の行数・サイズ | **73 ファイル / 959,059 B**（`20260427.tmp`〜。最大 `20260525.tmp` 64,003 B） |
| 根拠 | `.claude/hooks/consolidate_memory.py:50`, `:526`, `:529`, `:597`, `src/c3/recall_index.py:429-433`, `.claude/skills/dev-workflow/SKILL.md:357`, `src/c3/_excludes.py:32` |

> 棚卸し所見: 959 KB の過去セッション記録が、archive へ移った瞬間に **recall からも skill からも
> 到達不能**になる。「保存しているが誰も読まない」状態。`taxonomy.md` にも記載が無い（`[未文書化]`）。

### 7-6. `.gitkeep`

§6-14 と同一。`.claude/memory/.gitkeep`（0 B）と `.claude/memory/archive/.gitkeep`（0 B）。

---

## 8. `.claude/agent-memory/`

### 8-1. 共通仕様

| 項目 | 内容 |
|---|---|
| 用途 | agent ごとの永続メモリ。判断基準・許容例外・再発パターンを蓄積し、次回起動時に自動注入される |
| スキーマ / 書式 | `agent-memory/<agent-name>/MEMORY.md`（索引・自動注入対象）+ 任意個数のトピックファイル `<prefix>_<topic>.md`（`MEMORY.md` から `[title](file.md)` でリンク）。実測 prefix: `feedback_` / `patterns_` / `pattern_` / `project_` / `exemption(s)`。`tester` のみ `topics/` サブディレクトリを使用 |
| 書き込む主体 | 各 agent 自身（Write/Edit ツール）。指示は agent 定義に逐語で存在: `.claude/agents/code-reviewer.md:22` / `design-critic.md:21` / `developer.md:23` / `security-reviewer.md:22` / `systematic-debugger.md:22` / `tester.md:21` / `wt_developer.md:28` / `wt_systematic-debugger.md:27` / `wt_tester.md:26` |
| 読み込む主体 | (1) Claude Code ハーネス — agent frontmatter の `memory: project`（`.claude/agents/code-reviewer.md:4` 等）により `MEMORY.md` が起動時に自動注入される。(2) `src/c3/recall_index.py:435-441`（`**/*.md` を索引化・`source_type="agent-memory"`）。(3) `.claude/hooks/recall_inject.py:177` / `.claude/hooks/recall_autorebuild.py:56`（stale 判定の監視対象）。(4) `.claude/hooks/stop.py:399` `:439`（`MEMORY.md` が注入予算の 80% に達したら stderr 警告） |
| 現在の行数・サイズ | **7 ディレクトリ / 263 ファイル / 1,070,550 B** |
| git 管理 | tracked（`.claude/.gitignore` で除外していない。`.claude/docs/config-policy.md:88` が「引き継ぎ資産」として明記） |
| 配布 | wheel には含めない（`.claude/docs/taxonomy.md:77`） |
| 根拠 | `.claude/agents/code-reviewer.md:4`, `:22`, `src/c3/recall_index.py:435`, `.claude/hooks/stop.py:43`, `:399`, `:439`, `.claude/docs/taxonomy.md:60-62`, `:77` |

### 8-2. ディレクトリ別実測

| ディレクトリ | ファイル数 | サイズ | 最大ファイル | 備考 |
|---|---|---|---|---|
| `code-reviewer/` | 21 | 223,712 B | `patterns_misc_process_review.md` 44,076 B | `MEMORY.md` 27,579 B |
| `design-critic/` | 5 | 145,810 B | `feedback_recycle_audit_focus_on_fix_induced_defects.md` **128,614 B** | 1 ファイルに 88% が集中 |
| `developer/` | 31 | 61,247 B | — | 小粒多数 |
| `security-reviewer/` | **172** | 414,693 B | — | 全体の 65% のファイル数。`pattern_*` が大半 |
| `tester/` | 34 | 225,088 B | — | `topics/` サブディレクトリ構造を採用（唯一） |
| `wt_developer/` | **0** | 0 B | — | **`[書き手なし]`**（後述） |
| `wt_tester/` | **0** | 0 B | — | **`[書き手なし]`**（後述） |

### 8-3. `wt_developer/` `wt_tester/` — `[書き手なし]`

| 項目 | 内容 |
|---|---|
| 用途 | （設計意図）worktree 並列実行用 agent の永続メモリ |
| 書き込む主体 | **実測 0 ファイル**（`find .claude/agent-memory -type f` で 1 件もヒットしない）。ディレクトリだけが 2026-05-15 / 05-16 に作られて以後空のまま。agent 定義（`.claude/agents/wt_developer.md:28` / `wt_tester.md:26`）は「MEMORY.md に追記する」と指示しているが、実際に書かれた形跡が無い |
| 読み込む主体 | 同上（読む対象が無い） |
| 現在の行数・サイズ | 0 ファイル / 0 B |
| 根拠 | `.claude/agents/wt_developer.md:28`, `.claude/agents/wt_tester.md:26`（指示の存在）／ `find` の実測（ファイル 0 件） |

> 関連する `[未文書化]` の所見: `.claude/agents/systematic-debugger.md:22` と
> `wt_systematic-debugger.md:27` も同様に書き込みを指示しているが、
> **`.claude/agent-memory/systematic-debugger/` と `wt_systematic-debugger/` はディレクトリ自体が存在しない**。
> 「agent 定義に書け」と書いてあるのに実体が無い agent が 4 つ（wt_developer / wt_tester /
> systematic-debugger / wt_systematic-debugger）ある。作り直しの際、
> agent 定義と agent-memory の対応関係は機械検査の候補になる。

---

## 9. 未確認の項目

以下は本監査で断定に至らなかった。作り直しの設計時には別途確認が要る。

1. **`stop_exit2_test.flag` の本来の意図** — 名称から Stop hook の `exit 2` 試験用と推測されるが、
   コード・文書・CHANGELOG のいずれにも定義が無く、生成した経緯を特定できなかった。
   削除可否を断定するには `.dev/tests/test_run_loop_pure.py:5113` の実行結果確認が要る（本監査ではテスト未実行）。
2. **`agent_runs` の削除影響** — `tests/test_migrate.py:208` `:218-219` が
   「`agent_runs` テーブルと 2 つの INDEX が存在すること」を全件チェックしている。
   テーブルを撤去する migration を書く場合、これらのテストが赤になる。テスト未実行のため影響範囲は未検証。
3. **`security_audit_exceptions.json` の運用実態** — コードからの読み手が無いことは確定したが、
   人間が定期的に見返しているかは判断できない（最終更新 2026-05-21 という事実のみ）。
4. **`consolidated_summary.md` / `promotion-candidates.md` の LLM による偶発的な読み取り** —
   コード・skill・agent 定義からの明示的な読み取り経路が無いことは確定した。ただし LLM が
   Glob/Read で `.claude/memory/` を探索した際に偶発的に読む可能性は排除できない（設計上の経路ではない）。
5. **`recall.hnsw` / `recall_meta.json` の内部レイアウト** — バイナリ／巨大 JSON のため
   トップレベル構造（`model` / `dim` / `chunks` 等）のみ確認し、`chunks` 要素のスキーマ全体は未検証。
6. **WAL サイドカーの通常時の存在有無** — 監査開始時点では `c3.db-wal` / `c3.db-shm` は存在せず、
   監査中の read-only 接続で生成された。通常運用（hook 実行中）でどのタイミングで残存するかは未確認。
7. **`.claude/logs/` / `.claude/worktrees/` / `.claude/tmp/`** — `.claude/.gitignore:53-56` で
   除外されている実行時領域だが、本監査の対象外（指示の 6 項目に含まれない）のため未調査。
8. **配布側テンプレート（`src/c3/_template/.claude/`）の state/memory 初期状態** — 本監査は
   配布元の実データのみを対象にした。`c3 init` 直後の利用先で何が生成されるかは未確認。
