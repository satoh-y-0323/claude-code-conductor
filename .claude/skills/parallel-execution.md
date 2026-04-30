# Parallel Execution (parallel-orchestra)

`/develop` の **D-0** で「PO 並列実行」が選ばれたときに Read される手順。
parallel-orchestra (PO) を subprocess で起動し、plan-report を YAML フロントマター付きマニフェストとして並列実行する。

---

## 前提条件

- `.claude/reports/plan-report-*.md` の最新ファイルに YAML フロントマター（`po_plan_version: "0.1"` を含む）が付いていること
- `claude-code-conductor` がインストール済みで `c3` コマンドが PATH 上にあること

---

## Step 0: PO の利用可否を確認する

Bash ツールで以下を実行する:

```
c3 doctor --check po-only --quiet
```

- 終了コード `0`（出力なし）= PO 利用可能 → Step 1 へ
- 終了コード `1` = PO 未インストール → 以下の案内文を**そのまま提示**してこのスキルを終了する:

> 並列実行を使うには `pip install parallel-orchestra` を実行してください。
> 詳細: https://pypi.org/project/parallel-orchestra/

**注意:** 案内はエラーではなく **情報メッセージ**として出すこと。`/develop` 全体は失敗扱いにせず、ユーザーに「逐次実行（D-0 の選択肢 [A]）に切り替える」または「PO をインストールして再実行する」のいずれかを選んでもらう。

---

## Step 1: マニフェストの妥当性確認

1. Glob で `.claude/reports/plan-report-*.md` の最新ファイルパスを取得
2. Read で内容（フロントマター含む）を確認
3. Bash で以下を実行:

```
c3 po dry-run <plan-report-path>
```

- 終了コード `0` = マニフェスト妥当 → Step 2 へ
- 終了コード `2` = マニフェストエラー（フィールド欠損・agent 不在等）→ stderr のメッセージを整形してユーザーに提示し、`/start` の **フェーズ C（計画）** を再実行するか手動で plan-report を編集するよう案内してこのスキルを終了する
- 終了コード `1` = PO 未インストール（Step 0 をすり抜けた場合のフォールバック）→ Step 0 と同じ案内文を出して終了

---

## Step 2: ユーザー承認

承認なしで先に進まない。並列実行は git worktree を作って auto-commit するため副作用が大きい。

1. 親 Claude が plan-report の **フロントマター** を Read で再パースし、以下を要約してテキストで提示:
   - タスク総数
   - うち `read_only: false` のタスク数（= 作成される worktree 数）
   - 各タスクの `id` / `agent` / `writes`（あれば）の表
2. AskUserQuestion ツールで実行可否を確認:

```json
{
  "questions": [{
    "question": "並列実行を開始してよいですか？",
    "options": [
      { "label": "承認", "description": "parallel-orchestra で実行を開始する" },
      { "label": "max_workers を変更して承認", "description": "次の入力で並列度を指定する（デフォルトは PO 側で 3）" },
      { "label": "キャンセル", "description": "並列実行を中止する。/develop の D-0 から逐次実行に切り替えられる" }
    ]
  }]
}
```

- 「max_workers を変更して承認」が選ばれた場合、追加 AskUserQuestion で並列度（整数）を聞く
- 「キャンセル」の場合、セッションファイルに `## 試みたが失敗したアプローチ` として理由を追記してスキル終了

---

## Step 3: parallel-orchestra を起動する

Bash で以下を実行:

```
c3 po run <plan-report-path> --report .claude/reports/po-run-report-{timestamp}.json [--max-workers N]
```

- `{timestamp}` は `YYYYMMDD-HHMMSS` 形式。Bash で `python -c "from datetime import datetime; print(datetime.now().strftime('%Y%m%d-%H%M%S'))"` を実行して取得
- `--max-workers` は Step 2 でユーザーが指定した場合のみ付与
- 進捗ダッシュボードはターミナルに直接出力される（親 Claude のコンテキストには末尾の状態のみ流れる）

終了コードと意味:

| exit code | 意味 | 次のアクション |
|---|---|---|
| `0` | 全タスク成功 | Step 4 へ |
| `1` | 1件以上のタスクが失敗 | Step 4 へ（失敗タスクの再実行案内を出す） |
| `2` | マニフェストエラー（Step 1 をすり抜けた） | エラー内容を提示し計画フェーズへ戻るよう案内してスキル終了 |
| `3` | runner エラー（claude バイナリ不在等） | `c3 doctor` の結果を提示してスキル終了 |

---

## Step 4: レポートの要約とセッション更新

1. Step 3 で生成された `.claude/reports/po-run-report-{timestamp}.json` を Read
2. 親 Claude がタスクごとのステータスを Markdown 表に整形してユーザーに提示する:
   - 列: `id` / `agent` / `status` / `worktree` / `duration` / 失敗時は `last_error_summary`
3. 失敗タスクがある場合、該当タスクのみ `/develop` の D-A 経路（逐次実行）で再実行する選択肢を AskUserQuestion で提示:

```json
{
  "questions": [{
    "question": "失敗タスクをどうしますか？",
    "options": [
      { "label": "逐次実行で再修正する", "description": "失敗タスクのみ /develop の D-A（TDD 逐次）で再実行する" },
      { "label": "計画フェーズへ戻る", "description": "失敗の原因が設計レベルなら /start のフェーズ C で再計画する" },
      { "label": "ここで終わる", "description": "現状で停止し、フェーズ E（レビュー）には進まない" }
    ]
  }]
}
```

4. セッションファイル（`.claude/memory/sessions/YYYYMMDD.tmp`）の `- [ ] PO 並列実行` を `- [x]` に Edit する。失敗タスクが残った場合は `- [ ]` のままにし、失敗内容を `## 試みたが失敗したアプローチ` に追記する
5. 全タスク成功した場合のみ、フェーズ E（レビュー）へ進む案内を出す
