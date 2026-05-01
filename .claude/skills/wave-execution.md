# Wave Execution

`/develop` のフェーズ D で plan-report に YAML フロントマター（`po_plan_version: "0.1"`）が含まれるときに Read される手順。

C3 の親 Claude が plan-report の DAG を **wave 単位**で歩く。
- 単独 wave（タスク 1 件） → C3 が直接実行（Agent ツール起動 or ペルソナ採用）
- 複数 wave（タスク 2 件以上） → parallel-orchestra（PO）にスポット並列委譲

各 wave 完了後にユーザーの承認を取る。

---

## 前提条件

- `claude-code-conductor` がインストール済みで `c3` コマンドが PATH 上にあること
- plan-report が `.claude/reports/plan-report-*.md` の形式で配置され、YAML フロントマターを持つこと（フロントマターが無ければ `dev-workflow.md` の D-1〜D-5 ceremony へフォールバック）

---

## Step 0: 妥当性チェック

1. Glob で `.claude/reports/plan-report-*.md` の最新ファイルパスを取得する
2. Read で内容（フロントマター含む）を確認する
3. Bash で以下を実行する:

   ```
   c3 po dry-run <plan-report-path>
   ```

   - 終了コード `0` = マニフェスト妥当 → Step 1 へ
   - 終了コード `2` = マニフェストエラー（フィールド欠損・agent 不在等）→ stderr のメッセージを整形してユーザーに提示し、`/start` のフェーズ C（計画）を再実行するか手動で plan-report を編集するよう案内してこのスキルを終了する
   - 終了コード `1` = PO 未インストール → 以下の案内文を**そのまま提示**してこのスキルを終了する:

     > 並列実行を使うには `pip install parallel-orchestra` を実行してください。
     > 詳細: https://pypi.org/project/parallel-orchestra/

     親 Claude は逐次実行（`dev-workflow.md` の D-1〜D-5）に切り替えるか、PO をインストールして `/develop` を再実行するかをユーザーに選んでもらう。

---

## Step 1: wave 分解

Bash で以下を実行する:

```
c3 po waves <plan-report-path>
```

stdout の JSON は以下の形式になる:

```json
{
  "waves": [
    {
      "index": 0,
      "tasks": [
        { "id": "tdd-login", "agent": "tdd-develop", "read_only": false, "writes": ["src/auth/login.py"], "prompt": "..." },
        { "id": "tdd-logout", "agent": "tdd-develop", "read_only": false, "writes": ["src/auth/logout.py"], "prompt": "..." }
      ]
    },
    {
      "index": 1,
      "tasks": [
        { "id": "review-auth", "agent": "code-reviewer", "read_only": true, "writes": [], "prompt": "..." }
      ]
    }
  ]
}
```

- 終了コード `0` = 分解成功 → 親 Claude が JSON をパースして `waves` 配列を取得し Step 2 へ
- 終了コード `2` = フロントマター不正・循環依存等 → stderr メッセージを提示して終了

セッションファイル（`.claude/memory/sessions/YYYYMMDD.tmp`）に以下を追記する（未登録の場合のみ）:
- 各 wave につき `- [ ] Wave {N} ({task_count} tasks)` を 1 行ずつ

---

## Step 2: wave ごとに実行する

`waves` 配列を index 順にループする。各 wave で以下を順に行う。

### 2-A: wave 内容を提示する

親 Claude が wave のタスク一覧を Markdown 表で提示する:

| id | agent | read_only | writes |
|---|---|---|---|
| tdd-login | tdd-develop | false | src/auth/login.py |
| tdd-logout | tdd-develop | false | src/auth/logout.py |

### 2-B: 実行可否をユーザーに確認する

AskUserQuestion ツール:

```json
{
  "questions": [{
    "question": "Wave {N} を実行してよいですか？",
    "options": [
      { "label": "承認・進む", "description": "この wave を実行する" },
      { "label": "中断", "description": "ここで wave 実行を停止する。完了済みの wave はそのまま残る" }
    ]
  }]
}
```

「中断」の場合、セッションファイルの `## 試みたが失敗したアプローチ` に中断理由を追記してスキル終了。

### 2-C: ランナーを選んで実行する

`len(wave.tasks)` で分岐する。

#### case A: 単独 wave（タスク 1 件）

タスクの `agent` フィールドで更に分岐する。

##### A-1: `agent == "tdd-develop"` のとき

**ペルソナ採用パターン**で実行する。Agent ツールで起動するとサブエージェント depth 1 制限により内部の tester/developer サブエージェントが spawn できないため、親 Claude が直接 tdd-develop ペルソナで動く。

1. `.claude/agents/tdd-develop.md` を Read してペルソナを採用する
2. `.claude/skills/worktree-tdd-workflow.md` を Read して TDD ループ手順（tester→developer→tester）を取得する
3. タスクの `prompt` を実装内容として、worktree-tdd-workflow.md の Step 1〜4 を **親 Claude が直接** 実行する。tester / developer は Agent ツールでスポーン可能（depth 1 で完結する）
4. ループ完了後、結果を 2-D の集約に渡す

##### A-2: `agent` がそれ以外（`code-reviewer` / `security-reviewer` / `developer` / `tester` 等）

**Agent ツール起動パターン**で実行する。これらの agent は内部で更に subagent を spawn しないため depth 1 制限に抵触しない。

1. Agent ツールで `subagent_type` は指定せず（カスタム agent は subagent_type 不可）、プロンプトに以下を含める:
   - 「`.claude/agents/{agent_name}.md` を Read してペルソナを採用すること」
   - タスクの `prompt` 本文
2. Agent の返答を 2-D の集約に渡す

##### 将来 agent を追加する場合

「内部で更に subagent を呼ぶ agent」を新たに作る場合は、A-1 のペルソナ採用パターン側に追加する。判断基準: その agent の定義ファイルが「内部で Agent ツールを使う」前提になっているか。tdd-develop が現状の唯一の例。

#### case B: 複数 wave（タスク 2 件以上）

**PO スポット委譲**で実行する。

Bash で以下を実行する:

```
c3 po run-wave <plan-report-path> --wave-index {N} --report .claude/reports/po-run-report-wave-{N}-{ts}.json
```

- `{N}` は wave の index
- `{ts}` は `YYYYMMDD-HHMMSS` 形式。Bash で `python -c "from datetime import datetime; print(datetime.now().strftime('%Y%m%d-%H%M%S'))"` を実行して取得
- `c3 po run-wave` は `.claude/tmp/po-manifest-wave-{N}-{ts}.md` に ephemeral マニフェストを生成し、PO に subprocess 委譲する。各タスクは PO が `claude -p --agent <name>` を独立 Claude セッション（depth 0）として起動するため、tdd-develop も内部で tester/developer を spawn できる

終了コードと意味:

| exit code | 意味 | 次のアクション |
|---|---|---|
| `0` | wave 内全タスク成功 | 2-D へ |
| `1` | 1件以上のタスクが失敗 | 2-D へ（失敗一覧を含めて提示） |
| `2` | マニフェストエラー（Step 0 をすり抜けた）| エラー内容を提示しスキル終了 |
| `3` | runner エラー（claude バイナリ不在等）| `c3 doctor` の結果を提示してスキル終了 |

実行完了後、生成された `po-run-report-wave-{N}-{ts}.json` を Read してタスクごとのステータスを取得する。

### 2-D: 結果を集約してユーザーに提示する

ランナー種別ごとに以下の形式で結果を提示する。

**case A（単独 wave）:**
- A-1: 親 Claude の TDD ループ結果（tester/developer の Agent 返答の要約、最終 tester の合否）
- A-2: Agent の返答テキスト（必要なら要約）

**case B（複数 wave / PO 委譲）:**
- `po-run-report-wave-{N}-*.json` を Markdown 表に整形:

  | id | agent | status | worktree | duration | last_error_summary |
  |---|---|---|---|---|---|
  | tdd-login | tdd-develop | success | wt-tdd-login | 124s | - |
  | tdd-logout | tdd-develop | failure | wt-tdd-logout | 89s | TestLogout::test_csrf failed |

### 2-E: 失敗があったら方針を確認する

case A で TDD ループが不合格のまま終わった、または case B で 1 件以上失敗した場合、AskUserQuestion で次を確認する:

```json
{
  "questions": [{
    "question": "Wave {N} に失敗があります。どうしますか？",
    "options": [
      { "label": "リトライ", "description": "同じ wave をもう一度実行する" },
      { "label": "スキップして次の wave へ", "description": "失敗を残したまま次の wave に進む" },
      { "label": "中断", "description": "ここで wave 実行を停止する。完了済みの wave はそのまま残る" }
    ]
  }]
}
```

- 「リトライ」 → 2-A から同じ wave を再実行
- 「スキップして次の wave へ」 → 失敗内容をセッションファイルの `## 試みたが失敗したアプローチ` に追記して次の wave へ
- 「中断」 → セッションファイルの該当 wave 行は `- [ ]` のままにしてスキル終了

### 2-F: wave 完了をセッションに記録する

全タスク成功した wave のみ、セッションファイルの `- [ ] Wave {N} (M tasks)` を `- [x] Wave {N} (M tasks)` に Edit する。

---

## Step 3: フェーズ E への遷移

全 wave 完了後、フェーズ E（レビュー）への遷移を案内する。

- plan-report に reviewer タスク（agent が `code-reviewer` / `security-reviewer`）が含まれている場合は wave で既に実行済みなので、二重レビューを避けるためにユーザーへ「wave 内で reviewer タスクが完了済みなので E をスキップしてもよい」と提示する
- reviewer タスクが含まれていない場合は通常通りフェーズ E へ進む

---

## 知識蓄積

- wave 実行で**特定パターンが詰まりがち**（例: tdd-develop の persona 起動で context が膨らみやすい）と気付いたら、セッションファイルの `## 試みたが失敗したアプローチ` に追記し `patterns` に登録する
- wave 分解の精度（planner の出力品質）に関する観察も同様に記録する。これは将来 planner 側のルール改善に繋がる
