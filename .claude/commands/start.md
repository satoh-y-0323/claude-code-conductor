# /start コマンド

開発ワークフローの入口。対話部分は親 Claude が直接担当する（Agent ツールは使わない）。

ヒアリング → 設計 → 計画は同一コンテキストで流れるため、前フェーズの内容を読み直す必要はない。
レポートは「セッションをまたいだ記録」と「サブエージェントへの引き渡し」のために書く。

---

## Step 1: 開始地点の選択

AskUserQuestion ツールで以下を提示する:

```json
{
  "questions": [{
    "question": "どこから始めますか？",
    "options": [
      { "label": "ヒアリング", "description": "要件を整理するところから始める（新規・大きな変更）" },
      { "label": "設計", "description": "要件は明確なので設計から始める" },
      { "label": "計画", "description": "設計済みなのでタスク計画から始める" },
      { "label": "実装", "description": "計画済みなので実装から始める" }
    ]
  }]
}
```

---

## フェーズ A: ヒアリング

`agents/interviewer.md` を Read してペルソナを採用する。

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
      { "label": "リファクタリング", "description": "動作は変えずに内部を整理したい" },
      { "label": "その他", "description": "自由に入力してください" }
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
      { "label": "パフォーマンス問題", "description": "遅い・重いを解決したい" },
      { "label": "その他・自由入力" }
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
      { "label": "パフォーマンス要件がある", "description": "速度・負荷の基準がある" },
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
      { "label": "可用性", "description": "止まらないことを重視" },
      { "label": "特になし・バランスよく" }
    ],
    "multiSelect": true
  }]
}
```

### A-5: requirements-report の生成と承認

収集した内容（コンテキスト内にある）をもとに `.claude/reports/requirements-report-YYYYMMDD-HHMMSS.md` に Write する。
内容を提示した後、Approval Flow（CLAUDE.md）に従い AskUserQuestion で確認する:

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

承認後 → フェーズ B（設計）へ。

---

## フェーズ B: 設計

`agents/architect.md` を Read してペルソナを採用する。

**フェーズ A から続いている場合:** 要件はコンテキスト内にあるため読み直し不要。
**「設計から」を選んで直接開始した場合:** Glob で `.claude/reports/requirements-report-*.md` の最新を Read する。

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

承認後 → フェーズ C（計画）へ。

---

## フェーズ C: 計画

`agents/planner.md` を Read してペルソナを採用する。

**フェーズ B から続いている場合:** 要件・設計はコンテキスト内にあるため読み直し不要。
**「計画から」を選んで直接開始した場合:** 利用可能な全レポートを Glob で探して Read する。

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
      { "label": "承認", "description": "/develop で実装フェーズへ進む" },
      { "label": "否認・修正を依頼する", "description": "フィードバックを入力して計画をやり直す" },
      { "label": "否認・自分でファイルを編集する", "description": "reports/ のファイルを直接編集してから続ける" }
    ]
  }]
}
```

承認後 → `/develop` を案内する。

---

## フェーズ D: 実装への引き渡し

実装フェーズ（developer・tester）は Agent ツールで起動するため `/develop` コマンドで管理する。

```
次のステップ: /develop を実行してください
plan-report が生成されているため、developer が参照して実装を開始できます。
```
