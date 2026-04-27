# C3 設計判断記録

Claude Code Conductor（C3）の設計における重要な判断とその根拠を記録する。

---

## D-001: フレームワーク名

**決定:** Claude Code Conductor（略称: C3）

**経緯:**
- Clade の構造改善（Phase 0〜6 段階的リファクタ）ではなく、新規フレームワークとして作成する方針を選択
- 理由: 段階的リファクタは「壊さずに直す」制約が常にかかり設計の自由度が低い。新規なら フォルダ構成・タクソノミーを先に決めてから実装できる
- 「Claude Code」を固定プレフィックスとして、直感的にわかる単語を後ろに付ける方針
- "Conductor" = 複数エージェントを指揮するという役割が直感的に伝わる
- 略称は CCC または C3。ユーザーが自然に略称を決める想定（例: ECC → Everything-Claude-Code）

**参考にしたもの:** Clade（挙動・エージェント種別）、Claude Code Game Studios（構造・タクソノミー）

---

## D-002: hooks の実装言語

**決定:** Python（`.py`）

**根拠:**
1. **clade-parallel との統一** — 並列実行ランナー（clade-parallel）が Python 製。C3 が clade-parallel を採用する際に Python / JS の混在を避けられる
2. **pip での配布** — `pip install claude-code-conductor` 一発でインストール可能。Clade の `setup.sh` / `setup.ps1` の二重管理が不要になる
3. **Windows での入手しやすさ** — Microsoft Store から GUI でインストール可能。Node.js より非エンジニアに優しい

**却下した選択肢:**
- `sh/bash`: macOS/Linux ではネイティブだが Windows で Git Bash が必要。JSON パースに jq 依存
- `Node.js`: Clade がすでに使用しているが、pip 配布・clade-parallel 統一の観点で Python が優位

---

## D-003: フォルダ構成とタクソノミー

**決定:** 詳細は `taxonomy.md` を参照

**根拠:**
- Claude Code Game Studios の「各ファイルタイプの意味が明確に定義されている」構造を参考にした
- Clade の主な問題は「Skill とは何か」「Rule とは何か」の定義がなかったことによる混在
- C3 では **Skill = オーケストレーション手順** と明確に定義し、参照知識は `rules/`、人間向けドキュメントは `docs/` に分離

---

## D-004: パターン信用度システム（昇格管理）

**決定:** 時間ベースの信用度蓄積システム

**仕様:**
- `trust_score` = 観測セッション数 ÷ 登録以降の総セッション数（0.1〜1.0）
- 登録後3日間はクーリング期間（信用度計算のみ・昇格候補にならない）
- 4日目以降、`trust_score ≥ 0.8` で昇格候補
- 登録から30日経過で未昇格なら自動削除
- データは `memory/patterns.json` に保持

**分母を「日数」ではなく「セッション数」にした理由:**
作業しない日が続いても日数だけ増えてスコアが下がる問題を防ぐ。
作業頻度が異なるユーザー間で公平に評価できる。

**観測期間を60日→30日に変更した理由:**
週1サイクルのパターンでも30日で4回観測できれば信用度の判断材料として十分。
60日はデータが膨らむ割に精度が変わらない。

---

## D-005: hooks 構成方針

**採用したフック一覧（Game Studios を参考に選定）:**

| ファイル | イベント | 目的 |
|---|---|---|
| `stop.py` | Stop | セッション雛形作成・パターン信用度更新 |
| `pre_compact.py` | PreCompact | コンパクト発生を session ファイルに記録 |
| `pre_tool.py` | PreToolUse（Bash） | 危険コマンドのブロック |
| `log_agent.py` | PostToolUse（Agent） | エージェント起動の監査ログ |
| `validate_skill_change.py` | PostToolUse（Write/Edit） | skills/ 変更時のテスト実施リマインダー |

**却下したもの:**
- Windows トースト通知 — OS 限定のため不採用。クロスプラットフォーム維持を優先
- `session-start.sh` 相当 — Claude Code に SessionStart フックが存在しないため実装不可
- ドキュメント欠落検出 — C3 は汎用フレームワークなので特定ファイル構造を前提にできない

---

## D-006: セッション管理方針

**決定:** `/end-session` コマンドは作らない。タスク完了のたびに session ファイルを更新する運用にする。

**理由:**
- 「最後に一括で書く」は時間がかかり、忘れるリスクもある
- タスク完了のたびに書けば、セッションが突然終了しても情報が残る
- 1回あたりの更新量が少ないので、ユーザー負担も軽い

**session ファイルの更新タイミング（LLM への指示）:**
- タスク完了時: 残タスクを更新・うまくいったアプローチを追記
- パターン発見時: JSON ブロックの `patterns` 配列に追記
- `stop.py` はこれらをもとに `patterns.json` を自動更新する（機械処理のみ）

**コマンド構成:**
- `/init-session` — あり（セッション開始時の状態復元）
- `/end-session` — なし（stop.py が自動処理）

---

## D-007: 昇格ファイルの配置規則

**決定:** 昇格で生成されたファイルは `promoted/` サブフォルダに分離する

| 昇格先 | パス |
|---|---|
| ルール | `.claude/rules/promoted/YYYYMMDD-{id}.md` |
| スキル | `.claude/skills/promoted/YYYYMMDD-{id}.md` |

**サブフォルダ名を `promoted/` にした理由:**
「昇格の仕組みで作成された」ことが名前から直接わかる。
C3 がもともと持つ `rules/` `skills/` 直下のファイルと区別しやすい。

**promoted パターンの patterns.json 内での扱い:**
- `promoted: true` フラグを付与して保持（削除しない）
- 信用度の再計算・期限切れ削除の対象から外れる
- 昇格候補リストには表示しない

**Claude Code でのファイル読み込み:**
- `rules/promoted/` 配下はルールとして自動注入される
- `skills/promoted/` 配下はスキルのため明示的に参照が必要（agents や commands から呼び出す）

---

## D-008: エージェント定義フォーマット

**決定:** Game Studios スタイルの ✅/❌ スコープ明示フォーマットを採用

**フォーマット構成:**
```
frontmatter: model / description / tools
本文: Core Mandate / Key Scope(✅❌) / Workflow(Before・During・After) / Tools & Constraints / Related Agents
```

**Clade との差分:**
- ✅/❌ による担当範囲の明示を追加（「何をしないか」が明確になった）
- Related Agents セクションで上流・下流・ピアの関係を明示
- description フィールドを frontmatter に追加（Claude Code UI で表示される）

**採用エージェント（コア7種）:**

| エージェント | model | 主な出力 |
|---|---|---|
| interviewer | sonnet | requirements-report |
| architect | opus | architecture-report |
| planner | opus | plan-report |
| developer | sonnet | 実装コード |
| tester | sonnet | test-report |
| code-reviewer | sonnet | code-review-report |
| security-reviewer | sonnet | security-review-report |

doc-writer / mcp-setup / workflow-builder は第2弾で追加予定。

---
