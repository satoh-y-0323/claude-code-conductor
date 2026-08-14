"""tests/test_verify_wheel.py

`scripts/verify_wheel.py`（リリース前 wheel 実体検証）のテスト。

P1〜P8 は本体スライスで Green 済み（`scripts/verify_wheel.py` は実装済み）。
Q1〜Q6 は **E 周回 1 是正（plan-report-20260814-220825.md）の Red フェーズ**として
本ファイルへ追記した性質であり、sdist 側 2 検査・サニタイズ・PASS 行のモード分岐・
評価順序が未実装であるために失敗する（構文エラー・タイポではなく「新 API / 新挙動が
不在」であることが失敗理由）。Q1〜Q6 は E 周回 1 の impl で Green 済み。

R1〜R2 は **E 周回 2 是正（plan-report-20260815-003232.md）の Red フェーズ**として
追記した性質であり、

- R1: `_sanitize` の除去集合が正本（`.claude/hooks/_hook_utils.py` の
  `_TERMINAL_SANITIZE_RE`）より狭い（C0 + DEL のみで C1 / ゼロ幅 / 行区切り /
  双方向制御 / BOM が素通りする）
- R2: sdist member の読み取りが `(name, size)` の 2 要素かつ `isfile()` フィルタ固定で、
  link member と未知種別 member が全検査を素通りする（種別射程が未実装）

ために失敗する。失敗理由は「新挙動・新入力形が不在」であって構文エラーやタイポではない。

## 新 API を module-level の一括 import へ足さない（DC-AS-001・Red の構造要件）

`from verify_wheel import (...)`（`:101-127` 相当）へ Red の新 literal / 新関数を足すと、
実装不在の時点で **collection 時 ImportError** になり本ファイル全件が error になる
（ベースライン「意図した赤は凍結テスト 1 本のみ」が構造的に成立しなくなる）。
そのため Q1〜Q6 が参照する新 API は

- `import verify_wheel` 済みの名前空間経由の**属性アクセス**（`verify_wheel.XXX`）、または
- 文字列 literal（種別名の凍結そのものが目的の箇所）

でのみ書く。こうすると未実装は `AttributeError` として**そのテストだけ**の Red になる。
Green 後にこの規約を撤回して一括 import へ移してもよいが、次の Red で同じ罠に落ちるため
本ファイルではこの書き方を既定とする。

## 測る性質（P1〜P8: plan-report-20260814-195906.md / Q1〜Q6: plan-report-20260814-220825.md
## / R1〜R2: plan-report-20260815-003232.md）

| 性質 | 内容 | 主なテスト |
|---|---|---|
| P1 | EXCLUDE 違反検出（`should_skip` は明示引数・既定値 SSOT） | `TestExcludeViolation` |
| P2 | KEEP 欠落検出 | `TestKeepMissing` |
| P3 | FR-2 明示検査（breaking-changes.txt / state/c3_version.txt） | `TestFr2Violation` |
| P4 | 検証不能の区別（TEMPLATE_EMPTY / LAYOUT_ANOMALY）＋トップレベル検査 | `TestUnverifiable` / `TestUnexpectedToplevel` |
| P5 | exit code と識別子・種別の凍結 | `TestExitCodesAndIdentifiers` / `TestCli` |
| P5b | 既定ビルド経路と outdir 成果物選別の凍結 | `TestBuildInvocation` / `TestArtifactSelection` |
| P7 | 入力正規化（末尾 `/` は判定対象外） | `TestDirectoryEntriesAreIgnored` |
| P8 | 注入対照（sdist 対照の在/不在・独立性・候補件数） | `TestInjectedControl` |
| Q1 | sdist 側 EXCLUDE 混入検出（注入対照のみ許容・`should_skip` は正負両方向の注入） | `TestSdistExcludeViolation` |
| Q2 | 注入対照メンバーのサイズ 0 検証（非 0 は違反） | `TestControlSize` |
| Q3 | 違反 detail のサニタイズ（**正本と同一の全クラス**を**除去**・適用 3 サイト） | `TestDetailSanitization` |
| Q4 | 既定経路からの配線（sdist EXCLUDE 検査・サイズ検査が実際に呼ばれる） | `TestSdistChecksAreWired` |
| Q5 | PASS 行のモード分岐（`--wheel` は sdist 検査を主張しない） | `TestPassLineModeSplit` |
| Q6 | 評価順序（`CONTROL_MISSING` 最優先で即 return・違反同士は合算 1 回出力） | `TestEvaluationOrder` |
| R1a | sanitize 除去集合の代表文字が正負両方向で凍結される（除去側 / 残存側） | `TestSanitizeCoverage` |
| R1b | sanitize 除去集合が正本 `_TERMINAL_SANITIZE_RE` と **U+0000–U+FFFF 全域で一致** | `TestSanitizeMatchesCanonicalSource` |
| R1c | 除去後の detail は `str.splitlines()` で行分裂しない | `TestSanitizeCoverage` |
| R2 | sdist member 読み取りが 3 要素 `(name, size, kind)`・kind 値域と未知種別の fail-closed | `TestSdistMemberKinds` |
| R2c/e | 対照系 2 関数が 3 要素を受け、対照の成立・母数は **file member のみ** | `TestControlChecksTakeMemberKinds` |
| R2a/b/d | 既定経路での種別射程（dir 対象外・EXCLUDE は file+link・母数は file のみ・未知は fail-closed） | `TestMemberKindScopeViaDefaultRoute` |

P6（CI job の静的検査）は `tests/test_ci_workflows.py` に置く（同ファイルの既存 job 検査と
同じ道具立てを使うため）。

## 期待値の出どころ（SSOT）

EXCLUDE / KEEP の期待値は `c3._excludes` を import して参照する（パターンの複製をしない）。
テストが直書きするリテラルは「注入値」に限る:

- `_CONTROL_RELPATH = "state/setup_done.flag"` … 注入対照（`pyproject.toml` の
  sdist force-include で sdist にだけ入れ、wheel では実フィルタが落とすべきファイル）
- `_WHEEL_TEMPLATE_PREFIX = "c3/_template/.claude/"` … wheel 内の実パス（実測確定値）
- 違反種別 / 原因識別子の literal（凍結対象そのもの）

## `state/c3_version.txt` 不在検査の非対称性（ADR-2 改訂）

FR-2 のうち `breaking-changes.txt` の存在検査は配布元に実体があり実 wheel 層で有効だが、
`state/c3_version.txt` の**不在**検査は配布元に実体が無いため実 wheel 層では恒真である
（利用先で `c3 update` が生成するファイルであり、配布元のビルド入力には存在しない）。
それでも `/CLAUDE.md` §6 手順 5 の意図を退行から守るための検査であり、その実効は
本ファイルの合成 namelist テスト（`c3_version.txt` を含む namelist → `FR2_VIOLATION`）が持つ。
この非対称は script 内コメントではなく、この docstring に置くことで確定させる（ADR-2 改訂）。

## 凍結する API（tester による契約具体化・architecture 改訂 4 の写像）

architecture は「純粋検出器の分離」「`should_skip` を明示引数（既定値
`c3._excludes.should_skip`）で受ける」「対照検査は should_skip 非依存」「ビルド実行部は
注入可能」までを定めるが、関数名・戻り値形は「C/D 層の裁量」とされている。Red を書くには
形が要るため、本ファイルが以下を契約として固定する（実装細部＝内部分割はなお自由）:

- `find_violations(namelist, should_skip=c3._excludes.should_skip) -> list[tuple[str, str]]`
  … `(違反種別, 該当エントリ or パターン)` の並び。**wheel 側 5 種**（EXCLUDE / KEEP /
  FR-2 / 対照混入 / トップレベル逸脱）をここから観測する。sdist 側 2 種は下記の新関数から
  観測し、`VIOLATION_KINDS` は両者を合わせた **7 種**になる（Q1/Q2）。
  対照混入（`CONTROL_LEAKED`）の判定は `should_skip` 引数に依存してはならない
  （P8(b) が実測で固定する）
- `find_unverifiable(namelist) -> str | None` … `TEMPLATE_EMPTY` / `LAYOUT_ANOMALY` / None
- `count_exclude_candidates(names, should_skip=...) -> int` … sdist listing 中の
  `.claude/` 相対で should_skip True の件数。**名前のみを受ける形は不変**（R2(e)）。
  既定経路が渡すのは **kind == "file" の member 名だけ**（母数は file のみ・R2(d)）
- `find_sdist_control_reason(members, should_skip=...) -> str | None` …
  `CONTROL_MISSING` / None。**R2(e) で 3 要素 `(name, size, kind)` を受ける形へ改訂**。
  対照の存在判定・候補件数の母数はいずれも **kind == "file" の member のみ**を入力とする
  （対照名の member が link / dir なら「対照は不在」＝ `CONTROL_MISSING`・R2(c)）
- `find_sdist_violations(names, should_skip=c3._excludes.should_skip)
  -> list[tuple[str, str]]` （Q1・新規）… sdist member 名の一覧から、`.claude/` 相対で
  should_skip True かつ**注入対照以外**のものを `(SDIST_EXCLUDE_VIOLATION, rel)` で列挙する。
  対照のみなら空リスト。既存の名前ベース検出器と同じ入力形（member 名の列）を取り、
  ディレクトリエントリ・`.claude/` 境界外は対象外。**名前のみを受ける形は不変**（R2(e)）。
  既定経路が渡すのは **kind が "file" または "link" の member 名**（R2(b)）
- `find_control_size_violations(members) -> list[tuple[str, str]]` （Q2）…
  注入対照のサイズが 0 でなければ `(CONTROL_NOT_EMPTY, detail)` を返す。detail には対照の
  相対パスと**実測サイズ**を含める。対照が不在なら空リスト（不在は
  `find_sdist_control_reason` の `CONTROL_MISSING` の担当で、二重報告しない）。
  `should_skip` 引数は**持たない**（`CONTROL_LEAKED` と同じく SSOT 劣化から独立させるため）。
  **R2(e) で入力形を 3 要素 `(name, size, kind)` へ改訂**し、対照とみなすのは
  kind == "file" の member のみ（link / dir の同名 member は対照ではない・R2(c)）
- `read_sdist_members(path) -> tuple[list[tuple[str, int, str]] | None, str | None]` …
  **R2 で 3 要素 `(name, size, kind)` へ改訂**。kind の値域は `"file"` / `"link"` / `"dir"`
  の 3 値ちょうどで、`tarfile` の述語から `isfile()` → `"file"` /
  `issym()` または `islnk()` → `"link"` / `isdir()` → `"dir"` と導出する。
  **4 述語すべて False の member（FIFO・キャラクタ/ブロックデバイス等）は検証不能**
  として `(None, LAYOUT_ANOMALY)` を返す（fail-closed。種別フィルタで検査を素通りさせない）。
  tar として読めない場合は従来どおり `(None, ZIP_READ_ERROR)`
- `SDIST_EXCLUDE_VIOLATION` / `CONTROL_NOT_EMPTY` … 新規の違反種別 literal（exit 1 側）。
  `VIOLATION_KINDS` は 7 種になる（`test_violation_kinds_are_exactly_seven` が凍結）

## Q3〜Q6 が凍結する observable な挙動（関数名でなく出力で固定する）

- **サニタイズ（Q3 / R1）**: 違反 detail / 検証不能 detail に含まれる正本
  （`.claude/hooks/_hook_utils.py` の `_TERMINAL_SANITIZE_RE`）と**同一の全クラス**を
  **除去**する（置換ではない: 除去後の文字列が原文の連結と一致することを assert する）。
  適用 3 サイトは (1) wheel 違反ループ (2) sdist 違反出力 (3) 検証不能 detail
- **合算出力（Q6）**: 違反出力のヘッダ `配布物の退行を検出しました:` は 1 回だけ出し、
  sdist 側・wheel 側の違反をその下に全件並べて exit 1（種別ごとに別ブロックにしない）
- **PASS 行（Q5）**: 既定モードの PASS 行は `sdist EXCLUDE 混入なし` と `対照サイズ 0` を含み、
  PASS 行とは**別の stdout 行**に対照サイズの実測値（`対照サイズ` を含む 1 行）を出す。
  `--wheel` モードの PASS 行は既存文言のまま逐語不変とし、sdist 検査の主張を含めない。
  代わりに `sdist 側 2 検査は未実施` を PASS 行以外の stdout 行で明示する
- `select_single_artifact(outdir, pattern) -> tuple[str | None, str | None]` … (パス, 原因識別子)
- `read_namelist(path) -> tuple[list[str] | None, str | None]` … (namelist, 原因識別子)
- `run_build(outdir, runner=subprocess.run, find_spec=importlib.util.find_spec) -> str | None`
  … 既定ビルドの実行部。成功で None・失敗で原因識別子（`BUILD_TOOL_MISSING` /
  `BUILD_FAILED`）。argv とビルドツール判定をここで凍結する
- `main(argv=None, *, build_runner=run_build) -> int` … exit code を **返す**
  （`check_deletions.py` の `sys.exit(main())` 先例に合わせる）

**ビルドツール判定の置き場所（環境依存を作らないための契約）**: PyPA `build` の導入有無の
判定は `run_build`（＝注入で置き換わる側）に置き、`main` の前段に置かない。
`main` 側に置くと、`build_runner` を差し替えたテストまで「実行環境に build が
導入されているか」に依存してしまう（CI の pytest matrix は `build` を入れない）。
`main` は `build_runner` の戻り値だけで分岐すること。

## R1 / R2 が凍結する契約（E 周回 2 是正・plan-report-20260815-003232.md）

**R1: サニタイズ被覆は正本と同一（部分列挙をしない）**

除去対象は正本 `_TERMINAL_SANITIZE_RE` の全クラス（2026-08-15 に正本を Read して照合）:
C0（U+0000–U+001F）・DEL（U+007F）・C1（U+0080–U+009F・NEL U+0085 を含む）・
ゼロ幅 U+200B–U+200F・行区切り U+2028 / U+2029・双方向 U+202A–U+202E・U+2066–U+2069・
BOM U+FEFF。

- (a) 代表文字の除去を正負両方向で凍結する。**残存側の題材は ASCII・日本語・ZWJ を含まない
  単一コードポイント絵文字（U+1F600）に限定する**（ZWJ〔U+200D〕は正本の除去対象なので、
  ZWJ 連結絵文字を題材にすると「残存すべき」という前提自体が成り立たない）
- (b) 同一性の**機械突合**: テストから `.claude/hooks/_hook_utils.py` を
  `importlib.util.spec_from_file_location` で読み込み、**U+0000–U+FFFF の全コードポイント**
  について「正本の正規表現が除去するか」と「`verify_wheel._sanitize` が除去するか」の真偽が
  一致することを突合する。**BMP 外（U+10000 以上）は現行正本に対象が無いため走査対象外**
  とする（正本が BMP 外へ広がった場合、本突合はその追加分を検知しない。その場合は
  比較ドメインの拡張が必要になる）。`scripts/` は配布物 hooks に依存させない方針のため
  実装側は集合を**複製**する形になり、この突合が唯一の drift 検知手段になる
- (c) 除去後の文字列は `str.splitlines()` で行分裂しない（BMP 内で `splitlines` が行境界と
  みなす文字を機械導出して題材にする）

**R2: sdist member 種別の射程（SR [G-1] の死角を閉じる）**

入力形は `(name, size, kind)` の 3 要素。既定経路（`_verify_via_build`）は読み取った member を
kind で振り分けてから各検出器へ渡す:

| 検査 | 対象 kind | 根拠 |
|---|---|---|
| EXCLUDE 名前検査（`find_sdist_violations`） | file + link | link の名前自体が sdist として公開される（SR [G-1]） |
| 候補件数の母数（`count_exclude_candidates`） | **file のみ** | link は内容を配布しないため「実フィルタが実ファイルを落とした」観測にならない（R2(d)） |
| 対照の存在・空性（`find_sdist_control_reason` / `find_control_size_violations`） | **file のみ** | 対照は「実ファイルとして配布される内容の空性」を保証するものなので、link / dir の同名 member は対照ではない（R2(c)） |
| dir member | 全検査の対象外 | tar は dir member 名の末尾スラッシュを剥がすため、パス名検査に入れると `agent-memory/tester` のようなディレクトリが EXCLUDE 違反として誤検出される（R2(a)） |
| 4 述語すべて False（FIFO・デバイス等） | 検査せず `LAYOUT_ANOMALY` | 種別フィルタで新たな死角を作らないための fail-closed |

link member の合成は **`tarfile.TarInfo` の `type` に `tarfile.SYMTYPE`（hardlink は
`LNKTYPE`）を直接指定**して行う。FS 上に実体 symlink を作らない（Windows では
symlink 作成が権限依存になるため）。この理由で `skipif` による回避も行わない。

## 射程外（confirm へ受け渡す観測）

- `WHEEL_NOT_FOUND` の **CLI 層**での実測（`--wheel` に存在しないパスを与えて exit 3＋
  stderr 先頭行）は confirm タスクの契約。本ファイルでは検出器層
  （`select_single_artifact` の 0 件）でのみ観測する
- 実ビルド（`python -m build`）を伴う end-to-end の exit 0・改変 wheel での種別観測も confirm 側
"""

from __future__ import annotations

import importlib.util
import inspect
import io
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

# scripts/ を sys.path に追加して import できるようにする
# （tests/test_check_deletions.py の先例に従う）
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import verify_wheel  # noqa: E402

from c3 import _excludes  # noqa: E402
from c3._excludes import KEEP_PATTERNS, should_skip  # noqa: E402

# 注意（DC-AS-001）: 下記の一括 import には**実装済みの API だけ**を並べる。
# Red で追加する新 API（E 周回 1: `SDIST_EXCLUDE_VIOLATION` / `CONTROL_NOT_EMPTY` /
# `find_sdist_violations` / `find_control_size_violations`、E 周回 2: `read_sdist_members` の
# 3 要素化・`_sanitize` の被覆拡張）をここへ足すと、実装不在の時点で collection 時
# ImportError になり本ファイル全件が error になる。
# 新 API は上の `verify_wheel` 名前空間経由の属性アクセスで参照する
# （モジュール docstring「新 API を module-level の一括 import へ足さない」を参照）。
# E 周回 2 の Red も同じ規約に従い、`verify_wheel.read_sdist_members` /
# `verify_wheel._sanitize` を属性アクセスで参照する。
from verify_wheel import (  # noqa: E402
    BUILD_FAILED,
    BUILD_TOOL_MISSING,
    CONTROL_LEAKED,
    CONTROL_MISSING,
    EXCLUDE_VIOLATION,
    EXIT_PASS,
    EXIT_UNVERIFIABLE,
    EXIT_VIOLATION,
    FR2_VIOLATION,
    KEEP_MISSING,
    LAYOUT_ANOMALY,
    TEMPLATE_EMPTY,
    UNEXPECTED_TOPLEVEL,
    UNVERIFIABLE_REASONS,
    VIOLATION_KINDS,
    WHEEL_NOT_FOUND,
    ZIP_READ_ERROR,
    count_exclude_candidates,
    find_sdist_control_reason,
    find_unverifiable,
    find_violations,
    main,
    read_namelist,
    run_build,
    select_single_artifact,
)

# ---------------------------------------------------------------------------
# 注入値（少数リテラル・SSOT の複製ではない）
# ---------------------------------------------------------------------------

# wheel 内の実パス（決定実験 2026-08-14 の実測値）
_WHEEL_TEMPLATE_PREFIX = "c3/_template/.claude/"
# sdist tarball 内の実パス（ルートディレクトリはバージョン付き）
_SDIST_ROOT = "c3-9.9.9/"
_SDIST_CLAUDE_PREFIX = _SDIST_ROOT + ".claude/"
# 注入対照（sdist に在り wheel には無いべきファイル）
_CONTROL_RELPATH = "state/setup_done.flag"
# 実フィルタが落とすべき典型（v1.1.0 の混入 defect と同型）
_EXCLUDED_SAMPLE = "state/tier_selection.json"
# `should_skip` True になる**ディレクトリ**の `.claude/` 相対名（R2(a) の題材）。
# tar は dir member 名の末尾スラッシュを剥がすため、種別を見ずにパス名検査へ入れると
# このディレクトリが EXCLUDE 違反として誤検出される（`TestFixtureSanity` が前提を固定する）。
_EXCLUDED_DIR_SAMPLE = "agent-memory/tester"

# `.claude/` 相対で should_skip False の通常配布ファイル（fixture の素材）
_ORDINARY_RELPATHS = (
    "CLAUDE.md",
    "agents/planner.md",
    "hooks/stop.py",
    "skills/start/SKILL.md",
    "docs/config-policy.md",
)

# Q3（サニタイズ）の題材。C0 制御文字 `\x01`-`\x1f`（TAB / LF / CR / ESC を含む）と DEL。
# NUL（`\x00`）は zip / tar のエントリ名として往復しない（実測: `zipfile` は
# エントリ名を NUL で切り落とす）ため、アーカイブ経由の題材からは外し、
# アーカイブを経由しない検証不能 detail（`--wheel` の不在パス）でのみ使う。
_C0_AND_DEL = "".join(chr(c) for c in range(1, 32)) + "\x7f"
_NUL_C0_AND_DEL = "\x00" + _C0_AND_DEL

# ---------------------------------------------------------------------------
# R1: サニタイズ被覆（正本 `.claude/hooks/_hook_utils.py` の `_TERMINAL_SANITIZE_RE`）
# ---------------------------------------------------------------------------

# 正本の実体パス。テスト層だけが配布物 hooks を読む（`scripts/` 側は複製する方針のため、
# この機械突合が唯一の drift 検知手段になる）。
_HOOK_UTILS_PATH = (
    Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "_hook_utils.py"
)

# 機械突合の比較ドメイン（DC-AM-003 で確定）。BMP 外は現行正本に対象が無いため走査しない。
_SANITIZE_COMPARE_DOMAIN = range(0x10000)

# R1(a) 除去される側の代表文字（正本の全クラスから 1 つ以上ずつ・部分列挙にしない）。
# ソースに不可視文字を直書きしないため `chr()` で書く（正本 `_hook_utils.py` と同じ流儀）。
_SANITIZE_REMOVED_SAMPLES: tuple[tuple[str, str], ...] = (
    (chr(0x0000), "U+0000 NUL (C0)"),
    (chr(0x000A), "U+000A LF (C0 / 行分裂)"),
    (chr(0x001B), "U+001B ESC (C0 / ANSI エスケープ)"),
    (chr(0x007F), "U+007F DEL"),
    (chr(0x0085), "U+0085 NEL (C1 / 行分裂)"),
    (chr(0x009B), "U+009B CSI (C1)"),
    (chr(0x200B), "U+200B ZERO WIDTH SPACE (ゼロ幅)"),
    (chr(0x200D), "U+200D ZWJ (ゼロ幅)"),
    (chr(0x200F), "U+200F RLM (ゼロ幅)"),
    (chr(0x2028), "U+2028 LINE SEPARATOR (行区切り)"),
    (chr(0x2029), "U+2029 PARAGRAPH SEPARATOR (行区切り)"),
    (chr(0x202A), "U+202A LRE (双方向埋め込み)"),
    (chr(0x202E), "U+202E RLO (双方向上書き・ファイル名偽装)"),
    (chr(0x2066), "U+2066 LRI (双方向 isolate)"),
    (chr(0x2069), "U+2069 PDI (双方向 isolate)"),
    (chr(0xFEFF), "U+FEFF BOM"),
)

# R1(a) 残存する側の題材。ASCII・日本語・**ZWJ を含まない単一コードポイント絵文字**に限定する
# （ZWJ〔U+200D〕は正本の除去対象なので、ZWJ 連結絵文字は「残存すべき」前提が成り立たない）。
_SANITIZE_KEPT_SAMPLES: tuple[str, ...] = (
    "state/tier_selection.json",
    "reports/plan-report-20260815-003232.md",
    "docs/c3追加予定機能リスト.md",
    "エージェントメモリ.md",
    "\U0001f600",
    "smile\U0001f600.md",
)

# `str.splitlines()` が行境界とみなす BMP 内の全文字（機械導出・R1(c) の題材）。
# 手で列挙せずここで数え上げることで、「列挙漏れのぶんだけ検査が甘くなる」形を避ける。
_LINE_BREAKING_CHARS = "".join(
    # nul-boundary: allow(題材文字列の組み立てであり機械可読な行集合の区切りではない)
    ch
    for ch in (chr(cp) for cp in _SANITIZE_COMPARE_DOMAIN)
    if len(("a" + ch + "b").splitlines()) > 1
)


def load_hook_utils():
    """正本 `.claude/hooks/_hook_utils.py` をファイルパスから読み込む。

    hooks はスタンドアロン実行前提で package になっていないため、
    `importlib.util.spec_from_file_location` で読み込む（`tests/test_archive_reports.py`
    と同じ道具立て）。**不在時に skip しない**: この突合は複製された sanitize 集合の
    唯一の drift 検知手段であり、静かに無検査へ落とすと R1 の実効が消えるため。
    """
    if not _HOOK_UTILS_PATH.is_file():
        raise FileNotFoundError(
            f"sanitize 集合の正本が見つからない: {_HOOK_UTILS_PATH}\n"
            "（この突合が複製の drift 検知手段なので skip せず失敗させる）"
        )
    spec = importlib.util.spec_from_file_location(
        "_hook_utils_for_verify_wheel_test", _HOOK_UTILS_PATH
    )
    assert spec is not None and spec.loader is not None, (
        f"import spec を組めない: {_HOOK_UTILS_PATH}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical_bmp_removal_set() -> frozenset[int]:
    """正本 `_TERMINAL_SANITIZE_RE` が U+0000–U+FFFF のうち除去するコードポイントの集合。

    正本を 1 回だけ読み込み、比較ドメイン全域を走査して集合化する
    （テストごとに 65536 回 hooks を読み直さないため）。
    """
    pattern = load_hook_utils()._TERMINAL_SANITIZE_RE
    return frozenset(
        cp for cp in _SANITIZE_COMPARE_DOMAIN if pattern.sub("", chr(cp)) == ""
    )


# ---------------------------------------------------------------------------
# R2: sdist member の種別（kind）
# ---------------------------------------------------------------------------

# `read_sdist_members` が返す kind の値域（3 値ちょうど）。
_KIND_FILE = "file"
_KIND_LINK = "link"
_KIND_DIR = "dir"
_KINDS = (_KIND_FILE, _KIND_LINK, _KIND_DIR)

# 合成 sdist の**書き出し専用**の種別指定（`_write_sdist` の第 3 要素）。
# "file" / "link" / "dir" は読み戻したときの kind と同じ値になる。"hardlink" は kind "link" へ、
# "fifo" は 4 述語すべて False（＝未知種別）へ落ちる。R2(f): FS 上に実体 symlink は作らず
# `tarfile.TarInfo.type` を直接指定する（Windows で symlink 作成が権限依存になるため）。
_TAR_SPEC_HARDLINK = "hardlink"
_TAR_SPEC_FIFO = "fifo"
_TAR_TYPE_BY_SPEC = {
    _KIND_FILE: tarfile.REGTYPE,
    _KIND_LINK: tarfile.SYMTYPE,
    _KIND_DIR: tarfile.DIRTYPE,
    _TAR_SPEC_HARDLINK: tarfile.LNKTYPE,
    _TAR_SPEC_FIFO: tarfile.FIFOTYPE,
}

# PASS 行の識別と、`--wheel` モードで逐語不変であるべき既存文言（Q5）
_PASS_LINE_PREFIX = "検証 PASS"
_WHEEL_MODE_PASS_LINE = (
    "検証 PASS: EXCLUDE 混入なし・KEEP 全件あり・FR-2 充足・注入対照は wheel に不在"
)

# 違反出力のヘッダ（Q6: 合算して 1 回だけ出す）
_VIOLATION_HEADER = "配布物の退行を検出しました:"


# ---------------------------------------------------------------------------
# 合成 namelist ヘルパ
# ---------------------------------------------------------------------------


def _clean_relpaths(drop: tuple[str, ...] = ()) -> list[str]:
    """clean な wheel の `.claude/` 相対パス一覧（KEEP 全件＋通常ファイル）。"""
    return [p for p in KEEP_PATTERNS if p not in drop] + list(_ORDINARY_RELPATHS)


def clean_wheel_namelist(
    extra: tuple[str, ...] = (),
    drop_keep: tuple[str, ...] = (),
) -> list[str]:
    """違反ゼロであるべき wheel namelist を組み立てる。

    `extra` は完全な wheel 内パス（プレフィックス込み）で渡す。
    """
    names = [
        "c3/__init__.py",
        "c3/cli.py",
        "c3-9.9.9.dist-info/METADATA",
        "c3-9.9.9.dist-info/RECORD",
    ]
    names += [_WHEEL_TEMPLATE_PREFIX + rel for rel in _clean_relpaths(drop_keep)]
    names += list(extra)
    return names


def clean_sdist_namelist(
    extra: tuple[str, ...] = (),
    with_control: bool = True,
) -> list[str]:
    """sdist tarball の member 名一覧を組み立てる。"""
    names = [
        _SDIST_ROOT + "pyproject.toml",
        _SDIST_ROOT + "src/c3/__init__.py",
    ]
    names += [_SDIST_CLAUDE_PREFIX + rel for rel in _clean_relpaths()]
    if with_control:
        names.append(_SDIST_CLAUDE_PREFIX + _CONTROL_RELPATH)
    names += list(extra)
    return names


def clean_sdist_members(
    extra: tuple[tuple[str, int, str], ...] = (),
    with_control: bool = True,
    control_size: int = 0,
) -> list[tuple[str, int, str]]:
    """sdist tarball の `(member 名, サイズ, kind)` 一覧を組み立てる（Q2 / Q4 / R2 用）。

    `clean_sdist_namelist()` と同じ member 集合をサイズ・種別付きで返す。既定の member は
    すべて `kind == "file"`。注入対照だけは `control_size` でサイズを指定できる
    （既定 0 = 現行の実 sdist と同じ状態）。

    R2(e) で対照系 2 関数の入力形が 3 要素になったため、本ヘルパも 3 要素へ改訂した
    （`extra` も `(name, size, kind)` で渡す）。
    """
    members = [(name, 0, _KIND_FILE) for name in clean_sdist_namelist(with_control=False)]
    if with_control:
        members.append((_SDIST_CLAUDE_PREFIX + _CONTROL_RELPATH, control_size, _KIND_FILE))
    members += list(extra)
    return members


def member_names(members, kinds: tuple[str, ...] = _KINDS) -> list[str]:
    """3 要素 member 列から、指定 kind の member 名だけを取り出す。

    既定経路が名前ベース検出器へ渡す入力（EXCLUDE 名前検査 = file + link /
    候補件数の母数 = file のみ）を、テスト側で同じ形に組むためのヘルパ。
    """
    return [name for name, _size, kind in members if kind in kinds]


def kinds(violations) -> set[str]:
    """`find_violations` の戻り値から違反種別の集合を取り出す。"""
    return {v[0] for v in violations}


def details(violations) -> list[str]:
    """`find_violations` の戻り値から該当エントリ/パターンを取り出す。"""
    return [v[1] for v in violations]


def always_false(rel_posix: str) -> bool:
    """常に False を返す should_skip（注入対照の独立性検証用・P8(b)）。"""
    return False


def always_true(rel_posix: str) -> bool:
    """常に True を返す should_skip（Q1 の正方向の注入確認用）。"""
    return True


def assert_no_control_chars(text: str, expected_lines: int) -> None:
    """出力に C0 制御文字 / DEL が残っていないこと（Q3・除去方式の凍結）。

    改行は `print` が出す行区切りとして正当に残るため、行区切り由来の `\\n` を
    「残っていてよい制御文字」として扱い、代わりに **行数**で detail 中の改行が
    除去されたことを判定する（`str.splitlines` は `\\x0b` `\\x0c` `\\x1c`-`\\x1e` でも
    分割するが、それらは第 1 の assert で不在が保証済みのため判定は健全）。
    """
    leftover = sorted({ascii(c) for c in text if (ord(c) < 32 and c != "\n") or ord(c) == 127})
    assert not leftover, f"出力に制御文字 / DEL が残っている: {leftover} / raw={ascii(text)}"
    assert len(text.splitlines()) == expected_lines, (
        f"出力の行数が {expected_lines} でない（detail 中の改行が除去されていない可能性）: "
        f"{ascii(text)}"
    )


def pass_lines(out: str) -> list[str]:
    """stdout から PASS 行（`検証 PASS` で始まる行）だけを取り出す（Q5）。"""
    return [line for line in out.splitlines() if line.startswith(_PASS_LINE_PREFIX)]


# ---------------------------------------------------------------------------
# fixture 自体の健全性（空回り防止）
# ---------------------------------------------------------------------------


class TestFixtureSanity:
    """合成入力が「そもそも何も測っていない」状態に落ちていないことの番人。"""

    def test_keep_patterns_are_not_empty(self):
        assert KEEP_PATTERNS, "KEEP_PATTERNS が空。SSOT 参照が壊れている"

    def test_clean_namelist_contains_every_keep_pattern(self):
        names = clean_wheel_namelist()
        missing = [p for p in KEEP_PATTERNS if _WHEEL_TEMPLATE_PREFIX + p not in names]
        assert not missing, f"clean fixture が KEEP を欠いている: {missing}"

    def test_clean_relpaths_are_all_non_skip(self):
        skipped = [rel for rel in _clean_relpaths() if should_skip(rel)]
        assert not skipped, (
            f"clean fixture に should_skip True のパスが混ざっている: {skipped}"
        )

    def test_injected_samples_are_actually_excluded_by_ssot(self):
        """注入する「違反エントリ」が SSOT 上で本当に除外対象であること。"""
        assert should_skip(_EXCLUDED_SAMPLE) is True
        assert should_skip(_CONTROL_RELPATH) is True

    def test_dir_sample_would_be_a_violation_if_kind_were_ignored(self):
        """R2(a) の題材が「種別を見なければ違反になる」ものであること。

        tar は dir member 名の末尾スラッシュを剥がすため、この名前はパス名としては
        `should_skip` True になる。dir 除外の検査が空回りしていないことの前提。
        """
        assert should_skip(_EXCLUDED_DIR_SAMPLE) is True, (
            f"{_EXCLUDED_DIR_SAMPLE} が SSOT 上で除外対象でない"
            "（dir 対象外の検査が「そもそも違反にならない名前」を測ってしまう）"
        )

    def test_line_breaking_chars_are_actually_collected(self):
        """R1(c) の題材（機械導出した行境界文字）が空でないこと。"""
        assert _LINE_BREAKING_CHARS, "splitlines の行境界文字が 1 つも集まっていない"
        assert "\n" in _LINE_BREAKING_CHARS and chr(0x2028) in _LINE_BREAKING_CHARS, (
            f"行境界文字の導出が壊れている: {ascii(_LINE_BREAKING_CHARS)}"
        )


# ---------------------------------------------------------------------------
# P1: EXCLUDE 違反検出
# ---------------------------------------------------------------------------


class TestExcludeViolation:
    """`_template/.claude/` 配下に should_skip True のエントリがあれば違反。"""

    def test_clean_namelist_has_no_violation(self):
        """正: clean な namelist では違反 0 件。"""
        assert find_violations(clean_wheel_namelist()) == []

    @pytest.mark.parametrize(
        "rel",
        [
            _EXCLUDED_SAMPLE,
            "agent-memory/tester/MEMORY.md",
            "reports/plan-report-20260814-195906.md",
            "memory/patterns.json",
            "settings.local.json",
            "hooks/__pycache__/stop.cpython-312.pyc",
        ],
    )
    def test_excluded_entry_is_detected(self, rel):
        """負: 除外対象エントリの混入は EXCLUDE_VIOLATION として検出される。"""
        entry = _WHEEL_TEMPLATE_PREFIX + rel
        violations = find_violations(clean_wheel_namelist(extra=(entry,)))
        assert kinds(violations) == {EXCLUDE_VIOLATION}, (
            f"{rel} の混入が EXCLUDE_VIOLATION として検出されない: {violations!r}"
        )
        assert any(rel in d for d in details(violations)), (
            f"違反の詳細に該当エントリが含まれない: {details(violations)!r}"
        )

    def test_entries_outside_template_boundary_are_ignored(self):
        """境界の外（`_template/.claude/` を含まないエントリ）は EXCLUDE 判定の対象外。

        wheel には `c3/cli.py` のようなパッケージ実体が入る。これらを `.claude/` 相対と
        誤読して判定すると誤検出になる。
        """
        violations = find_violations(clean_wheel_namelist(extra=("c3/state/foo.json",)))
        assert EXCLUDE_VIOLATION not in kinds(violations)

    def test_should_skip_is_an_explicit_parameter_with_ssot_default(self):
        """ADR-7 追補: 検出器は should_skip を明示引数（既定値 SSOT）で受ける。

        monkeypatch でなく引数差し替えで注入できることを構造として固定する
        （no-op 注入・AttributeError の失敗モードを排除するため）。
        """
        for func in (find_violations, count_exclude_candidates, find_sdist_control_reason):
            params = inspect.signature(func).parameters
            assert "should_skip" in params, f"{func.__name__} に should_skip 引数が無い"
            assert params["should_skip"].default is _excludes.should_skip, (
                f"{func.__name__} の should_skip 既定値が c3._excludes.should_skip でない"
                f"（実際: {params['should_skip'].default!r}）"
            )


# ---------------------------------------------------------------------------
# P2: KEEP 欠落検出
# ---------------------------------------------------------------------------


class TestKeepMissing:
    """KEEP_PATTERNS のファイルが wheel に無ければ違反（存在検証）。"""

    def test_all_keep_present_is_clean(self):
        """正: KEEP 全件そろえば KEEP_MISSING は出ない。"""
        assert KEEP_MISSING not in kinds(find_violations(clean_wheel_namelist()))

    @pytest.mark.parametrize("pattern", list(KEEP_PATTERNS))
    def test_each_missing_keep_is_detected(self, pattern):
        """負: KEEP を 1 件欠くと KEEP_MISSING。

        成功条件 1 の検知力（FR-0 整備前の公開経路 wheel が `reports/.gitkeep` を欠いて
        いた実測）を、テスト側で固定するのが本性質。
        `breaking-changes.txt` を欠いた場合は FR-2 検査（P3）にも同時に当たるため、
        許容する種別は {KEEP_MISSING, FR2_VIOLATION} の部分集合とする。
        """
        violations = find_violations(clean_wheel_namelist(drop_keep=(pattern,)))
        assert KEEP_MISSING in kinds(violations), (
            f"KEEP パターン {pattern} の欠落が検出されない: {violations!r}"
        )
        assert kinds(violations) <= {KEEP_MISSING, FR2_VIOLATION}, (
            f"想定外の違反種別が出た: {violations!r}"
        )
        assert any(pattern in d for d in details(violations)), (
            f"違反の詳細に欠落パターンが含まれない: {details(violations)!r}"
        )


# ---------------------------------------------------------------------------
# P3: FR-2 明示検査
# ---------------------------------------------------------------------------


class TestFr2Violation:
    """`/CLAUDE.md` §6 手順 5 の期待（breaking-changes.txt 在・c3_version.txt 不在）。"""

    def test_clean_namelist_has_no_fr2_violation(self):
        """正: clean な namelist では FR2_VIOLATION は出ない。"""
        assert FR2_VIOLATION not in kinds(find_violations(clean_wheel_namelist()))

    def test_missing_breaking_changes_is_detected(self):
        """負: breaking-changes.txt の欠落は FR2_VIOLATION。"""
        violations = find_violations(clean_wheel_namelist(drop_keep=("breaking-changes.txt",)))
        assert FR2_VIOLATION in kinds(violations), (
            f"breaking-changes.txt 欠落が FR2_VIOLATION として検出されない: {violations!r}"
        )

    def test_c3_version_leak_is_detected(self):
        """負: state/c3_version.txt の混入は FR2_VIOLATION。

        この検査は実 wheel 層では恒真（配布元に実体が無い）であり、実効はこの合成
        namelist テストが持つ。詳細はモジュール docstring
        「`state/c3_version.txt` 不在検査の非対称性」を参照。
        """
        entry = _WHEEL_TEMPLATE_PREFIX + "state/c3_version.txt"
        violations = find_violations(clean_wheel_namelist(extra=(entry,)))
        assert FR2_VIOLATION in kinds(violations), (
            f"c3_version.txt の混入が FR2_VIOLATION として検出されない: {violations!r}"
        )


# ---------------------------------------------------------------------------
# P4: 検証不能の区別 / トップレベル検査
# ---------------------------------------------------------------------------


class TestUnverifiable:
    """「違反（配布物の退行）」と「検証不能（検査インフラの故障）」を分ける。"""

    def test_clean_namelist_is_verifiable(self):
        """正: clean な namelist は検証可能（None）。"""
        assert find_unverifiable(clean_wheel_namelist()) is None

    def test_zero_template_entries_is_template_empty(self):
        """負: `_template/.claude/` 配下が 0 件なら TEMPLATE_EMPTY（空の緑の防止）。"""
        namelist = ["c3/__init__.py", "c3-9.9.9.dist-info/RECORD"]
        assert find_unverifiable(namelist) == TEMPLATE_EMPTY

    def test_only_directory_entries_is_template_empty(self):
        """負: 境界配下がディレクトリエントリだけでも「実体 0 件」＝ TEMPLATE_EMPTY。"""
        namelist = [
            "c3/__init__.py",
            _WHEEL_TEMPLATE_PREFIX,
            _WHEEL_TEMPLATE_PREFIX + "agents/",
        ]
        assert find_unverifiable(namelist) == TEMPLATE_EMPTY

    def test_duplicated_boundary_is_layout_anomaly(self):
        """負: 境界が 1 エントリ内に複数回現れるのは LAYOUT_ANOMALY（違反ではない）。"""
        anomalous = _WHEEL_TEMPLATE_PREFIX + "x/_template/.claude/y.md"
        assert find_unverifiable(clean_wheel_namelist(extra=(anomalous,))) == LAYOUT_ANOMALY


class TestUnexpectedToplevel:
    """トップレベル検査（ADR-2 追補・force-include のセクション誤り検出）。"""

    def test_known_toplevels_are_accepted(self):
        """正: `c3` と `*.dist-info` だけなら UNEXPECTED_TOPLEVEL は出ない。"""
        assert UNEXPECTED_TOPLEVEL not in kinds(find_violations(clean_wheel_namelist()))

    @pytest.mark.parametrize(
        "entry",
        [
            # target 非依存の force-include セクションに書くと対照がトップレベルへ混入する
            ".claude/state/setup_done.flag",
            "src/c3/_template/.claude/CLAUDE.md",
            "scripts/verify_wheel.py",
        ],
    )
    def test_unknown_toplevel_is_detected(self, entry):
        """負: 既知集合以外のトップレベルエントリは UNEXPECTED_TOPLEVEL。"""
        violations = find_violations(clean_wheel_namelist(extra=(entry,)))
        assert UNEXPECTED_TOPLEVEL in kinds(violations), (
            f"{entry} のトップレベル逸脱が検出されない: {violations!r}"
        )


# ---------------------------------------------------------------------------
# P7: 入力正規化（末尾 `/`）
# ---------------------------------------------------------------------------


class TestDirectoryEntriesAreIgnored:
    """ディレクトリエントリ（末尾 `/`）は判定対象外。

    `fnmatch.fnmatchcase("reports/", "reports/*")` は True になるため、正規化を欠くと
    正常な wheel でも EXCLUDE 違反として誤検出される（実行確認済みの前提）。
    """

    def test_directory_entry_matching_exclude_pattern_is_not_a_violation(self):
        namelist = clean_wheel_namelist(
            extra=(
                _WHEEL_TEMPLATE_PREFIX + "reports/",
                _WHEEL_TEMPLATE_PREFIX + "memory/sessions/",
                _WHEEL_TEMPLATE_PREFIX,
            )
        )
        assert find_violations(namelist) == [], (
            "ディレクトリエントリが判定対象に入り誤検出されている"
        )

    def test_directory_entry_does_not_trigger_layout_anomaly(self):
        namelist = clean_wheel_namelist(extra=(_WHEEL_TEMPLATE_PREFIX,))
        assert find_unverifiable(namelist) is None

    def test_directory_entry_is_not_counted_as_exclude_candidate(self):
        """sdist 側の候補件数でもディレクトリエントリは数えない。"""
        names = clean_sdist_namelist(extra=(_SDIST_CLAUDE_PREFIX + "reports/",))
        assert count_exclude_candidates(names) == 1


# ---------------------------------------------------------------------------
# P8: 注入対照
# ---------------------------------------------------------------------------


class TestInjectedControl:
    """注入対照（`state/setup_done.flag`）による正の対照の実働実証。"""

    # --- (a) sdist 側の在/不在 ------------------------------------------

    def test_sdist_with_control_is_ok(self):
        """正: sdist member に対照（file）が在れば検証可能（None）。

        R2(e) 追随: `find_sdist_control_reason` の入力形が 3 要素 `(name, size, kind)` に
        なったため、`clean_sdist_namelist()`（名前のみ）から `clean_sdist_members()` へ変更。
        """
        assert find_sdist_control_reason(clean_sdist_members()) is None

    def test_sdist_without_control_is_control_missing(self):
        """負: 対照が sdist に無ければ CONTROL_MISSING（検証不能・対照喪失）。

        候補件数だけは 1 件以上ある member 列を使い、「対照の不在」が理由であることを
        (c) の候補件数 0 と分離する。

        R2(e) 追随: 入力形を 3 要素へ変更（母数の観測は file member 名で行う）。
        """
        members = clean_sdist_members(
            with_control=False,
            extra=((_SDIST_CLAUDE_PREFIX + _EXCLUDED_SAMPLE, 0, _KIND_FILE),),
        )
        assert count_exclude_candidates(member_names(members, (_KIND_FILE,))) >= 1
        assert find_sdist_control_reason(members) == CONTROL_MISSING

    # --- (b) 独立性の実証（ADR-7 追補） ----------------------------------

    def test_control_check_is_independent_of_should_skip(self):
        """always-False の should_skip を引数で与えたとき:

        (i) EXCLUDE 検査（P1）は違反 0 件へ反転する（＝注入が効いている正の確認）
        (ii) 同一入力で対照検査は CONTROL_LEAKED のまま（＝対照は should_skip に依存しない）

        SSOT（`_excludes.py`）とフィルタ側複製（`hatch_build.py`）が同時に劣化しても
        対照検査だけは生き残る、という設計の凍結。
        """
        leaked = clean_wheel_namelist(
            extra=(
                _WHEEL_TEMPLATE_PREFIX + _CONTROL_RELPATH,
                _WHEEL_TEMPLATE_PREFIX + _EXCLUDED_SAMPLE,
            )
        )

        real_kinds = kinds(find_violations(leaked))
        assert EXCLUDE_VIOLATION in real_kinds, "前提: 実 should_skip では EXCLUDE 違反が出る"
        assert CONTROL_LEAKED in real_kinds, "前提: 実 should_skip でも対照混入は違反"

        injected_kinds = kinds(find_violations(leaked, should_skip=always_false))
        assert EXCLUDE_VIOLATION not in injected_kinds, (
            "always-False の should_skip を引数で与えても EXCLUDE 違反が消えない"
            "（注入シームが効いていない＝以降の独立性の主張が成り立たない）"
        )
        assert CONTROL_LEAKED in injected_kinds, (
            "should_skip を always-False にすると対照検査まで死ぬ"
            "（対照が should_skip に依存している＝同時劣化ケースで検出が消える）"
        )

    def test_control_absent_from_wheel_is_clean(self):
        """正: wheel に対照が無ければ CONTROL_LEAKED は出ない。"""
        assert CONTROL_LEAKED not in kinds(find_violations(clean_wheel_namelist()))

    # --- (c) 候補件数 ----------------------------------------------------

    def test_candidate_count_is_one_for_the_injected_control(self):
        """正: 現行の sdist では実効候補は注入対照の 1 件。"""
        assert count_exclude_candidates(clean_sdist_namelist()) == 1

    def test_zero_candidates_is_control_missing(self):
        """負: 候補件数 0 は注入の退行を意味するため CONTROL_MISSING。

        always-False の should_skip を引数で与えて件数 0 を作る
        （＝ SSOT が劣化して「何も除外対象でない」と答える状況の再現）。

        R2(e) 追随: `find_sdist_control_reason` へ渡す入力形を 3 要素へ変更。
        """
        members = clean_sdist_members()
        names = member_names(members, (_KIND_FILE,))
        assert count_exclude_candidates(names, should_skip=always_false) == 0
        assert find_sdist_control_reason(members, should_skip=always_false) == CONTROL_MISSING


# ---------------------------------------------------------------------------
# P5: exit code / 識別子・種別の凍結
# ---------------------------------------------------------------------------


class TestExitCodesAndIdentifiers:
    """ADR-4 改訂 3 ＋追補の literal を凍結する。"""

    def test_exit_codes(self):
        assert EXIT_PASS == 0
        assert EXIT_VIOLATION == 1
        assert EXIT_UNVERIFIABLE == 3
        assert 2 not in {EXIT_PASS, EXIT_VIOLATION, EXIT_UNVERIFIABLE}, (
            "exit 2 は argparse の usage error と衝突するため使わない"
        )

    def test_violation_kinds_are_exactly_seven(self):
        """E 周回 1 是正で sdist 側 2 種を追加し 5 種 → 7 種になる（意図した Red）。

        新 literal は一括 import へ足さず属性アクセスで参照する（DC-AS-001）。
        """
        assert set(VIOLATION_KINDS) == {
            "EXCLUDE_VIOLATION",
            "KEEP_MISSING",
            "FR2_VIOLATION",
            "CONTROL_LEAKED",
            "UNEXPECTED_TOPLEVEL",
            "SDIST_EXCLUDE_VIOLATION",
            "CONTROL_NOT_EMPTY",
        }
        assert len(set(VIOLATION_KINDS)) == len(VIOLATION_KINDS)
        assert [
            EXCLUDE_VIOLATION,
            KEEP_MISSING,
            FR2_VIOLATION,
            CONTROL_LEAKED,
            UNEXPECTED_TOPLEVEL,
            verify_wheel.SDIST_EXCLUDE_VIOLATION,
            verify_wheel.CONTROL_NOT_EMPTY,
        ] == [
            "EXCLUDE_VIOLATION",
            "KEEP_MISSING",
            "FR2_VIOLATION",
            "CONTROL_LEAKED",
            "UNEXPECTED_TOPLEVEL",
            "SDIST_EXCLUDE_VIOLATION",
            "CONTROL_NOT_EMPTY",
        ]

    def test_unverifiable_reasons_are_exactly_seven(self):
        assert set(UNVERIFIABLE_REASONS) == {
            "BUILD_TOOL_MISSING",
            "BUILD_FAILED",
            "WHEEL_NOT_FOUND",
            "ZIP_READ_ERROR",
            "TEMPLATE_EMPTY",
            "LAYOUT_ANOMALY",
            "CONTROL_MISSING",
        }
        assert len(set(UNVERIFIABLE_REASONS)) == len(UNVERIFIABLE_REASONS)
        assert [
            BUILD_TOOL_MISSING,
            BUILD_FAILED,
            WHEEL_NOT_FOUND,
            ZIP_READ_ERROR,
            TEMPLATE_EMPTY,
            LAYOUT_ANOMALY,
            CONTROL_MISSING,
        ] == [
            "BUILD_TOOL_MISSING",
            "BUILD_FAILED",
            "WHEEL_NOT_FOUND",
            "ZIP_READ_ERROR",
            "TEMPLATE_EMPTY",
            "LAYOUT_ANOMALY",
            "CONTROL_MISSING",
        ]

    def test_zip_read_error_is_observed_by_direct_call(self, tmp_path):
        """zip として読めないファイルは ZIP_READ_ERROR。"""
        broken = tmp_path / "broken.whl"
        broken.write_bytes(b"not a zip file")
        namelist, reason = read_namelist(str(broken))
        assert namelist is None
        assert reason == ZIP_READ_ERROR

    def test_read_namelist_returns_entries_for_a_valid_zip(self, tmp_path):
        """正の対照: 正常な zip は namelist を返し原因識別子は None。"""
        wheel = _write_wheel(tmp_path / "ok-9.9.9-py3-none-any.whl", clean_wheel_namelist())
        namelist, reason = read_namelist(str(wheel))
        assert reason is None
        assert namelist is not None
        assert _WHEEL_TEMPLATE_PREFIX + "breaking-changes.txt" in namelist


# ---------------------------------------------------------------------------
# P5b: ビルド経路と成果物選別の凍結
# ---------------------------------------------------------------------------


class _RunnerSpy:
    """`subprocess.run` 互換のスパイ（実際にはビルドしない）。"""

    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _CompletedStub(self.returncode)


class _CompletedStub:
    def __init__(self, returncode: int):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


class _ExplodingRunner:
    def __call__(self, *args, **kwargs):  # pragma: no cover - 呼ばれてはいけない
        raise AssertionError("build 未導入なのにビルドが実行された")


def _build_available(name):
    """`importlib.util.find_spec` のスタブ（PyPA build が導入済みの状況）。"""
    return object()


class TestBuildInvocation:
    """既定ビルドが公開経路と同じ sdist 経由であることを凍結する。"""

    def test_default_build_argv_is_sdist_route(self, tmp_path):
        spy = _RunnerSpy()
        reason = run_build(str(tmp_path), runner=spy, find_spec=_build_available)

        assert reason is None, f"正常ビルドで原因識別子が返っている: {reason!r}"
        assert len(spy.calls) == 1, f"ビルド実行が 1 回ではない: {spy.calls!r}"
        args, kwargs = spy.calls[0]
        argv = args[0] if args else kwargs.get("args")
        assert argv == [sys.executable, "-m", "build", "--outdir", str(tmp_path)], (
            f"既定ビルドの argv が公開等価（sdist 経由）でない: {argv!r}"
        )
        assert "--wheel" not in argv, "既定ビルドに --wheel が入ると公開経路と別物になる"
        assert not kwargs.get("shell"), "shell=True は使わない（リスト引数・shell=False）"

    def test_build_failure_is_reported_as_build_failed(self, tmp_path):
        """負: ビルドプロセスが非 0 で終われば BUILD_FAILED。"""
        spy = _RunnerSpy(returncode=7)
        assert run_build(str(tmp_path), runner=spy, find_spec=_build_available) == BUILD_FAILED

    def test_missing_build_tool_is_reported_before_running(self, tmp_path):
        """負: PyPA build 未導入は BUILD_TOOL_MISSING（ビルドは実行しない）。

        この判定は `run_build` 側（＝注入で置き換わる側）に置くことを凍結する。
        `main` の前段に置くと、`build_runner` を差し替えたテストまで実行環境の
        build 導入有無に依存する（モジュール docstring「ビルドツール判定の置き場所」）。
        """
        reason = run_build(
            str(tmp_path), runner=_ExplodingRunner(), find_spec=lambda name: None
        )
        assert reason == BUILD_TOOL_MISSING


class TestArtifactSelection:
    """outdir の成果物選別（`*.whl` / `*.tar.gz` 各ちょうど 1 件）。"""

    def test_exactly_one_wheel_is_selected(self, tmp_path):
        (tmp_path / "c3-9.9.9-py3-none-any.whl").write_bytes(b"")
        path, reason = select_single_artifact(str(tmp_path), "*.whl")
        assert reason is None
        assert path is not None and Path(path).name == "c3-9.9.9-py3-none-any.whl"

    def test_zero_wheel_is_wheel_not_found(self, tmp_path):
        path, reason = select_single_artifact(str(tmp_path), "*.whl")
        assert path is None
        assert reason == WHEEL_NOT_FOUND

    def test_multiple_wheels_are_fail_loud(self, tmp_path):
        (tmp_path / "c3-9.9.9-py3-none-any.whl").write_bytes(b"")
        (tmp_path / "c3-9.9.8-py3-none-any.whl").write_bytes(b"")
        path, reason = select_single_artifact(str(tmp_path), "*.whl")
        assert path is None
        assert reason in UNVERIFIABLE_REASONS, (
            f"複数件が検証不能として扱われていない: {reason!r}"
        )

    def test_exactly_one_sdist_is_selected(self, tmp_path):
        (tmp_path / "c3-9.9.9.tar.gz").write_bytes(b"")
        path, reason = select_single_artifact(str(tmp_path), "*.tar.gz")
        assert reason is None
        assert path is not None and Path(path).name == "c3-9.9.9.tar.gz"

    def test_zero_sdist_is_fail_loud(self, tmp_path):
        path, reason = select_single_artifact(str(tmp_path), "*.tar.gz")
        assert path is None
        assert reason in UNVERIFIABLE_REASONS

    def test_multiple_sdists_are_fail_loud(self, tmp_path):
        (tmp_path / "c3-9.9.9.tar.gz").write_bytes(b"")
        (tmp_path / "c3-9.9.8.tar.gz").write_bytes(b"")
        path, reason = select_single_artifact(str(tmp_path), "*.tar.gz")
        assert path is None
        assert reason in UNVERIFIABLE_REASONS


# ---------------------------------------------------------------------------
# CLI（exit code の実観測）
# ---------------------------------------------------------------------------


def _write_wheel(path: Path, namelist) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name in namelist:
            zf.writestr(name, "")
    return path


def _write_sdist(path: Path, items) -> Path:
    """合成 sdist を書き出す。

    `items` の要素は次の 3 形のいずれか:

    - member 名（サイズ 0・通常ファイル）
    - `(member 名, サイズ)`（通常ファイル。Q2 の対照サイズ検査の負の対照）
    - `(member 名, サイズ, 種別指定)`（R2。種別指定は `_TAR_TYPE_BY_SPEC` のキー）

    **R2(f)**: link member は `tarfile.TarInfo.type` に `SYMTYPE`（hardlink は `LNKTYPE`）を
    **直接指定**して合成する。FS 上に実体 symlink を作らない（Windows では symlink 作成が
    権限依存になるため）。この理由で `skipif` による回避も行わない。
    `"fifo"` は 4 述語（isfile / issym / islnk / isdir）すべてが False になる代表として使う。
    """
    with tarfile.open(path, "w:gz") as tf:
        for item in items:
            if isinstance(item, tuple):
                name, size = item[0], item[1]
                spec = item[2] if len(item) > 2 else _KIND_FILE
            else:
                name, size, spec = item, 0, _KIND_FILE
            info = tarfile.TarInfo(name)
            info.type = _TAR_TYPE_BY_SPEC[spec]
            if info.type == tarfile.REGTYPE:
                info.size = size
                tf.addfile(info, io.BytesIO(b"x" * size))
                continue
            # 非通常ファイルは tar 本体にデータを持たない（size は 0 でなければならない）
            info.size = 0
            if info.type in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.linkname = "CLAUDE.md"
            tf.addfile(info)
    return path


class _FakeBuild:
    """`build_runner(outdir) -> str | None` 互換。実ビルドの代わりに合成成果物を置く。"""

    def __init__(self, wheel_namelist, sdist_names, reason: str | None = None):
        self.wheel_namelist = wheel_namelist
        self.sdist_names = sdist_names
        self.reason = reason
        self.calls: list[str] = []

    def __call__(self, outdir):
        self.calls.append(str(outdir))
        if self.reason is None:
            out = Path(outdir)
            _write_wheel(out / "c3-9.9.9-py3-none-any.whl", self.wheel_namelist)
            _write_sdist(out / "c3-9.9.9.tar.gz", self.sdist_names)
        return self.reason


class _ExplodingBuild:
    def __call__(self, outdir):  # pragma: no cover - 呼ばれてはいけない
        raise AssertionError("--wheel 指定時にビルドが実行された")


class TestCli:
    """`main()` の exit code マッピング（PASS=0 / 違反=1 / 検証不能=3）。"""

    def test_clean_build_exits_zero(self, capsys):
        fake = _FakeBuild(clean_wheel_namelist(), clean_sdist_namelist())
        assert main([], build_runner=fake) == EXIT_PASS
        assert fake.calls, "ビルド実行部が呼ばれていない"

    def test_excluded_entry_exits_one_with_kind(self, capsys):
        wheel = clean_wheel_namelist(extra=(_WHEEL_TEMPLATE_PREFIX + _EXCLUDED_SAMPLE,))
        fake = _FakeBuild(wheel, clean_sdist_namelist())
        assert main([], build_runner=fake) == EXIT_VIOLATION
        captured = capsys.readouterr()
        assert EXCLUDE_VIOLATION in (captured.out + captured.err), (
            "違反種別が出力に現れない（confirm は種別名で判別する）"
        )

    def test_control_leak_exits_one_with_kind(self, capsys):
        wheel = clean_wheel_namelist(extra=(_WHEEL_TEMPLATE_PREFIX + _CONTROL_RELPATH,))
        fake = _FakeBuild(wheel, clean_sdist_namelist())
        assert main([], build_runner=fake) == EXIT_VIOLATION
        captured = capsys.readouterr()
        assert CONTROL_LEAKED in (captured.out + captured.err)

    def test_failed_build_exits_three_with_reason_on_first_stderr_line(self, capsys):
        fake = _FakeBuild(clean_wheel_namelist(), clean_sdist_namelist(), reason=BUILD_FAILED)
        assert main([], build_runner=fake) == EXIT_UNVERIFIABLE
        captured = capsys.readouterr()
        first_line = captured.err.splitlines()[0] if captured.err.splitlines() else ""
        assert first_line.startswith(BUILD_FAILED), (
            f"stderr 先頭行に原因識別子が無い: {captured.err!r}"
        )

    def test_wheel_option_verifies_existing_wheel_without_building(self, tmp_path, capsys):
        wheel = _write_wheel(
            tmp_path / "c3-9.9.9-py3-none-any.whl", clean_wheel_namelist()
        )
        assert main(["--wheel", str(wheel)], build_runner=_ExplodingBuild()) == EXIT_PASS


# ===========================================================================
# E 周回 1 是正（plan-report-20260814-220825.md）の Red: Q1〜Q6
#
# 新 API はすべて `verify_wheel.<name>` の属性アクセスで参照する（DC-AS-001）。
# 未実装の間は AttributeError が「そのテストだけ」の Red になり、本ファイルの
# 既存テストは収集エラーにならない。
# ===========================================================================


# ---------------------------------------------------------------------------
# Q1: sdist 側 EXCLUDE 混入検出（SR [F-1]）
# ---------------------------------------------------------------------------


class TestSdistExcludeViolation:
    """sdist（第二の公開成果物）の `.claude/` 配下に注入対照以外の should_skip True が
    在れば `SDIST_EXCLUDE_VIOLATION`。

    sdist は wheel と違い `hatch_build.py` の実フィルタを通らないため、git 追跡されて
    しまった EXCLUDE 対象は sdist にだけ残る（SR [F-1] の構造ギャップ）。
    """

    def test_clean_sdist_has_no_violation(self):
        """正: 対照 1 件だけが should_skip True の clean な sdist では違反 0 件。"""
        names = clean_sdist_namelist()
        # 前提: この listing で should_skip True なのは注入対照ちょうど 1 件
        assert count_exclude_candidates(names) == 1
        assert verify_wheel.find_sdist_violations(names) == []

    @pytest.mark.parametrize(
        "rel",
        [
            _EXCLUDED_SAMPLE,
            "agent-memory/tester/MEMORY.md",
            "reports/plan-report-20260814-220825.md",
            "memory/patterns.json",
            "settings.local.json",
            "logs/session.log",
        ],
    )
    def test_excess_excluded_entry_is_detected(self, rel):
        """負: 対照以外の EXCLUDE 対象が sdist に在れば SDIST_EXCLUDE_VIOLATION。"""
        names = clean_sdist_namelist(extra=(_SDIST_CLAUDE_PREFIX + rel,))
        violations = verify_wheel.find_sdist_violations(names)
        assert kinds(violations) == {verify_wheel.SDIST_EXCLUDE_VIOLATION}, (
            f"{rel} の sdist 混入が検出されない: {violations!r}"
        )
        assert any(rel in d for d in details(violations)), (
            f"違反の詳細に該当エントリが含まれない: {details(violations)!r}"
        )

    def test_control_itself_is_never_reported(self):
        """注入対照は「在るべきもの」なので違反にしない（対照だけなら 0 件）。"""
        violations = verify_wheel.find_sdist_violations(clean_sdist_namelist())
        assert all(_CONTROL_RELPATH not in d for d in details(violations))

    def test_should_skip_injection_works_in_both_directions(self):
        """`should_skip` は明示引数注入で正負両方向に効く（no-op 注入の排除）。"""
        names = clean_sdist_namelist(extra=(_SDIST_CLAUDE_PREFIX + _EXCLUDED_SAMPLE,))

        assert kinds(verify_wheel.find_sdist_violations(names)) == {
            verify_wheel.SDIST_EXCLUDE_VIOLATION
        }, "前提: 実 should_skip では余剰エントリが違反になる"

        # 負方向: always-False を注入すると違反は消える（注入シームが効いている）
        assert verify_wheel.find_sdist_violations(names, should_skip=always_false) == [], (
            "always-False の should_skip を引数で与えても違反が消えない（注入が効いていない）"
        )

        # 正方向: always-True を注入すると通常ファイルまで違反になるが、
        # 注入対照だけは常に除外され続ける（対照の扱いは should_skip の結果に依らない）
        injected = details(verify_wheel.find_sdist_violations(names, should_skip=always_true))
        assert set(injected) == set(_clean_relpaths()) | {_EXCLUDED_SAMPLE}, (
            f"always-True 注入時の違反集合が想定と違う: {sorted(injected)!r}"
        )
        assert _CONTROL_RELPATH not in injected, (
            "always-True の should_skip で注入対照まで違反として報告されている"
        )

    def test_should_skip_is_an_explicit_parameter_with_ssot_default(self):
        """既存検出器（P1）と同じ ADR-7 追補の形を新関数にも要求する。"""
        params = inspect.signature(verify_wheel.find_sdist_violations).parameters
        assert "should_skip" in params, "find_sdist_violations に should_skip 引数が無い"
        assert params["should_skip"].default is _excludes.should_skip, (
            "find_sdist_violations の should_skip 既定値が c3._excludes.should_skip でない"
            f"（実際: {params['should_skip'].default!r}）"
        )

    def test_directory_entries_are_ignored(self):
        """P7 と同じ理由（`fnmatch("reports/", "reports/*")` は True）で末尾 `/` は対象外。"""
        names = clean_sdist_namelist(
            extra=(
                _SDIST_CLAUDE_PREFIX + "reports/",
                _SDIST_CLAUDE_PREFIX + "memory/sessions/",
            )
        )
        assert verify_wheel.find_sdist_violations(names) == [], (
            "ディレクトリエントリが判定対象に入り誤検出されている"
        )

    def test_entries_outside_claude_boundary_are_ignored(self):
        """sdist ルート直下 `.claude/` の外（`src/` 等）は `.claude/` 相対で読まない。"""
        names = clean_sdist_namelist(
            extra=(
                _SDIST_ROOT + "src/c3/state/tier_selection.json",
                _SDIST_ROOT + "pyproject.toml",
            )
        )
        assert verify_wheel.find_sdist_violations(names) == []


# ---------------------------------------------------------------------------
# Q2: 注入対照のサイズ 0 検証（SR [F-3]）
# ---------------------------------------------------------------------------


class TestControlSize:
    """注入対照メンバーが 0 バイトでなければ `CONTROL_NOT_EMPTY`（exit 1 側の違反）。

    非 0 バイトの対照は**その内容が sdist として実際に公開される**ため、検証不能
    （`CONTROL_MISSING`・exit 3）ではなく配布物の退行（違反）に置く（DC-AS-003）。
    """

    def test_zero_size_control_is_clean(self):
        """正: 現行どおり 0 バイトなら違反 0 件。"""
        assert verify_wheel.find_control_size_violations(clean_sdist_members()) == []

    @pytest.mark.parametrize("size", [1, 5, 4096])
    def test_non_empty_control_is_detected(self, size):
        """負: 対照のサイズが 0 でなければ CONTROL_NOT_EMPTY。"""
        members = clean_sdist_members(control_size=size)
        violations = verify_wheel.find_control_size_violations(members)
        assert kinds(violations) == {verify_wheel.CONTROL_NOT_EMPTY}, (
            f"対照 {size} バイトが CONTROL_NOT_EMPTY として検出されない: {violations!r}"
        )
        assert any(_CONTROL_RELPATH in d for d in details(violations)), (
            f"違反の詳細に対照の相対パスが含まれない: {details(violations)!r}"
        )
        assert any(str(size) in d for d in details(violations)), (
            f"違反の詳細に実測サイズが含まれない: {details(violations)!r}"
        )

    def test_absent_control_is_not_reported_here(self):
        """対照の不在は CONTROL_MISSING（検証不能）の担当。ここでは二重報告しない。"""
        members = clean_sdist_members(with_control=False)
        assert verify_wheel.find_control_size_violations(members) == []

    def test_other_members_size_is_irrelevant(self):
        """通常ファイルのサイズは検査対象外（対照だけの前提を測る検査）。

        R2(e) 追随: `extra` を 3 要素 `(name, size, kind)` へ変更。
        """
        members = clean_sdist_members(
            extra=((_SDIST_CLAUDE_PREFIX + "agents/tester.md", 4096, _KIND_FILE),)
        )
        assert verify_wheel.find_control_size_violations(members) == []

    def test_same_name_outside_claude_boundary_is_not_the_control(self):
        """`.claude/` 境界の外にある同名ファイルを対照と誤認しない。

        R2(e) 追随: `extra` を 3 要素 `(name, size, kind)` へ変更。
        """
        members = clean_sdist_members(
            extra=((_SDIST_ROOT + "src/" + _CONTROL_RELPATH, 999, _KIND_FILE),)
        )
        assert verify_wheel.find_control_size_violations(members) == []

    def test_size_check_is_independent_of_should_skip(self):
        """対照検査は `CONTROL_LEAKED` と同じく should_skip に依存させない。

        SSOT（`_excludes.py`）とフィルタ側複製（`hatch_build.py`）が同時に劣化しても
        対照検査だけは生き残らせる、という既存設計の踏襲（P8(b) と同じ趣旨）。
        """
        params = inspect.signature(verify_wheel.find_control_size_violations).parameters
        assert "should_skip" not in params, (
            "対照サイズ検査が should_skip を受け取っている"
            f"（SSOT 劣化時に検査が道連れになる）: {list(params)!r}"
        )


# ---------------------------------------------------------------------------
# Q3: 違反 detail のサニタイズ（SR [F-4]・方式は「除去」）
# ---------------------------------------------------------------------------


class TestDetailSanitization:
    """detail に含まれる C0 制御文字・DEL・改行を出力から**除去**する（3 サイト）。

    置換（`?` 等）ではなく除去であることは「除去後の文字列が原文の連結と一致する」
    ことで判定する。
    """

    def test_wheel_violation_detail_is_sanitized(self, capsys):
        """サイト (1): 既存の wheel 違反ループ。"""
        rel = "state/tier_" + _C0_AND_DEL + "selection.json"
        assert should_skip(rel) is True, "前提: 題材が SSOT 上で除外対象であること"
        wheel = clean_wheel_namelist(extra=(_WHEEL_TEMPLATE_PREFIX + rel,))
        fake = _FakeBuild(wheel, clean_sdist_members())

        assert main([], build_runner=fake) == EXIT_VIOLATION
        err = capsys.readouterr().err
        assert EXCLUDE_VIOLATION in err
        assert "state/tier_selection.json" in err, (
            f"制御文字を除去した名前が原文どおり残っていない（置換方式は不可）: {ascii(err)}"
        )
        assert_no_control_chars(err, expected_lines=2)

    def test_sdist_violation_detail_is_sanitized(self, capsys):
        """サイト (2): 新設の sdist 違反出力。"""
        rel = "agent-memory/tester/MEM" + _C0_AND_DEL + "ORY.md"
        assert should_skip(rel) is True, "前提: 題材が SSOT 上で除外対象であること"
        fake = _FakeBuild(
            clean_wheel_namelist(), clean_sdist_namelist(extra=(_SDIST_CLAUDE_PREFIX + rel,))
        )

        assert main([], build_runner=fake) == EXIT_VIOLATION
        err = capsys.readouterr().err
        assert verify_wheel.SDIST_EXCLUDE_VIOLATION in err
        assert "agent-memory/tester/MEMORY.md" in err, (
            f"制御文字を除去した名前が原文どおり残っていない: {ascii(err)}"
        )
        assert_no_control_chars(err, expected_lines=2)

    def test_unverifiable_detail_is_sanitized(self, capsys):
        """サイト (3): 検証不能の detail（アーカイブを経由しないので NUL も題材に含む）。"""
        bad_path = "missing" + _NUL_C0_AND_DEL + "wheel.whl"

        assert main(["--wheel", bad_path], build_runner=_ExplodingBuild()) == EXIT_UNVERIFIABLE
        err = capsys.readouterr().err
        assert err.splitlines()[0].startswith(WHEEL_NOT_FOUND), (
            f"stderr 先頭行に原因識別子が無い: {ascii(err)}"
        )
        assert "missingwheel.whl" in err, (
            f"制御文字を除去したパスが原文どおり残っていない: {ascii(err)}"
        )
        assert_no_control_chars(err, expected_lines=1)


# ---------------------------------------------------------------------------
# Q4: 既定経路からの配線（sdist 側 2 検査が実際に呼ばれること）
# ---------------------------------------------------------------------------


class TestSdistChecksAreWired:
    """純粋検出器を作っただけで既定経路から呼ばれない、という空振りを塞ぐ。

    資材は CLI 節の `_FakeBuild` / `_write_wheel` / `_write_sdist`（合成 outdir に
    wheel と sdist を実際に書き出す）と同型（DC-AS-004）。
    """

    def test_default_route_reports_sdist_exclude_violation(self, capsys):
        names = clean_sdist_namelist(extra=(_SDIST_CLAUDE_PREFIX + _EXCLUDED_SAMPLE,))
        fake = _FakeBuild(clean_wheel_namelist(), names)

        assert main([], build_runner=fake) == EXIT_VIOLATION
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert verify_wheel.SDIST_EXCLUDE_VIOLATION in combined, (
            "sdist EXCLUDE 検査が既定経路から呼ばれていない（種別が出力に現れない）"
        )
        assert _EXCLUDED_SAMPLE in combined
        assert fake.calls, "ビルド実行部が呼ばれていない"

    def test_default_route_reports_control_not_empty(self, capsys):
        fake = _FakeBuild(clean_wheel_namelist(), clean_sdist_members(control_size=7))

        assert main([], build_runner=fake) == EXIT_VIOLATION
        captured = capsys.readouterr()
        assert verify_wheel.CONTROL_NOT_EMPTY in (captured.out + captured.err), (
            "対照サイズ検査が既定経路から呼ばれていない（種別が出力に現れない）"
        )

    def test_sized_clean_sdist_still_passes(self, capsys):
        """fixture 健全性: サイズ付きで組んだ clean な sdist（対照 0 バイト）は PASS。"""
        fake = _FakeBuild(clean_wheel_namelist(), clean_sdist_members())
        assert main([], build_runner=fake) == EXIT_PASS


# ---------------------------------------------------------------------------
# Q5: PASS 行のモード分岐（DC-AM-001）
# ---------------------------------------------------------------------------


class TestPassLineModeSplit:
    """検査していない性質を PASS 行が真と主張しないこと。"""

    def test_default_mode_pass_line_asserts_sdist_checks(self, capsys):
        """既定モードは sdist 側 2 検査を実施しているので PASS 行で主張してよい。"""
        fake = _FakeBuild(clean_wheel_namelist(), clean_sdist_members())
        assert main([], build_runner=fake) == EXIT_PASS

        out = capsys.readouterr().out
        lines = pass_lines(out)
        assert len(lines) == 1, f"PASS 行が 1 行でない: {lines!r}"
        assert "sdist EXCLUDE 混入なし" in lines[0], f"PASS 行: {lines[0]!r}"
        assert "対照サイズ 0" in lines[0], f"PASS 行: {lines[0]!r}"

        measured = [
            line
            for line in out.splitlines()
            if "対照サイズ" in line and not line.startswith(_PASS_LINE_PREFIX)
        ]
        assert len(measured) == 1, (
            f"対照サイズの実測値行（PASS 行とは別行）が 1 行でない: {out!r}"
        )
        assert "0" in measured[0], f"実測値行にサイズが無い: {measured[0]!r}"

    def test_wheel_mode_pass_line_omits_sdist_claims(self, tmp_path, capsys):
        """`--wheel` は sdist を作らないので PASS 行で sdist 検査を主張してはならない。"""
        wheel = _write_wheel(tmp_path / "c3-9.9.9-py3-none-any.whl", clean_wheel_namelist())
        assert main(["--wheel", str(wheel)], build_runner=_ExplodingBuild()) == EXIT_PASS

        out = capsys.readouterr().out
        lines = pass_lines(out)
        assert len(lines) == 1, f"PASS 行が 1 行でない: {lines!r}"
        assert lines[0] == _WHEEL_MODE_PASS_LINE, (
            f"--wheel モードの PASS 行が既存文言から変わっている: {lines[0]!r}"
        )
        assert "sdist EXCLUDE 混入なし" not in lines[0]
        assert "対照サイズ" not in lines[0]
        assert "sdist 側 2 検査は未実施" in out, (
            "sdist 側 2 検査が未実施であることが stdout で明示されていない"
        )


# ---------------------------------------------------------------------------
# Q6: 評価順序（DC-AM-005）
# ---------------------------------------------------------------------------


class TestEvaluationOrder:
    """検証不能の最優先・即 return と、違反同士の合算 1 回出力を observable に凍結する。"""

    def test_control_missing_preempts_violations_and_violations_are_merged(self, capsys):
        wheel_dirty = clean_wheel_namelist(extra=(_WHEEL_TEMPLATE_PREFIX + _EXCLUDED_SAMPLE,))

        # (a) sdist 側の検証不能（CONTROL_MISSING）は最優先で即 return。
        #     この経路では sdist / wheel の違反を 1 件も出力しない。
        sdist_no_control = clean_sdist_namelist(
            with_control=False, extra=(_SDIST_CLAUDE_PREFIX + _EXCLUDED_SAMPLE,)
        )
        assert main([], build_runner=_FakeBuild(wheel_dirty, sdist_no_control)) == (
            EXIT_UNVERIFIABLE
        )
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert captured.err.splitlines()[0].startswith(CONTROL_MISSING), (
            f"stderr 先頭行が CONTROL_MISSING でない: {captured.err!r}"
        )
        for kind in (
            EXCLUDE_VIOLATION,
            verify_wheel.SDIST_EXCLUDE_VIOLATION,
            verify_wheel.CONTROL_NOT_EMPTY,
        ):
            assert kind not in combined, (
                f"検証不能の経路で違反種別 {kind} が出力されている: {combined!r}"
            )

        # (b) 違反同士（sdist・wheel）は合算して 1 回で全件出力・exit 1。
        # R2(e) 追随: `extra` を 3 要素 `(name, size, kind)` へ変更。
        sdist_dirty = clean_sdist_members(
            control_size=3, extra=((_SDIST_CLAUDE_PREFIX + _EXCLUDED_SAMPLE, 0, _KIND_FILE),)
        )
        assert main([], build_runner=_FakeBuild(wheel_dirty, sdist_dirty)) == EXIT_VIOLATION
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        for kind in (
            EXCLUDE_VIOLATION,
            verify_wheel.SDIST_EXCLUDE_VIOLATION,
            verify_wheel.CONTROL_NOT_EMPTY,
        ):
            assert kind in combined, (
                f"合算出力に違反種別 {kind} が含まれない: {combined!r}"
            )
        assert combined.count(_VIOLATION_HEADER) == 1, (
            f"違反出力のヘッダが 1 回でない（合算 1 回出力になっていない）: {combined!r}"
        )


# ===========================================================================
# E 周回 2 是正（plan-report-20260815-003232.md）の Red: R1〜R2
#
# 新 API / 新入力形はすべて `verify_wheel.<name>` の属性アクセスで参照する（DC-AS-001）。
# 既存の一括 import ブロックには足さない。
# ===========================================================================


# ---------------------------------------------------------------------------
# R1: サニタイズ被覆（CR [CR-M-001] / SR [G-2]）
# ---------------------------------------------------------------------------


class TestSanitizeCoverage:
    """R1(a)(c): 除去集合の代表文字を正負両方向で凍結し、行分裂しないことを測る。

    除去（置換ではない）であることは「除去後の文字列が原文の連結と一致する」ことで判定する
    （Q3 と同じ方式）。
    """

    @pytest.mark.parametrize(
        "ch, label",
        _SANITIZE_REMOVED_SAMPLES,
        ids=[label.split(" ")[0] for _ch, label in _SANITIZE_REMOVED_SAMPLES],
    )
    def test_representative_character_is_removed(self, ch, label):
        """負: 正本の全クラスの代表文字は detail から除去される。"""
        detail = "state/tier" + ch + "_selection.json"
        sanitized = verify_wheel._sanitize(detail)
        assert sanitized == "state/tier_selection.json", (
            f"{label} が除去されていない（または置換方式になっている）: {ascii(sanitized)}"
        )

    @pytest.mark.parametrize("text", _SANITIZE_KEPT_SAMPLES)
    def test_ordinary_text_is_left_untouched(self, text):
        """正: ASCII・日本語・単一コードポイント絵文字は 1 文字も削られない。

        残存側の題材を ZWJ 連結絵文字にしないのは、ZWJ（U+200D）自体が正本の除去対象で
        あり「残存すべき」という前提が成り立たないため（R1(a)）。
        """
        assert verify_wheel._sanitize(text) == text, (
            f"残存すべき文字列が削られている: {ascii(verify_wheel._sanitize(text))}"
        )

    def test_sanitized_detail_never_splits_into_multiple_lines(self):
        """(c) 除去後の detail は `str.splitlines()` で行分裂しない。

        題材は「BMP 内で `splitlines` が行境界とみなす全文字」を機械導出したもの
        （手で列挙しないので、列挙漏れのぶんだけ検査が甘くなる形にならない）。
        """
        detail = "head" + _LINE_BREAKING_CHARS + "tail"
        sanitized = verify_wheel._sanitize(detail)
        assert sanitized == "headtail", (
            f"行境界文字が除去されていない: {ascii(sanitized)}"
        )
        assert sanitized.splitlines() == ["headtail"], (
            f"除去後の detail が複数行に分裂している: {ascii(sanitized)}"
        )


class TestSanitizeMatchesCanonicalSource:
    """R1(b): 除去集合が正本 `_TERMINAL_SANITIZE_RE` と U+0000–U+FFFF 全域で一致する。

    `scripts/` は配布物 hooks に依存させない方針のため実装側は集合を**複製**する。
    `.dev/hooks/_sync_check.py` の SYNC_GROUP はこの複製対を監視しないので、本突合が
    唯一の drift 検知手段になる。

    **比較ドメインは U+0000–U+FFFF（BMP）**。BMP 外は現行正本に対象が 1 つも無いため
    走査対象外とする。したがって正本が将来 BMP 外へ広がった場合、本突合はその追加分を
    検知しない（そのときは `_SANITIZE_COMPARE_DOMAIN` の拡張が必要になる）。
    """

    def test_canonical_source_is_loaded_and_non_trivial(self):
        """突合オラクル自身の健全性（正本の読み込みが壊れていないこと）。"""
        canonical = canonical_bmp_removal_set()
        not_covered = [
            label for ch, label in _SANITIZE_REMOVED_SAMPLES if ord(ch) not in canonical
        ]
        assert not not_covered, (
            f"正本が代表文字を除去対象にしていない（読み込みが壊れている）: {not_covered}"
        )
        contaminated = [
            ascii(text)
            for text in _SANITIZE_KEPT_SAMPLES
            if any(ord(c) in canonical for c in text)
        ]
        assert not contaminated, (
            f"残存側の題材に正本の除去対象が混ざっている: {contaminated}"
        )

    def test_removal_set_matches_canonical_over_the_whole_bmp(self):
        """U+0000–U+FFFF の全コードポイントで「除去するか」の真偽が一致する。"""
        canonical = canonical_bmp_removal_set()
        script_removed = {
            cp for cp in _SANITIZE_COMPARE_DOMAIN if verify_wheel._sanitize(chr(cp)) == ""
        }
        only_canonical = sorted(canonical - script_removed)
        only_script = sorted(script_removed - canonical)
        assert not only_canonical and not only_script, (
            "`_sanitize` の除去集合が正本 `_hook_utils._TERMINAL_SANITIZE_RE` と一致しない"
            f"（正本のみ {len(only_canonical)} 件: "
            f"{[f'U+{cp:04X}' for cp in only_canonical[:20]]} / "
            f"実装のみ {len(only_script)} 件: "
            f"{[f'U+{cp:04X}' for cp in only_script[:20]]}）"
        )


# ---------------------------------------------------------------------------
# R2: sdist member 種別の射程（SR [G-1]）
# ---------------------------------------------------------------------------


class TestMemberFixtureSanity:
    """合成 member の種別が狙いどおりに作られていることの番人（R2(f)）。"""

    def test_link_fixture_is_a_tar_symlink_member(self, tmp_path):
        """link member は `TarInfo.type = tarfile.SYMTYPE` の直接指定で作る。

        FS 上に実体 symlink を作らない（Windows では symlink 作成が権限依存になるため）。
        この理由により `skipif` による回避も行わない。
        """
        path = _write_sdist(
            tmp_path / "c3-9.9.9.tar.gz",
            [(_SDIST_CLAUDE_PREFIX + _EXCLUDED_SAMPLE, 0, _KIND_LINK)],
        )
        with tarfile.open(path) as tf:
            info = tf.getmembers()[0]
        assert info.type == tarfile.SYMTYPE and info.issym(), (
            f"link fixture が tar symlink member になっていない: {info.type!r}"
        )

    def test_hardlink_fixture_is_a_tar_hardlink_member(self, tmp_path):
        """hardlink member は `TarInfo.type = tarfile.LNKTYPE` の直接指定で作る。"""
        path = _write_sdist(
            tmp_path / "c3-9.9.9.tar.gz",
            [(_SDIST_CLAUDE_PREFIX + _EXCLUDED_SAMPLE, 0, _TAR_SPEC_HARDLINK)],
        )
        with tarfile.open(path) as tf:
            info = tf.getmembers()[0]
        assert info.type == tarfile.LNKTYPE and info.islnk()

    def test_dir_fixture_loses_its_trailing_slash_when_read_back(self, tmp_path):
        """R2(a) の根拠の実測: tar は dir member 名の末尾スラッシュを剥がす。

        そのため種別を見ずにパス名検査へ入れると、ディレクトリが EXCLUDE 対象パスとして
        誤検出される（末尾 `/` による除外〔P7〕が効かない）。
        """
        path = _write_sdist(
            tmp_path / "c3-9.9.9.tar.gz",
            [(_SDIST_CLAUDE_PREFIX + _EXCLUDED_DIR_SAMPLE, 0, _KIND_DIR)],
        )
        with tarfile.open(path) as tf:
            info = tf.getmembers()[0]
        assert info.isdir()
        assert info.name == _SDIST_CLAUDE_PREFIX + _EXCLUDED_DIR_SAMPLE, (
            f"dir member 名の実測が前提と違う: {info.name!r}"
        )
        assert not info.name.endswith("/"), (
            "末尾スラッシュが残っている（R2(a) の前提が成り立たない）"
        )

    def test_fifo_fixture_matches_none_of_the_four_predicates(self, tmp_path):
        """未知種別の題材が本当に 4 述語すべて False であること。"""
        path = _write_sdist(
            tmp_path / "c3-9.9.9.tar.gz",
            [(_SDIST_CLAUDE_PREFIX + "tmp/pipe", 0, _TAR_SPEC_FIFO)],
        )
        with tarfile.open(path) as tf:
            info = tf.getmembers()[0]
        assert not (info.isfile() or info.issym() or info.islnk() or info.isdir()), (
            "fifo fixture が既知の 4 述語のいずれかに該当している"
        )


class TestSdistMemberKinds:
    """`read_sdist_members` は 3 要素 `(name, size, kind)` を返し、未知種別で fail-closed。"""

    def test_members_carry_kind_derived_from_tar_predicates(self, tmp_path):
        """kind は tarfile の述語から導出する（isfile→file / issym・islnk→link / isdir→dir）。"""
        path = _write_sdist(
            tmp_path / "c3-9.9.9.tar.gz",
            [
                (_SDIST_CLAUDE_PREFIX + "CLAUDE.md", 3, _KIND_FILE),
                (_SDIST_CLAUDE_PREFIX + _EXCLUDED_SAMPLE, 0, _KIND_LINK),
                (_SDIST_CLAUDE_PREFIX + _EXCLUDED_DIR_SAMPLE, 0, _KIND_DIR),
                (_SDIST_CLAUDE_PREFIX + "logs/hard.log", 0, _TAR_SPEC_HARDLINK),
            ],
        )
        members, reason = verify_wheel.read_sdist_members(str(path))
        assert reason is None, f"読める sdist で原因識別子が返っている: {reason!r}"
        assert members == [
            (_SDIST_CLAUDE_PREFIX + "CLAUDE.md", 3, "file"),
            (_SDIST_CLAUDE_PREFIX + _EXCLUDED_SAMPLE, 0, "link"),
            (_SDIST_CLAUDE_PREFIX + _EXCLUDED_DIR_SAMPLE, 0, "dir"),
            (_SDIST_CLAUDE_PREFIX + "logs/hard.log", 0, "link"),
        ], (
            "3 要素 (name, size, kind) で全種別が返っていない"
            f"（link / dir が isfile() フィルタで落ちていないか）: {members!r}"
        )

    def test_unknown_member_type_is_fail_closed(self, tmp_path):
        """4 述語すべて False の member（FIFO・デバイス等）は LAYOUT_ANOMALY（検証不能）。

        種別フィルタで静かに検査対象から外すと SR [G-1] と同じクラスの死角を新設することに
        なるため、fail-open にしない。
        """
        path = _write_sdist(
            tmp_path / "c3-9.9.9.tar.gz",
            [
                (_SDIST_CLAUDE_PREFIX + "CLAUDE.md", 3, _KIND_FILE),
                (_SDIST_CLAUDE_PREFIX + "tmp/pipe", 0, _TAR_SPEC_FIFO),
            ],
        )
        members, reason = verify_wheel.read_sdist_members(str(path))
        assert members is None, (
            f"未知種別を含む sdist で member 一覧が返っている（fail-open）: {members!r}"
        )
        assert reason == LAYOUT_ANOMALY, (
            f"未知種別が LAYOUT_ANOMALY として報告されない: {reason!r}"
        )

    def test_unreadable_tar_is_still_zip_read_error(self, tmp_path):
        """負の対照: tar として読めない場合は従来どおり ZIP_READ_ERROR。"""
        broken = tmp_path / "broken.tar.gz"
        broken.write_bytes(b"not a tarball")
        members, reason = verify_wheel.read_sdist_members(str(broken))
        assert members is None
        assert reason == ZIP_READ_ERROR


class TestControlChecksTakeMemberKinds:
    """R2(c)(e): 対照系 2 関数は 3 要素を受け、対照の成立・母数は **file member のみ**。

    対照は「実ファイルとして配布される内容の空性」を保証するものなので、同名の link / dir
    member は対照ではない（＝対照は不在）。
    """

    @pytest.mark.parametrize("kind", [_KIND_LINK, _KIND_DIR])
    def test_non_file_control_is_control_missing(self, kind):
        """(c) 対照名の member が file でなければ CONTROL_MISSING。"""
        as_non_file = clean_sdist_members(
            with_control=False,
            extra=(
                (_SDIST_CLAUDE_PREFIX + _CONTROL_RELPATH, 0, kind),
                (_SDIST_CLAUDE_PREFIX + _EXCLUDED_SAMPLE, 0, _KIND_FILE),
            ),
        )
        assert find_sdist_control_reason(as_non_file) == CONTROL_MISSING, (
            f"kind={kind} の対照が「存在する」と数えられている"
        )

        as_file = clean_sdist_members(
            with_control=False,
            extra=(
                (_SDIST_CLAUDE_PREFIX + _CONTROL_RELPATH, 0, _KIND_FILE),
                (_SDIST_CLAUDE_PREFIX + _EXCLUDED_SAMPLE, 0, _KIND_FILE),
            ),
        )
        assert find_sdist_control_reason(as_file) is None, (
            "前提: 同じ member 集合で kind だけ file なら対照は成立する"
            "（kind 以外の理由で CONTROL_MISSING になっていない）"
        )

    def test_candidate_count_source_is_file_members_only(self):
        """(d) 候補件数の母数は file member のみ（link は数えない）。

        link は内容を配布しないため「実フィルタが実ファイルを落とした」観測にならない。
        名前検査（file + link）との非対称はこの根拠による。
        """

        def only_the_sample(rel_posix: str) -> bool:
            """`_EXCLUDED_SAMPLE` だけを除外対象とみなす注入 should_skip。"""
            return rel_posix == _EXCLUDED_SAMPLE

        base = clean_sdist_members(with_control=False)
        control = (_SDIST_CLAUDE_PREFIX + _CONTROL_RELPATH, 0, _KIND_FILE)
        as_file = base + [control, (_SDIST_CLAUDE_PREFIX + _EXCLUDED_SAMPLE, 0, _KIND_FILE)]
        as_link = base + [control, (_SDIST_CLAUDE_PREFIX + _EXCLUDED_SAMPLE, 0, _KIND_LINK)]

        assert find_sdist_control_reason(as_file, should_skip=only_the_sample) is None, (
            "前提: 唯一の候補が file member なら母数 1 件で対照は成立する"
        )
        assert (
            find_sdist_control_reason(as_link, should_skip=only_the_sample) == CONTROL_MISSING
        ), (
            "唯一の候補が link member のとき母数が 1 件と数えられている"
            "（母数が file のみになっていない）"
        )

    @pytest.mark.parametrize("kind", [_KIND_LINK, _KIND_DIR])
    def test_size_check_ignores_non_file_control(self, kind):
        """(c) 系: 対照名の link / dir member はサイズ検査の対象にしない。

        対照ではないので「不在」として空リストを返す（不在の報告は
        `find_sdist_control_reason` の `CONTROL_MISSING` の担当・二重報告しない）。
        """
        members = clean_sdist_members(
            with_control=False,
            extra=((_SDIST_CLAUDE_PREFIX + _CONTROL_RELPATH, 999, kind),),
        )
        assert verify_wheel.find_control_size_violations(members) == [], (
            f"kind={kind} の同名 member が対照として扱われている"
        )


class TestMemberKindScopeViaDefaultRoute:
    """R2(a)(b)(d): 既定経路が member 種別で検査対象を振り分けること（配線の実観測）。"""

    def test_excluded_link_is_reported_and_excluded_dir_is_not(self, capsys):
        """(a)(b)(d) を 1 入力で同時に観測する。

        - (b) EXCLUDE 対象名の **link** member は `SDIST_EXCLUDE_VIOLATION`
        - (a) EXCLUDE 対象名の **dir** member は全検査の対象外（出力に現れない）
        - (d) 候補件数の母数は **file のみ**（注入対照の 1 件だけ）
        """
        members = clean_sdist_members(
            extra=(
                (_SDIST_CLAUDE_PREFIX + _EXCLUDED_SAMPLE, 0, _KIND_LINK),
                (_SDIST_CLAUDE_PREFIX + _EXCLUDED_DIR_SAMPLE, 0, _KIND_DIR),
            )
        )
        fake = _FakeBuild(clean_wheel_namelist(), members)

        assert main([], build_runner=fake) == EXIT_VIOLATION
        captured = capsys.readouterr()
        combined = captured.out + captured.err

        assert verify_wheel.SDIST_EXCLUDE_VIOLATION in combined, (
            "EXCLUDE 対象名の link member が検査を素通りしている（SR [G-1] の死角）"
        )
        assert _EXCLUDED_SAMPLE in combined, (
            f"違反の詳細に link member 名が含まれない: {combined!r}"
        )
        assert _EXCLUDED_DIR_SAMPLE not in combined, (
            "dir member が検査対象に入り誤検出されている"
            f"（tar が末尾スラッシュを剥がすため）: {combined!r}"
        )

        counted = [
            line for line in captured.out.splitlines() if "EXCLUDE 実効候補件数" in line
        ]
        assert len(counted) == 1, f"候補件数の行が 1 行でない: {captured.out!r}"
        assert counted[0].rstrip().endswith(": 1"), (
            "候補件数の母数に file 以外の member が数えられている"
            f"（注入対照 1 件のはず）: {counted[0]!r}"
        )

    def test_unknown_member_type_makes_the_run_unverifiable(self, capsys):
        """未知種別（FIFO 等）を含む sdist は exit 3・stderr 先頭行に LAYOUT_ANOMALY。"""
        members = clean_sdist_members(
            extra=((_SDIST_CLAUDE_PREFIX + "tmp/pipe", 0, _TAR_SPEC_FIFO),)
        )
        fake = _FakeBuild(clean_wheel_namelist(), members)

        assert main([], build_runner=fake) == EXIT_UNVERIFIABLE
        err = capsys.readouterr().err
        first_line = err.splitlines()[0] if err.splitlines() else ""
        assert first_line.startswith(LAYOUT_ANOMALY), (
            f"未知種別の member が fail-closed になっていない: {err!r}"
        )
