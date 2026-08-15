# C3 Config Policy — 設定階層・配布判断の公式リファレンス

**対象読者**: C3 利用先ユーザー / C3 自体をフォーク・拡張する開発者
**canonical 宣言**: 設定優先順位・配布判断に関しては本ドキュメントが唯一の公式情報源。
他ファイル（`CLAUDE.md` / `_excludes.py` docstring）から本ドキュメントへの参照リンクが張られている場合、
詳細は常に本ドキュメントを参照すること。

> 本ドキュメントは wheel に同梱され `c3 update` で更新されるため、内容は常にインストール済みの
> C3 と同じ版になる。バージョン番号は `c3 --version` / `.claude/state/c3_version.txt` で確認できる
> （かつて記載していたバージョンヘッダは、更新されないまま実態とずれるだけだったため撤去した）。

---

## 1. 設定ファイル一覧（所在マップ）

### 1-1. 配布元リポジトリ（C3 開発者向け）

```
/CLAUDE.md                             配布元専用（.gitignore で除外）
.claude/CLAUDE.md                      配布元・配布先共通（常時注入）
.claude/settings.json                  プロジェクト共通設定（配布される）
.claude/settings.local.json            個人 override（配布されない）
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
.claude/rules/                         c3 init で配置・c3 update で更新
.claude/rules/promoted/index.md        c3 init で空雛形のみ配置（c3 update は触らない）
.claude/hooks/                         c3 init で配置・c3 update で更新
.claude/state/                         実行時生成（c3.db 等は git 管理を推奨・一部除外。下記参照）
.claude/memory/                        実行時生成（チーム開発では git 管理を推奨。下記参照）
.claude/agent-memory/                  実行時生成（チーム開発では git 管理を推奨。下記参照）
.claude/reports/                       実行時生成（チーム開発では git 管理を推奨。下記参照）
.claude/worktrees/                     並列実行時一時生成（.claude/.gitignore が自動除外）
.claude/logs/                          実行時生成（.claude/.gitignore が自動除外）
```

> **`.claude/` 実行時生成領域のコミット方針**: **チーム開発では「載せない理由があるもの以外は
> git 管理する」を既定とする。** C3 は使いながら育てるフレームワークで、育った結果の大半は
> これらの領域に溜まる。載せなければ、その資産は最初に動かした人のマシンにしか存在しない。
> **`c3 init` は `.claude/.gitignore` を配置する**（プロジェクトルートの `.gitignore` は編集しない）。
> そこで除外されない領域は既定で tracked になる。
>
> **載せない**（載せても価値がないか、載せると邪魔になるもの）:
>
> | 対象 | 理由 |
> |---|---|
> | `state/recall.hnsw` / `state/recall_meta.json`（+ `.bak`） | **元データから再生成可能**。実測 63MB あり履歴を無用に太らせる |
> | `state/*.flag` / `state/tier_selection.json` | hook が動的生成する**セッション一時**ファイル |
> | `logs/` / `worktrees/` | 実行ログ・並列実行の一時領域 |
>
> > **既存プロジェクトへの移行（重要）**: **`.gitignore` は「既に tracked のファイル」には効かない。**
> > v2.58.0 より前から C3 を使っていて `state/recall.hnsw`（実測 63MB）等を既に commit している場合、
> > `c3 update` で `.claude/.gitignore` が配置されても**それだけでは追跡から外れない**。
> > 一度だけ以下を実行する（作業ツリーのファイルは消えない）:
> >
> > ```bash
> > git rm -r --cached .claude/state/recall.hnsw .claude/state/recall_meta.json \
> >                    .claude/logs .claude/worktrees 2>/dev/null
> > git status   # 削除予定が想定どおりか確認してから commit する
> > ```
> >
> > 履歴からも消したい場合（63MB が clone を重くしている等）は `git filter-repo` 等での
> > 履歴書き換えが必要で、共有ブランチでは force push を伴うためチームと合意してから行う。
> > **未 commit のプロジェクトでは何もしなくてよい**（`.claude/.gitignore` が最初から効く）。
> >
> > また、プロジェクトルートの `.gitignore` で `.claude/state/` のように**ディレクトリごと**除外して
> > いる場合、git はその配下へ降りないため `.claude/.gitignore` の否定パターン（`!state/setup_done.flag`
> > 等）が**効かない**。ルート側を `.claude/state/*` のようなワイルドカード形式にするか、
> > ルート側の当該行を削除して `.claude/.gitignore` に一本化する。
>
> **載せる**（引き継ぎ資産）:
>
> | 対象 | 何が引き継がれるか |
> |---|---|
> | `agent-memory/` | 各 agent が蓄積した判断基準。とりわけ reviewer の `[許容例外]`（この指摘はこのプロジェクトでは許容する、というユーザー判断の記録）は、コミットして初めてチーム全員の reviewer が同じ判断を再現できる |
> | `reports/` | 個々の変更の記録。PR レビュー時に「レポート＋実差分」を並べて読め、レビュアーが変更の意図まで追える（実運用で有効性を確認済み） |
> | `memory/` | セッション記録（現在地・残タスク・うまくいったアプローチ）と学習パターン。次に触る人が前回の文脈から再開できる |
> | `state/c3.db` | tier-routing の学習データと review-hint の判断記録（許容例外・是正履歴）。載せないと「このプロジェクトではこの指摘を許容する」という合意が記録した個人に閉じる |
> | `state/security_audit_exceptions.json` / `state/tier_autoapply.jsonl` | 許容例外の設定と tier 適用ログ |
>
> agent-memory は Claude Code の `memory: project` スコープの実体で、公式仕様でも「バージョン管理で
> 共有可能」な領域として設計されている（共有したくないものは `user` / `local` スコープに置く）。
>
> **c3.db のコンフリクトについて**: SQLite バイナリは git でマージできないため、同一プロジェクトを
> 複数人が**同時に**改修する運用では、衝突時の解決手段が「どちらかの DB を丸ごと選ぶ」しかなく
> 片方の記録が失われる。2026-07-28 時点の実運用ではコンフリクトは発生していない（同一プロジェクトへの
> 同時改修という運用形態を取っていないため）。**まず載せる方針とし、実際に衝突が問題化した時点で
> エクスポート方式（DB を JSONL 等へダンプして tracked にし、DB 本体は再構築する）を検討する。**
> 衝突の起きやすさはチームの運用形態に依存するため、一律の正解はない。
>
> **前提となる規律**: コミットする以上、agent 定義の `## Memory` 節が定める**記録対象の限定**
> （秘密情報・一時情報・雑記録を書かない）が守られている必要がある。共有するとこの規律は
> 「自分だけの問題」ではなくなる。
>
> **public リポジトリでの注意**: security-reviewer の MEMORY.md と `security-review-report-*.md`
> には構造上「この脅威はこのプロジェクトでは許容する・理由は〜」が蓄積される。private repo では
> チームの資産だが、public では「**既知の弱点と、それを見逃している理由の一覧**」を公開することに
> なる。公開プロジェクトでは security 系（reviewer の agent-memory と security-review-report）を
> `.gitignore` に加えるか `local` スコープへ移すことを検討する。
> （C3 配布元リポジトリ自身が `.claude/agent-memory/` と `.claude/reports/` を gitignore して
> いるのは、この理由と、配布物と開発ログを分けるためであって、「共有すべきでない領域だから」
> ではない。）
>
> なお各 `MEMORY.md` は起動時に**先頭 200 行 / 25KB まで**しか system prompt に載らない（超過分は
> 読まれない）。共有運用ではこれは「チーム全員の reviewer が過去の合意を読めなくなる」ことを意味する
> ため、`stop.py` が 80% 到達で stderr 警告を出す。警告が出たら価値の低いエントリから削って予算内に戻す。

### 1-3. ディレクトリの命名・配置チート

各ディレクトリの **命名・役割・配置判断** は `.claude/docs/taxonomy.md` を参照。
本ドキュメントは配布判断・優先順位に特化しており、taxonomy と住み分けている。

---

## 2. 設定優先順位と書き込み権限

設定は性質の異なる **3 つのレイヤー**（レイヤー B は v2.72.0 で削除済み）に分かれる。1 列に並べると誤解を生むため分離して記述する。

### レイヤー A: ツール権限（Claude Code 公式）

同じキー（`permissions.allow` など）が複数ファイルで定義されている場合、
**上位ファイルが下位ファイルを上書き**する（高 → 低の順）:

1. `.claude/settings.local.json` — 個人 override（`.claude/.gitignore` が自動除外。c3 update の対象外）
2. `.claude/settings.json` — プロジェクト共通設定（git 管理）
3. `~/.claude/settings.json` — グローバル個人設定（マシンローカル）

> **補足**: `hooks` は両ファイルの内容がマージされる（本配布元リポで `settings.json` の lifecycle hooks と `settings.local.json` の `.dev/hooks/*` が並走している実機事実）。
> ただし Claude Code 公式 docs では hooks のマージ挙動が明記されていないため、チーム全体で必要な hook は `settings.json` 側に集約するのが安全。
> 詳細は §7「既知の落とし穴」を参照。
> キー仕様の詳細は `.claude/docs/settings.json.md` を参照。

### レイヤー B: 自動承認パターン（C3 独自拡張）

（削除済み・v2.72.0）C3 独自の自動承認パターン（`.claude/permission_rules.json` の `auto_allow` 配列と
`permission_handler.py` hook）は v2.72.0 で削除された。自動承認は上流 Claude Code の
`permissions.allow`（レイヤー A）へ委譲する。移行手順は `.claude/breaking-changes.txt` の
v2.72.0 行を参照。

### レイヤー C: LLM 指示・知識（CLAUDE.md / rules）

- `.claude/CLAUDE.md` — 常時注入されるプロジェクト指示
- `.claude/rules/*.md` — 常時全文注入（`paths:` フロントマターは「適用範囲のドキュメント」であり、注入タイミングは変わらない）
- `.claude/rules/promoted/` — `rules/` 配下として **再帰的に自動ロード**される（Claude Code 公式仕様。
  CLAUDE.md からの `@import` は不要）
  （`/promote-pattern` skill が昇格ルールを `YYYYMMDD-{id}.md` として追加し、`index.md` のマーカー間に目録行を追記する **ユーザー所有領域**）

「優先順位」という概念は厳密には適用されない（全文ロードされる）が、
**`rules/promoted/` は c3 update が触らない**点が肝となる。

### 書き込み権限マトリクス

| ファイル | c3 init が初期配置 | c3 update が上書き | ユーザーが編集してよい |
|---|---|---|---|
| `.claude/settings.json` | ○ | ○ | △（チーム合意のもと） |
| `.claude/settings.local.json` | × | × | ○（個人 override / 秘匿情報） |
| `.claude/CLAUDE.md` | ○ | ○ | △（c3 update で上書きされる前提で） |
| `.claude/rules/*.md` | ○ | ○ | △（同上） |
| `.claude/rules/promoted/index.md` | ○（空雛形のみ） | × | ○（`/promote-pattern` が追記、手動編集も可） |
| `.claude/.gitignore` | ○ | ×（INIT_ONLY） | ○（追記は `c3 update` で失われない。C3 側の分類更新は手動マージ） |

---

## 3. 配布判断マトリクス（15 カテゴリ）

`_excludes.py` の `EXCLUDE_PATTERNS` / `KEEP_PATTERNS` を実装照合した結果。
各カテゴリに配布有無・c3 update の更新有無・理由を明示する。

| # | カテゴリ | 配布 | c3 update が更新 | 理由 |
|---|---|---|---|---|
| 1 | `.claude/hooks/*.py` | ○ | ○ | Claude Code lifecycle hook の実体。配布先で動作する。v2.20.0 で `hooks/schema.sql` を削除し SQLite スキーマは `src/c3/migrations/` に移管 |
| 2 | `.claude/agents/*.md` | ○ | ○ | ペルソナ定義。配布先で読まれる |
| 3 | `.claude/skills/*/` | ○ | ○ | オーケストレーション/ユーティリティ skill 定義（`scripts/` / `templates/` 等サブディレクトリ含む）|
| 4 | `.claude/rules/*.md` | ○ | ○ | C3 配布デフォルトルール（常時注入対象）。配布元は `judgment-principles.md` の 1 件（ほかは `promoted/` のみ）。利用先ではこれに `/setup` 生成の `coding-standards.md` 等が加わる |
| 5 | `.claude/rules/promoted/*` | ○ | ×（`index.md` は INIT_ONLY） | プロジェクト固有昇格ルール。配布元の `promoted/index.md` は空雛形のみ配布。利用先で `/promote-pattern` が追記する **ユーザー所有領域**。`index.md` は `INIT_ONLY_PATTERNS` で上書きから保護される。昇格ルール本体（`YYYYMMDD-{id}.md`）は template 側に存在しないため `_walk_diff` の走査対象にならず元から無事 |
| 6 | `.claude/docs/*.md` | △（一部のみ） | ○ | 利用先向けリファレンス。配布対象は `autonomous-mode-onboarding.md` / `config-policy.md`（本ドキュメント）/ `nul-boundary.md` / `parallel-agents-setup.md` / `platform-adapters.md` / `settings.json.md` の 6 ファイル。配布元固有の設計メモ等は `_excludes.py` で個別除外。`taxonomy.md` は tracked（GitHub 公開）だが EXCLUDE 対象のため wheel 非配布（詳細は §7 落とし穴 2 参照）。 |
| 7 | `.claude/CLAUDE.md` | ○ | ○ | 配布先で常時注入される共通ルール |
| 8 | `.claude/settings.json` | ○ | ○ | プロジェクト共通設定（hooks 登録・permissions など） |
| 9 | `.claude/permission_rules.json` | ×（削除済み・v2.72.0） | — | C3 独自の自動承認パターンは v2.72.0 で削除された。上流 `permissions.allow` へ委譲（移行手順は `.claude/breaking-changes.txt` の v2.72.0 行を参照） |
| 10 | `.claude/settings.local.json` | × | — | 個人 override・秘匿情報。`_excludes.py` でも除外、`.gitignore` でも除外、c3 update も触らない |
| 11 | `.claude/reports/*` / `memory/*` / `agent-memory/*` / `state/*` / `tmp/*` / `worktrees/*` / `logs/*` | × （`.gitkeep` のみ ○） | — | 実行時生成領域。空ディレクトリのみ `KEEP_PATTERNS` の `.gitkeep` で配布。データ本体は除外。v2.19.0 で `.claude/state/c3_version.txt`（バージョン checkpoint）を追加（`state/*` 一括除外により自動非配布） |
| 12 | `.dev/*` / `/CLAUDE.md` / `/AGENTS.md` / `/.codex/` / `/.cursor/` / `/.agents/` | × | — | 配布元専用または adapter 生成物。wheel には構造的に含まれない（`src/c3/_template/.claude/` 配下のみ同梱）が、配布元 `.gitignore` で commit 混入も防ぐ |
| 13 | `.claude/deletions.txt` | ○ | ○ | `c3 update` が読み込み、利用先 `.claude/` から該当ファイルを削除候補として扱う。配布元の `.claude/deletions.txt` に追記したエントリは次回 pip install → `c3 update` で利用先に伝播。`KEEP_PATTERNS` で明示配布。`c3 update` 自体は本ファイルを削除しない（§7 落とし穴 4 参照） |
| 14 | `.claude/breaking-changes.txt` | ○ | ○ | `c3 update` が読み込み、利用先の `.claude/state/c3_version.txt`（バージョン checkpoint）と diff を計算して破壊的変更を表示する。MAJOR bump 時は y/N 承認プロンプトを発火。`KEEP_PATTERNS` で明示配布。配布元 `.claude/breaking-changes.txt` を更新すれば次回 pip install → `c3 update` で利用先に伝播。利用先 git 管理は tracked（上書きされる） |
| 15 | `.claude/.gitignore` | ○ | ×（INIT_ONLY） | 利用先の実行時生成領域のうち「再生成可能」「セッション一時」だけを除外する配布 `.gitignore`（§1-2 の方針の実装）。プロジェクトルートの `.gitignore` は編集しないため既存設定と衝突しない。`INIT_ONLY_PATTERNS` により初回配置のみで **c3 update は上書きしない**（利用先の追記を守る）。C3 側で分類を更新した場合はリリースノートで手動マージを案内する |

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

> **注意**: `c3 init` はプロジェクトルートの `.gitignore` を自動編集しない。代わりに `.claude/.gitignore` を配置し、その中で `settings.local.json` を除外している（v2.58.0〜）。**それ以前に `c3 init` した既存プロジェクトには `.claude/.gitignore` が無い**ため、`c3 update` で配置されるまではユーザー自身がルートの `.gitignore` に `.claude/settings.local.json` を追加すること。

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
| `.gitignore` | git 追跡から除外（配布元リポジトリの実行時データ・開発ログを管理外に。利用先の推奨方針は §1-2 の注記を参照） | 配布元で新たに除外すべきファイルが増えた時 |
| `src/c3/_excludes.py` | `c3 init` / `c3 update` 時の除外判断（Python 実装） | 配布先への配布/非配布を変更する時 |
| `hatch_build.py` | wheel ビルド時の除外判断（`_excludes.py` の重複定義） | `_excludes.py` を変更した時（必ず同期） |

**`breaking-changes.txt` の同期義務（v2.19.0 追加）**: 新たな破壊的変更をリリースする際は、必ず `_excludes.py` / `hatch_build.py` の `KEEP_PATTERNS` に `"breaking-changes.txt"` が含まれていることを確認した上で、配布元 `.claude/breaking-changes.txt` にエントリを追記する。`scripts/extract_breaking_changes.py --check` で CHANGELOG との整合性を確認し、未記載があれば追記してから wheel を再ビルドする（詳細は配布元 `/CLAUDE.md` §6 参照）。

`hatch_build.py` の重複が必要な理由: hatch build hook はパッケージ import 前に走るため、
`_excludes.py` を import できない。2 ファイルの完全一致が必須。

**KEEP 対象が sdist exclude 配下にある場合は `pyproject.toml` の force-include も同期対象**:
公開ビルドは sdist を経由するため、sdist から落ちたファイルは wheel にも届かない。

この辺（KEEP ↔ sdist exclude / force-include）は `tests/test_three_file_sync.py` が双方向に機械検証する
（救済漏れと、用途不明な force-include キーの増殖の両方を検出する）。
ただし **KEEP に相当する配布必須ファイルは force-include による救済を認めず、ignore 規則そのもの（否定行）で戻す**
（force-include で救うと git 履歴から消える脆い救済になるため。force-include が担うのは sdist exclude 経路の救済に限る）。
この検査は**配布元 C3 のリポジトリ専用**である: 配布元 C3 が公開する wheel に `tests/` は含まれず、
利用先プロジェクトにこの検査は配置されない。ただし**配布元 C3 が PyPI へ公開する第二の成果物である
sdist には `tests/` が含まれる**（`pyproject.toml` の sdist `include`）。sdist は PyPI で公開されるため
`tests/` 配下の内容は第三者が閲覧可能であり、機密情報を書いてはならない。

### 同期確認の方法

`.dev/hooks/_sync_check.py`（PostToolUse hook）が、3 ファイルのいずれかを変更した時に
残り 2 ファイルの同期を `stderr` で警告する。警告が出たら必ず対応する。

同期の実効一致はテスト 2 本が機械検証する（hook の警告は編集時の即時注意喚起、テストは CI /
フルスイートでの機械判定という役割分担）:

| テスト | 検証する辺 |
|---|---|
| `tests/test_excludes.py` | `_excludes.py` ↔ `hatch_build.py`（パターン集合の完全一致） |
| `tests/test_three_file_sync.py` | `.gitignore` ↔ `_excludes.py`（挙動の一致）・`pyproject.toml` の sdist exclude / force-include |

これらのテストも**配布元 C3 のリポジトリ専用**である: 配布元 C3 が公開する wheel に `tests/` は
含まれず、利用先プロジェクトにこれらのテストは配置されない。ただし**配布元 C3 が PyPI へ公開する
第二の成果物である sdist には `tests/` が含まれる**（`pyproject.toml` の sdist `include`）。
sdist は PyPI で公開されるため `tests/` 配下の内容は第三者が閲覧可能であり、機密情報を書いてはならない。

### 変更手順

1. `.gitignore` / `_excludes.py` / `hatch_build.py` のいずれかを変更
2. `_sync_check.py` の警告を確認
3. 残り 2 ファイルに同じパターンを追加（または削除）。3 ファイルが既に同じ意図を持っている場合は、出遅れた 1 ファイルを合わせるだけでよい（配布元の root `.gitignore` を `KEEP_PATTERNS` の意図へ合わせた 2026-08-14 の是正が実例）
4. 意図的差分（3 ファイルの意図をあえて揃えない例外）を増減する場合は、`tests/test_three_file_sync.py` の許容リスト（理由文字列必須）と件数定数群を同時更新する（配布元専用。増減いずれの場合もレビュー対象）
5. 配布元では `python scripts/verify_wheel.py` で wheel 実体を機械検証する（`scripts/` は配布物に含まれないため、利用先には存在しないコマンド）

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
c3 update が上書きすると利用先で `/promote-pattern` が追記した目録行が消失するため、
`_excludes.py` の **`INIT_ONLY_PATTERNS`** で「初回配置のみ・update は触らない」に指定している（§3 カテゴリ #5 参照）。

> **経緯（v2.58.0）**: それ以前は「意図的に除外している」と書きながら実装が伴っておらず
> （`should_skip("rules/promoted/index.md")` は `False`）、実際には `c3 update` の上書き対象だった。
> 原因は `should_skip` が「wheel 収録 / init 配置 / update 上書き」の 3 つを 1 つの真偽値で兼ねており、
> 「init では配置するが update では触らない」を表現できなかったこと。`INIT_ONLY_PATTERNS` で
> 3 番目の軸を分離して解消した。

**対処**: `promoted/` への変更は `c3 update` に委ねず、手動または `/promote-pattern` skill で管理する。
C3 side で `promoted/` の雛形を更新した場合は、リリースノートで手動マージ手順を案内する。

**削除経路**: INIT_ONLY ファイルは `_walk_diff` だけでなく `.claude/deletions.txt` 経由の削除からも保護される。
配布元が誤って `rules/promoted/index.md` を `deletions.txt` に追記しても、利用先ユーザーが育てた
目録行が完全削除されることはない（セーフガード step 13 で弾かれ、warning が出力される）。

**意図的廃止の場合**: INIT_ONLY をユーザーの同意のもと廃止したい場合、`deletions.txt` は使えない
（セーフガード構造上、INIT_ONLY は削除不能に設計）。代わりにリリースノートで「利用先ユーザーが
手動で削除するよう案内する」手順を記載する（c3 update では自動削除できない制約を明記）。

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
  - INIT_ONLY ファイル（`.gitignore` / `rules/promoted/index.md`）。判定は解決済み実体パスに対して
    成分正規化（各パートに `.lower().rstrip('. ')` 適用）して行われるため、表記ゆれ（二重スラッシュ
    `rules//promoted/`・末尾スラッシュ `rules/promoted/index.md/`・大小違い `RULES/PROMOTED/INDEX.MD` や
    `.GITIGNORE`）でも全 OS で保護される。実在しないファイルに対しても保護が成立する（従来は実体ケース
    正準化に依存していたが、実体がない場合は `Path.resolve()` はケース正準化を行わないため、全 OS 対応には
    成分正規化が必須。また Unicode 正規化形の差（macOS の NFD など）は `.lower()` では畳まれないという制限あり）

**対処**:
- 削除候補に書くパスは「.claude/ からの相対 POSIX パス」のみ（例: `agents/foo.md`）
- ディレクトリを丸ごと削除したい場合は、配下のファイルを 1 つずつ列挙
- `c3 update --dry-run` で warning が出ていないか確認
- `deletions.txt` 自身を更新したい場合は通常の `c3 update` の add/update ロジックが処理する

### 落とし穴 5: `permission_rules.json` は `settings.json.permissions.deny` を覆せない（要検証）

対象機能は v2.72.0 で削除済み（C3 独自の自動承認パターン一式）。上流 `permissions.allow` へ委譲した。
移行手順は `.claude/breaking-changes.txt` の v2.72.0 行を参照。

### 落とし穴 6: `permissions.allow` のワイルドカードはプレフィックス一致のため `../` パストラバーサルに対応していない

**症状**: `settings.json` の `permissions.allow` に `"Bash(c3 run .claude/hooks/stop.py*)"` と書いても、
`c3 run .claude/hooks/stop.py/../../../malicious.py` という形でパストラバーサルが可能に見える。

**原因**: Claude Code の `permissions.allow` マッチング機構は **コマンド文字列のテキストプレフィックス一致** に基づいている。
`../` を含むパスでも許可パターンのプレフィックスに一致する限り、許可される。完全なパス正規化・相対パス内の `..` 検証は
行われない（Claude Code 本体のハーネス実装に依存）。

**仕様的な扱い**: 
- `c3 run` への切り替え時点で「新規に生じたリスク」ではなく、旧 `python` 時代から同一構造で存在していた。
- ADR-3「python と同等で新権限を追加しない」は成立している（新規の穴を開けていない）。
- ただし「既存の穴が継続している」ことは認識・記録の価値がある。

**対処**:
- 完全な解決には Claude Code 側の許可マッチング仕様変更が必要（スコープ外）。
- 次善策: 通常のワークフローでは `c3 run` は決められたスクリプトのみを呼ぶため、実害は限定的。
  コマンドを工作する（`../` を含める）動機が無ければこのリスクは発動しない。
- 注意: `worktree_guard.py` は `Write`/`Edit` ツール専用の worktree 境界チェックであり、
  `Bash` 経由のコマンド実行（本落とし穴の `c3 run` パストラバーサル）には適用されない（本落とし穴には無関係）。

**確認方法**: セキュリティ厳密性が必要な環境では、`settings.json` の `permissions.allow` パターンを
定期的にレビューすること（`c3 run <固定スクリプトパス>` 形式のみを許可し、`../` を含みうる可変パスを
プレフィックス許可しない）。

**追記（SR-AI-001）: 破壊操作を伴うスクリプトは追記系と同列に扱わない**

上記のプレフィックス一致という性質は、許可対象のスクリプトが**何をするか**によって危険度が変わる。
`c3 run <script>*` の末尾ワイルドカード 1 本は、そのスクリプトが取りうる**あらゆる引数列**を
無確認で許可する。追記系スクリプト（記録・注入・インデックス更新）ならこれで実害は限定的だが、
**ファイルの移動・削除・上書きを行うスクリプトでは、引数 1 つで対象ディレクトリごと差し替えられる**。
実例: `archive_reports.py` は `--reports-dir <任意ディレクトリ>` を取り、その直下の `*.md` を
移動して移動元を削除する。末尾ワイルドカードで allow すると、プロンプトインジェクション経由で
任意ディレクトリの `*.md` が無確認で移動・削除されうる。

**対処**（新しい allow を追加する際・レビューで拾う際のチェック）:
- そのスクリプトが破壊操作（削除・移動・上書き）を伴うかをまず判定する
- 伴う場合、allow は**正規経路が実際に使う形だけ**に絞る（引数なしの完全一致 + 正規のオプションで
  始まる形）。末尾ワイルドカード 1 本にしない
- allow の絞り込みだけでは足りない場合がある。`<script> --opt *` 形のパターンは、後続に別の
  オプションが続く文字列とも前方一致しうるため、**危険な引数自体に env ゲートを課す**
  （例: `archive_reports.py` の `--reports-dir` は `C3_ARCHIVE_REPORTS_DIR_OK=1` がある場合のみ有効）。
  env は allow パターンの前方一致では満たせないため、単一のコマンド文字列では越えられない関所になる

**追記（SR-NEW-2）: env ゲートの射程は単一コマンド文字列に対する関所であり、セッション跨ぎの env 継承は射程外**

上記の env ゲート（例: `C3_ARCHIVE_REPORTS_DIR_OK=1`）が防いでいるのは「1 本の許可済みコマンド文字列の
前方一致だけでは越えられない」という点であり、**シェルセッションを跨いだ環境変数の永続化・継承は
別の話**である。`export C3_ARCHIVE_REPORTS_DIR_OK=1` を `.bashrc` 等の起動ファイルに書く・
CI のジョブ環境変数として設定する・親プロセスから子プロセスへ継承させる、といった経路で
ゲートが「常時開いた状態」になれば、env ゲート自体は無力化される。ゲートは「未設定が既定」
という運用を前提にした関所であり、恒久的な env 設定と組み合わせて使わないこと。

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

### 実装ファイル

| ファイル | 場所 | 内容 |
|---|---|---|
| 除外パターン定義 | `src/c3/_excludes.py` | `EXCLUDE_PATTERNS` / `KEEP_PATTERNS` 定数 + `should_skip()` |
| ビルド時除外 | `hatch_build.py` | `_excludes.py` の重複定義（ビルドフック用） |
| 同期確認 hook | `.dev/hooks/_sync_check.py` | 3 ファイル変更時の警告（配布元専用） |

### 残課題リンク（v2.18.0 以降）

- `~/.claude/settings.json` のグローバル設定と `.claude/settings.json` の同キー競合時のマージ範囲確認
- ~~`c3 update` の削除検出（`deletions.txt` 方式、v2.18.0 予定）~~ → v2.18.0 で実装
- `c3 update` 時の Breaking changes 警告（v2.19.0 予定）
