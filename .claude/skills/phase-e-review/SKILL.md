---
description: レビューフェーズ。code-reviewer と security-reviewer を順次起動し、指摘の対応方針を確定する。review-hint 注入と tier-routing 結果記録も担当する。dev-workflow フェーズ E 専用 skill。
disable-model-invocation: false
user-invocable: false
---

# Phase E: レビュー

今日のセッションファイルに以下を追記する（未登録の場合のみ）:
- `- [ ] code-review`
- `- [ ] security-review`

---

## E-1: code-reviewer エージェントの起動

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
全指摘に `> **[対応予定]**` をマークし、セッションファイルの `- [ ] code-review` を `- [x]` に Edit してから **phase-c-plan** へ。

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
5. セッションファイルの `- [ ] code-review` を `- [x]` に Edit してから **phase-c-plan** へ。

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

## E-2: security-reviewer エージェントの起動

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

承認後 → セッションファイルの `- [ ] security-review` を `- [x]` に Edit する。続けて **「引き継ぎバックログの照合」**（後述の共通ステップ）を実行してからコミットを提案する。
**tier-routing 結果記録**: フェーズ E の最終承認時のみ Bash で記録する（多重カウント防止のため E-1 では記録しない）:
```bash
python .claude/skills/dev-workflow/scripts/record_tier_outcome.py --outcome success
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
全指摘に `> **[対応予定]**` をマークし、セッションファイルの `- [ ] security-review` を `- [x]` に Edit してから **phase-c-plan** へ。

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
5. セッションファイルの `- [ ] security-review` を `- [x]` に Edit してから **phase-c-plan** へ。

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
   python .claude/skills/dev-workflow/scripts/record_tier_outcome.py --outcome success
   ```

**「否認・再診断を依頼する」の場合:**
追加の AskUserQuestion でフィードバックを確認し再実行する。
セッションファイルの `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する。
**tier-routing 結果記録**: 否認は「失敗」としてカウント:
```bash
python .claude/skills/dev-workflow/scripts/record_tier_outcome.py --outcome failure
```

**「全て対応する」「対応する指摘を選ぶ」の場合（phase-c-plan へ戻る）:**
これらも tier の選択がコスト最適でなかったとみなし、**tier-routing 結果記録**で失敗をカウントしてから **phase-c-plan** へ:
```bash
python .claude/skills/dev-workflow/scripts/record_tier_outcome.py --outcome failure
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

---

## 次フェーズへの遷移

レビュー完了後はコミット提案へ進む。指摘ありで戻る場合は **phase-c-plan** へ。
