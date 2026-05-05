---
description: patterns.json の現状（信用度・昇格候補・期限残・既昇格）を表形式で可視化する読み取り専用コマンド。/promote-pattern の前段としてパターンの状態確認に使う。
disable-model-invocation: true
---

# pattern-status

`patterns.json` の現状を可視化する。**読み取り専用** で patterns.json を変更しない。

---

## Step 1: patterns.json を読み込む

`.claude/memory/patterns.json` を Read する。
ファイルが存在しない、または `patterns` 配列が空の場合は以下を表示して終了する:

```
## パターン統計

現在、登録されているパターンはありません。
セッション内で発見したパターンは session ファイルの JSON ブロックに記録され、
セッション終了時に stop.py が patterns.json へ統合します。
```

---

## Step 2: パターンを分類する

各パターンを以下の 4 区分に分類する:

| 区分 | 条件 |
|---|---|
| 昇格済み | `promoted: true` |
| 昇格候補 | `promotion_candidate: true` かつ `promoted: true` でない |
| 蓄積中 | `promotion_candidate` が false または未設定で、`promoted` が true でない |
| 期限切れ間近 | `registered_date` から 25 日以上経過（30 日で自動削除） |

「期限切れ間近」は他区分と排他ではない（蓄積中かつ期限切れ間近 → 両方に表示してよい）。

---

## Step 3: サマリを表形式で表示する

以下の形式で表示する。空のセクションは「（なし）」と書く。

```
## パターン統計 ({今日の日付})

### 昇格候補（{N}件）
| trust | id | description | 登録日 | 経過 |
|---|---|---|---|---|
| 0.85 | example_pattern | パターンの説明 | 2026-04-30 | 5日 |
...

→ `/promote-pattern` で昇格できます。

### 蓄積中（{N}件）
| trust | id | description | 登録日 | 観測 |
|---|---|---|---|---|
| 0.33 | another_pattern | パターンの説明 | 2026-05-02 | 1回 |
...

### 昇格済み（{N}件）
| id | 昇格先 | 昇格日 | trust |
|---|---|---|---|
| promoted_pattern | rules/promoted/20260503-promoted_pattern.md | 2026-05-03 | 0.90 |
...

### 期限切れ間近（25日以上経過 / 30日で自動削除）（{N}件）
| trust | id | 登録日 | 残り日数 |
|---|---|---|---|
| 0.20 | stale_pattern | 2026-04-10 | 5日 |
...

### 信用度別分布
- 0.8 以上: {N}件（昇格候補入り）
- 0.5 〜 0.8: {N}件
- 0.3 〜 0.5: {N}件
- 0.1 〜 0.3: {N}件

合計: {N}件
```

---

## Step 4: 次のアクションを案内する

表示の最後に、状況に応じた一文を案内する:

| 状況 | 案内 |
|---|---|
| 昇格候補が 1 件以上 | `/promote-pattern` で昇格できます。 |
| 期限切れ間近が 1 件以上 | あと {N} 日で削除されるパターンがあります。観測されると信用度が上がり、昇格候補になります。 |
| 蓄積中のみで昇格候補なし | 信用度 0.8 以上・登録から 3 日以上で昇格候補になります。 |
| すべて空 | （Step 1 のメッセージで終了済み） |

---

## 注意事項

- patterns.json を **絶対に変更しない**。読み取り専用のコマンドである
- ファイルの修正・パターンの追加削除は `/promote-pattern` または `stop.py` の担当
- 経過日数は `registered_date` (YYYYMMDD) と今日の日付から計算する
