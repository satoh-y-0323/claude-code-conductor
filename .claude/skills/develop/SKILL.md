---
description: 'plan-report に基づいて実装フェーズ（フェーズ D）を実行する。po_plan_version の値に応じて分岐: "0.1" なら parallel-agents（Agent ツール並列起動 + isolation:worktree）、"sequential" または無しなら逐次 TDD、不正値は fail-loud 停止。'
---

# develop

plan-report に基づいて実装フェーズを実行する。

## 必ず守ること

1. **最初に必ず** `.claude/skills/dev-workflow/SKILL.md` を Read する。記憶・推測で進めない
2. **フェーズ D（実装）** から実行する
3. `.claude/skills/dev-workflow/SKILL.md` の AskUserQuestion・Edit・セッションファイル更新の手順を省略しない
4. D-0 で plan-report に YAML フロントマター内の `po_plan_version: "0.1"` を検出した場合は、続けて **必ず** `.claude/skills/parallel-agents/SKILL.md` を Read してその手順に従う（親 Claude の Agent ツール並列起動 + 公式 `isolation:worktree`）
5. `po_plan_version: "sequential"` の場合、またはフロントマター自体が無い場合（後方互換）は legacy の D-1〜D-5 ceremony（tester→developer→tester の TDD 逐次実行）にフォールバックする。フロントマターがあるのに値が不正・キー欠落の場合は D-0 の validate 実行で fail-loud 停止する（dev-workflow SKILL.md D-0 参照）
