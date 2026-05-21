---
description: 計画フェーズ。planner ペルソナでタスク分解・plan-report 生成を行う。dev-workflow フェーズ C 専用 skill。
disable-model-invocation: false
user-invocable: false
---

# Phase C: 計画

`.claude/agents/planner.md` を Read してペルソナを採用する。

**上流フェーズから続いている場合:** 要件・設計はコンテキスト内にあるため読み直し不要。
**直接開始またはレビューから戻った場合:** Glob で `.claude/reports/` 内の全レポートを Read する（`[対応予定]` マーク付きの指摘を修正計画に反映する）。

今日のセッションファイルに `- [ ] 計画` を追記する（未登録の場合のみ）。

---

## 必読: 並列実行の設計指針

plan-report 生成前に必ず以下を Read する:

- `.claude/rules/plan-design-guidelines.md` — depends_on の付け方、TDD 3-wave 分解、writes 衝突回避、自動検査ルール R2/R3/R4、出力直前の自己チェックリストを含む

このガイドラインに従って計画を組み立てる。

---

## C-1: マイルストーンの確認

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

「設ける」を選んだ場合、`plan-design-guidelines.md` の「直列・並列交互パターンの取り扱い」セクションに従って stage 分割を行う。

---

## C-2: plan-report の生成と承認

`.claude/reports/plan-report-YYYYMMDD-HHMMSS.md` に Write する（タイムスタンプは `report-timestamp` skill で取得）。

### C-2-A: 出力直前の自己チェックリスト

plan-report を Write する前に以下を確認する（`plan-design-guidelines.md` の自己チェックリストと同じ）:

- [ ] `depends_on` チェーンの最大長 ≦ タスク数 / 2 か（直列化していないか）
- [ ] `writes` が空のタスクが残っていないか（`read_only: true` タスクは `writes` 自体を省略していること）
- [ ] 同じファイルを書く複数タスクで衝突対策が取られているか
- [ ] レビュータスク（`read_only: true`）が全 dev タスクに `depends_on` を持っているか
- [ ] `tasks[].id` が一意で、`depends_on` の参照先が全て存在するか
- [ ] `depends_on` を空配列（`[]`）で書いていないか（無依存ならフィールド自体を省略）
- [ ] TDD を伴う機能は Red tester / Green developer / 確認 tester の 3 タスクに分解しているか
- [ ] 想定実行時間が 15 分を超えるタスクがないか
- [ ] R2: reviewer の writes ファイル名は task_id ベース・タイムスタンプなしか
- [ ] R3: writes に `src/c3/_template/` パスが含まれていないか
- [ ] R4: 同一 writes パスを宣言する task が `depends_on` で順序付けされているか

### C-2-B: 承認前に agent 種別を明示する

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

### C-2-C: 承認

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

承認後 → セッションファイルの `- [ ] 計画` を `- [x] 計画` に Edit する。

**知識蓄積:**
- 「否認・修正を依頼する」: `## 試みたが失敗したアプローチ` に教訓をルール形式で追記し `patterns` に追加する
- 承認かつ非自明なアプローチが有効だった場合: `## うまくいったアプローチ` に追記し `patterns` にも追加する

---

## 次フェーズへの遷移

承認後、次は **phase-d-implement** へ進む。
LLM は Skill ツールで `phase-d-implement` を呼び出すか、
`.claude/skills/phase-d-implement/SKILL.md` を Read してフェーズ D を開始する。
