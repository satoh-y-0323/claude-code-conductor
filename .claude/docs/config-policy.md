# C3 Config Policy — 設定階層・配布判断の公式リファレンス

**バージョン**: v2.17.0
**対象読者**: C3 利用先ユーザー / C3 自体をフォーク・拡張する開発者
**canonical 宣言**: 設定優先順位・配布判断に関しては本ドキュメントが唯一の公式情報源。
他ファイル（`CLAUDE.md` / `_excludes.py` docstring）から本ドキュメントへの参照リンクが張られている場合、
詳細は常に本ドキュメントを参照すること。

---

## 1. 設定ファイル一覧（所在マップ）

### 1-1. 配布元リポジトリ（C3 開発者向け）

```
/CLAUDE.md                             配布元専用（.gitignore で除外）
.claude/CLAUDE.md                      配布元・配布先共通（常時注入）
.claude/settings.json                  プロジェクト共通設定（配布される）
.claude/settings.local.json            個人 override（配布されない）
.claude/permission_rules.json          C3 独自の自動承認パターン（配布される）
.claude/rules/                         C3 配布デフォルトルール（配布される）
.claude/rules/promoted/                プロジェクト固有昇格ルール（配布される、update は触らない）
.claude/hooks/                         Claude Code lifecycle hooks（配布される）
.dev/hooks/                            配布元専用 hook（配布されない）
src/c3/_excludes.py                    wheel 除外パターン（配布元のビルド制御）
hatch_build.py                         _excludes.py の重複定義（ビルド時専用）
src/c3/migrations/                     SQLite schema migration SQL ファイル群（Python package・wheel 同梱・.claude/ 配下ではないため 3 ファイル同期対象外）
```

### 1-2. 利用先プロジェクト（C3 ユーザー向け）

```
.claude/CLAUDE.md                      c3 init で配置・c3 update で更新
.claude/settings.json                  c3 init で配置・c3 update で更新
.claude/settings.local.json            ユーザーが個別作成（c3 は触らない）
.claude/permission_rules.json          c3 init で配置・c3 update で更新
.claude/rules/                         c3 init で配置・c3 update で更新
.claude/rules/promoted/index.md        c3 init で空雛形のみ配置（c3 update は触らない）
.claude/hooks/                         c3 init で配置・c3 update で更新
.claude/state/                         実行時生成（gitignore 推奨）
.claude/memory/                        実行時生成
.claude/agent-memory/                  実行時生成
.claude/reports/                       実行時生成（gitignore 推奨）
.claude/worktrees/                     並列実行時一時生成
.claude/logs/                          実行時生成
```

### 1-3. ディレクトリの命名・配置チート

各ディレクトリの **命名・役割・配置判断** は `.claude/docs/taxonomy.md` を参照。
本ドキュメントは配布判断・優先順位に特化しており、taxonomy と住み分けている。

---

## 2. 設定優先順位と書き込み権限

設定は性質の異なる **3 つのレイヤー** に分かれる。1 列に並べると誤解を生むため分離して記述する。

### レイヤー A: ツール権限（Claude Code 公式）

同じキー（`permissions.allow` など）が複数ファイルで定義されている場合、
**上位ファイルが下位ファイルを上書き**する（高 → 低の順）:

1. `.claude/settings.local.json` — 個人 override（`.gitignore` 推奨。c3 update の対象外）
2. `.claude/settings.json` — プロジェクト共通設定（git 管理）
3. `~/.claude/settings.json` — グローバル個人設定（マシンローカル）

> **補足**: `hooks` は両ファイルの内容がマージされる（本配布元リポで `settings.json` の lifecycle hooks と `settings.local.json` の `.dev/hooks/*` が並走している実機事実）。
> ただし Claude Code 公式 docs では hooks のマージ挙動が明記されていないため、チーム全体で必要な hook は `settings.json` 側に集約するのが安全。
> 詳細は §7「既知の落とし穴」を参照。
> キー仕様の詳細は `.claude/docs/settings.json.md` を参照。

### レイヤー B: 自動承認パターン（C3 独自拡張）

- `.claude/permission_rules.json` の `auto_allow` 配列
- `permission_handler.py`（`PermissionRequest` hook）が読み込んで、パターンにマッチすれば自動承認
- レイヤー A で `deny` 判定されたものを覆すことはできない（hook の決定権の範囲内）
- `notify_on_auto: false` で通知を抑止できる

> **注意**: `permission_rules.json` は Claude Code 公式の `permissions.allow` とは **独立した別レイヤー**。
> 同列に並べると「どちらが優先されるか」で誤解が生じる。レイヤー A と B は並立している。

> **注意**: `auto_allow` パターンは最小限に留めること。広範なパターン（例: `Bash(*)`）は C3 の意図する動作範囲を超えた危険コマンドも自動承認する可能性がある。

### レイヤー C: LLM 指示・知識（CLAUDE.md / rules）

- `.claude/CLAUDE.md` — 常時注入されるプロジェクト指示
- `.claude/rules/*.md` — 常時全文注入（`paths:` フロントマターは「適用範囲のドキュメント」であり、注入タイミングは変わらない）
- `.claude/rules/promoted/index.md` — `@rules/promoted/index.md` で CLAUDE.md から include される
  （`/promote-pattern` skill が追記する **ユーザー所有領域**）

「優先順位」という概念は厳密には適用されない（全文ロードされる）が、
**`rules/promoted/` は c3 update が触らない**点が肝となる。

### 書き込み権限マトリクス

| ファイル | c3 init が初期配置 | c3 update が上書き | ユーザーが編集してよい |
|---|---|---|---|
| `.claude/settings.json` | ○ | ○ | △（チーム合意のもと） |
| `.claude/settings.local.json` | × | × | ○（個人 override / 秘匿情報） |
| `.claude/permission_rules.json` | ○ | ○ | △（ファイル全体を上書き編集する場合は注意） |
| `.claude/CLAUDE.md` | ○ | ○ | △（c3 update で上書きされる前提で） |
| `.claude/rules/*.md` | ○ | ○ | △（同上） |
| `.claude/rules/promoted/index.md` | ○（空雛形のみ） | × | ○（`/promote-pattern` が追記、手動編集も可） |

---

## 3. 配布判断マトリクス（14 カテゴリ）

`_excludes.py` の `EXCLUDE_PATTERNS` / `KEEP_PATTERNS` を実装照合した結果。
各カテゴリに配布有無・c3 update の更新有無・理由を明示する。

| # | カテゴリ | 配布 | c3 update が更新 | 理由 |
|---|---|---|---|---|
| 1 | `.claude/hooks/*.py` | ○ | ○ | Claude Code lifecycle hook の実体。配布先で動作する。例外: `subagent_log.py` のみ除外（個人デバッグ用）。v2.20.0 で `hooks/schema.sql` を削除し SQLite スキーマは `src/c3/migrations/` に移管 |
| 2 | `.claude/agents/*.md` | ○ | ○ | ペルソナ定義。配布先で読まれる。例外: `tdd-develop.md` のみ除外（v2.1.0 廃止） |
| 3 | `.claude/skills/*/` | ○ | ○ | オーケストレーション/ユーティリティ skill 定義（`scripts/` / `templates/` 等サブディレクトリ含む）。例外: `worktree-tdd-workflow/*` のみ除外（v2.1.0 廃止） |
| 4 | `.claude/rules/*.md` | ○ | ○ | C3 配布デフォルトルール（常時注入対象） |
| 5 | `.claude/rules/promoted/*` | ○ | × | プロジェクト固有昇格ルール。配布元の `promoted/index.md` は空雛形のみ配布。利用先で `/promote-pattern` が追記する **ユーザー所有領域**。c3 update が触ると昇格内容が失われる |
| 6 | `.claude/docs/*.md` | △（一部のみ） | ○ | 利用先向けリファレンス。配布対象は `platform-adapters.md` / `settings.json.md` / `parallel-agents-setup.md` / `config-policy.md`（本ドキュメント）の 4 ファイル。配布元固有の設計メモ等は `_excludes.py` で個別除外。`taxonomy.md` は tracked（GitHub 公開）だが EXCLUDE 対象のため wheel 非配布（詳細は §7 落とし穴 2 参照）。 |
| 7 | `.claude/CLAUDE.md` | ○ | ○ | 配布先で常時注入される共通ルール |
| 8 | `.claude/settings.json` | ○ | ○ | プロジェクト共通設定（hooks 登録・permissions など） |
| 9 | `.claude/permission_rules.json` | ○ | ○ | C3 独自の自動承認パターン（PermissionRequest hook が参照） |
| 10 | `.claude/settings.local.json` | × | — | 個人 override・秘匿情報。`_excludes.py` でも除外、`.gitignore` でも除外、c3 update も触らない |
| 11 | `.claude/reports/*` / `memory/*` / `agent-memory/*` / `state/*` / `tmp/*` / `worktrees/*` / `logs/*` | × （`.gitkeep` のみ ○） | — | 実行時生成領域。空ディレクトリのみ `KEEP_PATTERNS` の `.gitkeep` で配布。データ本体は除外。v2.19.0 で `.claude/state/c3_version.txt`（バージョン checkpoint）を追加（`state/*` 一括除外により自動非配布） |
| 12 | `.dev/*` / `/CLAUDE.md` / `/AGENTS.md` / `/.codex/` / `/.cursor/` / `/.agents/` | × | — | 配布元専用または adapter 生成物。wheel には構造的に含まれない（`src/c3/_template/.claude/` 配下のみ同梱）が、配布元 `.gitignore` で commit 混入も防ぐ |
| 13 | `.claude/deletions.txt` | ○ | ○ | `c3 update` が読み込み、利用先 `.claude/` から該当ファイルを削除候補として扱う。配布元の `.claude/deletions.txt` に追記したエントリは次回 pip install → `c3 update` で利用先に伝播。`KEEP_PATTERNS` で明示配布。`c3 update` 自体は本ファイルを削除しない（§7 落とし穴 5 参照） |
| 14 | `.claude/breaking-changes.txt` | ○ | ○ | `c3 update` が読み込み、利用先の `.claude/state/c3_version.txt`（バージョン checkpoint）と diff を計算して破壊的変更を表示する。MAJOR bump 時は y/N 承認プロンプトを発火。`KEEP_PATTERNS` で明示配布。配布元 `.claude/breaking-changes.txt` を更新すれば次回 pip install → `c3 update` で利用先に伝播。利用先 git 管理は tracked（上書きされる） |

> **注意 (カテゴリ #6)**: `taxonomy.md` は `_excludes.py` の EXCLUDE 対象だが、`.gitignore` では tracked 状態（GitHub に公開済み）。
> **wheel には含まれない**点に注意。gitignore と wheel 配布は別レイヤーであり、
> 「tracked = 配布される」ではない。

---

## 4. settings.local.json 運用の原則

`settings.local.json` は以下の **3 原則**で運用する:

### 原則 1: 個人 override / 秘匿情報はここに書く

- 個人の `permissions.allow` 追加（チーム全員には不要なもの）
- API キー・個人トークン等の秘匿情報（`env` セクションに記述）
- 個人的な `mcpServers` 設定

`.gitignore` に含まれているため、通常の `git add` では staging されずリモートに上がらない（ただし `git status` で確認を推奨）。

> **注意**: `git add -f` の強制オプションを使うと `.gitignore` が無効化されるため、秘匿情報が誤ってコミットされるリスクがある。CI/CD パイプラインでの自動 `add` 設定にも注意すること。

> **注意**: `c3 init` はプロジェクトルートの `.gitignore` を自動編集しない。`settings.local.json` を新規作成する場合は、ユーザー自身が `.gitignore` に `.claude/settings.local.json` を追加すること。

### 原則 2: c3 update は触らない

`settings.local.json` は `c3 init` も `c3 update` も作成・上書きしない。
個人設定を安全に保ちつつ、C3 のアップデートを受け取れる設計。

### 原則 3: `hooks` は「個人専用・配布元専用」のみに留める

`settings.local.json` の `hooks` は `settings.json` の `hooks` とマージされる（本配布元リポで `.dev/hooks/*` を `settings.local.json` に登録して `.claude/hooks/*` と並走させている実例あり）。

ただし以下の理由から、`settings.local.json` には「個人 / 配布元専用 hook」のみを書く:
- Claude Code 公式 docs では hooks のマージ挙動が明記されておらず、将来仕様が変わるリスクがある
- チーム全体で必要な hook は `settings.json` 側に置いてチーム合意を経るべき

詳細は §7「既知の落とし穴」の項目 1 を参照。

---

## 5. プラットフォーム別 config 整合（canonical 宣言）

### canonical は `.claude/` に置く

C3 の設定・ルール・スキルの **唯一の公式ソース** は `.claude/` ディレクトリ。
Codex / Cursor 向けの adapter 生成物は **派生生成物** であり、primary source ではない。

| プラットフォーム | 設定参照経路 | 生成コマンド |
|---|---|---|
| Claude Code | `.claude/settings.json` / `.claude/CLAUDE.md` | — |
| Codex | `.codex/` / `/AGENTS.md` | `c3 init --platform codex` |
| Cursor | `.cursor/rules/c3-core.mdc` | `c3 init --platform cursor` |

### adapter 生成物の位置付け

- `.codex/` / `/AGENTS.md` / `/.cursor/` / `/.agents/` は **c3 init が生成する派生物**
- これらを直接編集すると `c3 init --platform` の再実行で上書きされる
- 配布元リポジトリでは `.gitignore` に含まれる（配布対象外）

### 複数プラットフォームを切り替える場合

同じプロジェクトで Claude Code / Codex / Cursor を切り替えて使う場合、
設定の変更は必ず `.claude/` 側に行い、必要に応じて `c3 init --platform` で adapter を再生成する。

詳細は `.claude/docs/platform-adapters.md` を参照。

---

## 6. 3 ファイル同期ルール

### なぜ 3 ファイルが同期されなければならないか

C3 の wheel 配布除外パターンは **3 つのファイルに分散して定義** されている:

| ファイル | 役割 | 変更が必要な場面 |
|---|---|---|
| `.gitignore` | git 追跡から除外（配布元リポジトリの個人作業ファイルを管理外に） | 配布元で新たに除外すべきファイルが増えた時 |
| `src/c3/_excludes.py` | `c3 init` / `c3 update` 時の除外判断（Python 実装） | 配布先への配布/非配布を変更する時 |
| `hatch_build.py` | wheel ビルド時の除外判断（`_excludes.py` の重複定義） | `_excludes.py` を変更した時（必ず同期） |

**`breaking-changes.txt` の同期義務（v2.19.0 追加）**: 新たな破壊的変更をリリースする際は、必ず `_excludes.py` / `hatch_build.py` の `KEEP_PATTERNS` に `"breaking-changes.txt"` が含まれていることを確認した上で、配布元 `.claude/breaking-changes.txt` にエントリを追記する。`scripts/extract_breaking_changes.py --check` で CHANGELOG との整合性を確認し、未記載があれば追記してから wheel を再ビルドする（詳細は配布元 `/CLAUDE.md` §6 参照）。

`hatch_build.py` の重複が必要な理由: hatch build hook はパッケージ import 前に走るため、
`_excludes.py` を import できない。2 ファイルの完全一致が必須。

### 同期確認の方法

`.dev/hooks/_sync_check.py`（PostToolUse hook）が、3 ファイルのいずれかを変更した時に
残り 2 ファイルの同期を `stderr` で警告する。警告が出たら必ず対応する。

### 変更手順

1. `.gitignore` / `_excludes.py` / `hatch_build.py` のいずれかを変更
2. `_sync_check.py` の警告を確認
3. 残り 2 ファイルに同じパターンを追加（または削除）
4. `python -m build --wheel` で wheel を再生成して実体検証

### 過去の同期漏れ defect

| バージョン | 内容 | カテゴリ #（§3 参照） |
|---|---|---|
| v1.1.0 | `state/tier_selection.json` が wheel に混入した | #11 |
| v2.14.1 | `worktrees/` 配下ファイルが wheel に混入した | #11 |

いずれも `_excludes.py` / `hatch_build.py` の同期漏れが原因。

---

## 7. 既知の落とし穴

**ここを読まないと事故る**ポイントを集約。症状が出たらまず確認すること。

### 落とし穴 1: `hooks` のマージ挙動は公式 docs 未明記

**現状**: 実機では `settings.local.json` の `hooks` と `settings.json` の `hooks` はマージされる（本配布元リポの `.dev/hooks/*` 登録が実例）。
ただし Claude Code 公式 docs では hooks のマージ挙動が明記されていないため、将来の Claude Code バージョンで挙動が変わるリスクがある。

**対処**: チーム全体で必要な hook は `settings.json` 側に置く（git 管理 / チーム合意）。`settings.local.json` には個人 / 配布元専用 hook のみを書く（本リポでは `.dev/hooks/*` 配下のみ）。
万が一 lifecycle hook が動かなくなった場合は、`settings.local.json` を一時退避して `settings.json` 単独で動作するか切り分ける。

### 落とし穴 2: `taxonomy.md` は tracked だが wheel 配布されない

**症状**: `_excludes.py` で除外されているはずの `taxonomy.md` が GitHub に公開されている、
または「なぜ `.gitignore` に入っていないのか」と疑問に思う。

**原因**: `taxonomy.md` は `.gitignore` では tracked（GitHub に公開済み）だが、
`_excludes.py` の `EXCLUDE_PATTERNS` で除外されているため **wheel には含まれない**。
「git tracked = 配布される」ではない。wheel 配布と git 追跡は独立したレイヤー。

**対処**: 混乱した場合は §3 のカテゴリ #6 の注意書きを参照。
`config-policy.md`（本ドキュメント）は wheel に含まれる設計になっている（`_excludes.py` に除外パターンなし）。

### 落とし穴 3: `rules/promoted/` を `c3 update` が上書きしない

**症状**: `c3 update` 後に `/promote-pattern` で追加したルールが消えている（実際には消えないが消えると思って不安）、
または「なぜ promoted/ は更新されないのか」と疑問に思う。

**原因**: `.claude/rules/promoted/index.md` は **ユーザー所有領域**。
c3 update が上書きすると利用先で `/promote-pattern` が追記したルールが消失するため、意図的に除外している（§3 カテゴリ #5 参照）。

**対処**: `promoted/` への変更は `c3 update` に委ねず、手動または `/promote-pattern` skill で管理する。
C3 side で `promoted/` の雛形を更新した場合は、リリースノートで手動マージ手順を案内する。

### 落とし穴 4: `deletions.txt` 自身は削除されない・絶対パスは無視される

**症状**: `deletions.txt` が利用先に残り続ける / 絶対パスを書いたのに削除されない / `..` 含みパスが効かない。

**原因**:
- `deletions.txt` は **`c3 update` が読み取るための指示書** であり、削除対象として
  `deletions.txt` 自身を含めても無視される（自分自身を削除すると次回 update で
  ブートストラップが効かなくなるため）
- セーフガードにより以下は **silent ではなく warning を出して無視** される:
  - 絶対パス（先頭 `/`、`~`、Windows ドライブレター `C:`）
  - `..` または `.` を含むパス
  - `.claude/` プレフィックス（`.claude/agents/x.md` は不可、`agents/x.md` と書く）
  - シンボリックリンク経由のパス
  - ディレクトリ（ファイルのみサポート）
  - `\` （バックスラッシュ）を含むパス

**対処**:
- 削除候補に書くパスは「.claude/ からの相対 POSIX パス」のみ（例: `agents/foo.md`）
- ディレクトリを丸ごと削除したい場合は、配下のファイルを 1 つずつ列挙
- `c3 update --dry-run` で warning が出ていないか確認
- `deletions.txt` 自身を更新したい場合は通常の `c3 update` の add/update ロジックが処理する

### 落とし穴 5: `permission_rules.json` は `settings.json.permissions.deny` を覆せない（要検証）

**現状**: `permission_handler.py` の実装を読む限り、`PermissionRequest` hook は
Claude Code 側で `deny` 判定が出た後に発火するため、`permission_rules.json` の `auto_allow` で
`deny` を覆すことはできないはず。ただし Claude Code 公式仕様に明記なし。

**残課題**: v2.18.0 以降の検証タスクとして記録。現時点では「覆せない前提で設計する」ことを推奨。

---

## 8. 参照先

### 一次資料

| 資料 | 場所 | 内容 |
|---|---|---|
| 配布元ルール | `/CLAUDE.md` | 3 ファイル同期・wheel 実体検証手順（配布元開発者向け） |
| 共通ルール | `.claude/CLAUDE.md` | LLM 行動規範・承認フロー |
| 設定キー仕様 | `.claude/docs/settings.json.md` | settings.json の各キー詳細仕様 |
| ディレクトリ命名 | `.claude/docs/taxonomy.md` | ディレクトリの命名・役割・配置判断 |
| プラットフォーム別 | `.claude/docs/platform-adapters.md` | Codex / Cursor adapter の生成物と参照経路 |
| 背景・設計判断 | `.claude/docs/C3のconfig_policyとversion_upgradeの考慮点と超えるべき壁.md` | 本ドキュメント作成の背景・version upgrade の残課題 |

### 実装ファイル

| ファイル | 場所 | 内容 |
|---|---|---|
| 除外パターン定義 | `src/c3/_excludes.py` | `EXCLUDE_PATTERNS` / `KEEP_PATTERNS` 定数 + `should_skip()` |
| ビルド時除外 | `hatch_build.py` | `_excludes.py` の重複定義（ビルドフック用） |
| 同期確認 hook | `.dev/hooks/_sync_check.py` | 3 ファイル変更時の警告（配布元専用） |
| 自動承認 hook | `.claude/hooks/permission_handler.py` | `permission_rules.json` を読んで自動承認 |

### 残課題リンク（v2.18.0 以降）

- `permission_rules.json` の `auto_allow` が `settings.json.permissions.deny` を覆せるか検証
- `~/.claude/settings.json` のグローバル設定と `.claude/settings.json` の同キー競合時のマージ範囲確認
- ~~`c3 update` の削除検出（`deletions.txt` 方式、v2.18.0 予定）~~ → v2.18.0 で実装
- `c3 update` 時の Breaking changes 警告（v2.19.0 予定）
