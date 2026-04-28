# Dev Workflow

要件定義から実装・レビューまでを複数エージェントで連携させるフルワークフロー。
`commands/` の各コマンドからこのファイルを Read して指定フェーズから実行する。
フェーズ間の遷移はこのファイル内で完結する（外部コマンド呼び出し不要）。

---

## フェーズ A: ヒアリング

`agents/interviewer.md` を Read してペルソナを採用する。

今日のセッションファイル（`.claude/memory/sessions/YYYYMMDD.tmp`）に以下を追記する（未登録の場合のみ）:
- `- [ ] ヒアリング` / `- [ ] 設計` / `- [ ] 計画`

### A-1: 目的

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "このプロジェクト・機能の目的を教えてください",
    "options": [
      { "label": "新機能の追加", "description": "新しい機能を実装したい" },
      { "label": "既存機能の改善", "description": "現在の動作を変えたい・良くしたい" },
      { "label": "バグ修正", "description": "問題のある動作を直したい" },
      { "label": "リファクタリング", "description": "動作は変えずに内部を整理したい" }
    ]
  }]
}
```

### A-2: 背景・きっかけ

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

### A-3: 制約・前提条件

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

### A-4: 非機能要件

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

### A-5: requirements-report の生成と承認

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

`agents/architect.md` を Read してペルソナを採用する。

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

`agents/planner.md` を Read してペルソナを採用する。

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

## フェーズ D: TDD

**フェーズ C から続いている場合:** plan-report はコンテキスト内にあるため読み直し不要。
**直接開始の場合:** Glob で `.claude/reports/plan-report-*.md` の最新を Read する。存在しない場合はフェーズ C から始めるよう案内して終了する。

今日のセッションファイルに以下を追記する（未登録の場合のみ）:
- `- [ ] tester: Red フェーズ`
- `- [ ] developer: Green フェーズ`
- `- [ ] developer: Refactor フェーズ`
- `- [ ] tester: 最終確認`

### D-1: tester（Red フェーズ）

Agent ツールで `tester` エージェントを起動する。→ 失敗するテストを先に作成する。

完了後 → セッションファイルの `- [ ] tester: Red フェーズ` を `- [x]` に Edit する。

### D-2: developer（Green フェーズ）

Agent ツールで `developer` エージェントを起動する。→ テストが通る実装を行う。

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

Agent ツールで `tester` エージェントを起動する。→ 全テストの合否を確認する。

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

Agent ツールで `tester` エージェントを起動する。

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
3. セッションファイルの `## うまくいったアプローチ` に `[許容例外] {指摘内容} → {許容理由}` の形式で追記し `patterns` に記録する
4. セッションファイルの `- [ ] code-review` を `- [x]` に Edit してから**フェーズ C** へ（内部遷移・Step 0 なし）。

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
2. セッションファイルの `## うまくいったアプローチ` に `[許容例外] {指摘内容} → {許容理由}` の形式で追記し `patterns` に記録する
3. セッションファイルの `- [ ] code-review` を `- [x]` に Edit して E-2 へ。

**「否認・再レビューを依頼する」の場合:**
追加の AskUserQuestion でフィードバックを確認し再実行する。
セッションファイルの `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する。

---

### E-2: security-reviewer エージェントの起動

Agent ツールで `security-reviewer` エージェントを起動する。

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

承認後 → セッションファイルの `- [ ] security-review` を `- [x]` に Edit してコミットを提案する。

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
3. セッションファイルの `## うまくいったアプローチ` に `[許容例外] {指摘内容} → {許容理由}` の形式で追記し `patterns` に記録する
4. セッションファイルの `- [ ] security-review` を `- [x]` に Edit してから**フェーズ C** へ（内部遷移・Step 0 なし）。

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
2. セッションファイルの `## うまくいったアプローチ` に `[許容例外] {指摘内容} → {許容理由}` の形式で追記し `patterns` に記録する
3. セッションファイルの `- [ ] security-review` を `- [x]` に Edit してコミットを提案する。

**「否認・再診断を依頼する」の場合:**
追加の AskUserQuestion でフィードバックを確認し再実行する。
セッションファイルの `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する。
