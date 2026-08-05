# C3 仕様目次 — 配布境界層

> **⛔ 凍結: 2026-08-05 時点のスナップショット。**
> 実装を変更しても更新しない（2026-08-05 裁定）。**現行仕様として読まないこと。**
> 理由と、現在の姿を知る方法は [`00-index.md`](00-index.md) の冒頭を参照。

現行 C3 が「何を入力すると何が出るのか」を、配布境界（作り直しで壊してはいけない外部契約）に限定して全列挙する。
すべての記述は実ファイルの Read に基づき、根拠を `file:line` で示す。読み取れなかったものは末尾「未確認の項目」に隔離した。

対象コミット: `d9037a2` (v2.61.0) / `src/c3/__init__.py:3`

---

## サマリ

| マーカー | 件数 | 意味 |
|---|---|---|
| `[手動のみ]` | **9** | 規約（配布元 `/CLAUDE.md`）上は必須の関所だが CI で自動実行されていない |
| `[三重定義]` | **4** | 除外パターンの重複定義。SSOT と複製の対応を各節に明示 |
| `[未文書化]` | **7** | 実装はあるがドキュメントに無い（または記述が実体とズレている）配布挙動 |

リリース関所は全 **11 件**（CI 自動 **2** / 手動のみ **9**）。詳細は「7. リリース関所」を参照。

---

## 1. wheel / sdist に何が入るか

### 1-1. wheel

| 項目 | 内容 |
|---|---|
| 入力 | `pyproject.toml` の `[tool.hatch.build.targets.wheel]` / `force-include`、ビルドフック生成物 `src/c3/_template/` |
| 出力 | wheel 内 `c3/`（`src/c3` パッケージ本体）、`c3/_template/`（`src/c3/_template` を force-include）、`c3/LICENSES/`（ルート `LICENSES/` を force-include） |
| 契約 | 利用先の `c3 init` / `c3 update` が読む template は wheel 内 `c3/_template/.claude/` に必ず存在する（`paths.templates_dir()` の解決先） |
| 根拠 | `pyproject.toml:50-55` / `src/c3/paths.py:50-53` |

| 項目 | 内容 |
|---|---|
| 入力 | `src/c3/migrations/*.sql`（`001_initial.sql` 〜 `007_review_decisions_resolution.sql`） |
| 出力 | wheel 内 `c3/migrations/`（`packages = ["src/c3"]` に含まれるため自動収録） |
| 契約 | SQLite スキーマは wheel 側で管理され、利用先 `.claude/hooks/schema.sql` は存在しない（v2.20.0 破壊的変更） |
| 根拠 | `pyproject.toml:51` / `.claude/breaking-changes.txt:8` / `.claude/deletions.txt:41-42` |

| 項目 | 内容 |
|---|---|
| 入力 | ルート `LICENSES/`（`fastembed-LICENSE` / `fastembed-NOTICE` / `numpy-LICENSE` / `onnxruntime-LICENSE` / `paraphrase-multilingual-MiniLM-L12-v2-LICENSE` の 5 ファイル） |
| 出力 | wheel 内 `c3/LICENSES/` |
| 契約 | 依存ライブラリ・埋め込みモデルのライセンス表示義務を wheel 単体で満たす |
| 根拠 | `pyproject.toml:55` |

### 1-2. sdist

| 項目 | 内容 |
|---|---|
| 入力 | `pyproject.toml` の `[tool.hatch.build.targets.sdist]` `include` = `src/c3` / `tests` / `hatch_build.py` / `.claude` / `README.md` / `LICENSE` / `LICENSES` / `CHANGELOG.md` / `ARCHITECTURE.md` |
| 出力 | sdist tarball。`.claude/` を丸ごと同梱するため、sdist からの wheel ビルド時に `hatch_build.py` が `_template/` を再生成できる |
| 契約 | sdist 単体で wheel を再現ビルドできる（`.claude` と `hatch_build.py` の両方が入る） |
| 根拠 | `pyproject.toml:57-68` / `hatch_build.py:83-92` |

| 項目 | 内容 |
|---|---|
| 入力 | `[tool.hatch.build.targets.sdist] exclude`（8 パターン） |
| 出力 | sdist から除外されるファイル |
| 契約 | 個人作業ファイル（reports / session memory / patterns.json / tmp / 一部 docs）を PyPI へ出さない |
| 根拠 | `pyproject.toml:69-78` |

**`[三重定義] #4 / [未文書化] #4`**: sdist の `exclude` は **8 パターン**しかなく、`src/c3/_excludes.py` の `EXCLUDE_PATTERNS`（**30 パターン**）と一致していない。かつ `/CLAUDE.md` §2 が定める 3 ファイル同期グループ（`.gitignore` / `_excludes.py` / `hatch_build.py`）に `pyproject.toml` は**含まれていない**（第 4 の重複定義でありながら同期義務の対象外）。
実測: git tracked な `.claude/` 配下 89 ファイルのうち、`should_skip()` が True（＝ wheel 非収録）かつ sdist `exclude` のどのパターンにも一致しないものが **1 件** ある。

- `.claude/state/setup_done.flag` — wheel からは `state/*` で除外されるが、sdist の exclude 一覧には `state/*` が無い
- 根拠: `src/c3/_excludes.py:59`（`"state/*"`） / `pyproject.toml:69-78`（`state/*` 不在） / `.claude/.gitignore:36`（`!state/setup_done.flag` により tracked 化） / `.gitignore:33-34`

> gitignored なファイルが hatchling の VCS-ignore 既定挙動で sdist から落ちるかどうかは wheel/sdist を実ビルドしていないため **未確認**（本作業ではビルド禁止）。上記 1 件は「tracked である」ことが確定しているため、VCS-ignore の挙動に依存せず sdist に入る側の候補として挙げている。

---

## 2. `hatch_build.py` — `.claude/` → `src/c3/_template/` のステージング

| 項目 | 内容 |
|---|---|
| 入力 | リポジトリルートの `.claude/` ディレクトリ（`self.root / ".claude"`） |
| 出力 | `src/c3/_template/.claude/`（既存ツリーを `shutil.rmtree` で**全削除してから**再生成） |
| 契約 | `src/c3/_template/` はビルド生成物であり手編集は無意味。`.claude/` が唯一の編集点 |
| 根拠 | `hatch_build.py:83-92` |

| 項目 | 内容 |
|---|---|
| 入力 | `EXCLUDE_PATTERNS`（30 件） / `KEEP_PATTERNS`（8 件） / `_should_skip()` |
| 出力 | フィルタ通過ファイルのみ `shutil.copy2`。全要素が skip された階層のディレクトリは `rmdir` で消える |
| 契約 | `__pycache__` / `*.pyc` / `*.pyo` は無条件除外。`KEEP_PATTERNS` は `EXCLUDE_PATTERNS` に**優先**する |
| 根拠 | `hatch_build.py:26-64`（EXCLUDE） / `hatch_build.py:66-75`（KEEP） / `hatch_build.py:95-107`（copy + 空ディレクトリ削除） / `hatch_build.py:110-116`（優先順位） |

| 項目 | 内容 |
|---|---|
| 入力 | hatchling の `[tool.hatch.build.hooks.custom]` 宣言（target 指定なし＝全 target 共通） |
| 出力 | wheel / sdist いずれのビルドでも `initialize()` が走る |
| 契約 | sdist からの wheel ビルド時も `_template/` が同じロジックで再生成される |
| 根拠 | `pyproject.toml:47-48` / `hatch_build.py:78-83` |

**`[三重定義] #1〜#3`**（SSOT と複製の対応）:

| 定義 | SSOT | 複製 | 複製が必要な理由 | 機械照合 |
|---|---|---|---|---|
| `EXCLUDE_PATTERNS` | `src/c3/_excludes.py:29-67` | `hatch_build.py:26-64` | ビルドフックは package import 前に走るため `c3._excludes` を import できない | **あり**（`tests/test_excludes.py:146-153` がタプル完全一致を検査） |
| `KEEP_PATTERNS` | `src/c3/_excludes.py:69-78` | `hatch_build.py:66-75` | 同上 | **あり**（`tests/test_excludes.py:156-159`） |
| `should_skip()` 本体ロジック | `src/c3/_excludes.py:107-114` | `hatch_build.py:110-116`（`_should_skip`） | 同上 | **なし**（関数実装は照合されていない。パターン一覧のみ照合） |
| `.gitignore` の除外行 | — | ルート `.gitignore:1-95` | git 追跡と wheel 配布は別レイヤー。パターン表記も `.claude/` プレフィックス付きで形式が違う | **なし** → `[手動のみ]`（後述の関所 #10） |

`.dev/hooks/_sync_check.py` は 3 ファイルのいずれかを Write/Edit した際に stderr 警告するのみで、**ブロックしない・照合もしない**（根拠: `.dev/hooks/_sync_check.py:30`（SYNC_GROUP 定義） / `:62-71`（警告のみ・exit 0））。かつ `.dev/` は配布元専用 hook 置き場で CI では動かない（根拠: `/CLAUDE.md:50-68`）。

---

## 3. `src/c3/_excludes.py` — 除外の 2 軸

| 項目 | 内容 |
|---|---|
| 入力 | `.claude/` からの相対 POSIX パス文字列（例 `"reports/x.md"`。`.claude/` プレフィックスは付けない） |
| 出力 | `should_skip(rel) -> bool` |
| 契約 | True のファイルは (a) wheel に入らない (b) `c3 init` が配置しない (c) `c3 update` が上書きしない — **3 つを 1 つの真偽値で兼ねる** |
| 根拠 | `src/c3/_excludes.py:1-23`（docstring に 3 用途を明記） / `:107-114` |

| 項目 | 内容 |
|---|---|
| 入力 | `EXCLUDE_PATTERNS`（30 件、`fnmatch.fnmatchcase` でケース依存一致） |
| 出力 | 除外対象判定 |
| 契約 | 実行時生成領域（`reports/*` `memory/sessions/*` `memory/archive/*` `agent-memory/*` `tmp/*` `logs/*` `state/*` `worktrees/*`）・個人設定（`settings.local.json`）・配布元固有 docs・廃止済み資産（`agents/tdd-develop.md` `skills/worktree-tdd-workflow/*`）は配布されない |
| 根拠 | `src/c3/_excludes.py:29-67` |

| 項目 | 内容 |
|---|---|
| 入力 | `KEEP_PATTERNS`（8 件） |
| 出力 | `EXCLUDE_PATTERNS` を打ち消す例外 |
| 契約 | `reports/.gitkeep` `memory/.gitkeep` `memory/sessions/.gitkeep` `memory/archive/.gitkeep` `tmp/.gitkeep` `state/.gitkeep` `deletions.txt` `breaking-changes.txt` は必ず配布される |
| 根拠 | `src/c3/_excludes.py:69-78` / 優先順位は `:112-114` |

| 項目 | 内容 |
|---|---|
| 入力 | `INIT_ONLY_PATTERNS`（2 件: `rules/promoted/index.md` / `.gitignore`） |
| 出力 | `is_init_only(rel) -> bool` |
| 契約 | `c3 init` は配置するが `c3 update` は**絶対に上書きしない**。wheel には収録される（`should_skip` とは独立の第 3 軸） |
| 根拠 | `src/c3/_excludes.py:81-104`（設計意図） / `:117-124`（判定） / `:98-103`（パターン本体） |

パターンは小文字・末尾ドット/スペースなしの正規形で書く規約がある（`fnmatchcase` がケース依存のため）。根拠: `src/c3/_excludes.py:92-97`。

**実測（`should_skip` 適用結果 / 2026-08-05 時点）**: `.claude/` 配下 1901 ファイル中、配布対象は **88 ファイル**。

| ディレクトリ | 配布ファイル数 |
|---|---|
| `.claude/` 直下 | 6（`.gitignore` / `CLAUDE.md` / `breaking-changes.txt` / `deletions.txt` / `permission_rules.json` / `settings.json`） |
| `agents/` | 14 |
| `docs/` | 6 |
| `hooks/` | 24 |
| `memory/` | 2（`.gitkeep` のみ） |
| `reports/` | 1（`.gitkeep`） |
| `rules/` | 1（`promoted/index.md` のみ） |
| `skills/` | 33 |
| `state/` | 1（`.gitkeep`） |

**`[未文書化] #1`**: `KEEP_PATTERNS` に列挙されている `tmp/.gitkeep` と `memory/sessions/.gitkeep` は**配布元に実体が存在しない**（実測）。したがって `c3 init` は利用先に `.claude/tmp/` と `.claude/memory/sessions/` を作らない。にもかかわらず配布される `.claude/.gitignore` は `!tmp/.gitkeep` という否定パターンを持ち、存在しないファイルを前提にしている。
根拠: `src/c3/_excludes.py:73,74`（KEEP に記載） / `.claude/.gitignore:55-56`（`tmp/*` + `!tmp/.gitkeep`） / 実体不在は `ls` 実測。

**`[未文書化] #2`**: `.claude/docs/` の配布対象は実測 **6 件**（`autonomous-mode-onboarding.md` / `config-policy.md` / `nul-boundary.md` / `parallel-agents-setup.md` / `platform-adapters.md` / `settings.json.md`）だが、配布判断マトリクスは「配布対象は 4 ファイル」と書いており `autonomous-mode-onboarding.md` と `nul-boundary.md` が記載されていない。
根拠: `.claude/docs/config-policy.md:196` vs `should_skip` 実測。

**`[未文書化] #3`**: 配布判断マトリクスのカテゴリ #4「`.claude/rules/*.md` = C3 配布デフォルトルール」は、実体としては **0 件**（`rules/` の配布ファイルは `rules/promoted/index.md` の 1 件のみ）。
根拠: `.claude/docs/config-policy.md:194` vs `should_skip` 実測 / `.claude/rules/promoted/index.md`。

---

## 4. `c3 init` — 利用先への展開

| 項目 | 内容 |
|---|---|
| 入力 | `paths.templates_dir()` が返す template ディレクトリ、引数 `--force` / `--target` / `--platform` / `--git` / `--no-git` |
| 出力 | `<target>/.claude/`（`should_skip` を通過した全ファイル）＋ 選択プラットフォームの adapter 生成物 |
| 契約 | 既存 `.claude/` があり `--force` 無しなら **exit 1 で拒否**し、`c3 update` を案内する |
| 根拠 | `src/c3/cli_init.py:72-96`（拒否は `:78-84`） / `:184-208`（`_copytree` + `should_skip` は `:204`） |

| 項目 | 内容 |
|---|---|
| 入力 | template の解決順序 |
| 出力 | (1) editable/source install（`<root>/src/c3/paths.py` から読まれ、`<root>/.claude/` と `<root>/pyproject.toml` が両方存在）→ live `.claude/` (2) wheel install → `importlib.resources.files("c3")/"_template"/".claude"` |
| 契約 | 配布元の開発者は編集が即反映される。利用先は wheel 同梱の template を読む。どちらも解決できなければ `FileNotFoundError` |
| 根拠 | `src/c3/paths.py:28-62` / `:65-76`（dev 判定） |

| 項目 | 内容 |
|---|---|
| 入力 | `INIT_ONLY_PATTERNS` 対象ファイル（`rules/promoted/index.md` / `.gitignore`） |
| 出力 | 初回のみ配置される（`_copytree` は `should_skip` しか見ないため通常ファイルとして配置） |
| 契約 | 配置後は利用先ユーザー所有。以後 `c3 update` は触らない |
| 根拠 | `src/c3/cli_init.py:204`（`should_skip` のみ参照） / `src/c3/_excludes.py:81-104` |

| 項目 | 内容 |
|---|---|
| 入力 | `--force` |
| 出力 | 既存 `.claude/` を `shutil.rmtree` してから再展開 |
| 契約 | **init-only のユーザー所有ファイル（`rules/promoted/index.md` / `.gitignore`）も失われる**（help に明記） |
| 根拠 | `src/c3/cli_init.py:35-42`（help 文言） / `:87-88`（rmtree） |

| 項目 | 内容 |
|---|---|
| 入力 | git 管理状態（`gitutil.detect_git_status`）、`--git` / `--no-git`、TTY 有無 |
| 出力 | 非 git ディレクトリなら同意プロンプト → `git init`。git 未インストール／非 TTY／拒否なら誘導メッセージのみ |
| 契約 | git の成否は `c3 init` の exit code に影響しない。メッセージは全て stdout |
| 根拠 | `src/c3/cli_init.py:113-181`（契約は `:117-120` の docstring） |

**`[未文書化] #5`**: `c3 init` は `.claude/state/c3_version.txt`（バージョン checkpoint）を**書かない**（`c3_version` の参照は `cli_update.py` にしか存在しない）。したがって `c3 init` 直後の初回 `c3 update` は必ず `bump == "initial"` になり、`breaking-changes.txt` の**全エントリが表示される**。
根拠: `src/c3/cli_init.py`（`c3_version` 参照なし・grep 実測） / `src/c3/cli_update.py:357-381`（不在なら `None`） / `:99-100`（`prev is None` → `"initial"`） / `:314-320`（initial は全件表示）。

**`[未文書化] #6`**: 既存 `.claude/` に対する拒否ガードは `platforms == ("claude",)` の場合にしか効かない。`c3 init --platform codex|cursor|opencode|all` は既存 `.claude/` があっても `--force` 無しで通り、`using existing .claude` と表示して adapter だけを生成する。逆に `--force` の rmtree は `"claude" in platforms` のときだけ走る。
根拠: `src/c3/cli_init.py:78`（拒否条件） / `:87`（rmtree 条件） / `:94-95`（既存利用パス）。

---

## 5. `c3 update` — 何を上書きし、何を上書きしないか

| 項目 | 内容 |
|---|---|
| 入力 | template（`templates_dir()`）と利用先 `<target>/.claude/` の差分、引数 `--dry-run` / `--target` / `--platform` / `--yes` |
| 出力 | `add`（利用先に無いファイルを配置）と `update`（内容が異なるファイルを上書き）のみ |
| 契約 | **`c3 update` は差分から削除を導かない**。template から消えたファイルは利用先に残り続ける |
| 根拠 | `src/c3/cli_update.py:992-1016`（docstring `:994-995` "Only ``add`` and ``update`` are emitted; we never delete files in dest."） |

| 項目 | 内容 |
|---|---|
| 入力 | `should_skip(rel_posix)` |
| 出力 | 該当ファイルを走査対象から外す |
| 契約 | 利用先の `reports/` `memory/sessions/` `agent-memory/` `state/` `tmp/` `logs/` `worktrees/` `settings.local.json` は絶対に上書きされない |
| 根拠 | `src/c3/cli_update.py:1007-1008` / `src/c3/_excludes.py:29-67` |

| 項目 | 内容 |
|---|---|
| 入力 | `is_init_only(rel_posix)` |
| 出力 | 利用先に**既に存在する**場合はスキップ、存在しなければ `add` |
| 契約 | `rules/promoted/index.md`（`/promote-pattern` の追記先）と `.claude/.gitignore`（利用先の独自除外行）はユーザー所有として保護される |
| 根拠 | `src/c3/cli_update.py:1010-1012` / `:999-1002`（docstring） |

| 項目 | 内容 |
|---|---|
| 入力 | `filecmp.cmp(src_file, target, shallow=False)` |
| 出力 | バイト単位で異なれば `update` |
| 契約 | 利用先が framework ファイルを直接編集していると、次回 `c3 update` で無条件に上書きされる（プロンプトは無い） |
| 根拠 | `src/c3/cli_update.py:1013-1015` / `:498-504`（上書き実行。`  update: <rel>` を事後表示するのみ） |

| 項目 | 内容 |
|---|---|
| 入力 | `.claude/state/c3_version.txt`（利用先の前回バージョン） / `c3.__version__` |
| 出力 | bump 判定（`initial` / `major` / `minor` / `patch` / `same` / `downgrade`）と breaking changes 表示 |
| 契約 | MAJOR bump かつ該当エントリありなら `y/N` プロンプトでブロック。`N` なら**ファイルを一切変更せず** exit 0 |
| 根拠 | `src/c3/cli_update.py:463-485`（`:464` にコメント「add/update の前に実行することで、MAJOR cancel 時にファイル変更も防ぐ」） / `:87-120`（bump 判定） |

| 項目 | 内容 |
|---|---|
| 入力 | 実行成功（非 dry-run・非 downgrade・deletions 非キャンセル） |
| 出力 | `.claude/state/c3_version.txt` に現バージョンを atomic write（tmp + `os.replace`） |
| 契約 | checkpoint は利用先の `state/` に生成され、`state/*` 除外により配布物には決して入らない |
| 根拠 | `src/c3/cli_update.py:384-408` / `:529-530` / `src/c3/_excludes.py:59` |

| 項目 | 内容 |
|---|---|
| 入力 | `--dry-run` |
| 出力 | 変更予定の一覧表示のみ。checkpoint も削除も書き込まない |
| 契約 | 副作用なしで差分確認できる |
| 根拠 | `src/c3/cli_update.py:490-496` / `:529`（dry-run では checkpoint を更新しない） / `:830-831`（削除も予告のみ） |

| 項目 | 内容 |
|---|---|
| 入力 | `--yes` / `-y` |
| 出力 | MAJOR bump プロンプトと削除確認プロンプトをスキップ |
| 契約 | `--dry-run` との併用では効果なし（help に明記） |
| 根拠 | `src/c3/cli_update.py:440-443` / `:475-476` / `:846-854` |

---

## 6. `deletions.txt` / `breaking-changes.txt`

### 6-1. `.claude/deletions.txt`

| 項目 | 内容 |
|---|---|
| 入力 | template 側 `deletions.txt`（`_load_deletions(template_dir)`）。1 行 1 パス・`.claude/` 相対・`#` コメント可 |
| 出力 | 利用先 `.claude/` 配下の該当ファイルを `unlink()`（確認プロンプト `y/N` 付き） |
| 契約 | `c3 update` が削除を検出しない性質を補う唯一の経路。**配布元がここに書かない限り利用先の旧ファイルは永久に残る** |
| 根拠 | `src/c3/cli_update.py:561-644`（ロード） / `:783-864`（適用） / `.claude/deletions.txt:1-11`（仕様コメント） / `/CLAUDE.md:101-105` |

| 項目 | 内容 |
|---|---|
| 入力 | 1 エントリの文字列 |
| 出力 | 15 段のセーフガードを通過した絶対 Path、または警告 |
| 契約 | 絶対パス / `~` / バックスラッシュ / ドライブレター / `.claude/` プレフィックス / `.` `..` / symlink / `.claude/` 外への脱出 / `deletions.txt` 自身 / init-only ファイル / ディレクトリ — はすべて拒否される |
| 根拠 | `src/c3/cli_update.py:647-780`（段数一覧は `:660-677` の docstring、init-only 保護は `:754-770`） |

| 項目 | 内容 |
|---|---|
| 入力 | UTF-8 BOM / 不正 UTF-8 / ANSI エスケープ |
| 出力 | BOM 検出時は**ファイル全体を破棄**して警告。ANSI 含み行は skip |
| 契約 | 壊れた `deletions.txt` は「何も削除しない」側に倒れる（fail-safe） |
| 根拠 | `src/c3/cli_update.py:584-591` / `:635-638` |

**同期義務**: 前リリース以降に `.claude/` から削除された**配布対象**ファイルは `deletions.txt` に追記しなければならない。検出は `scripts/check_deletions.py --check`。
根拠: `/CLAUDE.md:101-118` / `scripts/check_deletions.py:1-17`。
同スクリプトは配布判定に `c3._excludes.should_skip` を、パースに `c3.cli_update._load_deletions` を**再利用**しており、除外判定・パーサの重複定義は作っていない（SSOT 遵守）。根拠: `scripts/check_deletions.py:30-31`, `:196`。

### 6-2. `.claude/breaking-changes.txt`

| 項目 | 内容 |
|---|---|
| 入力 | template 側 `breaking-changes.txt`。形式 `vX.Y.Z|<English summary>|<Japanese summary>`（`|` 区切り・`maxsplit=2`） |
| 出力 | 半開区間 `(prev, curr]` に該当するエントリを bump レベルに応じて表示 |
| 契約 | 英語フィールドに `|` を含めてはならない。SemVer 純粋形式のみ（pre-release / build metadata は不可）。重複 version は先勝ち |
| 根拠 | `src/c3/cli_update.py:127-205`（ロード） / `:212-258`（区間抽出） / `:265-350`（表示） / `.claude/breaking-changes.txt:1-5`（形式コメント） / `src/c3/cli_update.py:65-76`（SemVer 制約） |

| 項目 | 内容 |
|---|---|
| 入力 | bump レベル |
| 出力 | `initial` / `major` はエントリ 0 件でもヘッダ表示、`minor` / `patch` は 0 件なら無出力、`same` は無出力、`downgrade` は stderr 1 行警告のみ |
| 契約 | downgrade 時は checkpoint を更新しない |
| 根拠 | `src/c3/cli_update.py:281-288`（docstring） / `:297-305` / `:529` |

**同期義務**: `CHANGELOG.md` に `### 破壊的変更` サブセクションを持つ version は、必ず `breaking-changes.txt` に 1 行記載する。検出は `scripts/extract_breaking_changes.py --check`。
根拠: `/CLAUDE.md:76-99` / `scripts/extract_breaking_changes.py:79-129`（CHANGELOG パーサ） / `:136-164`（記載済み version 取得） / `.claude/docs/config-policy.md:286`。

両ファイルとも `KEEP_PATTERNS` で明示的に配布される（`state/*` 等の一括除外に巻き込まれない）。根拠: `src/c3/_excludes.py:76-77` / `hatch_build.py:73-74`。

---

## 7. リリース関所 — 規約と CI の差分

CI が `scripts/` を実行しているのは `extract_breaking_changes.py --check` **1 本のみ**（根拠: `.github/workflows/test.yml:56-57`）。
`.github/workflows/` の他 3 本は配布関所ではない: `publish.yml`（タグ push で build + PyPI 公開・検証なし / `publish.yml:29-30`）、`docs.yml`（MkDocs デプロイ / `docs.yml:35-36`）、`macos-smoke.yml`（`workflow_dispatch` 手動トリガーのみ / `macos-smoke.yml:8-9`）。

| # | 関所 | 規約の根拠 | 実行者 | 判定 |
|---|---|---|---|---|
| 1 | `src/c3/_template/` 直接編集禁止 | `/CLAUDE.md:10-19` | ローカル PreToolUse hook（`.dev/hooks/_template_guard.py:56-65` で exit 2）。`.dev/` は配布元専用で CI 非実行（`/CLAUDE.md:50-56`） | **[手動のみ]**（ローカル hook のみ） |
| 2 | 3 ファイル同期: `_excludes.py` ↔ `hatch_build.py`（EXCLUDE） | `/CLAUDE.md:21-35` | `tests/test_excludes.py:146-153` → `.github/workflows/test.yml:53-54` | **CI 自動** |
| 3 | 3 ファイル同期: `_excludes.py` ↔ `hatch_build.py`（KEEP） | `/CLAUDE.md:21-35` | `tests/test_excludes.py:156-159` → `.github/workflows/test.yml:53-54` | **CI 自動** |
| 4 | リリース前 wheel 実体検証（`python -m build --wheel` + zip 中身確認） | `/CLAUDE.md:37-48` | 人間の PowerShell 実行 | **[手動のみ]** |
| 5 | `CHANGELOG.md` に `## [X.Y.Z] - YYYY-MM-DD` セクションを記載 | `/CLAUDE.md:80` | 人間。CHANGELOG に新 version セクションがあるかを検査する CI ジョブ・テストは存在しない | **[手動のみ]** |
| 6 | 破壊的変更があれば `### 破壊的変更` サブセクションを設ける | `/CLAUDE.md:81` | 人間（セクションの有無自体は誰も検査しない。書かれていれば #7 が拾う） | **[手動のみ]** |
| 7 | `python scripts/extract_breaking_changes.py --check` | `/CLAUDE.md:82-85` | `.github/workflows/test.yml:56-57` | **CI 自動** |
| 8 | `exit 1` の version について対話追記（`extract_breaking_changes.py` 引数なし） | `/CLAUDE.md:86-90` | 人間（`input()` による対話が必須） | **[手動のみ]** |
| 9 | wheel 再生成して `breaking-changes.txt` 収録 / `c3_version.txt` 非収録を確認 | `/CLAUDE.md:91-96` | 人間 | **[手動のみ]** |
| 10 | `.claude/state/c3_version.txt` を配布元で更新しない | `/CLAUDE.md:97` | 人間（`state/*` 除外により wheel 混入は構造的に防止されるが、「更新しない」こと自体の検査は無い） | **[手動のみ]** |
| 11 | `python scripts/check_deletions.py --check` | `/CLAUDE.md:101-118` | 人間。CI workflow からの参照は皆無（grep 実測）。`tests/test_check_deletions.py` は純粋関数のみ検査し実リポジトリを見ない | **[手動のみ]** |

**関所 11 件中 CI 自動 2 件 / 手動のみ 9 件。**

さらに、3 ファイル同期グループのうち **`.gitignore` レッグだけは機械照合が一切無い**（関所 #2/#3 は `_excludes.py` ↔ `hatch_build.py` の 2 者しか比較しない）。`.dev/hooks/_sync_check.py` は「他 2 ファイルを確認せよ」と stderr に出すだけで、内容の一致は見ていない。
根拠: `tests/test_excludes.py:146-159`（比較対象は 2 ファイルのみ） / `.dev/hooks/_sync_check.py:62-71`。

過去の同期漏れ defect（v1.1.0 `state/tier_selection.json` / v2.14.1 `worktrees/` の wheel 混入）はいずれもこのグループが原因。根拠: `/CLAUDE.md:33` / `.claude/docs/config-policy.md:303-310`。

補足: 関所の定義そのものが配布元専用の `/CLAUDE.md` にしか存在せず、`.gitignore:86` で配布・commit 対象から除外されている。したがってリリース手順は利用先・外部貢献者からは参照できない。

---

## 8. adapter 生成物（`c3 init --platform codex|cursor|opencode`）

| 項目 | 内容 |
|---|---|
| 入力 | `--platform` 値。`claude` / `codex` / `cursor` / `opencode` / `all`（`all` は 4 つ全部） |
| 出力 | `claude` 以外の platform について `scaffold_adapters(target_root, platforms)` を実行 |
| 契約 | adapter は**利用先の `.claude/` を読んで生成する**（wheel の template ではない）。`.claude/` が無ければ `FileNotFoundError` |
| 根拠 | `src/c3/platforms.py:5-17` / `src/c3/adapters.py:128-147`（`:137-139` で `.claude/` 必須） / `src/c3/cli_init.py:97-103` / `src/c3/cli_update.py:533-545` |

| 項目 | 内容 |
|---|---|
| 入力 | Codex platform |
| 出力 | (1) `AGENTS.md` の managed block（`<!-- BEGIN C3 CODEX ADAPTER -->` … `END`） (2) `.codex/config.toml` の managed block（`# BEGIN C3 CODEX ADAPTER`） (3) `.agents/skills/<rel>`（`SKILL.md` は変換、他はコピー） (4) `.codex/agents/<name>.toml` |
| 契約 | managed block 外のユーザー記述は保持される。`.codex/config.toml` が managed block 外に `[mcp_servers.c3]` を持つ場合は `ValueError` で中断 |
| 根拠 | `src/c3/adapters.py:177-189` / `:274-291`（衝突検出は `:277-284`） / `:203-219` / `:222-244` / `:294-319`（managed block 置換） |

| 項目 | 内容 |
|---|---|
| 入力 | Cursor platform |
| 出力 | (1) `.cursor/rules/c3-core.mdc`（全文置換） (2) `.cursor/mcp.json` の `mcpServers.c3` キー |
| 契約 | `.cursor/mcp.json` は既存 JSON を読み込んで `mcpServers.c3` のみ差し替える（他サーバー定義は保持）。不正 JSON は `ValueError` |
| 根拠 | `src/c3/adapters.py:192-200` / `:247-271`（`:250-260` で既存読込・検証） |

| 項目 | 内容 |
|---|---|
| 入力 | OpenCode platform |
| 出力 | (1) `AGENTS.md` の managed block（`<!-- BEGIN C3 OPENCODE ADAPTER -->`、`.claude/CLAUDE.md` 本文と `.claude/rules/*.md` を埋め込む） (2) `.opencode/agents/c3-<name>.md` (3) `.opencode/agents/c3-skill-<name>.md` |
| 契約 | OpenCode 向けには **MCP 設定を生成しない**（3 生成物のみ） |
| 根拠 | `src/c3/adapters.py:160-174` / `:423-440` / `:522-550` / `:582-614` / `.claude/CLAUDE.md` Platform Compatibility 節 |

| 項目 | 内容 |
|---|---|
| 入力 | `ADAPTER_EXCLUDE_PATTERNS = ("skills/autonomous-mode/*",)` |
| 出力 | `_adapter_skip()` = `should_skip()` OR adapter 固有除外 |
| 契約 | autonomous-mode skill は wheel には収録されるが adapter 生成物には**写像されない**（Claude Code 専用機能） |
| 根拠 | `src/c3/adapters.py:23-51` / `.claude/CLAUDE.md` Platform Compatibility 節（自律モードは Claude Code 専用） |

| 項目 | 内容 |
|---|---|
| 入力 | `.claude/rules/` / `.claude/agents/` / `.claude/skills/` 配下の symlink |
| 出力 | 解決先が元ディレクトリ外なら黙って skip |
| 契約 | adapter 生成が `.claude/` 外のファイルを読み出さない |
| 根拠 | `src/c3/adapters.py:459-466`（rules） / `:536-542`（agents） / `:601-606`（skills） |

| 項目 | 内容 |
|---|---|
| 入力 | 埋め込む Markdown 本文（`CLAUDE.md` / rules） |
| 出力 | managed block マーカー行と `@` 始まりの行を除去した本文 |
| 契約 | 埋め込み内容が managed block の境界を破壊できない |
| 根拠 | `src/c3/adapters.py:99-119` / `:70-75`（全マーカー一覧） |

配布元リポジトリでは adapter 生成物（`/AGENTS.md` `/.codex/` `/.cursor/` `/.agents/`）を `.gitignore` で除外している。根拠: `.gitignore:76-82` / `.claude/docs/config-policy.md:259-263`。

**`[未文書化] #7`**: `c3 update --platform <adapter>` は adapter 生成に失敗しても（`FileNotFoundError` / `ValueError`）、その直前に `claude` block の checkpoint 更新まで完了している。つまり adapter 失敗時も `.claude/state/c3_version.txt` は新バージョンに進む。コード内コメントには「Q-02 確定」とだけあり、外部ドキュメントには記載がない。
根拠: `src/c3/cli_update.py:523-531`（`:524` "Q-02 確定: adapter 失敗時も claude block 成功なら checkpoint 更新"） / `:541-543`（adapter 失敗は return 1）。

---

## 未確認の項目

| # | 項目 | 未確認である理由 |
|---|---|---|
| 1 | sdist tarball の実際のファイル一覧 | 本作業では wheel / sdist のビルドを禁止されているため、hatchling の VCS-ignore 既定挙動（gitignored ファイルが sdist から落ちるか）を実測できていない。§1-2 の `.claude/state/setup_done.flag` は「tracked である」ことのみ確定 |
| 2 | wheel tarball の実際のファイル一覧 | 同上。§1・§3 の収録内容は `pyproject.toml` / `hatch_build.py` / `_excludes.py` の静的読解と `should_skip` の実行結果からの導出であり、生成物の実測ではない |
| 3 | `python -m build --wheel` 実行時に `src/c3/_template/` が実際に何件になるか | 同上（現在ディスク上にある `src/c3/_template/` は過去ビルドの残骸で、内容の鮮度を検証していない） |
| 4 | `hooks/` 24 ファイル・`skills/` 33 ファイル・`agents/` 14 ファイルの個別内訳 | 本層（配布境界）の契約は「`should_skip` を通過するか」であり個別ファイルの役割は別層（エージェント／フック層）の範囲。件数のみ実測値を記載した |
| 5 | `c3 update` の削除処理が Windows で読み取り専用属性ファイルに対してどう振る舞うか | `unlink()` の `OSError` を `errors` に積む実装は確認済み（`cli_update.py:857-863`）だが、実機での挙動は未検証 |
| 6 | v0.2.0 〜 v1.x 系の実公開 wheel に何が入っていたか | `.claude/deletions.txt:51-87` の遡及記載コメントに依拠しており、当時の wheel 実体は確認していない |
