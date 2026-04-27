# /promote-pattern コマンド

`patterns.json` の昇格候補を `rules/promoted/` または `skills/promoted/` に昇格させる。

## 実行手順

### Step 1: 昇格候補を読み込む

`.claude/memory/patterns.json` を Read する。
`promotion_candidate: true` かつ `promoted: true` でないパターンを抽出して一覧表示する。

候補がない場合は以下を表示して終了する:
> 現在、昇格候補はありません。
> （信用度 0.8 以上・登録から3日以上・未昇格が対象です）

### Step 2: 昇格するパターンを選ぶ

番号付きで候補を表示し、どれを昇格するかユーザーに選んでもらう。

```
昇格候補:
  1. [trust: 0.92] {description}
  2. [trust: 0.85] {description}

番号を選択してください:
```

### Step 3: ルールかスキルかを選ぶ

```
このパターンをどちらに昇格しますか？
  [rule]   rules/promoted/ — 背景知識・制約として登録
  [skill]  skills/promoted/ — オーケストレーション手順として登録
```

判断の目安:
- 「こうしろ / これを知っておけ」→ rule
- 「この順番で複数エージェントを動かせ」→ skill

### Step 4: ファイルを生成して保存する

パターンの `description` をもとに内容を生成し、Write ツールで保存する。

**rule の保存先:** `.claude/rules/promoted/YYYYMMDD-{id}.md`

```markdown
---
promoted_from: {pattern id}
promoted_date: YYYY-MM-DD
trust_score: {スコア}
---

# {タイトル}

{ルール本文。「何をすべきか / 何を知っておくべきか」を簡潔に記述する}
```

**skill の保存先:** `.claude/skills/promoted/YYYYMMDD-{id}.md`

```markdown
---
promoted_from: {pattern id}
promoted_date: YYYY-MM-DD
trust_score: {スコア}
---

# {タイトル}

## 使うタイミング
{どんな状況でこのスキルを使うか}

## 手順
{エージェント間のオーケストレーション手順をステップで記述する}
```

### Step 5: index.md に追記する

昇格先に応じたファイルを Read し、マーカー間に1行追記する。

**rule の場合** — `.claude/rules/promoted/index.md` の `<!-- C3:PROMOTED_RULES:BEGIN -->` と `<!-- C3:PROMOTED_RULES:END -->` の間に追記:
```
- **{タイトル}** (`rules/promoted/YYYYMMDD-{id}.md`) — {description を1行で}
```

**skill の場合** — `.claude/skills/promoted/index.md` の `<!-- C3:PROMOTED_SKILLS:BEGIN -->` と `<!-- C3:PROMOTED_SKILLS:END -->` の間に追記:
```
- **{タイトル}** (`skills/promoted/YYYYMMDD-{id}.md`) — {description を1行で}
```

### Step 6: patterns.json を更新する

Edit ツールで昇格したパターンの entry に以下を追加する:

```json
"promoted": true,
"promoted_date": "YYYYMMDD",
"promoted_to": ".claude/rules/promoted/YYYYMMDD-{id}.md"
```

### Step 7: 完了を報告する

```
昇格完了:
  パターン : {description}
  保存先   : .claude/rules/promoted/YYYYMMDD-{id}.md
  信用度   : {trust_score}

rules/promoted/index.md または skills/promoted/index.md にも追記しました。
他に昇格するパターンがあれば続けて選択してください。
```

候補が残っている場合は Step 2 に戻る。
