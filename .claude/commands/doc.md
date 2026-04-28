# /doc コマンド

ドキュメントを対話形式でヒアリングして生成する。
対話部分は親 Claude が担当し、生成は doc-writer エージェントが行う。

---

## Step 1: ドキュメント種類の選択

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "作成するドキュメントの種類を選択してください",
    "options": [
      { "label": "mermaid図", "description": "フロー図・クラス図・ER図・シーケンス図など" },
      { "label": "README", "description": "プロジェクト概要・セットアップ手順・使い方など" },
      { "label": "操作手順書・運用手順書", "description": "画面操作・コマンド手順など" },
      { "label": "API仕様書", "description": "エンドポイント・リクエスト/レスポンス定義など" }
    ]
  }]
}
```

「その他」が選ばれた場合（Other 入力）: 自由記述の内容をそのまま記録する。

---

## Step 2: 対象ファイル・ディレクトリの指定

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "ドキュメント化する対象ファイル・ディレクトリを教えてください（例: src/api/、models/user.py）"
  }]
}
```

---

## Step 3: 読み手の確認

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "このドキュメントの読み手は誰ですか？",
    "options": [
      { "label": "開発チームの新メンバー", "description": "コードを読み始める人" },
      { "label": "開発チーム内", "description": "既存メンバーへの共有・引継ぎ" },
      { "label": "運用・保守担当", "description": "エンジニアだがコードを書かない" },
      { "label": "業務担当・非エンジニア", "description": "システムの利用者・管理者" }
    ]
  }]
}
```

「外部レビュアー・顧客・発注者」や「その他」が選ばれた場合（Other 入力）: 自由記述の内容をそのまま記録する。

---

## Step 4: 目的の確認

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "このドキュメントを作る目的を教えてください",
    "options": [
      { "label": "全体把握・初見理解のため" },
      { "label": "引継ぎ・担当交代のため" },
      { "label": "レビュー・承認を得るため" },
      { "label": "障害時の調査・対応のため" }
    ]
  }]
}
```

「新メンバーのオンボーディング」や「その他」が選ばれた場合（Other 入力）: 自由記述の内容をそのまま記録する。

---

## Step 5: 粒度の確認

**mermaid図が選ばれた場合: 必須。**
**それ以外の場合: AskUserQuestion で確認する（不要であれば Other で「指定なし」と入力してもらう）。**

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "どのレベルの粒度で記述しますか？",
    "options": [
      { "label": "高レベル", "description": "モジュール・サービス単位。全体の流れを一目で把握できる" },
      { "label": "中レベル", "description": "クラス・関数単位。主要な処理の繋がりがわかる" },
      { "label": "低レベル", "description": "メソッド・フィールド単位。詳細な実装がわかる" }
    ]
  }]
}
```

---

## Step 6: 出力先の確認

AskUserQuestion ツール:
```json
{
  "questions": [{
    "question": "ドキュメントの出力先を選択してください",
    "options": [
      { "label": "レポートとして保存", "description": ".claude/reports/doc-{名前}.md に保存（一時保管）" },
      { "label": "プロジェクト内の指定パスに保存", "description": "次の入力でパスを指定する（例: docs/architecture.md）" },
      { "label": "ここに表示するだけ", "description": "ファイル保存せずチャットに出力" }
    ]
  }]
}
```

「プロジェクト内の指定パスに保存」が選ばれた場合: 続けて出力パスを確認する:
```json
{
  "questions": [{
    "question": "保存先のパスを入力してください（例: docs/architecture.md）"
  }]
}
```

---

## Step 7: 確認・承認

収集した内容を以下の形式で提示する:

```
ドキュメント種類 : {種類}
対象            : {ファイル・ディレクトリ}
読み手          : {読み手}
目的            : {目的}
粒度            : {粒度 / 指定なし}
出力先          : {出力先とパス}
```

AskUserQuestion で確認する:
```json
{
  "questions": [{
    "question": "内容を確認してください。どうしますか？",
    "options": [
      { "label": "承認・ドキュメント生成", "description": "このまま doc-writer エージェントを起動する" },
      { "label": "否認・最初からやり直す", "description": "Step 1 に戻る" }
    ]
  }]
}
```

---

## Step 8: doc-writer エージェントの起動

Agent ツールで `doc-writer` エージェントを起動する。
プロンプトに収集した全要件を含める:

```
以下の要件でドキュメントを生成してください。

ドキュメント種類: {種類}
対象ファイル・ディレクトリ: {パス}
読み手: {読み手}
目的: {目的}
粒度: {粒度 / 指定なし}
出力先: {出力先とパス}
```
