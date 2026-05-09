# PO Worktree Writes (F-002 Phase 2)

PO（Parallel Orchestra）の worktree 内で動く子 Claude プロセスから、親リポの
`.claude/state/c3.db` に直接書き込むための配管仕様。

## 概要

F-002 Phase 1 では「親 Claude の `runner.py` が完了後にまとめて INSERT」する
形で集約レイヤを SQLite 化した。Phase 2 では以下が worktree 内から直接書ける
ようになっている：

| 書き込み対象 | 入口 | 動作モード |
|---|---|---|
| `po_status` (heartbeat) | `python .claude/hooks/po_heartbeat.py --state running ...` | 子 Claude が任意のタイミングで明示呼び出し |
| `po_status` (Subagent 自動連動) | `subagent_log.py` フック (SubagentStart / Stop) | 環境変数あり時のみ自動 UPSERT |
| `review_decisions` | `python .claude/hooks/record_review_decision.py --decision ...` | dev-workflow フェーズ E から呼ばれる |
| `tier_outcome` | `python .claude/hooks/record_tier_outcome.py --outcome ...` | 親 Claude のフェーズ E-2 専用（worktree 内では tier_selection.json が無いため no-op） |
| `po_results` | `runner.py` の `record_task_results()` | 親 Claude が完了後に一括 INSERT（Phase 1 から変更なし） |

## 環境変数仕様

`runner.py` の `_execute_task` が subprocess.Popen 起動時に以下 4 変数を子の env に注入する。
`po_session_id` 引数を受けたときのみ注入される（後方互換のため）。

| 変数名 | 値 | read_only:true | write task |
|---|---|---|---|
| `C3_PO_DB_PATH` | 親リポの `.claude/state/c3.db` 絶対パス | 設定（DB 存在時） | 設定（DB 存在時） |
| `C3_PO_SESSION_ID` | `f"{manifest_name}_{YYYYmmddTHHMMSSZ}"` | 設定 | 設定 |
| `C3_PO_TASK_ID` | task.id | 設定 | 設定 |
| `C3_PO_WORKTREE_ID` | `"(read-only)"` または ブランチ名 | `"(read-only)"` | `parallel-orchestra/<task-id>-<uuid8>` |
| `PO_WORKTREE_GUARD` | `"1"` | 未設定 | 設定（既存 Phase 1 から） |

`C3_PO_DB_PATH` は親リポ起点で `locate_c3_db()` が見つけたパスのみ注入される。
DB 不在環境（C3 利用先で `init_c3_db.py` がまだ走っていない等）では未設定。

## locate_c3_db の解決順

`parallel_orchestra.c3_db.locate_c3_db(start)` は以下の順で DB パスを解決する。

1. 環境変数 `C3_PO_DB_PATH` が有効ファイルを指していればそれを返す
2. 起点ディレクトリ `start` から親方向に `.claude/state/c3.db` を探索
3. 見つからなければ `None`

Phase 2 以降、worktree 内の子プロセスは (1) 経由で親リポの DB を直接引ける。

## po_heartbeat.py の使い方

子 Claude が dev-workflow の任意のタイミングで自身の進捗を報告するための CLI。
`Bash(python .claude/hooks/po_heartbeat.py*)` を `.claude/settings.local.json` の
allow リストに追加しておくと検証が楽。

```pwsh
# 進行中
python .claude/hooks/po_heartbeat.py --state running --step "Wave 2 - tester" --progress 50

# 完了
python .claude/hooks/po_heartbeat.py --state completed --step "all green"

# 失敗
python .claude/hooks/po_heartbeat.py --state failed --step "tester reported regressions"
```

`--state` の値: `starting` | `running` | `completed` | `failed`

環境変数 `C3_PO_SESSION_ID` / `C3_PO_WORKTREE_ID` が無いときはフェイルセーフで
exit 0（PO 経由でない単独実行で誤って呼ばれてもクラッシュしない）。

## subagent_log.py の挙動変更点

worktree 内で subagent (例: tdd-develop の中で起動した tester) が走った場合、
SubagentStart / SubagentStop イベントで自動的に `po_status` を UPSERT する。

| イベント | UPSERT する state |
|---|---|
| SubagentStart | `running` |
| SubagentStop (status='success') | `completed` |
| SubagentStop (status≠'success') | `failed` |

ただし `C3_PO_WORKTREE_ID` + `C3_PO_SESSION_ID` の **両方** が設定されている
ときのみ動作する。親 Claude セッション（環境変数なし）では完全 no-op で
副作用ゼロ。

`current_step` には `payload.agent_type` または `agent_id` を 200 文字で切り
詰めて記録する（DB 容量保護のため）。

## terminal state 保護

`upsert_po_status` の SQL は `completed` / `failed` 状態の行を保護する。
親 heartbeat スレッドと worktree 内子プロセスからの heartbeat が競合しても、
子が completed を書いた直後に親が running で逆行上書きすることを防ぐ。

```sql
ON CONFLICT(session_id, worktree_id) DO UPDATE SET
  state = CASE
    WHEN po_status.state IN ('completed', 'failed') THEN po_status.state
    ELSE excluded.state
  END,
  current_step = excluded.current_step,
  progress_pct = excluded.progress_pct,
  last_heartbeat = excluded.last_heartbeat
```

`current_step` / `progress_pct` / `last_heartbeat` は terminal state 後でも
更新される（最終時点の情報は反映されてよい）。

## 並列書き込み耐性

`c3_db.py` の全 write 関数 + 主要 read 関数で `PRAGMA busy_timeout=5000`
を冪等適用しているため、5 worker × 子 heartbeat 1Hz 程度の競合では DB
ロック衝突が発生しても 5 秒以内にリトライで通過する想定。

それ以上の極端な並列度を想定する場合は `_BUSY_TIMEOUT_MS` 定数を引き上げる。

## hook 別の worktree 内動作

| hook | worktree 内動作 | 備考 |
|---|---|---|
| `record_review_decision.py` | ✅ 動作 | env 経由で親リポ c3.db に書く |
| `review_hint_inject.py` | ✅ 動作 | レポートパスを引数で受け取る |
| `record_tier_outcome.py` | ⚠️ no-op | tier_selection.json は親リポにあるため worktree 内では見えない（設計通り） |
| `select_tier.py` | N/A | 親 Claude の UserPromptSubmit hook 専用 |
| `po_heartbeat.py` | ✅ 動作 | worktree 専用に新規作成 |
| `subagent_log.py` | ✅ 動作 | C3_PO_WORKTREE_ID 設定時のみ po_status UPSERT |

`record_tier_outcome.py` は親 Claude の dev-workflow フェーズ E-2 専用。
worktree 内 dev-workflow から呼んでも `tier_selection.json` が見つからず exit 0
で終わる（意図通り）。

## 関連リスク

- **重複書き込み**: 親 heartbeat と子 heartbeat が同じ (session_id, worktree_id)
  に対して並列 UPSERT する。冪等で衝突しないが、極端な並列で writer が詰まる
  可能性は busy_timeout で 5 秒緩和。
- **環境変数の孫プロセス継承**: `C3_PO_*` は子プロセスがさらに spawn した
  孫プロセスにも継承される。ネスト PO は現状非対応のため、孫プロセスが
  古い session_id を見て混線する可能性がある。`po_heartbeat.py` は env 不在時
  warning を出す。
- **read_only タスクからの DB 書き込み**: review_decisions は read-only エージェント
  (code-reviewer 等) も書く想定。worktree_guard は file Write/Edit のみブロック
  するので Bash 経由 sqlite3 書き込みは想定通り通過する。

## テスト

- `tests/parallel_orchestra/test_po_worktree_writes.py`: env 注入 / po_heartbeat CLI / subagent_log fold-in
- `tests/parallel_orchestra/test_po_status_visibility.py`: terminal state 保護
- `tests/parallel_orchestra/test_po_results_recording.py`: locate_c3_db env-aware

## 関連コミット

- F-002 Phase 1: `7b3ede7` (v1.1.0 リリースコミットに含む)
- F-002 Phase 2-A: `1ea6b7f` env 注入と locate_c3_db env-aware
- F-002 Phase 2-B: `c37e576` po_heartbeat CLI + subagent_log fold-in + terminal state 保護
- F-002 Phase 2-C: 本ドキュメント追加 + 動作確認
