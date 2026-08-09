# 到達可能性マップ

規約・対策をどこに置けば読み手に届くか。対象読み手ごとに、有効な配置先を表引きで示す。

## 到達経路一覧

表の列構成: 経路（配置方法）、実パス（ファイルパスまたは「—」）、届く role（対象読み手）、条件（前提・性質）

| 経路 | 実パス | 届く role | 条件 |
|---|---|---|---|
| .claude/CLAUDE.md | .claude/CLAUDE.md | 全 role | 自動ロード・常駐コスト有。全員に効かせたい安全境界のみ置く |
| .claude/rules/ (再帰自動ロード) | — | 全 role | 再帰自動ロード。サブエージェント含む。`.claude/rules/` 配下のサブディレクトリも対象 |
| agents/<role>.md 本文 | — | 当該 role のみ | サブエージェント起動時に frontmatter とともに載る。persona 実行 role（interviewer/architect/planner）へは dev-workflow SKILL.md 各フェーズ冒頭の Read 指示に依存して親経由で届く |
| dev-workflow/SKILL.md | .claude/skills/dev-workflow/SKILL.md | 親 Claude のみ | worker（developer/tester）には届かない。フェーズ遷移・承認ゲートの定義・手順 |
| references/*.md (導線経由) | — | 導線を持つ role のみ | 導線が無ければ誰にも届かない。SKILL.md 各フェーズから Read 指示で参照される |
| plan-report タスク prompt | — | 当該タスクの worker のみ | worker へ届く唯一の動的経路。planner が転記した内容のみ有効 |
| 起動プロンプト（E-0 等） | — | 起動された agent のみ | E-0 の枠付けが正例。データ/指示の明確な区分 |
| agent-memory/<role>/ | — | 当該 role（Before に Read 指示を持つ role のみ） | gitignored: worktree に存在せず、ディレクトリ横断検索は空振りする。ファイルを列挙して個別に Read。上流（architect）には既定では届かない（architect.md の Before が能動的な確認を指示して補完する） |
| design-critic-rubric.md | .claude/skills/dev-workflow/references/design-critic-rubric.md | design-critic（C-3 ステップ 2） | C-3 設計監査ゲート内で Read 指示される |
| interview-rubric.md | .claude/skills/dev-workflow/references/interview-rubric.md | interviewer（A-1〜A-3） | フェーズ A 冒頭で Read 指示される |
| design-rubric.md | .claude/skills/dev-workflow/references/design-rubric.md | architect（B-1〜B-2） | フェーズ B 冒頭で Read 指示される |
| plan-design-guidelines.md | .claude/skills/dev-workflow/references/plan-design-guidelines.md | planner（C-2） | フェーズ C 計画作成時に参照・守るべき規約 |

## 置き場所デシジョン（3 手順）

**① 対象読み手を決める**
恒久規約・対策の対象読み手は誰か。例: developer のみ、全 role、特定フェーズの worker、など。

**② 表で経路を引く**
上記の表から対象読み手に届く経路を探す。複数経路がある場合は「コスト」と「確実性」で選ぶ。
- 常駐コスト（CLAUDE.md 肥大化）vs. 導線による確実性（参照側の負担）
- 安全境界（コミット・削除・権限）はより上流（CLAUDE.md / rules/）に置く

**③ 経路が無ければ agent 本文か plan 契約に置く**
表を見ても対象読み手に届く経路が無い場合（例: worker 向け恒久規約、developer のみ向けのパターン）は、以下を選ぶ:
- 多くの worker が参照するなら plan-report の task prompt に書く（動的経路）
- 特定フェーズ限定なら agent.md 本文に書く
- サブエージェント（design-critic など）の動作規約なら agent 本文か SKILL.md の起動フェーズに書く
