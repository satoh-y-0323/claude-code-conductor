---
description: ヒアリング→設計→計画→実装→レビューの全フェーズワークフロー。/start・/develop・/code-review スキルが内部参照する。
disable-model-invocation: false
user-invocable: false
---

# Dev Workflow

要件定義から実装・レビューまでを複数エージェントで連携させるフルワークフロー。
`.claude/skills/` の各スキルからこのファイルを Read して指定フェーズから実行する。
フェーズ間の遷移はこのファイル内で完結する（外部コマンド呼び出し不要）。

---

## フェーズ A: ヒアリング

`.claude/agents/interviewer.md` を Read してペルソナを採用する。

### TASK_TYPE の確認

今日のセッションファイル（`.claude/memory/sessions/YYYYMMDD.tmp`）の冒頭から
`^TASK_TYPE: (\S+)$` を抽出してコンテキストに保持する。

抽出した値が `feature / bug-fix / refactor / security-audit / docs` のいずれでもない場合
（空欄、enum 外の文字列など）は「TASK_TYPE 未確定」とみなして以下のフォールバックを実施する。

`TASK_TYPE` が空欄や行が無い、または enum 外の場合（`/develop` 直接呼び出しなど Step 0.5 を経由しないケース）:
- Skill ツールで `task-routing` を呼ぶ。`args` に `from_start=true` を渡すことで
  「種別確認のみモード」で動作させ、Step 2〜4 をスキップさせる（`/start` Step 0.5 と同じ呼び方）:
  ```
  Skill(skill="task-routing", args="from_start=true")
  ```
- 戻ってきた種別を当日 tmp 冒頭の `TASK_TYPE:` 行に Edit で書き込む（dev-workflow 側で書き込む）
- これにより task-routing が再帰的に `/start` を Read して再び `task-routing` を呼ぶ事態を回避する

`TASK_TYPE` が `feature` 以外（bug-fix / refactor / security-audit / docs）の場合は、
本来 dev-workflow フェーズ A〜E のフルパスを通す必要がない種別であることを通知し、
ユーザーに本当に dev-workflow を実行するか確認する（誤起動の防止）。

今日のセッションファイルに以下を追記する（未登録の場合のみ）:
- `- [ ] ヒアリング` / `- [ ] 設計` / `- [ ] 計画`

### A-1: 背景・きっかけ

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "背景・きっかけを教えてください（なぜ今これが必要ですか？）",
    "options": [
      { "label": "ユーザーからの要望", "description": "具体的な声があった" },
      { "label": "ビジネス上の要件", "description": "事業的な理由がある" },
      { "label": "技術的な負債解消", "description": "将来のために今直したい" },
      { "label": "パフォーマンス問題", "description": "遅い・重いを解決したい" }
    ]
  }]
}
```

### A-2: 制約・前提条件

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "制約や前提条件はありますか？（複数選択可）",
    "options": [
      { "label": "納期がある", "description": "期日を後で教えてください" },
      { "label": "既存APIを壊せない", "description": "後方互換性が必要" },
      { "label": "特定の技術スタックに限定", "description": "使える技術が決まっている" },
      { "label": "特になし" }
    ],
    "multiSelect": true
  }]
}
```

制約を選んだ場合は補足情報（納期の日付など）を追加で確認する。

### A-3: 非機能要件

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "特に重視したい品質特性はありますか？",
    "options": [
      { "label": "セキュリティ", "description": "認証・認可・データ保護を重視" },
      { "label": "パフォーマンス", "description": "速度・スループットを重視" },
      { "label": "保守性", "description": "読みやすさ・変更しやすさを重視" },
      { "label": "特になし・バランスよく" }
    ],
    "multiSelect": true
  }]
}
```

### A-4: requirements-report の生成と承認

収集した内容をもとに `.claude/reports/requirements-report-YYYYMMDD-HHMMSS.md` に Write する。
ファイル冒頭のフロントマターに `task_type: {task_type}` を含めること（後段の architect / planner が参照する）:

```yaml
---
task_type: {task_type}   # /start Step 0.5 で確定した種別（feature / bug-fix / refactor / security-audit / docs のいずれか）を埋める
---
```

`{task_type}` は plain string で、前述の TASK_TYPE 確認フェーズで保持した値と同じものを書く。

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

承認後 → セッションファイルの `- [ ] ヒアリング` を `- [x] ヒアリング` に Edit して**フェーズ B** へ。

**知識蓄積:**
- 「否認・修正を依頼する」: `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する
- 承認かつ非自明なアプローチが有効だった場合: `## うまくいったアプローチ` に追記し `patterns` にも追加する

---

## フェーズ B: 設計

`.claude/agents/architect.md` を Read してペルソナを採用する。

**フェーズ A から続いている場合:** 要件はコンテキスト内にあるため読み直し不要。
**直接開始の場合:** Glob で `.claude/reports/requirements-report-*.md` の最新を Read する。
requirements-report のフロントマターに `task_type:` があれば読み取ってコンテキストに保持する
（後段で agent 起動方針の判定に使う）。

今日のセッションファイルに以下を追記する（未登録の場合のみ）:
- `- [ ] 設計` / `- [ ] 計画`

### B-1: 技術スタックの確認

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "技術スタックについて制約はありますか？",
    "options": [
      { "label": "既存スタックに合わせる", "description": "使用中の言語・FWに統一する" },
      { "label": "最適なものを選んでほしい", "description": "推薦に任せる" },
      { "label": "指定がある", "description": "使う技術を具体的に伝えます" }
    ]
  }]
}
```

### B-2: 設計と不明点の確認

要件をもとに設計案を作成する。不明点があれば AskUserQuestion ツールで確認する。

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

承認後 → セッションファイルの `- [ ] 設計` を `- [x] 設計` に Edit して**フェーズ C** へ。

**知識蓄積:**
- 「否認・修正を依頼する」: `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する
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

承認後 → セッションファイルの `- [ ] 計画` を `- [x] 計画` に Edit して**フェーズ D** へ。

**知識蓄積:**
- 「否認・修正を依頼する」: `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する
- 承認かつ非自明なアプローチが有効だった場合: `## うまくいったアプローチ` に追記し `patterns` にも追加する

---

## フェーズ D: 実装

**フェーズ C から続いている場合:** plan-report はコンテキスト内にあるため読み直し不要。
**直接開始の場合:** Glob で `.claude/reports/plan-report-*.md` の最新を Read する。存在しない場合はフェーズ C から始めるよう案内して終了する。

### D-0: 実行モード自動判別

plan-report の冒頭を Read し、YAML フロントマター（`---` で始まり `po_plan_version: "0.1"` を含む）の有無を確認する。

**フロントマターありの場合:**
1. **最初に必ず** `.claude/skills/parallel-agents/SKILL.md` を Read する（記憶・推測で進めない）
2. `.claude/skills/parallel-agents/SKILL.md` の手順に完全に従って wave 単位で実装を進める
3. 全 wave 完了後はフェーズ E（レビュー）へ進む（wave に reviewer タスクが含まれていれば E をスキップ可能と案内する）

**フロントマターなしの場合（legacy フォールバック）:**

今日のセッションファイルに以下を追記する（未登録の場合のみ）:
- `- [ ] tester: Red フェーズ`
- `- [ ] developer: Green フェーズ`
- `- [ ] developer: Refactor フェーズ`
- `- [ ] tester: 最終確認`

D-1 へ進む。

### D-1: tester（Red フェーズ）

Agent ツールで `tester` エージェントを起動する。→ 失敗するテストを先に作成する。**必ず `.claude/reports/test-report-YYYYMMDD-HHMMSS.md` を Write してから終了すること。**

完了後 → セッションファイルの `- [ ] tester: Red フェーズ` を `- [x]` に Edit する。

### D-2: developer（Green フェーズ）

Agent ツールで `developer` エージェントを起動する。→ テストが通る実装を行う。

### D-2.5: Stuck チェック

Glob で `.claude/reports/debug-needed-*.md` の最新を確認する。

**ファイルが存在する場合:**
1. Agent ツールで `systematic-debugger` を起動する。プロンプトに debug-needed ファイルのパスと内容を含める
2. 生成された `.claude/reports/debug-analysis-*.md` を Glob で取得して Read する
3. D-2 の developer を再実行する。プロンプトに debug-analysis の内容を追加注入する
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

承認後 → セッションファイルの `- [ ] developer: Green フェーズ` を `- [x]` に Edit する。

**知識蓄積:**
- 「否認・再実装を依頼する」: `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する

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

### D-4: developer（Refactor フェーズ）

Agent ツールで `developer` エージェントを起動する。→ テストを壊さずにコードを整理する。

完了後 → セッションファイルの `- [ ] developer: Refactor フェーズ` を `- [x]` に Edit する。

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

承認後 → セッションファイルの `- [ ] tester: 最終確認` を `- [x]` に Edit して**フェーズ E** へ。

**知識蓄積:**
- 「否認・修正を依頼する」: `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する

---

## フェーズ E: レビュー

今日のセッションファイルに以下を追記する（未登録の場合のみ）:
- `- [ ] code-review`
- `- [ ] security-review`

### E-1: code-reviewer エージェントの起動

Agent ツールで `code-reviewer` エージェントを起動する。

**F-001 過去判断ヒント注入（レポート生成後）:**
code-reviewer がレポートを Write し終えたら、Bash で `.claude/hooks/review_hint_inject.py` を呼んで過去判断ヒントをレポート末尾に追記する:

```bash
python .claude/hooks/review_hint_inject.py .claude/reports/code-review-report-{timestamp}.md
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

承認後 → セッションファイルの `- [ ] code-review` を `- [x]` に Edit して E-2 へ。

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
全指摘に `> **[対応予定]**` をマークし、セッションファイルの `- [ ] code-review` を `- [x]` に Edit してから**フェーズ C** へ（内部遷移・Step 0 なし）。

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
3. **F-001 判断記録**: 各指摘について Bash で c3.db に記録する（`[CR-XX-NNN]` を含むもののみ）:
   ```bash
   python .claude/hooks/record_review_decision.py \
     --checklist-id CR-Q-001 \
     --finding "{指摘本文を 1 行で}" \
     --decision {fixed|accepted} \
     --reason "{許容理由（accepted の時のみ）}" \
     --reviewer code-reviewer
   ```
4. セッションファイルの `## うまくいったアプローチ` に `[許容例外] {指摘内容} → {許容理由}` の形式で追記し `patterns` に記録する
5. セッションファイルの `- [ ] code-review` を `- [x]` に Edit してから**フェーズ C** へ（内部遷移・Step 0 なし）。

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
2. **F-001 判断記録**: 全 `[CR-XX-NNN]` 指摘について `record_review_decision.py --decision accepted` で記録する
3. セッションファイルの `## うまくいったアプローチ` に `[許容例外] {指摘内容} → {許容理由}` の形式で追記し `patterns` に記録する
4. セッションファイルの `- [ ] code-review` を `- [x]` に Edit して E-2 へ。

**「否認・再レビューを依頼する」の場合:**
追加の AskUserQuestion でフィードバックを確認し再実行する。
セッションファイルの `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する。

---

### E-2: security-reviewer エージェントの起動

Agent ツールで `security-reviewer` エージェントを起動する。

**F-001 過去判断ヒント注入（レポート生成後）:**
security-reviewer がレポートを Write し終えたら、Bash で `.claude/hooks/review_hint_inject.py` に **両レポートのパス** を渡して呼ぶ。両方渡すことで重複指摘フラグ（同じ checklist_id を CR と SR が指摘）が判定される:

```bash
python .claude/hooks/review_hint_inject.py \
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

承認後 → セッションファイルの `- [ ] security-review` を `- [x]` に Edit する。続けて **「引き継ぎバックログの照合」**（後述の共通ステップ）を実行してからコミットを提案する。
**F-005 結果記録**: フェーズ E の最終承認時のみ Bash で記録する（多重カウント防止のため E-1 では記録しない）:
```bash
python .claude/hooks/record_tier_outcome.py --outcome success
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
全指摘に `> **[対応予定]**` をマークし、セッションファイルの `- [ ] security-review` を `- [x]` に Edit してから**フェーズ C** へ（内部遷移・Step 0 なし）。

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
3. **F-001 判断記録**: 各指摘について Bash で c3.db に記録する（`[SR-XX-NNN]` を含むもののみ）:
   ```bash
   python .claude/hooks/record_review_decision.py \
     --checklist-id SR-K-002 \
     --finding "{指摘本文を 1 行で}" \
     --decision {fixed|accepted} \
     --reason "{許容理由（accepted の時のみ）}" \
     --reviewer security-reviewer
   ```
4. セッションファイルの `## うまくいったアプローチ` に `[許容例外] {指摘内容} → {許容理由}` の形式で追記し `patterns` に記録する
5. セッションファイルの `- [ ] security-review` を `- [x]` に Edit してから**フェーズ C** へ（内部遷移・Step 0 なし）。

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
2. **F-001 判断記録**: 全 `[SR-XX-NNN]` 指摘について `record_review_decision.py --decision accepted` で記録する
3. セッションファイルの `## うまくいったアプローチ` に `[許容例外] {指摘内容} → {許容理由}` の形式で追記し `patterns` に記録する
4. セッションファイルの `- [ ] security-review` を `- [x]` に Edit する。続けて **「引き継ぎバックログの照合」**（後述の共通ステップ）を実行してからコミットを提案する。
5. **F-005 結果記録**: 全許容で完了するのも「成功」としてカウント:
   ```bash
   python .claude/hooks/record_tier_outcome.py --outcome success
   ```

**「否認・再診断を依頼する」の場合:**
追加の AskUserQuestion でフィードバックを確認し再実行する。
セッションファイルの `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する。
**F-005 結果記録**: 否認は「失敗」としてカウント:
```bash
python .claude/hooks/record_tier_outcome.py --outcome failure
```

**「全て対応する」「対応する指摘を選ぶ」の場合（フェーズ C へ戻る）:**
これらも tier の選択がコスト最適でなかったとみなし、**F-005 結果記録**で失敗をカウントしてからフェーズ C へ:
```bash
python .claude/hooks/record_tier_outcome.py --outcome failure
```

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
