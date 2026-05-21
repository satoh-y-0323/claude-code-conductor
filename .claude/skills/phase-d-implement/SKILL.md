---
description: 実装フェーズ。plan-report の po_plan_version 有無で parallel-agents モードと legacy TDD モードを自動判別する。当日の debug-analysis があれば bug-fix モードに分岐。dev-workflow フェーズ D 専用 skill。
disable-model-invocation: false
user-invocable: false
---

# Phase D: 実装

**フェーズ C から続いている場合:** plan-report はコンテキスト内にあるため読み直し不要。
**直接開始の場合:** D-0 で実行モードを判定する。

---

## D-0: 実行モード自動判別

以下の順で実行モードを判定する:

1. Glob で `.claude/reports/plan-report-*.md` の最新が存在する場合、冒頭の YAML フロントマター
   （`---` で始まり `po_plan_version: "0.1"` を含む）の有無を確認する。
2. plan-report が存在せず、**当日タイムスタンプ**の `.claude/reports/debug-analysis-*.md` が存在する場合は **bug-fix モード** とする。
   当日判定は LLM のテキスト解釈ではなく以下の Bash で機械的に取得すること（前セッションの残骸 debug-analysis による意図しない bug-fix モード突入を防ぐ）:

   ```bash
   python -c "import os, glob, datetime; today = datetime.datetime.now().strftime('%Y%m%d'); files = sorted(glob.glob('.claude/reports/debug-analysis-*.md')); today_files = [f for f in files if os.path.basename(f).startswith(f'debug-analysis-{today}-')]; print(today_files[-1] if today_files else '')"
   ```

   標準出力が空でなければそのパスを bug-fix モードの入力として保持する。空なら debug-analysis を「無し」とみなし判定 3 へ進む。
3. plan-report も当日 debug-analysis も存在しない場合は **phase-c-plan** から始めるよう案内して終了する。

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
通常通り **phase-c-plan**（計画）へ戻る。次回からは plan-report が生成されるため、
legacy TDD モードまたは parallel-agents モードで実装される。

---

## D-1: tester（Red フェーズ）

Agent ツールで `tester` エージェントを起動する。→ 失敗するテストを先に作成する。**必ず `.claude/reports/test-report-YYYYMMDD-HHMMSS.md` を Write してから終了すること。**

完了後 → セッションファイルの `- [ ] tester: Red フェーズ` を `- [x]` に Edit する。

## D-2: developer（Green フェーズ）

Agent ツールで `developer` エージェントを起動する。→ テストが通る実装を行う。

## D-2.5: Stuck チェック

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

## D-3: tester（確認）

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

## D-4: developer（Refactor フェーズ）

Agent ツールで `developer` エージェントを起動する。→ テストを壊さずにコードを整理する。

完了後 → セッションファイルの `- [ ] developer: Refactor フェーズ` を `- [x]` に Edit する。

## D-5: tester（最終確認）

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

## 次フェーズへの遷移

承認後、次は **phase-e-review** へ進む。
LLM は Skill ツールで `phase-e-review` を呼び出すか、
`.claude/skills/phase-e-review/SKILL.md` を Read してフェーズ E を開始する。
