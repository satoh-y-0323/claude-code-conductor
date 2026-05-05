---
description: patterns.json の昇格候補を rules/promoted/ または skills/promoted-YYYYMMDD-{id}/ に昇格させる。信用度が高いパターンをルール・スキルとして永続化する。
---

# promote-pattern

`patterns.json` の昇格候補を `rules/promoted/` または `skills/promoted-YYYYMMDD-{id}/` に昇格させる。

---

## Step 1: 昇格候補を読み込む

`.claude/memory/patterns.json` を Read する。
`promotion_candidate: true` かつ `promoted: true` でないパターンを抽出する。

候補がない場合は以下を表示して終了する:
> 現在、昇格候補はありません。
> （信用度 0.8 以上・登録から3日以上・未昇格が対象です）

---

## Step 2: 昇格するパターンを選ぶ

AskUserQuestion ツールで候補を複数選択式で提示する:

```json
{
  "questions": [{
    "question": "昇格するパターンを選んでください（複数選択可）",
    "options": [
      { "label": "[trust: {スコア}] {id}", "description": "{description}" },
      ...候補の数だけ追加...,
      { "label": "今回は昇格しない", "description": "すべてスキップして終了する" }
    ],
    "multiSelect": true
  }]
}
```

「今回は昇格しない」のみ選択された場合、または何も選択されなかった場合 → 終了する。

---

## Step 3: 各パターンの昇格先を選ぶ

選択されたパターンごとに AskUserQuestion ツールで昇格先を確認する:

```json
{
  "questions": [{
    "question": "「{description}」をどちらに昇格しますか？",
    "options": [
      { "label": "rule", "description": "rules/promoted/ — 背景知識・制約として登録（「こうしろ / これを知っておけ」系）" },
      { "label": "skill", "description": "skills/promoted-YYYYMMDD-{id}/ — オーケストレーション手順として登録（「この順番で動かせ」系）。Claude が自動的に使用する" }
    ]
  }]
}
```

複数パターンを選んだ場合は1つずつ順番に確認する。

---

## Step 4: ファイルを生成して保存する

パターンの `description` をもとに内容を生成し Write ツールで保存する。

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

**skill の保存先:** `.claude/skills/promoted-YYYYMMDD-{id}/SKILL.md`

```markdown
---
name: promoted-YYYYMMDD-{id}
description: {昇格理由と期待効果を1文で。例: "wave 実行でのペルソナ採用パターン。tdd-develop を単独 wave で実行する際に自動適用される"}
promoted_from: {pattern id}
promoted_date: YYYY-MM-DD
trust_score: {スコア}
---

# {タイトル}

## 使うタイミング
{どんな状況でこのスキルを使うか — description の詳細版}

## 手順
{エージェント間のオーケストレーション手順をステップで記述する}
```

---

## Step 5: index.md に追記する

**rule の場合のみ** — `.claude/rules/promoted/index.md` の `<!-- C3:PROMOTED_RULES:BEGIN -->` と `<!-- C3:PROMOTED_RULES:END -->` の間に追記:
```
- **{タイトル}** (`.claude/rules/promoted/YYYYMMDD-{id}.md`) — {description を1行で}
```

**skill の場合** — index.md への追記は不要。Claude Code がスキルを自動検出する。

---

## Step 6: patterns.json を更新する

昇格したパターンの entry に以下を追加する:

rule の場合:
```json
"promoted": true,
"promoted_date": "YYYYMMDD",
"promoted_to": ".claude/rules/promoted/YYYYMMDD-{id}.md"
```

skill の場合:
```json
"promoted": true,
"promoted_date": "YYYYMMDD",
"promoted_to": ".claude/skills/promoted-YYYYMMDD-{id}/SKILL.md"
```

複数昇格した場合は Step 4〜6 を全パターン分まとめて処理してから Step 7 へ進む。

---

## Step 7: 完了を報告する

```
昇格完了（{N}件）:
  ✅ {description} → .claude/rules/promoted/YYYYMMDD-{id}.md        [trust: {スコア}]
  ✅ {description} → .claude/skills/promoted-YYYYMMDD-{id}/SKILL.md [trust: {スコア}]

スキルは Claude が関連する場面で自動的に使用します。
```
