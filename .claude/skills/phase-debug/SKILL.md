---
description: デバッグ調査フェーズ。systematic-debugger を起動して根本原因調査・debug-analysis-report を生成し、修正は phase-d-implement の bug-fix モードに任せる。/start のデバッグ調査選択や Stuck Signal 検出時のエントリ。
disable-model-invocation: false
user-invocable: false
---

# Phase Debug: デバッグ調査

実装が詰まったとき・既知の不具合を分析したいときの入り口。
systematic-debugger エージェントが根本原因を特定して `debug-analysis-*.md` を出力し、
その内容を踏まえて修正は phase-d-implement の bug-fix モードに任せる。

---

## D-Debug-1: 入力ソースの確認

以下のいずれかが入力となる。AskUserQuestion でユーザーに確認する:

```json
{
  "questions": [{
    "question": "デバッグ調査の入力ソースを選択してください。",
    "options": [
      { "label": "developer の Stuck Signal", "description": ".claude/reports/debug-needed-*.md が存在する（Phase D の D-2.5 から来た場合）" },
      { "label": "ユーザーが提供する症状", "description": "エラーログ・再現手順を直接プロンプトに入力する" }
    ]
  }]
}
```

**「developer の Stuck Signal」の場合:**
Glob で `.claude/reports/debug-needed-*.md` の最新を取得し、ファイルパスのみコンテキストに保持する（内容は agent 側で Read させる）[SR-AI-001]

**「ユーザーが提供する症状」の場合:**
AskUserQuestion でエラーログ・再現手順・期待される動作などを聴取する。

---

## D-Debug-2: systematic-debugger 起動

Agent ツールで `systematic-debugger` を起動する。

入力が debug-needed の場合: プロンプトに debug-needed ファイルのパスのみを含め、内容は agent 側で Read させる（プロンプトに直接展開しない）[SR-AI-001]

systematic-debugger は調査後に `.claude/reports/debug-analysis-YYYYMMDD-HHMMSS.md` を Write する。

---

## D-Debug-3: 調査結果の確認

生成された debug-analysis を提示した後、AskUserQuestion で確認する:

```json
{
  "questions": [{
    "question": "debug-analysis を確認してください。次はどうしますか？",
    "options": [
      { "label": "phase-d-implement で修正する", "description": "bug-fix モードで実装フェーズへ進む" },
      { "label": "phase-c-plan で計画から立て直す", "description": "影響範囲が広い場合は計画を組み直す" },
      { "label": "否認・再調査を依頼する", "description": "フィードバックを入力して再実行する" }
    ]
  }]
}
```

---

## 次フェーズへの遷移

- **「phase-d-implement で修正する」**: debug-analysis を入力として **phase-d-implement** の bug-fix モードへ進む（D-0 が当日 debug-analysis を検出して bug-fix モードを自動選択する）
- **「phase-c-plan で計画から立て直す」**: **phase-c-plan** へ進み、debug-analysis を考慮した plan-report を生成する
- **「否認・再調査を依頼する」**: 追加情報を入力して D-Debug-2 を再実行

`- [ ] developer: 修正実装` をセッションファイルに追加するなどの bug-fix モード固有のセッション更新は phase-d-implement D-0 が担当する。本 skill では追加しない。
