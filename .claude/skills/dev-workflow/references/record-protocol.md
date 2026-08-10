# tier-routing 結果記録プロトコル

各フェーズの承認ゲート・タスク単位で `.claude/skills/dev-workflow/scripts/record_agent_outcome.py` を呼び、role 別に実際に使われた tier の成功/失敗を記録する（architecture-report-20260702-214748.md §3-4）。全記録ブロック共通のルール:

## 記録規約

### --complexity（必須）

値は**ワークフロー開始時に UserPromptSubmit hook が表示した `[tier-routing 推奨]` 表示の複雑度**（simple/medium/complex）で、フェーズ A 冒頭でセッションファイルへ `tier-routing複雑度:` 行として 1 度だけ永続化する（本ワークフロー中に再判定しない）。以降の各ブロックの `{セッションファイルの tier-routing複雑度: 行の値}` はこの永続化された値を参照するプレースホルダ（`現在地:` フィールドと同じ、compaction・長時間セッション耐性のための設計）。

### --execution（必須）

interviewer/architect/planner を**親 Claude ペルソナ**で採用した場合は `persona`（bandit 更新なし・イベントログのみ）、**Agent ツールでサブエージェント起動した場合は `subagent`**（bandit 更新あり）を渡す。developer/tester は dev-workflow では常に `subagent`

### --tier（execution 別・省略可）

- `--execution persona` のブロックは `--tier {親モデルのtier名（haiku/sonnet/opus）。判別不能なら unknown}` も明記する。record_agent_outcome.py は persona 実行かつ `--tier` 省略時に常に `unknown` 固定で記録するため、実際の親モデル tier を記録に残したい場合は明示が必要（`--execution subagent` のブロックは frontmatter から自己解決するため `--tier` 不要）
- **developer の記録ブロックは `--tier` を付けない**（tier_autoapply.py が実適用 model を `.claude/state/tier_autoapply.jsonl` に記録し、record_agent_outcome.py が applied-state を session_id 一致で読んで実適用 tier を機械解決する＝適用者=記録 SSOT。tier 値の LLM 申告を行わない。明示指定で推奨と異なる Tier を使った場合も、その実適用値が applied-state に記録されるため `--tier` の付与は不要）

### --task（任意）

同一 gate/role/outcome の記録がタスクごとに繰り返し発生するゲート（D-2.5/D-3 等）では `--task {plan タスクID}` を付与して dedupe キーの粒度を上げる。1 ワークフロー 1 判定のゲート（A-4/B-3/C-2/C-3/D-5/E-1/E-2）は付与不要

### --note（条件付き・ゲート別）

指摘本文を逐語引用せず、シェルメタ文字（引用符・バッククォート・`$` 等）を含まない短い要約で書く（コマンドライン展開事故・シェルインジェクションを避けるため。record_agent_outcome.py 側で長さ上限と秘密情報マスクも適用されるが、そもそも逐語引用しないことが第一防御）。

**帰属判定の場合（E-1/E-2 のみ）**: 帰属根拠トークンを必須含める。迷いなく特定=`帰属根拠:明確`、迷った末に選んだ=`帰属根拠:要判断`

### エラーハンドリング

全エラー exit 0 のため呼び出しが失敗してもワークフローは止めない（記録漏れの可能性はあるが後続フェーズはブロックしない）

## 機械適用（推奨 Tier の `model:` 自動注入）

developer を Agent ツールで起動する箇所（**D-2 / D-2.5 の再実行 / D-4**）では、PreToolUse hook（`.claude/hooks/tier_autoapply.py`）が `[tier-routing 推奨]` の推奨 Tier を Agent 呼び出しの `model:` に自動適用する（機械適用・学習データ収集中の期間も含め常に適用する。親 Claude が `model:` を転記する必要はない。fork は model 上書き不可のため対象外）。推奨と異なる Tier を使いたい場合のみ Agent 呼び出しで `model:` を明示指定する（明示指定は hook に尊重され上書きされない）。

**tester は Red 起動（D-1 のマーカー付き `test-` タスク）のみ機械適用対象**（RED_APPLY_ROLES・Red 限定注入）で、D-3/D-5 等の非 Red 起動と systematic-debugger は対象外＝従来どおり frontmatter 任せとする。interviewer/architect/planner は親 Claude ペルソナで動かし tier レバーが無いため対象外。

### opus 固定不変則（ADR-6）

機械適用（`model:` 自動注入）の対象に追加してよいのは frontmatter が `model: sonnet` の role（developer / wt_developer / tester / wt_tester）のみで、opus 5 体（architect / planner / design-critic / doc-writer / project-setup）は恒久的に注入対象外とする（強 model 固定の設計判断・機械検査で保護）。

## 推奨 Tier の SSOT

「推奨 Tier」の唯一のソースは `.claude/state/tier_selection.json` の `tier`（無ければ `suggested_model`）であり、`[tier-routing 推奨]` の additionalContext テキストはその値を人間可読に射影した派生表示で SSOT ではない。この値は kickoff プロンプトの UserPromptSubmit で select_tier が 1 度だけ書き、E-3 完了側の `--gate E-2` 記録（`--final` 付き）で削除されるまで wave/ゲートをまたいで安定する（承認応答は UserPromptSubmit を発火しないため途中で上書きされない）。

## 集計注記（DC-AS-002 / ADR-25-3）

- bandit params・escalation 判定の**集計対象 gate は role 別（`BANDIT_GATES_BY_ROLE`）**である: developer 等の既定 role は **BANDIT_GATES（D-2.5/D-3/D-5/D-2.5-stuck）**、**tester は D-1 のみ**（D-3/D-5 の tester 記録はイベントログとして残るが集計対象外）。E-1/E-2（レビュー指摘由来）のイベントは全 gate 不可逆に記録するが、その成否は従来どおり個別の集計から除外される（意図どおり・read-side フィルタで実現）。
- **reviewer role（code-reviewer/security-reviewer・および E-gate のみの role）は BANDIT_GATES に該当 gate を持たないため、`c3 tier stats` 等の表示で当該 role の bandit は常に uniform（全 tier `(1.0,1.0,0)`・0 trials）になる**。これは設計意図の帰結であり退行ではない。tier 選択の実消費者は `select_tier`（developer role 固定）のみで、reviewer role の bandit が uniform でも tier 選択ロジックには影響しない。

## E-3 統合裁定の帰属判定共通規定

### role 判定基準

CR・SR 指摘をそれぞれ分析し、各々から最上流起因 role を判定する。許容される帰属先: developer / tester / architect / planner。

### 帰属根拠トークンの判定規約

記録の `--note` に固定トークン（`帰属根拠:明確` / `帰属根拠:要判断`。値の定義は上記「--note（条件付き・ゲート別）」節）が含まれることで帰属の確信度を示す（含有判定・先頭一致にしない）。

トークン不在・上記 2 トークン以外の値・note が取得できない・note から帰属根拠が一意に読み取れない場合は、**いずれも「帰属判定不成立」として扱う**（fail-safe）。

### note のシェルメタ文字禁止

note の内容はシェルメタ文字（引用符・バッククォート・`$` 等）を含まない（コマンドライン展開事故を避けるため）。該当文字は置換・省略する。
