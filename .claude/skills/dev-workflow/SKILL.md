---
description: ヒアリング→設計→計画→実装→レビューの全フェーズワークフロー。/start・/develop・/review-phase スキルが内部参照する。
disable-model-invocation: false
user-invocable: false
---

# Dev Workflow

要件定義から実装・レビューまでを複数エージェントで連携させるフルワークフロー。
`.claude/skills/` の各スキルからこのファイルを Read して指定フェーズから実行する。
フェーズ間の遷移はこのファイル内で完結する（外部コマンド呼び出し不要）。

---

## セッションファイル運用総則

各フェーズ承認後は、セッションファイルの `- [ ] {フェーズ名}` を `- [x]` に Edit すると同時に、`現在地:` 行を次フェーズ名へ Edit すること。

例: `現在地: フェーズC 計画中`、フェーズ E 完了時は `現在地: 完了`

`現在地:` は行フィールド形式（`現在地: {値}` 1行）で維持する。`## 現在地` 見出しにしない。

> 注: 「フェーズ承認後」が原則だが、フェーズ完了直後に更新するケースもある（D-1・D-4 等の tester 完了トリガーなど）。承認とタスク完了が同一ステップになる場合は完了時点で即 Edit する（CR L-02）。

---

## tier-routing 結果記録の運用

各フェーズの承認ゲート・タスク単位で `record_agent_outcome.py` を呼び、role 別に実際に使われた tier の成功/失敗を記録する（architecture-report-20260702-214748.md §3-4）。全記録ブロック共通のルール:

- `--complexity` は必須。値は**ワークフロー開始時に UserPromptSubmit hook が表示した `[tier-routing 推奨]` 表示の複雑度**（simple/medium/complex）で、フェーズ A 冒頭でセッションファイルへ `tier-routing複雑度:` 行として 1 度だけ永続化する（本ワークフロー中に再判定しない）。以降の各ブロックの `{セッションファイルの tier-routing複雑度: 行の値}` はこの永続化された値を参照するプレースホルダ（`現在地:` フィールドと同じ、compaction・長時間セッション耐性のための設計）
- `--execution` は必須。interviewer/architect/planner を**親 Claude ペルソナ**で採用した場合は `persona`（bandit 更新なし・イベントログのみ）、**Agent ツールでサブエージェント起動した場合は `subagent`**（bandit 更新あり）を渡す。developer/tester は dev-workflow では常に `subagent`
- `--execution persona` のブロックは `--tier {親モデルのtier名（haiku/sonnet/opus）。判別不能なら unknown}` も明記する。record_agent_outcome.py は persona 実行かつ `--tier` 省略時に常に `unknown` 固定で記録するため、実際の親モデル tier を記録に残したい場合は明示が必要（`--execution subagent` のブロックは frontmatter から自己解決するため `--tier` 不要）
- `--task` は任意引数。同一 gate/role/outcome の記録がタスクごとに繰り返し発生するゲート（D-2.5/D-3 等）では `--task {plan タスクID}` を付与して dedupe キーの粒度を上げる。1 ワークフロー 1 判定のゲート（A-4/B-3/C-2/C-3/D-5/E-1/E-2）は付与不要
- `--note` は指摘本文を逐語引用せず、シェルメタ文字（引用符・バッククォート・`$` 等）を含まない短い要約で書く（コマンドライン展開事故・シェルインジェクションを避けるため。record_agent_outcome.py 側で長さ上限と秘密情報マスクも適用されるが、そもそも逐語引用しないことが第一防御）
- 全エラー exit 0 のため呼び出しが失敗してもワークフローは止めない（記録漏れの可能性はあるが後続フェーズはブロックしない）

**ソフト適用（推奨 Tier の `model:` 指定・ADR-AS-1/ADR-AS-2）:**

- developer を Agent ツールで起動する箇所（**D-2 / D-2.5 の再実行 / D-4**）では、`[tier-routing 推奨]` が指示した推奨 Tier を Agent 呼び出しの `model:` に明示指定する（ソフト適用・学習データ収集中の期間も含め常に適用する。fork は model 上書き不可のため対象外）。**tester / systematic-debugger は対象外**で従来どおり frontmatter 任せとする。interviewer/architect/planner は親 Claude ペルソナで動かし tier レバーが無いため対象外。
- **推奨 Tier の SSOT**: 「推奨 Tier」の唯一のソースは `.claude/state/tier_selection.json` の `tier`（無ければ `suggested_model`）であり、`[tier-routing 推奨]` の additionalContext テキストはその値を人間可読に射影した派生表示で SSOT ではない。この値は kickoff プロンプトの UserPromptSubmit で select_tier が 1 度だけ書き、E-2 の `--final` で削除されるまで wave/ゲートをまたいで安定する（承認応答は UserPromptSubmit を発火しないため途中で上書きされない）。
- developer の record ブロックは**原則 `--tier` を付けない**（record_agent_outcome.py が tier_selection.json から実適用 tier を機械解決する。tier 値の LLM 申告を行わない）。**例外（機械条件・ADR-AS-2）**: developer 起動時に指定した `model:` が推奨 Tier（tier_selection.json.tier）と**異なる値だった場合は、必ずその実際に指定した値を `--tier {実 model の tier}` に付けて**実態を記録する（「指定 model: ≠ 推奨 Tier なら必ず付与」という観測可能な二値条件で判定する）。

---

## フェーズ A: ヒアリング

`.claude/agents/interviewer.md` を Read してペルソナを採用する。

今日のセッションファイルに以下を追記する（未登録の場合のみ）:
- `- [ ] ヒアリング` / `- [ ] 設計` / `- [ ] 計画`
- `tier-routing複雑度: {ワークフロー開始時に UserPromptSubmit hook が表示した [tier-routing 推奨] の複雑度}`（1 度だけ追記。既にあれば追記しない。以降の全 tier-routing 記録ブロックの `--complexity` はこの行の値を参照する）

### A-1〜A-3: 動的ヒアリング（ルーブリック型）

**最初に必ず** `.claude/skills/dev-workflow/references/interview-rubric.md` を Read する（記憶・推測で進めない）。
その指針に従い、床 5 観点（①背景・目的 ②スコープ境界 ③制約・前提 ④非機能要件 ⑤成功条件）を
**動的にヒアリング**する。要点:

- 質問文・選択肢はタスク固有に**その場で生成**する（固定テンプレの 4 択を使わない）。`Other`（自由記述）は常設。
- 会話コンテキストと既存 `requirements-report` で**判明済みの観点は再質問しない**。
- 1 回の `AskUserQuestion` = 1 問（CLAUDE.md「質問は 1 回に 1 つ」）。深掘りは設計を左右する不明点のみ 1 問。
- **停止条件**: 床 5 観点が十分 / 質問総数 **上限 6 問** 到達 / ユーザーが「もう十分」。床充足なら即停止する。
- requirements-report 生成前に **self-check**: 5 観点に確定内容があるか点検し、空欄は推測で埋めず「未確定事項」として明示する。

詳細手順・予測可能性の担保根拠は `references/interview-rubric.md` を参照。

### A-4: requirements-report の生成と承認

収集した内容をもとに `.claude/reports/requirements-report-YYYYMMDD-HHMMSS.md` に Write する。

内容を提示した後、AskUserQuestion で確認する:

```json
{
  "questions": [{
    "question": "requirements-report の内容を確認してください。どうしますか？",
    "options": [
      { "label": "承認", "description": "設計フェーズへ進む" },
      { "label": "否認・修正を依頼する", "description": "フィードバックを入力してヒアリングをやり直す" },
      { "label": "否認・自分でファイルを編集する", "description": "reports/ のファイルを直接編集してから続ける" }
    ]
  }]
}
```

承認後 → セッションファイルの `- [ ] ヒアリング` を `- [x] ヒアリング` に Edit し、`現在地:` を `現在地: フェーズB 設計中` に Edit して**フェーズ B** へ。

**tier-routing 結果記録**:
```bash
python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
  --role interviewer --outcome success --gate A-4 \
  --execution persona --complexity {セッションファイルの tier-routing複雑度: 行の値} \
  --tier {親モデルのtier名（haiku/sonnet/opus）。判別不能なら unknown}
```
（interviewer を Agent ツールでサブエージェント起動した場合は `--execution subagent` を渡す。その場合 `--tier` は不要）

**知識蓄積:**
- 「否認・修正を依頼する」「否認・自分でファイルを編集する」: `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する
- **tier-routing 結果記録（否認時）**:
  ```bash
  python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
    --role interviewer --outcome failure --gate A-4 \
    --execution persona --complexity {セッションファイルの tier-routing複雑度: 行の値} \
    --tier {親モデルのtier名（haiku/sonnet/opus）。判別不能なら unknown}
  ```
- 承認かつ非自明なアプローチが有効だった場合: `## うまくいったアプローチ` に追記し `patterns` にも追加する

---

## フェーズ B: 設計

`.claude/agents/architect.md` を Read してペルソナを採用する。

**フェーズ A から続いている場合:** 要件はコンテキスト内にあるため読み直し不要。
**直接開始の場合:** Glob で `.claude/reports/requirements-report-*.md` の最新を Read する。

今日のセッションファイルに以下を追記する（未登録の場合のみ）:
- `- [ ] 設計` / `- [ ] 計画`

### B-1〜B-2: 動的設計確認（ルーブリック型）

**最初に必ず** `.claude/skills/dev-workflow/references/design-rubric.md` を Read する（記憶・推測で進めない）。
その指針に従い、床 4 観点（①技術スタック制約 ②要件から導く設計判断ポイント ③非機能の実現方針 ④トレードオフ分岐）を
**動的に確認**する。要点:

- 技術スタック制約を起点に、要件から設計判断ポイントを**その場で列挙**して確認する（固定テンプレの 3 択を使わない）。
- 要件から自明に決まる設計判断（非機能の実現方針が要件から定まる場合を含む）は再質問しない。複数案があるものは比較の軸も併せて提示する。
- 1 回の `AskUserQuestion` = 1 問。設計を左右する不明点のみ深掘り（控えめ）。
- **停止条件**: 床 4 観点が十分 / 質問総数 **上限 4 問** 到達 / ユーザーが「もう十分」。床充足なら即停止する。
- architecture-report 生成前に **self-check**: 4 観点に確定内容があるか点検し、空欄は推測で埋めず「未確定事項」として明示する。

詳細手順は `references/design-rubric.md` を参照。

### B-3: architecture-report の生成と承認

`.claude/reports/architecture-report-YYYYMMDD-HHMMSS.md` に Write する。
内容を提示した後、AskUserQuestion で確認する:

```json
{
  "questions": [{
    "question": "architecture-report の内容を確認してください。どうしますか？",
    "options": [
      { "label": "承認", "description": "計画フェーズへ進む" },
      { "label": "否認・修正を依頼する", "description": "フィードバックを入力して設計をやり直す" },
      { "label": "否認・自分でファイルを編集する", "description": "reports/ のファイルを直接編集してから続ける" }
    ]
  }]
}
```

承認後 → セッションファイルの `- [ ] 設計` を `- [x] 設計` に Edit し、`現在地:` を `現在地: フェーズC 計画中` に Edit して**フェーズ C** へ。

**tier-routing 結果記録**:
```bash
python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
  --role architect --outcome success --gate B-3 \
  --execution persona --complexity {セッションファイルの tier-routing複雑度: 行の値} \
  --tier {親モデルのtier名（haiku/sonnet/opus）。判別不能なら unknown}
```
（architect を Agent ツールでサブエージェント起動した場合は `--execution subagent` を渡す。その場合 `--tier` は不要）

**知識蓄積:**
- 「否認・修正を依頼する」「否認・自分でファイルを編集する」: `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する
- **tier-routing 結果記録（否認時）**:
  ```bash
  python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
    --role architect --outcome failure --gate B-3 \
    --execution persona --complexity {セッションファイルの tier-routing複雑度: 行の値} \
    --tier {親モデルのtier名（haiku/sonnet/opus）。判別不能なら unknown}
  ```
- 承認かつ非自明なアプローチが有効だった場合: `## うまくいったアプローチ` に追記し `patterns` にも追加する

---

## フェーズ C: 計画

`.claude/agents/planner.md` を Read してペルソナを採用する。

**上流フェーズから続いている場合:** 要件・設計はコンテキスト内にあるため読み直し不要。
**直接開始またはレビューから戻った場合:** Glob で `.claude/reports/` 内の全レポートを Read する（`[対応予定]` マーク付きの指摘を修正計画に反映する）。

今日のセッションファイルに `- [ ] 計画` を追記する（未登録の場合のみ）。

### C-1: マイルストーンの確認

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "マイルストーン（途中で確認したいポイント）を設けますか？",
    "options": [
      { "label": "設ける", "description": "一定の区切りで確認しながら進めたい" },
      { "label": "設けない", "description": "一気に完了まで進める" }
    ]
  }]
}
```

### C-2: plan-report の生成と承認

`.claude/reports/plan-report-YYYYMMDD-HHMMSS.md` に Write する。

**承認前に agent 種別を明示する:**
plan-report の全タスクを走査し、以下の形式でテキスト出力する（AskUserQuestion の前に必ず行う）:

```
## タスク一覧（agent 種別確認）
| タスク ID | agent | read_only |
|---|---|---|
| {id} | {agent} | {true/false} |
...
```

`read_only: false` のタスクに `tester` / `developer` 以外の agent が使われている場合は、
その理由をテキストで説明した上で承認を求めること。

内容を提示した後、AskUserQuestion で確認する:

```json
{
  "questions": [{
    "question": "plan-report の内容を確認してください。どうしますか？",
    "options": [
      { "label": "承認", "description": "実装フェーズへ進む" },
      { "label": "否認・修正を依頼する", "description": "フィードバックを入力して計画をやり直す" },
      { "label": "否認・自分でファイルを編集する", "description": "reports/ のファイルを直接編集してから続ける" }
    ]
  }]
}
```

承認後 → セッションファイルの `- [ ] 計画` を `- [x] 計画` に Edit し、`現在地:` を `現在地: フェーズD 実装中` に Edit して**C-3（計画監査ゲート）** へ。

**tier-routing 結果記録**:
```bash
python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
  --role planner --outcome success --gate C-2 \
  --execution persona --complexity {セッションファイルの tier-routing複雑度: 行の値} \
  --tier {親モデルのtier名（haiku/sonnet/opus）。判別不能なら unknown}
```
（planner を Agent ツールでサブエージェント起動した場合は `--execution subagent` を渡す。その場合 `--tier` は不要）

**知識蓄積:**
- 「否認・修正を依頼する」「否認・自分でファイルを編集する」: `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する
- **tier-routing 結果記録（否認時）**:
  ```bash
  python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
    --role planner --outcome failure --gate C-2 \
    --execution persona --complexity {セッションファイルの tier-routing複雑度: 行の値} \
    --tier {親モデルのtier名（haiku/sonnet/opus）。判別不能なら unknown}
  ```
- 承認かつ非自明なアプローチが有効だった場合: `## うまくいったアプローチ` に追記し `patterns` にも追加する

---

### C-3: 計画監査ゲート（opt-in）

**セッションファイル運用:** C-3 は C-2 で `計画 [x]` 化が完了した後の独立ゲートである。セッションファイルへの新規 `- [ ]` フェーズ行の追加は不要。

**ステップ 1: opt-in の確認（AskUserQuestion 単独ターン）**

AskUserQuestion で確認する（このブロックに Agent 起動などの副作用ツールを混在させない）:

```json
{
  "questions": [{
    "question": "実装前に設計・計画を design-critic で監査しますか？",
    "options": [
      { "label": "監査する", "description": "前提・曖昧さ・抜け漏れを敵対的にチェック（手戻りが多そう/重要な変更で推奨）" },
      { "label": "スキップ", "description": "小さい・自明な変更。そのまま実装へ" }
    ]
  }]
}
```

「スキップ」→ そのまま**フェーズ D** へ（ノーオペ）。
「監査する」→ ステップ 2 へ（別ターンで実行）。

**ステップ 2: design-critic の起動（AskUserQuestion と別ターン）**

Agent ツールで `design-critic` を起動する。

- `subagent_type: "design-critic"`（固有名を明示。`'claude'` や省略は禁止）
- `isolation: worktree` は使わない（read-only・並列なし）
- プロンプトには以下の起動指示のみを含める（レポートの内容は agent 側で Glob・Read させる [SR-AI-001]）:
  - 「`design-critic-rubric.md` を Read し、requirements / architecture / plan の各最新レポートを Glob で取得して 3 レンズで監査せよ」

design-critic は `.claude/reports/design-review-report-YYYYMMDD-HHMMSS.md` を Write して終了する。

完了後 → ステップ 3 へ。

**ステップ 3: findings の分岐**

design-review-report を Read して findings の有無を確認する。design-critic の起動失敗・中断によりレポートが存在しない場合は、AskUserQuestion でユーザーに「再実行する」か「スキップしてフェーズ D へ進む」かを確認する。

**findings なし（report に「findings なし」と記載されている場合）:**
そのまま**フェーズ D** へ。

**findings あり:**
指摘一覧をテキストで提示してから AskUserQuestion で方針を確認する（このブロックに副作用ツールを混在させない）:

```json
{
  "questions": [{
    "question": "design-review-report に指摘があります。対応方針を選択してください。",
    "options": [
      { "label": "全て対応する", "description": "全指摘に [対応予定] を付けて起因層へ戻す" },
      { "label": "対応する指摘を選ぶ", "description": "指摘ごとに対応/許容を決める" },
      { "label": "全て許容して進む", "description": "全指摘を許容してフェーズ D へ" },
      { "label": "否認・再監査を依頼する", "description": "フィードバックして design-critic を再実行" }
    ]
  }]
}
```

**「全て対応する」の場合:**
全指摘に `> **[対応予定]**` をマークしてから層別ルーティング（ステップ 4）へ。

**「対応する指摘を選ぶ」の場合:**
続けて AskUserQuestion で確認する（別ターン）:
```json
{
  "questions": [{
    "question": "どの指摘を対応しますか？対応する指摘 ID（例: DC-AS-001）と、許容する指摘の理由を教えてください。"
  }]
}
```
1. 対応する指摘に `> **[対応予定]**` を Edit で追記する
2. 許容する指摘の直下に `> **[許容]** {理由}` を Edit で追記する（検出記録は削除しない）
3. `[対応予定]` を付けた finding が 1 件以上あれば層別ルーティング（ステップ 4）へ。全て許容した場合はフェーズ D へ。

**「全て許容して進む」の場合:**
続けて AskUserQuestion で許容理由を確認する（別ターン）:
```json
{
  "questions": [{
    "question": "全指摘を許容する理由を教えてください。"
  }]
}
```
全指摘の直下に `> **[許容]** {理由}` を Edit で追記してから**フェーズ D** へ。

**「否認・再監査を依頼する」の場合:**
続けて AskUserQuestion でフィードバックを確認してからステップ 2（design-critic 再起動）へ。

**ステップ 4: 層別ルーティング**

（ステップ 3 で「全て対応する」または「対応する指摘を選ぶ」を選び、`[対応予定]` が 1 件以上ある場合のみこのステップへ到達する）

`[対応予定]` を付けた finding の起因層（`A要件` / `B設計` / `C計画`）を集計し、最も上流の層へ戻る（上流順: **A要件 < B設計 < C計画**）。戻り先はピンポイント修正（`[対応予定]` の finding のみ対象。フルやり直しはしない）。**最上流の判定は `[対応予定]` を付けた finding だけで行う（`[許容]` にした finding の起因層は戻り先に影響しない）。**

| `[対応予定]` finding の最上流起因 | 戻り先 | 修正する担当 | その後の連鎖 |
|---|---|---|---|
| A要件 を含む | フェーズ A | interviewer（該当点のみ追加確認） | A → B → C → C-3 |
| B設計 が最上流 | フェーズ B | architect（該当設計のみ修正） | B → C → C-3 |
| C計画 のみ | フェーズ C | planner（該当タスクのみ修正） | C → C-3 |

**tier-routing 結果記録**: 上表により層別ルーティングが発動した場合（`[対応予定]` が 1 件以上ある場合）のみ、戻り先の最上流 role へ failure を 1 件記録する（`[許容]` のみで `[対応予定]` が 0 件・findings なしの場合は記録しない）:
```bash
python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
  --role {interviewer|architect|planner（戻り先 role）} --outcome failure --gate C-3 \
  --execution persona --complexity {セッションファイルの tier-routing複雑度: 行の値} \
  --tier {親モデルのtier名（haiku/sonnet/opus）。判別不能なら unknown} \
  --note "{層別ルーティングの起因概要（コード断片の逐語引用禁止）}"
```
（戻り先 role を Agent ツールでサブエージェント起動した場合は `--execution subagent` を渡す。その場合 `--tier` は不要）

上流フェーズ修正後は下流へ自然に連鎖する（例: B 修正 → C 再計画 → C-2 再承認 → C-3）。各フェーズでも該当 `[対応予定]` の finding のみを反映する。

連鎖が C-2 を経て C-3 に戻ったら → ステップ 5（再監査の選択）へ。

**ステップ 5: 修正後の再監査（選択式・無限ループ防止）**

上流修正の連鎖を経て C-3 に戻ってきた場合、毎回自動再走はしない。AskUserQuestion で再監査の要否を確認する（このブロックに副作用ツールを混在させない）:

```json
{
  "questions": [{
    "question": "修正が入りました。design-critic で再監査しますか？",
    "options": [
      { "label": "再監査する", "description": "更新後のレポート群を再度チェック（重要案件）" },
      { "label": "再監査せず実装へ", "description": "フェーズ D へ進む" }
    ]
  }]
}
```

「再監査する」→ ステップ 2（design-critic 起動）へ（別ターンで実行）。
「再監査せず実装へ」→ **フェーズ D** へ。

---

## フェーズ D: 実装

**フェーズ C から続いている場合:** plan-report はコンテキスト内にあるため読み直し不要。
**直接開始の場合:** D-0 で実行モードを判定する。

### D-0: 実行モード自動判別

以下の順で実行モードを判定する:

1. Glob で `.claude/reports/plan-report-*.md` の最新が存在する場合、冒頭の YAML フロントマター
   （`---` で始まり `po_plan_version: "0.1"` を含む）の有無を確認する。
2. plan-report が存在せず、**当日タイムスタンプ**の `.claude/reports/debug-analysis-*.md` が存在する場合は **bug-fix モード** とする。
   当日判定は LLM のテキスト解釈ではなく以下の Bash で機械的に取得すること（前セッションの残骸 debug-analysis による意図しない bug-fix モード突入を防ぐ）:

   ```bash
   python -c "import os, glob, datetime; today = datetime.datetime.now().strftime('%Y%m%d'); files = sorted(glob.glob('.claude/reports/debug-analysis-*.md')); today_files = [f for f in files if os.path.basename(f).startswith(f'debug-analysis-{today}-')]; print(today_files[-1] if today_files else '')"
   ```

   標準出力が空でなければそのパスを bug-fix モードの入力として保持する。空なら debug-analysis を「無し」とみなし判定 3 へ進む。
3. plan-report も当日 debug-analysis も存在しない場合はフェーズ C から始めるよう案内して終了する。

**フロントマターありの場合（parallel-agents モード）:**
1. **最初に必ず** `.claude/skills/parallel-agents/SKILL.md` を Read する（記憶・推測で進めない）
2. `.claude/skills/parallel-agents/SKILL.md` の手順に完全に従って wave 単位で実装を進める
3. 全 wave 完了後はフェーズ E（レビュー）へ進む（wave に reviewer タスクが含まれていれば E をスキップ可能と案内する）

**フロントマターなしの場合（legacy TDD モード）:**

今日のセッションファイルに以下を追記する（未登録の場合のみ）:
- `- [ ] tester: Red フェーズ`
- `- [ ] developer: Green フェーズ`
- `- [ ] developer: Refactor フェーズ`
- `- [ ] tester: 最終確認`

D-1 へ進む。

**bug-fix モードの場合:**

Glob で当日の `.claude/reports/debug-analysis-*.md` の最新を取得し、ファイルパスのみコンテキストに保持する（内容は後段の agent 側で Read させる。プロンプトに直接展開しない[SR-AI-001] 対策）。

今日のセッションファイルに以下を追記する（未登録の場合のみ）:
- `- [ ] developer: 修正実装`
- `- [ ] tester: 動作確認`

bug-fix モードでは D-1（Red tester）と D-4（Refactor）をスキップする。
以下の順で実行する: **D-2（bug-fix モード）→ D-2.5（Stuck チェック・bug-fix モード）→ D-3（bug-fix モード）→ フェーズ E**。

**D-2（bug-fix モード）**: Agent ツールで `developer` を起動する。プロンプトには debug-analysis の**ファイルパスのみ**を含め、内容は agent 側で Read させる（プロンプトに展開しない）。
完了後、セッションファイルの `- [ ] developer: 修正実装` を `- [x]` に Edit し、`現在地:` を `現在地: bug-fix 動作確認中` に Edit する。

**D-2.5（bug-fix モード）**: 通常の D-2.5 と同じ Stuck チェック手順（debug-needed-*.md 検出時の systematic-debugger 起動・developer 再実行）を実行する。
ただし末尾の AskUserQuestion 承認後の Edit 対象は `- [ ] developer: 修正実装` ではなく既に D-2 で `[x]` 化済みのため**スキップする**（bug-fix モードの修正実装承認は D-3 の動作確認で代替する）。
通常モードの「`- [ ] developer: Green フェーズ` を `[x]` に Edit する」は bug-fix モードでは適用しない。

**D-3（bug-fix モード）**: Agent ツールで `tester` を起動して全テストの Green を確認する。
AskUserQuestion で確認する:

```json
{
  "questions": [{
    "question": "bug-fix の動作確認結果を確認してください。どうしますか？",
    "options": [
      { "label": "全合格・レビューへ進む", "description": "フェーズ E（レビュー）へ進む" },
      { "label": "不合格あり・再修正を依頼する", "description": "D-2（developer）に戻って再修正する" },
      { "label": "不合格あり・自分で修正する", "description": "自分で修正してから tester を再実行する" }
    ]
  }]
}
```

「全合格」承認後 → セッションファイルの `- [ ] tester: 動作確認` を `- [x]` に Edit し、`現在地:` を `現在地: フェーズE レビュー中` に Edit して**フェーズ E** へ。
「不合格あり」を選んだ場合 → D-2 に戻る。合格するまで繰り返す。

bug-fix モード固有の動作:
- D-1（Red フェーズ）をスキップする理由: 既存の不具合自体が「足りないテスト」を示しており、developer が修正と一緒に回帰テストを追加する運用とする
- D-4（Refactor フェーズ）をスキップする理由: 不具合修正のスコープを最小に保つため。リファクタが必要な場合は後段のレビュー指摘で改めて計画化する

フェーズ E（レビュー）で指摘があり「全て対応する」「対応する指摘を選ぶ」を選んだ場合は、
通常通りフェーズ C（計画）へ戻る。次回からは plan-report が生成されるため、
legacy TDD モードまたは parallel-agents モードで実装される。

### D-1: tester（Red フェーズ）

Agent ツールで `tester` エージェントを起動する。→ 失敗するテストを先に作成する。**必ず `.claude/reports/test-report-YYYYMMDD-HHMMSS.md` を Write してから終了すること。**

完了後 → セッションファイルの `- [ ] tester: Red フェーズ` を `- [x]` に Edit し、`現在地:` を `現在地: フェーズD 実装中 / 次: developer Green` に Edit する。

### D-2: developer（Green フェーズ）

Agent ツールで `developer` エージェントを起動する（`model:` に推奨 Tier を指定・上記「tier-routing 結果記録の運用」節参照）。→ テストが通る実装を行う。

### D-2.5: Stuck チェック

Glob で `.claude/reports/debug-needed-*.md` の最新を確認する。

**ファイルが存在する場合:**
1. Agent ツールで `systematic-debugger` を起動する。プロンプトに debug-needed ファイルのパスのみを含め、内容は agent 側で Read させる（プロンプトに直接展開しない）[SR-AI-001]
2. 生成された `.claude/reports/debug-analysis-*.md` を Glob で取得してパスのみコンテキストに保持する（内容は次段で agent に Read させる）
3. D-2 の developer を再実行する（`model:` に推奨 Tier を指定・上記「tier-routing 結果記録の運用」節参照）。プロンプトに debug-analysis の**ファイルパスのみ**を含め、内容は agent 側で Read させる（プロンプトに展開しない）[SR-AI-001]
4. debug-needed ファイルを削除する

**ファイルが存在しない場合:** そのまま次へ進む

AskUserQuestion で確認する:
```json
{
  "questions": [{
    "question": "実装内容を確認してください。どうしますか？",
    "options": [
      { "label": "承認", "description": "テスト確認フェーズへ進む" },
      { "label": "否認・再実装を依頼する", "description": "フィードバックを入力して developer を再起動する" },
      { "label": "否認・自分でコードを修正する", "description": "自分でコードを修正してから続ける" }
    ]
  }]
}
```

承認後 → セッションファイルの `- [ ] developer: Green フェーズ` を `- [x]` に Edit し、`現在地:` を `現在地: フェーズD 実装中 / 次: Refactor` に Edit する（**bug-fix モードではこの Edit をスキップする**。D-2 で `- [ ] developer: 修正実装` を既に `[x]` 化済みのため、Green フェーズ行自体がセッションファイルに存在しない）。

**tier-routing 結果記録**:
```bash
python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
  --role developer --outcome success --gate D-2.5 \
  --execution subagent --complexity {セッションファイルの tier-routing複雑度: 行の値} \
  --task {plan タスクID}
```

**知識蓄積:**
- 「否認・再実装を依頼する」「否認・自分でコードを修正する」: `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する
- **tier-routing 結果記録（否認時）**:
  ```bash
  python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
    --role developer --outcome failure --gate D-2.5 \
    --execution subagent --complexity {セッションファイルの tier-routing複雑度: 行の値} \
    --task {plan タスクID}
  ```

### D-3: tester（確認）

Agent ツールで `tester` エージェントを起動する。→ 全テストの合否を確認する。**必ず `.claude/reports/test-report-YYYYMMDD-HHMMSS.md` を Write してから終了すること。**

AskUserQuestion で確認する:
```json
{
  "questions": [{
    "question": "テスト結果を確認してください。どうしますか？",
    "options": [
      { "label": "全合格・次へ進む", "description": "Refactor フェーズへ進む" },
      { "label": "不合格あり・再実装を依頼する", "description": "フィードバックを入力して developer を再起動する" },
      { "label": "不合格あり・自分でコードを修正する", "description": "自分で修正してから tester を再実行する" }
    ]
  }]
}
```

不合格の場合: D-2（developer）に戻る。合格するまで繰り返す。

**tier-routing 結果記録（不合格時のみ・D-2.5 と重複回避のため全合格時は記録しない・欠陥の所在で判定）**: D-5 否認ブロックと同型で、テストコード欠陥は `tester` failure、プロダクトコード欠陥は `developer` failure、両方または判別不能な場合は `developer` のみ記録する:
```bash
python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
  --role {tester|developer（欠陥の所在で判定・テストコード欠陥=tester／プロダクトコード欠陥=developer／両方または判別不能=developer）} --outcome failure --gate D-3 \
  --execution subagent --complexity {セッションファイルの tier-routing複雑度: 行の値} \
  --task {plan タスクID}
```

### D-4: developer（Refactor フェーズ）

Agent ツールで `developer` エージェントを起動する（`model:` に推奨 Tier を指定・上記「tier-routing 結果記録の運用」節参照）。→ テストを壊さずにコードを整理する。

完了後 → セッションファイルの `- [ ] developer: Refactor フェーズ` を `- [x]` に Edit し、`現在地:` を `現在地: フェーズD 実装中 / 次ゲート: tester 最終確認` に Edit する。

### D-5: tester（最終確認）

Agent ツールで `tester` エージェントを起動する。**必ず `.claude/reports/test-report-YYYYMMDD-HHMMSS.md` を Write してから終了すること。**

AskUserQuestion で確認する:
```json
{
  "questions": [{
    "question": "最終テスト結果と実装内容を確認してください。どうしますか？",
    "options": [
      { "label": "承認・レビューへ進む", "description": "レビューフェーズへ進む" },
      { "label": "否認・修正を依頼する", "description": "フィードバックを入力して再修正を依頼する" },
      { "label": "否認・自分でコードを修正する", "description": "自分で修正してから再テストする" }
    ]
  }]
}
```

承認後 → セッションファイルの `- [ ] tester: 最終確認` を `- [x]` に Edit し、`現在地:` を `現在地: フェーズE レビュー中` に Edit して**フェーズ E** へ。

**tier-routing 結果記録**:
```bash
python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
  --role tester --outcome success --gate D-5 \
  --execution subagent --complexity {セッションファイルの tier-routing複雑度: 行の値}
```

**知識蓄積:**
- 「否認・修正を依頼する」「否認・自分でコードを修正する」: `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する
- **tier-routing 結果記録（否認時・欠陥の所在で判定）**: テストコード欠陥は `tester` failure、プロダクトコード欠陥は `developer` failure、両方または判別不能な場合は `developer` のみ記録する:
  ```bash
  python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
    --role {tester|developer（欠陥の所在で判定）} --outcome failure --gate D-5 \
    --execution subagent --complexity {セッションファイルの tier-routing複雑度: 行の値}
  ```

---

## フェーズ E: レビュー

今日のセッションファイルに以下を追記する（未登録の場合のみ）:
- `- [ ] code-review`
- `- [ ] security-review`

### E-1: code-reviewer エージェントの起動

Agent ツールで `code-reviewer` エージェントを起動する。

**review-hint 過去判断ヒント注入（レポート生成後）:**
code-reviewer がレポートを Write し終えたら、Bash で `.claude/skills/dev-workflow/scripts/review_hint_inject.py` を呼んで過去判断ヒントをレポート末尾に追記する:

```bash
python .claude/skills/dev-workflow/scripts/review_hint_inject.py .claude/reports/code-review-report-{timestamp}.md
```

ヒントは独立セクションとして追加されるだけで、code-reviewer の指摘本文は変更されない。
DB に過去判断が無ければ何も追記されない（no-op）。

レポートの指摘の有無で分岐する。

**指摘がない場合:**
AskUserQuestion で確認する:
```json
{
  "questions": [{
    "question": "code-review-report を確認してください。どうしますか？",
    "options": [
      { "label": "承認・セキュリティレビューへ進む", "description": "問題なし" },
      { "label": "否認・再レビューを依頼する", "description": "フィードバックを入力して再実行する" }
    ]
  }]
}
```

承認後 → セッションファイルの `- [ ] code-review` を `- [x]` に Edit し、`現在地:` を `現在地: フェーズE レビュー中 / 次: security-review` に Edit して E-2 へ。

**tier-routing 結果記録**:
```bash
python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
  --role developer --outcome success --gate E-1 \
  --execution subagent --complexity {セッションファイルの tier-routing複雑度: 行の値}
```

**指摘がある場合:**
指摘一覧をテキストで提示してから AskUserQuestion で方針を確認する:
```json
{
  "questions": [{
    "question": "code-review-report に指摘があります。対応方針を選択してください。",
    "options": [
      { "label": "全て対応する", "description": "全指摘を修正計画に含めてフェーズ C へ" },
      { "label": "対応する指摘を選ぶ", "description": "指摘ごとに対応する/許容するを決める" },
      { "label": "全て許容して進む", "description": "全指摘を許容してセキュリティレビューへ進む" },
      { "label": "否認・再レビューを依頼する", "description": "フィードバックを入力して再実行する" }
    ]
  }]
}
```

**「全て対応する」の場合:**
全指摘に `> **[対応予定]**` をマークし、セッションファイルの `- [ ] code-review` を `- [x]` に Edit し、`現在地:` を `現在地: フェーズC 計画中（レビュー差し戻し）` に Edit してから**フェーズ C** へ（内部遷移・Step 0 なし）。

**tier-routing 結果記録（帰属判定）**: 指摘内容から最上流起因 role を判定し failure を記録する（デフォルト developer・迷ったらこれ／**テストコード欠陥起因の指摘は tester failure**／設計不備なら architect／計画不備なら planner。`--note` に理由必須。developer・tester は `--execution subagent`、architect/planner は `--execution persona`。developer・tester は `--tier` を省略（frontmatter/tier_selection から自己解決）、architect/planner の場合は `--tier` も明記する）:
```bash
python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
  --role {developer|tester|architect|planner（帰属判定）} --outcome failure --gate E-1 \
  --execution {developer・tester=subagent / architect・planner=persona} \
  --complexity {セッションファイルの tier-routing複雑度: 行の値} \
  --tier {developer・tester は省略（frontmatter/tier_selection から自己解決） / architect・planner は親モデルのtier名（haiku/sonnet/opus）。判別不能なら unknown} \
  --note "{帰属理由を1行で（コード断片の逐語引用禁止）}"
```

**「対応する指摘を選ぶ」の場合:**
続けて AskUserQuestion で確認する:
```json
{
  "questions": [{
    "question": "どの指摘を対応しますか？対応する指摘番号と、許容する指摘の理由を教えてください。"
  }]
}
```
1. 対応する指摘に `> **[対応予定]**` を追記する
2. 許容する指摘の直下に `> **[許容]** {理由}` を Edit で追記する（検出記録は削除しない）
3. **review-hint 判断記録**: 各指摘について Bash で c3.db に記録する（`[CR-XX-NNN]` を含むもののみ。`[CR-NEW]` は記録対象外、チェックリスト追加候補として別途扱う）:
   ```bash
   python .claude/skills/dev-workflow/scripts/record_review_decision.py \
     --checklist-id CR-Q-001 \
     --finding "{指摘本文を 1 行で}" \
     --decision {fixed|accepted} \
     --reason "{許容理由（accepted の時のみ）}" \
     --reviewer code-reviewer
   ```
4. セッションファイルの `## うまくいったアプローチ` に `[許容例外] {指摘内容} → {許容理由}` の形式で追記し `patterns` に記録する
5. セッションファイルの `- [ ] code-review` を `- [x]` に Edit し、`現在地:` を `現在地: フェーズC 計画中（レビュー差し戻し）` に Edit してから**フェーズ C** へ（内部遷移・Step 0 なし）。
6. **tier-routing 結果記録（帰属判定）**: 指摘内容から最上流起因 role を判定し failure を記録する（デフォルト developer・迷ったらこれ／**テストコード欠陥起因の指摘は tester failure**／設計不備なら architect／計画不備なら planner。`--note` に理由必須。developer・tester は `--execution subagent`、architect/planner は `--execution persona`。developer・tester は `--tier` を省略（frontmatter/tier_selection から自己解決）、architect/planner の場合は `--tier` も明記する）:
   ```bash
   python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
     --role {developer|tester|architect|planner（帰属判定）} --outcome failure --gate E-1 \
     --execution {developer・tester=subagent / architect・planner=persona} \
     --complexity {セッションファイルの tier-routing複雑度: 行の値} \
     --tier {developer・tester は省略（frontmatter/tier_selection から自己解決） / architect・planner は親モデルのtier名（haiku/sonnet/opus）。判別不能なら unknown} \
     --note "{帰属理由を1行で（コード断片の逐語引用禁止）}"
   ```

**「全て許容して進む」の場合:**
AskUserQuestion で許容理由を確認する:
```json
{
  "questions": [{
    "question": "全指摘を許容する理由を教えてください。"
  }]
}
```
1. 全指摘の直下に `> **[許容]** {理由}` を Edit で追記する（検出記録は削除しない）
2. **review-hint 判断記録**: 全 `[CR-XX-NNN]` 指摘について `record_review_decision.py --decision accepted` で記録する
3. セッションファイルの `## うまくいったアプローチ` に `[許容例外] {指摘内容} → {許容理由}` の形式で追記し `patterns` に記録する
4. セッションファイルの `- [ ] code-review` を `- [x]` に Edit し、`現在地:` を `現在地: フェーズE レビュー中 / 次: security-review` に Edit して E-2 へ。
5. **tier-routing 結果記録**:
   ```bash
   python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
     --role developer --outcome success --gate E-1 \
     --execution subagent --complexity {セッションファイルの tier-routing複雑度: 行の値}
   ```

**「否認・再レビューを依頼する」の場合:**
追加の AskUserQuestion でフィードバックを確認し再実行する。
セッションファイルの `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する。
**tier-routing 結果記録**: なし（否認・再レビュー依頼は記録対象外）。

---

### E-2: security-reviewer エージェントの起動

Agent ツールで `security-reviewer` エージェントを起動する。

**review-hint 過去判断ヒント注入（レポート生成後）:**
security-reviewer がレポートを Write し終えたら、Bash で `.claude/skills/dev-workflow/scripts/review_hint_inject.py` に **両レポートのパス** を渡して呼ぶ。両方渡すことで重複指摘フラグ（同じ checklist_id を CR と SR が指摘）が判定される:

```bash
python .claude/skills/dev-workflow/scripts/review_hint_inject.py \
  .claude/reports/code-review-report-{ts1}.md \
  .claude/reports/security-review-report-{ts2}.md
```

これにより SR レポートにも過去判断ヒント + 重複指摘フラグが追記される。
CR レポートも上書きされる（既にヒントセクションがあれば二重追記は回避される）。

レポートの指摘の有無で分岐する。

**指摘がない場合:**
AskUserQuestion で確認する:
```json
{
  "questions": [{
    "question": "security-review-report を確認してください。どうしますか？",
    "options": [
      { "label": "承認・完了", "description": "問題なし。コミットを提案する" },
      { "label": "否認・再診断を依頼する", "description": "フィードバックを入力して再実行する" }
    ]
  }]
}
```

承認後 → セッションファイルの `- [ ] security-review` を `- [x]` に Edit し、`現在地:` を `現在地: 完了` に Edit する。続けて **「引き継ぎバックログの照合」**（後述の共通ステップ）を実行してからコミットを提案する。

**tier-routing 結果記録**:
```bash
python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
  --role developer --outcome success --gate E-2 \
  --execution subagent --complexity {セッションファイルの tier-routing複雑度: 行の値} \
  --final
```

**指摘がある場合:**
指摘一覧をテキストで提示してから AskUserQuestion で方針を確認する:
```json
{
  "questions": [{
    "question": "security-review-report に指摘があります。対応方針を選択してください。",
    "options": [
      { "label": "全て対応する", "description": "全指摘を修正計画に含めてフェーズ C へ" },
      { "label": "対応する指摘を選ぶ", "description": "指摘ごとに対応する/許容するを決める" },
      { "label": "全て許容して完了", "description": "全指摘を許容してコミットを提案する" },
      { "label": "否認・再診断を依頼する", "description": "フィードバックを入力して再実行する" }
    ]
  }]
}
```

**「全て対応する」の場合:**
全指摘に `> **[対応予定]**` をマークし、セッションファイルの `- [ ] security-review` を `- [x]` に Edit し、`現在地:` を `現在地: フェーズC 計画中（レビュー差し戻し）` に Edit してから**フェーズ C** へ（内部遷移・Step 0 なし）。

**tier-routing 結果記録（帰属判定）**: 指摘内容から最上流起因 role を判定し failure を記録する（デフォルト developer・迷ったらこれ／**テストコード欠陥起因の指摘は tester failure**／設計不備なら architect／計画不備なら planner。`--note` に理由必須。developer・tester は `--execution subagent`、architect/planner は `--execution persona`。developer・tester は `--tier` を省略（frontmatter/tier_selection から自己解決）、architect/planner の場合は `--tier` も明記する）:
```bash
python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
  --role {developer|tester|architect|planner（帰属判定）} --outcome failure --gate E-2 \
  --execution {developer・tester=subagent / architect・planner=persona} \
  --complexity {セッションファイルの tier-routing複雑度: 行の値} \
  --tier {developer・tester は省略（frontmatter/tier_selection から自己解決） / architect・planner は親モデルのtier名（haiku/sonnet/opus）。判別不能なら unknown} \
  --note "{帰属理由を1行で（コード断片の逐語引用禁止）}"
```

**「対応する指摘を選ぶ」の場合:**
続けて AskUserQuestion で確認する:
```json
{
  "questions": [{
    "question": "どの指摘を対応しますか？対応する指摘番号と、許容する指摘の理由を教えてください。"
  }]
}
```
1. 対応する指摘に `> **[対応予定]**` を追記する
2. 許容する指摘の直下に `> **[許容]** {理由}` を Edit で追記する（検出記録は削除しない）
3. **review-hint 判断記録**: 各指摘について Bash で c3.db に記録する（`[SR-XX-NNN]` を含むもののみ。`[SR-NEW]` は記録対象外、チェックリスト追加候補として別途扱う）:
   ```bash
   python .claude/skills/dev-workflow/scripts/record_review_decision.py \
     --checklist-id SR-K-002 \
     --finding "{指摘本文を 1 行で}" \
     --decision {fixed|accepted} \
     --reason "{許容理由（accepted の時のみ）}" \
     --reviewer security-reviewer
   ```
4. セッションファイルの `## うまくいったアプローチ` に `[許容例外] {指摘内容} → {許容理由}` の形式で追記し `patterns` に記録する
5. セッションファイルの `- [ ] security-review` を `- [x]` に Edit し、`現在地:` を `現在地: フェーズC 計画中（レビュー差し戻し）` に Edit してから**フェーズ C** へ（内部遷移・Step 0 なし）。
6. **tier-routing 結果記録（帰属判定）**: 指摘内容から最上流起因 role を判定し failure を記録する（デフォルト developer・迷ったらこれ／**テストコード欠陥起因の指摘は tester failure**／設計不備なら architect／計画不備なら planner。`--note` に理由必須。developer・tester は `--execution subagent`、architect/planner は `--execution persona`。developer・tester は `--tier` を省略（frontmatter/tier_selection から自己解決）、architect/planner の場合は `--tier` も明記する）:
   ```bash
   python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
     --role {developer|tester|architect|planner（帰属判定）} --outcome failure --gate E-2 \
     --execution {developer・tester=subagent / architect・planner=persona} \
     --complexity {セッションファイルの tier-routing複雑度: 行の値} \
     --tier {developer・tester は省略（frontmatter/tier_selection から自己解決） / architect・planner は親モデルのtier名（haiku/sonnet/opus）。判別不能なら unknown} \
     --note "{帰属理由を1行で（コード断片の逐語引用禁止）}"
   ```

**「全て許容して完了」の場合:**
AskUserQuestion で許容理由を確認する:
```json
{
  "questions": [{
    "question": "全指摘を許容する理由を教えてください。"
  }]
}
```
1. 全指摘の直下に `> **[許容]** {理由}` を Edit で追記する（検出記録は削除しない）
2. **review-hint 判断記録**: 全 `[SR-XX-NNN]` 指摘について `record_review_decision.py --decision accepted` で記録する
3. セッションファイルの `## うまくいったアプローチ` に `[許容例外] {指摘内容} → {許容理由}` の形式で追記し `patterns` に記録する
4. セッションファイルの `- [ ] security-review` を `- [x]` に Edit し、`現在地:` を `現在地: 完了` に Edit する。続けて **「引き継ぎバックログの照合」**（後述の共通ステップ）を実行してからコミットを提案する。
5. **tier-routing 結果記録**: 全許容で完了するのも「成功」としてカウント:
   ```bash
   python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \
     --role developer --outcome success --gate E-2 \
     --execution subagent --complexity {セッションファイルの tier-routing複雑度: 行の値} \
     --final
   ```

**「否認・再診断を依頼する」の場合:**
追加の AskUserQuestion でフィードバックを確認し再実行する。
セッションファイルの `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する。
**tier-routing 結果記録**: なし（否認・再診断依頼は記録対象外）。

---

## 引き継ぎバックログの照合（フェーズ E 共通ステップ）

フェーズ E の最終承認後、コミット提案の直前に必ず実行する。

引き継ぎバックログ（過去セッションから繰り越された `## 残タスク` 内の `- [ ]` 行のうち、ワークフローフェーズではない高レベル項目）が今回の作業で完了する場合、ここで `[x]` 化する。リリース時など節目の取りこぼしを防ぐ。

### 手順

1. session.tmp の `## 残タスク` セクションから `- [ ]` 行を抽出する
2. 当セッションの作業内容（DURATION・requirements-report タイトル・plan-report タイトル・関連コミット予定の内容）と、各 `- [ ]` 行を**キーワード照合**する（`F-XXX` / `Phase X` / 機能名 / 「Zenn」「リリース」「ドキュメント」などの名詞）
3. ワークフローフェーズ項目（`ヒアリング` / `設計` / `計画` / `tester:` / `developer:` / `code-review` / `security-review` で始まる行）は対象外。引き継ぎバックログのみを候補にする
4. 候補が**ゼロ件**ならこのステップをスキップしてそのままコミット提案へ
5. 候補が**1 件以上**あれば AskUserQuestion を提示する:

```json
{
  "questions": [{
    "question": "今回の作業で完了する引き継ぎバックログ項目があれば [x] にしますか？",
    "options": [
      { "label": "全て [x] にする", "description": "候補を全て完了扱いにする" },
      { "label": "個別に選ぶ", "description": "項目ごとに確認する" },
      { "label": "更新しない", "description": "後で手動確認する" }
    ]
  }]
}
```

6. 承認された項目は Edit で `- [ ] {元の文}` を `- [x] {元の文} → 完了` に置換する（コミット直前のためハッシュは未確定）
7. ステップ完了後、通常通りコミット提案へ進む

> 補足: バックログの陳腐化（例: 「v1.0.0〜v1.6.0 の Zenn 記事化」のように完了済みバージョンを含む）も検出したらユーザーに記述更新を促す。
