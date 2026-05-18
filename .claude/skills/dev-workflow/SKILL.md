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
完了後、セッションファイルの `- [ ] developer: 修正実装` を `- [x]` に Edit する。

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

「全合格」承認後 → セッションファイルの `- [ ] tester: 動作確認` を `- [x]` に Edit して**フェーズ E** へ。
「不合格あり」を選んだ場合 → D-2 に戻る。合格するまで繰り返す。

bug-fix モード固有の動作:
- D-1（Red フェーズ）をスキップする理由: 既存の不具合自体が「足りないテスト」を示しており、developer が修正と一緒に回帰テストを追加する運用とする
- D-4（Refactor フェーズ）をスキップする理由: 不具合修正のスコープを最小に保つため。リファクタが必要な場合は後段のレビュー指摘で改めて計画化する

フェーズ E（レビュー）で指摘があり「全て対応する」「対応する指摘を選ぶ」を選んだ場合は、
通常通りフェーズ C（計画）へ戻る。次回からは plan-report が生成されるため、
legacy TDD モードまたは parallel-agents モードで実装される。

### D-1: tester（Red フェーズ）

Agent ツールで `tester` エージェントを起動する。→ 失敗するテストを先に作成する。**必ず `.claude/reports/test-report-YYYYMMDD-HHMMSS.md` を Write してから終了すること。**

完了後 → セッションファイルの `- [ ] tester: Red フェーズ` を `- [x]` に Edit する。

### D-2: developer（Green フェーズ）

Agent ツールで `developer` エージェントを起動する。→ テストが通る実装を行う。

### D-2.5: Stuck チェック

Glob で `.claude/reports/debug-needed-*.md` の最新を確認する。

**ファイルが存在する場合:**
1. Agent ツールで `systematic-debugger` を起動する。プロンプトに debug-needed ファイルのパスのみを含め、内容は agent 側で Read させる（プロンプトに直接展開しない）[SR-AI-001]
2. 生成された `.claude/reports/debug-analysis-*.md` を Glob で取得してパスのみコンテキストに保持する（内容は次段で agent に Read させる）
3. D-2 の developer を再実行する。プロンプトに debug-analysis の**ファイルパスのみ**を含め、内容は agent 側で Read させる（プロンプトに展開しない）[SR-AI-001]
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

承認後 → セッションファイルの `- [ ] developer: Green フェーズ` を `- [x]` に Edit する（**bug-fix モードではこの Edit をスキップする**。D-2 で `- [ ] developer: 修正実装` を既に `[x]` 化済みのため、Green フェーズ行自体がセッションファイルに存在しない）。

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

**review-hint 過去判断ヒント注入（レポート生成後）:**
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
3. **review-hint 判断記録**: 各指摘について Bash で c3.db に記録する（`[CR-XX-NNN]` を含むもののみ。`[CR-NEW]` は記録対象外、チェックリスト追加候補として別途扱う）:
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
2. **review-hint 判断記録**: 全 `[CR-XX-NNN]` 指摘について `record_review_decision.py --decision accepted` で記録する
3. セッションファイルの `## うまくいったアプローチ` に `[許容例外] {指摘内容} → {許容理由}` の形式で追記し `patterns` に記録する
4. セッションファイルの `- [ ] code-review` を `- [x]` に Edit して E-2 へ。

**「否認・再レビューを依頼する」の場合:**
追加の AskUserQuestion でフィードバックを確認し再実行する。
セッションファイルの `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する。

---

### E-2: security-reviewer エージェントの起動

Agent ツールで `security-reviewer` エージェントを起動する。

**review-hint 過去判断ヒント注入（レポート生成後）:**
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
**tier-routing 結果記録**: フェーズ E の最終承認時のみ Bash で記録する（多重カウント防止のため E-1 では記録しない）:
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
3. **review-hint 判断記録**: 各指摘について Bash で c3.db に記録する（`[SR-XX-NNN]` を含むもののみ。`[SR-NEW]` は記録対象外、チェックリスト追加候補として別途扱う）:
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
2. **review-hint 判断記録**: 全 `[SR-XX-NNN]` 指摘について `record_review_decision.py --decision accepted` で記録する
3. セッションファイルの `## うまくいったアプローチ` に `[許容例外] {指摘内容} → {許容理由}` の形式で追記し `patterns` に記録する
4. セッションファイルの `- [ ] security-review` を `- [x]` に Edit する。続けて **「引き継ぎバックログの照合」**（後述の共通ステップ）を実行してからコミットを提案する。
5. **tier-routing 結果記録**: 全許容で完了するのも「成功」としてカウント:
   ```bash
   python .claude/hooks/record_tier_outcome.py --outcome success
   ```

**「否認・再診断を依頼する」の場合:**
追加の AskUserQuestion でフィードバックを確認し再実行する。
セッションファイルの `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する。
**tier-routing 結果記録**: 否認は「失敗」としてカウント:
```bash
python .claude/hooks/record_tier_outcome.py --outcome failure
```

**「全て対応する」「対応する指摘を選ぶ」の場合（フェーズ C へ戻る）:**
これらも tier の選択がコスト最適でなかったとみなし、**tier-routing 結果記録**で失敗をカウントしてからフェーズ C へ:
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
