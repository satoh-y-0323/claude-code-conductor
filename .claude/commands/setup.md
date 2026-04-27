# /setup コマンド

プロジェクトのコーディング規約を設定する。
ヒアリングは親 Claude が担当し、収集後に project-setup エージェントを起動する。

---

## Phase 1: 技術スタックのヒアリング（親 Claude が実施）

### Q1: 言語・バージョン

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "使用する言語とバージョンを教えてください",
    "options": [
      { "label": "Python", "description": "バージョンを後で確認します" },
      { "label": "TypeScript / JavaScript", "description": "バージョンを後で確認します" },
      { "label": "Go", "description": "バージョンを後で確認します" },
      { "label": "Java / Kotlin", "description": "バージョンを後で確認します" },
      { "label": "C# / .NET", "description": "バージョンを後で確認します" },
      { "label": "その他・自由入力" }
    ]
  }]
}
```

選択後にバージョンを確認する。

### Q2: フレームワーク

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "使用するフレームワーク・主要ライブラリを教えてください",
    "options": [
      { "label": "使用するものがある", "description": "具体的に入力してください" },
      { "label": "まだ決めていない" },
      { "label": "使用しない" }
    ]
  }]
}
```

### Q3: 実行環境

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "実行環境はどれですか？",
    "options": [
      { "label": "サーバーサイド（API・バックエンド）" },
      { "label": "ブラウザ（フロントエンド）" },
      { "label": "両方（フルスタック）" },
      { "label": "CLI ツール" },
      { "label": "その他・自由入力" }
    ]
  }]
}
```

### Q4: データベース

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "データベースは使いますか？",
    "options": [
      { "label": "PostgreSQL" },
      { "label": "MySQL / MariaDB" },
      { "label": "SQLite" },
      { "label": "MongoDB" },
      { "label": "その他・自由入力" },
      { "label": "使わない" }
    ]
  }]
}
```

### Q5: テストフレームワーク

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "テストフレームワークは決まっていますか？",
    "options": [
      { "label": "決まっている", "description": "具体的に入力してください" },
      { "label": "まだ決めていない" }
    ]
  }]
}
```

---

## Phase 2: 独自規約のヒアリング（親 Claude が実施）

### Q6: 参考スタイルガイド

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "参考にするスタイルガイドはありますか？",
    "options": [
      { "label": "公式スタイルガイドに従う", "description": "PEP8・Google・StandardJS 等" },
      { "label": "Airbnb スタイル" },
      { "label": "独自ルールがある", "description": "後で詳しく教えてください" },
      { "label": "特になし・おすすめに任せる" }
    ]
  }]
}
```

### Q7: コメント方針

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "コードコメントの方針を教えてください",
    "options": [
      { "label": "最小限にする", "description": "コード自体を読みやすく書く" },
      { "label": "積極的に書く" },
      { "label": "英語で統一" },
      { "label": "日本語で統一" },
      { "label": "特になし" }
    ],
    "multiSelect": true
  }]
}
```

### Q8: テストカバレッジ

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "テストカバレッジの目標はありますか？",
    "options": [
      { "label": "80% 以上" },
      { "label": "クリティカルパスのみ" },
      { "label": "数値目標なし" },
      { "label": "その他・自由入力" }
    ]
  }]
}
```

### Q9: その他の規約

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "他に規約として残しておきたいことはありますか？",
    "options": [
      { "label": "ある", "description": "自由に入力してください" },
      { "label": "特になし" }
    ]
  }]
}
```

---

## Phase 3: project-setup エージェントの起動

全ヒアリング結果を整理して Agent ツールで `project-setup` エージェントを起動する。

プロンプト形式:
```
以下の情報をもとに rules/ ファイルを生成してください。

## 技術スタック
- 言語・バージョン: {回答}
- フレームワーク: {回答}
- 実行環境: {回答}
- データベース: {回答}
- テストフレームワーク: {回答}

## チーム規約
- 参考スタイルガイド: {回答}
- コメント方針: {回答}
- テストカバレッジ目標: {回答}
- その他: {回答}
```

---

## Phase 4: 完了報告

```
セットアップ完了:
  rules/coding-standards.md    — 標準コーディング規約
  rules/project-conventions.md — プロジェクト固有の規約

次回から architect・developer・tester・code-reviewer・security-reviewer
がこれらのルールを参照して作業します。
```
