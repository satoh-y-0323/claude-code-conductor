# /develop コマンド

plan-report に基づいて実装フェーズを実行する。

## 必ず守ること

1. **最初に必ず** `.claude/skills/dev-workflow.md` を Read する。記憶・推測で進めない
2. **フェーズ D（実装）** から実行する
3. dev-workflow.md の AskUserQuestion・Edit・セッションファイル更新の手順を省略しない
4. D-0 で plan-report に YAML フロントマター（`po_plan_version`）が検出された場合は、続けて **必ず** `.claude/skills/wave-execution.md` を Read してその手順に従う（C3 メイン + PO スポット並列モード）
5. フロントマターが無い場合は legacy の D-1〜D-5 ceremony（tester→developer→tester の TDD 逐次実行）にフォールバックする
