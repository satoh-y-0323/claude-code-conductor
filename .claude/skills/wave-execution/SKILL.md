---
description: PO（Parallel Orchestra）を用いた wave 単位の並列実装実行手順。dev-workflow がフェーズ D で条件付き参照する。
user-invocable: false
---

# Wave Execution

`/develop` のフェーズ D で plan-report に YAML フロントマター（`po_plan_version: "0.1"`）が含まれるときに Read される手順。

C3 の親 Claude が plan-report の DAG を **wave 単位**で歩く。
- 単独 wave（タスク 1 件） → C3 が直接実行（Agent ツール起動 or ペルソナ採用）
- 複数 wave（タスク 2 件以上） → parallel-orchestra（PO）にスポット並列委譲

各 wave 完了後にユーザーの承認を取る。

---

## 前提条件

- `claude-code-conductor` がインストール済みで `c3` コマンドが PATH 上にあること
- plan-report が `.claude/reports/plan-report-*.md` の形式で配置され、YAML フロントマターを持つこと（フロントマターが無ければ `dev-workflow/SKILL.md` の D-1〜D-5 ceremony へフォールバック）

---

## Step 0-pre: ワーキングツリーの事前確認

PO は worktree からの auto-merge で main に成果物を取り込む仕様で、**main 側に未コミット変更や untracked ファイルがあると、worktree が同名ファイルを再生成して必ず衝突する**。実行前に `git status` でクリーンであることを確認する。

1. Bash で `git status --short` を実行する
2. stdout が空（クリーン）→ Step 0 へ
3. クリーンでない場合:
   - 検出された変更ファイルをユーザーに提示する
   - AskUserQuestion で次を確認する:

     ```json
     {
       "questions": [{
         "question": "PO 実行前に main がクリーンである必要があります。どうしますか？",
         "options": [
           { "label": "コミットしてから続行", "description": "親 Claude が変更内容を確認してコミットしてから wave 実行に進む" },
           { "label": "stash してから続行", "description": "git stash で退避してから wave 実行に進む。完了後に stash pop するかは別途判断" },
           { "label": "キャンセル", "description": "wave 実行を中止し、ユーザーが自分で整理してから /develop を再実行する" }
         ]
       }]
     }
     ```

   - 「コミットしてから続行」→ 親 Claude が `git status` / `git diff` を見て妥当な単位でコミットしてから Step 0 へ
   - 「stash してから続行」→ Bash で `git stash push -u -m "wave-execution pre-clean"` を実行してから Step 0 へ
   - 「キャンセル」→ スキル終了

**特に注意すべきファイル:**

- `.claude/settings.local.json` — Claude Code が permission 自動追加で勝手に更新する。気付かないうちに dirty になっているケースが多い
- `package.json` / `vitest.config.js` 等の事前準備（W0）ファイル — 未コミットで PO に入ると worktree でも同ファイルが書き換わって必ず衝突する
- `.claude/reports/` 配下の中間生成物

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
   - 終了コード `3` = ランナーエラー（claude バイナリ不在等）→ stderr のメッセージをそのまま提示してこのスキルを終了する。親 Claude は逐次実行（`dev-workflow/SKILL.md` の D-1〜D-5）に切り替えるか、環境を整備して `/develop` を再実行するかをユーザーに選んでもらう

   PO は `c3` パッケージに同梱されているため、別途 `pip install` は不要。

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
2. `.claude/skills/worktree-tdd-workflow/SKILL.md` を Read して TDD ループ手順（tester→developer→tester）を取得する
3. タスクの `prompt` を実装内容として、worktree-tdd-workflow/SKILL.md の Step 1〜4 を **親 Claude が直接** 実行する。tester / developer は Agent ツールでスポーン可能（depth 1 で完結する）
4. ループ完了後、結果を 2-D の集約に渡す

##### A-2: `agent` がそれ以外（`code-reviewer` / `security-reviewer` / `developer` / `tester` 等）

**Agent ツール起動パターン**で実行する。これらの agent は内部で更に subagent を spawn しないため depth 1 制限に抵触しない。

1. Agent ツールで `subagent_type` は指定せず（カスタム agent は subagent_type 不可）、プロンプトに以下を含める:
   - 「`.claude/agents/{agent_name}.md` を Read してペルソナを採用すること」
   - タスクの `prompt` 本文
   - **「git add / git commit / git push を実行しないこと。コミットは親 Claude がユーザー承認後に行う」を明示**
2. Agent の返答を 2-D の集約に渡す

**git 禁止ルールの根拠:** 動作確認で developer が独断コミットして Red テストや test-report が untracked のまま実装ファイルだけが main に入る事故が起きた。コミット粒度・承認タイミング・成果物の取りこぼしは親 Claude が一元管理する責務。

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
| `1` | 1件以上のタスクが失敗 | 2-D へ（失敗一覧を含めて提示）。**注:** PO は 1 タスクあたり 15 分（`_INTERNAL_TIMEOUT_SEC = 900`、ハードコード上書き不可）でタイムアウトする。15 分超で failure 扱いになっていれば planner の粒度を見直す |
| `2` | マニフェストエラー（Step 0 をすり抜けた）| エラー内容を提示しスキル終了 |
| `3` | auto-merge 衝突（worktree → main の取り込みに失敗）| 下記「auto-merge が衝突した場合」のリカバリ手順へ |

実行完了後、生成された `po-run-report-wave-{N}-{ts}.json` を Read してタスクごとのステータスを取得する。

##### auto-merge が衝突した場合（exit code 3）

PO は worktree でタスクを実行したあと main に auto-merge する。Step 0-pre をすり抜けて main にダーティな状態が残っていたり、複数 worktree が同じ周辺ファイル（CLAUDE.md / settings.local.json 等）を触っていると衝突する。selective checkout でコア成果物だけ救う:

1. 残っている PO ブランチを列挙する: `git branch --list "parallel-orchestra/*"`
2. 各ブランチを個別に確認する: `git log --stat parallel-orchestra/<task>-<hash>`
3. **コア成果物のみ**を抽出する: `git checkout parallel-orchestra/<task>-<hash> -- <files>`
   - 取り込むのはタスクの `writes` フィールドに列挙されたファイルのみ
   - **取り込まない**: worktree が touch したが本タスクの責務でない周辺ファイル
     - `CLAUDE.md` / `package.json` / `vitest.config.js` 等の事前準備系ファイル
     - `.claude/settings.local.json`（permission 自動追加で worktree 側でも更新されている）
     - `.claude/reports/` 配下の中間生成物
4. PO ブランチを削除する: `git branch -D parallel-orchestra/<task>-<hash>` を全ブランチに対して実行
5. 抽出した成果物 + 既存の main 変更分を親 Claude が確認のうえコミットする
6. `git status --short` で main がクリーンになったことを確認してから次の wave に進む

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

**さらに `session_utils.append_checkpoint()` を呼び出して checkpoint ブロックを追記する。** これにより wave 完了時の状態（成功 wave 数・残 wave 数・成果物の要約）が時系列で残り、後続の `/init-session` や `/pattern-status` で進捗が追跡できる。

呼び出し例（Bash 経由）:
```bash
python -c "from session_utils import append_checkpoint, SESSIONS_DIR; import os; \
  append_checkpoint(os.path.join(SESSIONS_DIR, '{YYYYMMDD}.tmp'), \
    'Wave {N} success', \
    '- 成功タスク: {M}件\n- 残 wave: {K}/{TOTAL}\n- 成果物: {要約}')" \
  --hooks-dir .claude/hooks
```

または Python ヘルパーを直接呼び出してもよい。**checkpoint の summary には KEEP ルール（設計判断・決定事項・解決済みのハマりどころ）に該当する情報のみ書く**。雑談・進捗報告はセッションファイル本体（`## うまくいったアプローチ` 等）へ。

**失敗 wave を「スキップして次の wave へ」した場合も同じ仕組みで記録する。** label は `Wave {N} skipped`、summary に失敗内容と判断理由を書く。

**次の wave に進む前に main をコミットしてクリーンに保つ。** wave 成果物を未コミットのまま次の wave で PO に入ると、worktree が同名ファイルを再生成して必ず auto-merge 衝突が起きる（Step 0-pre と同じ理由）。親 Claude が wave の成果物を確認のうえ「Wave {N}: {要約}」のメッセージでコミットしてから 2-A の次のループに戻る。

---

## Step 3: フェーズ E への遷移

全 wave 完了後、フェーズ E（レビュー）への遷移を案内する。

- plan-report に reviewer タスク（agent が `code-reviewer` / `security-reviewer`）が含まれている場合は wave で既に実行済みなので、二重レビューを避けるためにユーザーへ「wave 内で reviewer タスクが完了済みなので E をスキップしてもよい」と提示する
- reviewer タスクが含まれていない場合は通常通りフェーズ E へ進む

---

## 知識蓄積

- wave 実行で**特定パターンが詰まりがち**（例: tdd-develop の persona 起動で context が膨らみやすい）と気付いたら、セッションファイルの `## 試みたが失敗したアプローチ` に追記し `patterns` に登録する
- wave 分解の精度（planner の出力品質）に関する観察も同様に記録する。これは将来 planner 側のルール改善に繋がる
