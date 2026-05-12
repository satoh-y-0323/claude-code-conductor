---
description: plan-report に基づいて実装フェーズ（フェーズ D）を実行する。po_plan_version があれば parallel-agents（Agent ツール並列起動 + isolation:worktree）、なければ逐次 TDD を実行する。
---

# develop

plan-report に基づいて実装フェーズを実行する。

## 必ず守ること

1. **最初に必ず** `.claude/skills/dev-workflow/SKILL.md` を Read する。記憶・推測で進めない
2. **フェーズ D（実装）** から実行する
3. `.claude/skills/dev-workflow/SKILL.md` の AskUserQuestion・Edit・セッションファイル更新の手順を省略しない
4. D-0 で plan-report に YAML フロントマター（`po_plan_version`）が検出された場合は、続けて **必ず** `.claude/skills/parallel-agents/SKILL.md` を Read してその手順に従う（親 Claude の Agent ツール並列起動 + 公式 `isolation:worktree`）
5. フロントマターが無い場合は legacy の D-1〜D-5 ceremony（tester→developer→tester の TDD 逐次実行）にフォールバックする
