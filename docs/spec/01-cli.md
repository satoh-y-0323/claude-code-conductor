# C3 仕様目次 — CLI 層

対象: `src/c3/cli.py` および `src/c3/cli_*.py`（pip エントリポイント `c3 = c3.cli:main` / `pyproject.toml:41`）。
用途: 現行 C3 の「何を入力すると何が出るか」の全列挙。根拠のない記述は書かない。読み取れないものは「未確認」と明記する。

## サマリ

| 項目 | 値 |
|---|---|
| 実行確認日 / バージョン | 2026-08-05 / `c3 --version` = `c3 2.61.0`（`src/c3/__init__.py:3` と一致・pip 通常インストール） |
| `c3 --help` 列挙サブコマンド数 | **12**（init / update / list-agents / list-skills / list-commands / doctor / plan / tier / metrics / ask / recall / run）— 全件を本目次に収録 |
| サブサブコマンド数 | 6（plan validate / plan waves / tier stats / recall search / recall rebuild / recall stats） |
| **[実体なし] 件数** | **5** |
| **[未文書化] 件数** | **16** |
| 未確認の項目 | 8（末尾に一覧） |

突き合わせ対象ドキュメント: `docs/cli-reference.md` / `README.md`（`README.md:157-174` の CLI 表）/ `.claude/docs/config-policy.md`。

---

## c3（グローバル）

| 項目 | 内容 |
|---|---|
| 入力 | `--version` / `-h`／サブコマンド必須（`sub.required = True`）／`c3 recall <query>` は `c3 recall search <query>` へ argv 書き換え |
| 出力 | stdout: version 文字列 `c3 {__version__}` またはヘルプ。exit: サブコマンド未指定・不正名は argparse により **2**（実測 `c3` / `c3 badcmd` ともに 2） |
| 副作用 | プロセス起動時に stdout/stderr を UTF-8 へ reconfigure（失敗は握り潰す）。`run` のみ argparse を迂回し `cli_run.handle` を直接呼ぶ |
| 根拠 | `src/c3/cli.py:34-58`（parser 構築）/ `src/c3/cli.py:39-45`（--version・required）/ `src/c3/cli.py:70-73`（run 迂回）/ `src/c3/cli.py:84-105`（recall 省略形）/ `src/c3/cli.py:108-120`（UTF-8 reconfigure）/ `pyproject.toml:41` |

## init

| 項目 | 内容 |
|---|---|
| 入力 | `--force` / `--target PATH`（既定 cwd）/ `--platform {claude,codex,cursor,opencode,all}`（既定 claude）/ `--git` \| `--no-git`（排他）。読むファイル: バンドル済みテンプレート `templates_dir()`（editable は `<root>/.claude/`、wheel は `c3/_template/.claude/`）。TTY 時は stdin から `[Y/n]` を読む |
| 出力 | stdout: `initialized {dest} ({copied} files copied)` / `using existing {dest}` / adapter アクション一覧 / git 誘導メッセージ。stderr: 既存 `.claude/` の上書き拒否メッセージ、`adapter init failed: {exc}`。exit: 0 / 拒否時 1 / adapter 例外時 1 |
| 副作用 | `.claude/` へテンプレートを再帰コピー（`should_skip` で個人ファイル除外・空になったディレクトリは削除）／`--force` 時は既存 `.claude/` を `shutil.rmtree`／adapter 指定時は `AGENTS.md` `.codex/` `.cursor/` `.opencode/` を生成／非 git ディレクトリで `git init` サブプロセス起動（成否は exit code に影響しない） |
| 根拠 | `src/c3/cli_init.py:24-69`（オプション定義）/ `:72-110`（handle・拒否 exit 1 は `:78-84`）/ `:86-88`（rmtree）/ `:91-93`（copytree）/ `:97-103`（adapters）/ `:113-181`（git 同意モデル）/ `:184-208`（should_skip コピー）/ `src/c3/paths.py:31-79`（templates_dir）/ `src/c3/gitutil.py:16-71`（subprocess）/ `src/c3/adapters.py:128-147`（scaffold_adapters） |

## update

| 項目 | 内容 |
|---|---|
| 入力 | `--dry-run` / `--target PATH` / `--platform {claude,codex,cursor,opencode,all}` / `--yes,-y`。読むファイル: テンプレート全体、テンプレート内 `breaking-changes.txt`、`deletions.txt`、利用先 `.claude/state/c3_version.txt`。stdin: MAJOR bump 承認 `[y/N]` と削除承認 `[y/N]`（`--yes` 時はスキップ） |
| 出力 | stdout: breaking changes 一覧 / `N file(s) would change:` + `add\|update: rel` / 削除レポート / `up to date` / `N file(s) updated`。stderr: `.claude/` 不在エラー、downgrade 警告、breaking-changes.txt パース警告、checkpoint 保存失敗警告。exit: 0（削除エラーがあっても 0）/ `.claude/` 不在 1 / adapter 例外 1 |
| 副作用 | テンプレートと差分のあるファイルを `shutil.copy2` で上書き（init-only は既存なら不変更・削除は行わない）／`deletions.txt` 記載ファイルを `unlink`（15 段のセーフガード通過分のみ）／`.claude/state/c3_version.txt` を atomic write（dry-run・downgrade・削除キャンセル時は書かない）／adapter 生成物の再生成 |
| 根拠 | `src/c3/cli_update.py:411-444`（オプション定義）/ `:447-457`（.claude/ 不在 exit 1）/ `:465-470`（breaking changes 表示）/ `:472-484`（MAJOR 承認・キャンセルは return 0）/ `:487-504`（差分適用）/ `:507-521`（deletions 適用）/ `:529-530`（checkpoint 書き込み）/ `:533-545`（adapter）/ `:547-551`（終了メッセージ・常に return 0）/ `:838-853`（削除プロンプト）/ `:992-1015`（_walk_diff・init-only 保護）/ `:357-408`（checkpoint I/O）/ `:127-205`（breaking-changes.txt ロード）/ `:561-644`（deletions.txt ロード）/ `:647-729`（削除パス検証） |

## list-agents

| 項目 | 内容 |
|---|---|
| 入力 | `--target PATH`（既定: cwd から上位へ `.claude/` を探索）。読むファイル: `<root>/.claude/agents/*.md`（frontmatter `description:` → 無ければ H1） |
| 出力 | stdout: `  {stem}  {summary}` の整列一覧 / `(no agents found)`。stderr: `.claude/` 不在・`agents/` 不在メッセージ。exit: 0 / 不在時 1 |
| 副作用 | なし（読み取りのみ） |
| 根拠 | `src/c3/cli_list.py:17-29`（3 コマンド一括登録）/ `:32-56`（handle・exit 1 は `:36-45`）/ `:47`（`*.md` glob）/ `:59-72`（summary 抽出） |

## list-skills

| 項目 | 内容 |
|---|---|
| 入力 | `--target PATH`。読むファイル: `<root>/.claude/skills/*.md`（**トップレベルの .md のみ**・再帰しない） |
| 出力 | stdout: 実質常に `(no skills found)`、exit **0**（実測: 本リポジトリで skills が 20 ディレクトリ以上あるのに `(no skills found)`） |
| 副作用 | なし |
| 根拠 | `src/c3/cli_list.py:17-29`（`for kind in ("agents","skills","commands")` の共通実装）/ `:47`（`target_dir.glob("*.md")`）/ `:49-50`（空時 exit 0）。実体は `.claude/skills/<name>/SKILL.md` 構造（例: `.claude/skills/dev-workflow/SKILL.md`） |
| 判定 | **[実体なし]** — `docs/cli-reference.md:38-45` / `README.md:164` が「設置済みスキルを一覧表示」と記載するが、実装は該当ファイルを 1 件も拾えず機能として到達不能 |

## list-commands

| 項目 | 内容 |
|---|---|
| 入力 | `--target PATH`。読むファイル: `<root>/.claude/commands/*.md` |
| 出力 | stdout: 一覧 / `(no commands found)`。stderr: `no .claude/commands/ directory at {root}`。exit: 0 / 不在時 1（実測: 本リポジトリは `commands/` が無く exit 1） |
| 副作用 | なし |
| 根拠 | `src/c3/cli_list.py:17-29`（`commands` を含む登録）/ `:42-45`（ディレクトリ不在 exit 1） |
| 判定 | **[未文書化]** — `docs/cli-reference.md` / `README.md` のどちらにも `list-commands` の記載なし（grep 実施） |

## doctor

| 項目 | 内容 |
|---|---|
| 入力 | `--quiet` / `--platform {claude,codex,cursor,opencode,all}`（既定 claude）。読むファイル: `.claude/settings.json`（hooks/statusLine の command 先頭トークン抽出）、`.codex/config.toml`、`.cursor/mcp.json`。環境変数: `NO_COLOR`（色出力抑止・`_terminal.py:35`） |
| 出力 | stdout: `  [OK]/[WARN]/[ERR] {label}: {detail}` 行（`--quiet` は OK を抑止）。exit: ERR が 1 件でもあれば 1、それ以外 0（WARN のみは 0・実測） |
| 副作用 | **サブプロセス起動**: 各ランチャートークンに対する `<resolved> --version`（timeout 10 秒）、MCP command に対する `<command> -c "import c3"`（timeout 10 秒）。`shutil.which` による PATH 探索 |
| 根拠 | `src/c3/cli_doctor.py:24-40`（オプション）/ `:43-68`（findings 収集・exit 判定は `:60-67`）/ `:82-101`（settings.json・JSON 構文エラーは ERR）/ `:104-112`（claude バイナリ）/ `:115-173`（launcher 検査）/ `:219-274`（c3/python/その他の 3 分岐）/ `:277-290`（--version プローブ）/ `:293-314`（codex）/ `:317-333`（opencode）/ `:336-353`（cursor）/ `:389-422`（MCP 起動検査） |

## plan validate

| 項目 | 内容 |
|---|---|
| 入力 | 位置引数 `plan_report`（パス）。`.claude/` は plan-report の親→cwd の順に探索。読むファイル: plan-report の YAML frontmatter と `<root>/.claude/agents/*.md`（存在確認） |
| 出力 | stdout: なし。stderr: エラー行の列挙。exit: 0（妥当）/ **2**（ファイル不在・`.claude/` 不明・検証エラー。実測 `plan validate /nonexistent.md` = 2） |
| 副作用 | なし（読み取りのみ） |
| 根拠 | `src/c3/cli_plan.py:30-35`（登録）/ `:45-46`（root 解決）/ `:49-62`（handle）/ `:19`（`_EXIT_MANIFEST_ERROR = 2`）/ `src/c3/plan_validator.py`（検証本体） |

## plan waves

| 項目 | 内容 |
|---|---|
| 入力 | 位置引数 `plan_report`。validate と同じ `.claude/` 解決 |
| 出力 | stdout: wave 分解結果の JSON（indent=2・`ensure_ascii=False`・末尾改行）。stderr: 検証エラー / `split_waves` の ValueError。exit: 0 / **2** |
| 副作用 | なし |
| 根拠 | `src/c3/cli_plan.py:37-42`（登録）/ `:65-86`（validate 先行 → split_waves → JSON 出力） |
| 判定 | **[未文書化]**（部分）— `docs/cli-reference.md:106` は `waves` の exit code を「0」とだけ記載し、exit 2 の経路（`cli_plan.py:78,83`）に言及がない |

## tier stats

| 項目 | 内容 |
|---|---|
| 入力 | `--json` / `--recent N`（既定 10）/ `--role ROLE`（`AGENT_ROLES` = interviewer/architect/planner/developer/tester のいずれか）。読むファイル: `.claude/state/c3.db`（`locate_c3_db`）。環境変数: `C3_DB_PATH` / `C3_PO_DB_PATH`（DB パス上書き）、`C3_TIER_COST_LAMBDA` / `C3_TIER_EPSILON` / `C3_ESCALATION_THRESHOLD`（表示する routing パラメータ） |
| 出力 | stdout: role 別 bandit 表・直近 outcome 履歴・記録チャネル説明・agent 別コスト集計・tier 別 USD/MTok レート・routing パラメータ（`--json` 時は同内容の JSON）。stderr: `--role` 不正値、DB 不在、DB アクセスエラー。exit: 0 / 1（上記 3 種のエラー） |
| 副作用 | c3.db への読み取り接続のみ（`connect` は commit しない。書き込みヘルパーは呼ばない） |
| 根拠 | `src/c3/cli_tier.py:41-70`（オプション）/ `:73-107`（handle・exit 1 は `:75-81`,`:83-90`,`:92-100`）/ `:110-170`（snapshot 構築・routing パラメータは `:165-169`）/ `:173-277`（人間向け描画）/ `src/c3/db.py:132-176`（locate_c3_db と環境変数）/ `src/c3/_db_params.py:47`（AGENT_ROLES）/ `:150-201`（3 環境変数の解決） |

## metrics

| 項目 | 内容 |
|---|---|
| 入力 | `--json` / `--since YYYY-MM-DD` / `--months N`（既定 12・1 以上 120 以下）/ `--examples N`（既定 5・1 以上）。読むファイル: `.claude/state/c3.db`。環境変数: `C3_DB_PATH` / `C3_PO_DB_PATH`（`locate_c3_db` 経由） |
| 出力 | stdout: 3 セクション（[1] 事前検出実績 / [2] 差し戻しの傾向と帰属 / [3] 手戻りコスト概況）または JSON。stderr: `--since` 書式不正、`--months`/`--examples` 範囲外、DB 不在、`DB アクセスエラー: {型名}`。exit: 0 / 1（実測 `--months 0` = 1） |
| 副作用 | c3.db 読み取りのみ（read-only 集計・DB 由来文字列は端末制御文字をサニタイズ） |
| 根拠 | `src/c3/cli_metrics.py:50-78`（オプション）/ `:81-144`（handle・検証は `:84-113`、DB 不在は `:115-122`）/ `:47`（`MAX_MONTHS = 120`）/ `:165-224`（DB ヘルパー 6 本の呼び出し）/ `:227-266`（headline・role 分類）/ `:269-351`（描画）/ `src/c3/db.py:459,517,604,684,735,824`（読み取り関数群） |

## ask

| 項目 | 内容 |
|---|---|
| 入力 | `--file PATH` \| `--json TEXT`（排他・必須）、`--response TEXT`（質問間は `;`、複数選択は `,`、ラベルまたは 1 始まり番号）、`--pretty`。stdin: TTY の場合は対話選択、非 TTY で `--response` 無しはエラー |
| 出力 | stdout: `{"answers": [...]}` の JSON（`--pretty` で indent=2）。stderr: `c3 ask: --response is required in non-interactive mode` / `c3 ask: {exc}` / `c3 ask: input aborted`。exit: 0 / 1（入力エラー・OSError・ValueError・EOF）/ **130**（KeyboardInterrupt） |
| 副作用 | 対話時に stdin/stdout を消費。ファイル書き込みなし |
| 根拠 | `src/c3/cli_ask.py:13-46`（オプション）/ `:49-73`（handle・exit 130 は `:67-69`）/ `src/c3/question.py:32-70`（load/answer）/ `:219-222`（`;` 分割）/ `:225-245`（`,` 分割・番号/ラベル解決） |

## recall search

| 項目 | 内容 |
|---|---|
| 入力 | 位置引数 `query`、`--top N`（既定 5）、`--source {all,sessions,agent-memory,reports,patterns}`（既定 all）、`--min-score F`（既定 0.3）、`--json`、`--target PATH`。読むファイル: `.claude/state/recall.hnsw` と `.claude/state/recall_meta.json`。`c3 recall <query>` の省略形は `cli.py` が書き換え |
| 出力 | stdout: 人間向けヒット一覧（`NO_COLOR` 未設定かつ TTY なら ANSI 装飾）または `{"query":..,"hits":[..]}` JSON。stderr: `.claude/` 不在 / index 不在 / index ロード失敗 / fastembed 未導入 / `[recall] WARN: index is older ...`。exit: 0 / 1（上記エラー） |
| 副作用 | fastembed モデルのロード（未取得ならダウンロード＝**ネットワークアクセス**・キャッシュ先は fastembed 側の `FASTEMBED_CACHE_PATH`）。index ファイルへの書き込みはしない |
| 根拠 | `src/c3/cli_recall.py:76-105`（オプション）/ `:151-202`（handle・index 不在 exit 1 は `:161-166`）/ `:463-498`（embedder 生成と失敗時メッセージ）/ `:514-543`（フィルタ）/ `src/c3/recall_index.py:610-616`（index パス）/ `:619-632`（stale 警告）/ `src/c3/embedding.py:64-80`（fastembed 遅延 import）/ site-packages `fastembed/common/utils.py:54`（`FASTEMBED_CACHE_PATH` を読むのは fastembed 側） |

## recall rebuild

| 項目 | 内容 |
|---|---|
| 入力 | `--force`、`--source {all,sessions,agent-memory,reports,patterns}`、`--target PATH`。読むファイル: `.claude/memory/sessions/*.tmp`、`.claude/agent-memory/**/*.md`、`.claude/reports/archive`、`.claude/memory/patterns.json`（`collect_sources`） |
| 出力 | stdout: `[recall] embedded N / reused M chunks` または `[recall] embedding N chunks...`、`[recall] wrote N chunks to .claude/state/recall.hnsw`。stderr: `.claude/` 不在、ソース 0 件、既存 index 読み込み失敗の fallback 通知。exit: 0 / 1 |
| 副作用 | **ファイル書き込み**: `.claude/state/recall.hnsw` / `recall_meta.json` を tmp → `os.replace` で atomic 置換し直前版を `.bak` として残す。`--force` 時は既存 index/meta を先に `unlink`。fastembed によるモデルダウンロード（ネットワーク） |
| 根拠 | `src/c3/cli_recall.py:107-124`（オプション）/ `:257-404`（handle・ソース 0 件 exit 1 は `:273-280`、増分再利用は `:292-322`、`--force` の unlink は `:384-390`、保存は `:392-403`）/ `:205-254`（再利用判定）/ `src/c3/recall_index.py:417-442`（収集対象）/ `:260-290`（atomic save と .bak） |

## recall stats

| 項目 | 内容 |
|---|---|
| 入力 | `--json`、`--target PATH`。読むファイル: `.claude/state/recall_meta.json`（fastembed 不要）と `recall.hnsw` の `stat().st_size` |
| 出力 | stdout: チャンク総数・ソース別内訳・index パスと MB・最終 rebuild 日時・モデル名/次元、または同内容の JSON。stderr: `.claude/` 不在 / meta 未作成 / meta 読み取り失敗。exit: 0 / 1 |
| 副作用 | なし（読み取りのみ） |
| 根拠 | `src/c3/cli_recall.py:126-132`（オプション）/ `:407-452`（handle・meta 不在 exit 1 は `:417-419`）/ `:574-598`（人間向け描画） |

## run

| 項目 | 内容 |
|---|---|
| 入力 | `c3 run <script.py> [args...]` / `c3 run -m <module> [args...]` / `c3 run -c <code> [args...]`。`run` 以降の全トークン（`--` 含む）を逐語転送。argparse を通らず `args._raw_argv` を直接読む |
| 出力 | 起動したスクリプトの stdout/stderr をそのまま継承。stderr: `c3 run: expected a script path, '-m <module>', or '-c <code>'` / `-m requires a module name` / `-c requires a code string` / 未捕捉例外の traceback。exit: スクリプトの `SystemExit.code`（None→0・非 int は stderr 出力して 1）／自身の引数エラーは 1／未捕捉例外は 1（exit 2 は使わない） |
| 副作用 | **同一プロセス内で任意コードを実行**（`runpy.run_path` / `runpy.run_module(alter_sys=True)` / `exec`）。`sys.argv` と `sys.path` を退避・復元するが `sys.modules` は復元しない。スクリプト実行前に script ディレクトリ（`-m`/`-c` は `""`）を `sys.path` 先頭へ挿入 |
| 根拠 | `src/c3/cli.py:70-73`（argparse 迂回）/ `src/c3/cli_run.py:50-58`（help 表示専用の register・`add_help=False`）/ `:61-98`（handle・引数エラー exit 1）/ `:101-135`（3 形態）/ `:138-159`（実行と復元）/ `:162-171`（exit code マップ） |
| 判定 | **[実体なし]** — `c3 run --help` / `-h` は到達不能。`--help` がスクリプトパスとして扱われ `FileNotFoundError` の traceback + exit 1（実測 exit 1） |

---

## [実体なし] 一覧（5 件）

ドキュメントに書かれているのに実装が無い、または到達不能なもの。

| # | 内容 | ドキュメント側根拠 | 実装側根拠 |
|---|---|---|---|
| 1 | `c3 list-skills` が「設置済みスキルを一覧表示」と書かれているが、実装は `.claude/skills/*.md` を glob するのみで `<name>/SKILL.md` 構造を拾えず常に `(no skills found)`（機能として到達不能） | `docs/cli-reference.md:38-45` / `README.md:164` | `src/c3/cli_list.py:47` / `:49-50` |
| 2 | `c3 tier stats` の「Tier 別累積」に累積コスト `total_cost_usd` / `cost_samples` 列があると記載されているが、snapshot にも描画にも該当キー・列が存在しない | `docs/cli-reference.md:146` | `src/c3/cli_tier.py:133-140`（rows のキー）/ `:189-199`（出力列） |
| 3 | 学習データを記録するスクリプトとして `record_tier_outcome.py` が挙げられているが、当該ファイルは存在しない（実在は `record_agent_outcome.py`。CLI 自身の出力文言も後者） | `docs/cli-reference.md:152` | `src/c3/cli_tier.py:223` / 実ディレクトリ `.claude/skills/dev-workflow/scripts/`（record_agent_outcome.py・record_review_decision.py・review_hint_inject.py・detect_execution_verification.py の 4 本のみ） |
| 4 | `c3 tier stats` が `tier_bandit` / `tier_recent_outcomes` テーブルを可視化すると記載されているが、実装は `agent_outcomes` からの導出集計であり、旧フラット `tier_bandit` は v2.41.0 で廃止済み | `docs/cli-reference.md:135` | `src/c3/cli_tier.py:16-17`（廃止の記述）/ `src/c3/db.py:1002-1006`（agent_outcomes 導出への移行） |
| 5 | `c3 run` のヘルプが到達不能（`c3 run --help` / `-h` が traceback + exit 1）。`c3 --help` には `run` が列挙されるため、ヘルプがあるかのように見える | `docs/cli-reference.md:78-92` / `README.md:167`（用法記載あり） | `src/c3/cli.py:70-73`（argparse 迂回）/ `src/c3/cli_run.py:54-56`（`add_help=False`）/ `:98`（`--help` を script パスとして `_run_path` へ） |

## [未文書化] 一覧（16 件）

実装はあるが `docs/cli-reference.md` / `README.md` / `.claude/` 配下のいずれにも記載が見当たらないもの（grep で確認）。

| # | 内容 | 根拠 |
|---|---|---|
| 1 | サブコマンド `c3 list-commands` 自体（`c3 --help` には出るがドキュメント記載ゼロ） | `src/c3/cli_list.py:17-29` |
| 2 | `c3 init --target` | `src/c3/cli_init.py:43-48` |
| 3 | `c3 init --git` / `--no-git`（および非 TTY 時の誘導・TTY 時の `[Y/n]` 同意プロンプト） | `src/c3/cli_init.py:58-68` / `:113-181` |
| 4 | `c3 init` は `--platform` に claude 以外を含むと既存 `.claude/` があっても拒否せず続行する（拒否条件が `platforms == ("claude",)` 限定） | `src/c3/cli_init.py:78` |
| 5 | `c3 update --target` | `src/c3/cli_update.py:427-432` |
| 6 | `c3 update --yes` / `-y`（削除・MAJOR 承認プロンプトのスキップ） | `src/c3/cli_update.py:439-443` / `:475-476` / `:847-854` |
| 7 | `c3 update` は deletions の削除失敗（`errors`）があっても exit 0 を返す | `src/c3/cli_update.py:857-862` / `:551` |
| 8 | `c3 doctor --quiet` | `src/c3/cli_doctor.py:33-36` |
| 9 | `c3 tier stats --role`（および不正値時の exit 1） | `src/c3/cli_tier.py:65-69` / `:75-81` |
| 10 | `c3 recall search` / `rebuild` / `stats` の `--target`（`.claude/hooks/recall_autorebuild.py:10` が実運用で使用しているが利用者向けドキュメントに無し） | `src/c3/cli_recall.py:54-60` |
| 11 | `c3 metrics --months` の上限 120（`MAX_MONTHS`）と 1 未満・`--examples` 1 未満の exit 1 | `src/c3/cli_metrics.py:47` / `:93-113` |
| 12 | `c3 plan waves` の exit 2 経路（ドキュメントは 0 のみ記載） | `src/c3/cli_plan.py:65-83` |
| 13 | `c3 ask` の exit 130（KeyboardInterrupt） | `src/c3/cli_ask.py:67-69` |
| 14 | 環境変数 `C3_DB_PATH` / 非推奨 `C3_PO_DB_PATH`（`tier stats` / `metrics` の DB 解決を上書き） | `src/c3/db.py:154-166` |
| 15 | 環境変数 `NO_COLOR`（doctor / update / recall の ANSI 出力抑止） | `src/c3/_terminal.py:32-35` |
| 16 | サブコマンド未指定・不正名の exit 2（argparse 既定） | `src/c3/cli.py:44-45`（実測 `c3` / `c3 badcmd` = 2） |

### 参考: 「利用者向けドキュメントには無いが `.claude/docs/` にはある」もの（上記件数には含めない）

| 内容 | 記載場所 |
|---|---|
| `c3 update` の `deletions.txt` 経由の削除・`breaking-changes.txt` 表示・MAJOR bump 承認プロンプト・`.claude/state/c3_version.txt` checkpoint。`docs/cli-reference.md:25-36` の `c3 update` 節には一切記載がない | `.claude/docs/config-policy.md:203-204` / `:364-390` |

### 参考: `.claude/docs/` の構想メモに出る未実装コマンド（提案文書のため [実体なし] に数えない）

`c3 status --recall`（`.claude/docs/C3_hnsw_機能追加詳細設計.md:507`）/ `c3 recall config`（同 `:499,520`）/ `c3 sync claude|codex|all`・`c3 test equivalence`（`.claude/docs/codex対応/03-c3-migration-plan.md:30-33`）/ `c3 grill export|import`（`.claude/docs/grill-me機能を実装する際の考慮点とC3との相性や超えるべき壁.md:87,118`）/ `c3 update --clean`（`.claude/docs/C3のconfig_policyとversion_upgradeの考慮点と超えるべき壁.md:178`）。いずれも `c3 --help` に存在せず、実装も無い。

---

## 未確認の項目（8 件）

| # | 未確認の内容 | 理由 |
|---|---|---|
| 1 | `c3 init --platform codex\|cursor\|opencode\|all` が生成する**ファイルの完全な一覧** | `src/c3/adapters.py`（35KB）の分岐を全読していない。`scaffold_adapters` から `_write_codex_adapter` / `_write_cursor_adapter` / `_write_opencode_adapter` を呼ぶこと（`adapters.py:128-147`）と、MCP command に `sys.executable -m c3.mcp_server` を書くこと（`adapters.py:266-267,373-375`）のみ確認済み |
| 2 | `c3 update` の実削除経路（`deletions.txt` に一致するファイルが実在する場合の挙動） | 破壊的操作のため未実行。コード読解のみ（`cli_update.py:790-864`） |
| 3 | `c3 recall rebuild` の実行時挙動（チャンク数・所要時間・モデル未取得時のダウンロード動作） | fastembed のモデル取得とインデックス上書きを伴うため未実行 |
| 4 | `c3 ask` の TTY 対話経路（`_select_interactively`）の実挙動 | 対話 UI のため未実行。非対話経路（`--response` / 非 TTY エラー）のみコードで確認 |
| 5 | `c3 doctor --platform all` の実出力 | サブプロセス起動（`--version` / `import c3`）を伴うため未実行。`--quiet`（claude 既定）の exit 0 のみ実測 |
| 6 | `c3 tier stats` / `c3 metrics` の実出力 | 指示により実 `c3.db` を読まないため未実行。列構成・分岐はコードで確認 |
| 7 | `c3 run` 実行時の `sys.modules` 非復元が実運用で問題を起こすか | 実装コメント（`cli_run.py:138-145`）の記述のみ。実挙動は未検証 |
| 8 | `c3 recall search` の `--source` 値のうち `agent-memory` 以外の複数形→単数形マッピングが `collect_sources` 側の実収集対象と完全一致するか | `cli_recall.py:501-511`（マッピング）と `recall_index.py:417-442`（収集）を個別に読んだのみで、突き合わせ実行はしていない |

## 収録漏れがないことの確認

`c3 --help` の positional arguments 列挙（実行日 2026-08-05・c3 2.61.0）: `init` / `update` / `list-agents` / `list-skills` / `list-commands` / `doctor` / `plan` / `tier` / `metrics` / `ask` / `recall` / `run` の **12 件**。本目次はこの 12 件すべてに `##` セクションを持ち、さらに `plan` / `tier` / `recall` のサブサブコマンド 6 件を個別セクションに分解している。
