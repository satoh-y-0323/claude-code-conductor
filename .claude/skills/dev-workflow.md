# Dev Workflow

要件定義から実装・レビューまでを複数エージェントで連携させるフルワークフロー。

## 使うタイミング

- 新機能の設計から実装まで一通り進めたい場合
- 既存機能への大きな変更
- バグ修正（原因調査・設計変更を伴う場合）

小さな修正や調査だけであれば `/agent-developer` を単独で使えば十分。

---

## フェーズ構成

```
Phase 1: 要件定義・設計
  Step 1. /agent-interviewer  → requirements-report
  Step 2. /agent-architect    → architecture-report

Phase 2: 計画立案
  Step 3. /agent-planner      → plan-report
          ※ 初回は requirements-report + architecture-report のみ読み込む

Phase 3: 実装（TDD サイクル）
  Step 4. /agent-tester       → 失敗テスト作成（Red）
  Step 5. /agent-developer    → 実装（Green）
  Step 6. /agent-tester       → テスト再実行・test-report 出力

Phase 4: レビュー
  Step 7. /agent-code-reviewer     → code-review-report
  Step 8. /agent-security-reviewer → security-review-report

Phase 5: 再計画（指摘がある場合）
  Step 9. /agent-planner      → 全レポートを統合・更新 plan-report
          指摘がなくなるまで Phase 3〜5 を繰り返す
```

---

## TDD サイクル（Phase 3 詳細）

```
tester（Red: 失敗テスト作成）
    ↓
developer（Green: テストを通す実装）
    ↓
tester（確認: 全テスト合格？）
    ├── 合格 → developer（Refactor: リファクタ）→ tester（再確認）→ Phase 4 へ
    └── 不合格 → developer（修正）→ tester（再確認）→ 繰り返し
```

---

## レビュー後の判断基準（Phase 4 → Phase 5）

| レビュー結果 | 次のアクション |
|---|---|
| 指摘なし / Low のみ | 完了。コミットを提案する |
| Medium 以上の指摘あり | `/agent-planner` で指摘を反映した plan-report を作成し Phase 3 へ戻る |
| Critical / High の脆弱性あり | 即座に `/agent-planner` へ。security-review-report を優先的に反映する |

---

## フェーズをスキップできるケース

| ケース | スキップできるフェーズ |
|---|---|
| 要件が明確で設計変更なし | Phase 1（interviewer・architect）をスキップして planner から開始 |
| バグ修正のみ | Phase 1 をスキップ。既存 plan-report があればそのまま developer から開始 |
| テストが既に存在する | Step 4（Red フェーズ）をスキップして developer から開始 |

---

## レポートの流れ

```
requirements-report
        ↓
architecture-report
        ↓
    plan-report ←────────────────────────────┐
        ↓                                    │
   test-report                               │
        ↓                                    │ 指摘があれば再計画
 code-review-report                          │
        ↓                                    │
security-review-report ──────────────────────┘
```

各レポートは `.claude/reports/` に `{種別}-YYYYMMDD-HHMMSS.md` の形式で保存される。
複数回サイクルを回した場合は最新のタイムスタンプのものを使う。
