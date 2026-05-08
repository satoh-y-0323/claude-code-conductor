---
description: PO（Parallel Orchestra）並列処理の現在状況を c3.db.po_status から DuckDB 経由で取得する skill。実行中 / 完了 / 失敗の各 worktree をリアルタイム可視化する。
disable-model-invocation: false
---

# po-status

PO 経由で起動した並列処理の現在状況（実行中・完了・失敗）を確認する skill。
F-003 で `runner.py` の heartbeat スレッドが 30 秒ごとに `c3.db.po_status` テーブルを
UPSERT しているので、本 skill は読み取り専用で SQL を発行するだけ。

---

## Step 1: 確認したい範囲を選択する

AskUserQuestion ツール:

```json
{
  "questions": [{
    "question": "PO の状況をどう見たいですか？",
    "header": "範囲選択",
    "options": [
      { "label": "直近の active タスク", "description": "過去 5 分以内に heartbeat があった worktree（実行中の可能性が高い）" },
      { "label": "特定 session の全状態", "description": "session_id を指定して、その実行の全タスクを表示" },
      { "label": "stale タスク検出", "description": "90 秒以上 heartbeat が無い実行中タスク（hung の疑い）" },
      { "label": "全履歴サマリ", "description": "全 session の最新状態を最大 100 件表示" }
    ]
  }]
}
```

---

## Step 2: 選択に応じて DuckDB で SELECT する

### 共通: DuckDB ATTACH の準備

Bash ツールで以下を実行する。`.claude/state/c3.db` が存在することが前提（F-009 の `init_c3_db.py` が SessionStart で初期化）。

DuckDB を直接呼ぶ:
```bash
duckdb -c "INSTALL sqlite; LOAD sqlite; \
  ATTACH '.claude/state/c3.db' AS c3 (TYPE sqlite); \
  {SELECT 文}"
```

### 「直近の active タスク」の場合

```bash
duckdb -c "INSTALL sqlite; LOAD sqlite; \
  ATTACH '.claude/state/c3.db' AS c3 (TYPE sqlite); \
  SELECT session_id, worktree_id, state, current_step, last_heartbeat \
  FROM c3.po_status \
  WHERE last_heartbeat > strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-5 minutes')) \
  ORDER BY last_heartbeat DESC;"
```

### 「特定 session の全状態」の場合

AskUserQuestion で session_id を確認する:
```json
{
  "questions": [{
    "question": "対象の session_id を入力してください（例: my-manifest_20260509T120000Z）",
    "header": "session_id"
  }]
}
```

入力された session_id を `${session_id}` として:
```bash
duckdb -c "INSTALL sqlite; LOAD sqlite; \
  ATTACH '.claude/state/c3.db' AS c3 (TYPE sqlite); \
  SELECT worktree_id, state, current_step, progress_pct, last_heartbeat \
  FROM c3.po_status \
  WHERE session_id = '${session_id}' \
  ORDER BY worktree_id;"
```

### 「stale タスク検出」の場合

90 秒以上 heartbeat が無い running 状態の worktree を表示:
```bash
duckdb -c "INSTALL sqlite; LOAD sqlite; \
  ATTACH '.claude/state/c3.db' AS c3 (TYPE sqlite); \
  SELECT session_id, worktree_id, state, current_step, last_heartbeat, \
         (julianday('now') - julianday(last_heartbeat)) * 86400 AS sec_since_heartbeat \
  FROM c3.po_status \
  WHERE state IN ('starting', 'running') \
    AND (julianday('now') - julianday(last_heartbeat)) * 86400 > 90 \
  ORDER BY sec_since_heartbeat DESC;"
```

heartbeat スレッドは 30 秒間隔なので、90 秒経過は「3 サイクル分応答なし」を意味し、PO がハング・タイムアウトしている可能性を示す。

### 「全履歴サマリ」の場合

```bash
duckdb -c "INSTALL sqlite; LOAD sqlite; \
  ATTACH '.claude/state/c3.db' AS c3 (TYPE sqlite); \
  SELECT session_id, worktree_id, state, last_heartbeat \
  FROM c3.po_status \
  ORDER BY last_heartbeat DESC \
  LIMIT 100;"
```

---

## Step 3: 結果を整理して提示する

DuckDB の出力結果をそのまま貼るのではなく、以下の形式で整理する:

```markdown
## PO 状況（{現在時刻}）

### 実行中（{件数}）
| session | worktree | current_step | 経過 |
|---|---|---|---|
| {short_session} | {worktree_id} | {current_step} | {sec_since_heartbeat}s |

### 完了（{件数}）
（同様）

### 失敗（{件数}）
（同様。state='failed' のもののみ。詳細調査が必要なら po_results テーブルの error_message も参照する）
```

**stale タスクが検出された場合は警告を強調する**: `⚠ {N} 件の stale タスク（90s+ heartbeat 無し）`。

---

## Step 4: 追加調査の選択

stale や failed があれば、追加で AskUserQuestion で深掘りを提案する:

```json
{
  "questions": [{
    "question": "追加で調査しますか？",
    "header": "追加調査",
    "options": [
      { "label": "po_results の error_message を見る", "description": "失敗タスクの詳細メッセージを取得" },
      { "label": "agent-runs.jsonl を確認", "description": "subagent_log の JSONL ログから関連エントリを探す" },
      { "label": "現状で十分", "description": "状況把握ができたので終了" }
    ]
  }]
}
```

---

## 補足: c3.db が存在しない / 空の場合

- `.claude/state/c3.db` が無ければ `duckdb` の `ATTACH` がエラーになる。その場合はユーザーに `init_c3_db.py` 未実行か、c3 利用先で SessionStart が走っていない可能性を伝える。
- `po_status` テーブルが空の場合は「PO がまだ実行されていないか、heartbeat が走る前にすぐ完了した」可能性。`po_results`（F-002）も合わせて確認する。
