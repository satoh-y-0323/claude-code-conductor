"""tests/test_verify_wheel.py

`scripts/verify_wheel.py`（リリース前 wheel 実体検証・**未実装**）の Red フェーズテスト。

Red フェーズの期待失敗: `scripts/verify_wheel.py` が存在しないため、本ファイルは
モジュール import の時点で `ModuleNotFoundError` になり収集エラーで全件失敗する
（`tests/test_check_deletions.py` の Red と同型。構文エラー・タイポではなく
「対象が不在」であることが失敗理由）。

## 測る性質（plan-report-20260814-195906.md の契約 P1〜P8）

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
  … `(違反種別, 該当エントリ or パターン)` の並び。EXCLUDE / KEEP / FR-2 / 対照混入 /
  トップレベル逸脱の 5 種すべてをここから観測する。対照混入（`CONTROL_LEAKED`）の判定は
  `should_skip` 引数に依存してはならない（P8(b) が実測で固定する）
- `find_unverifiable(namelist) -> str | None` … `TEMPLATE_EMPTY` / `LAYOUT_ANOMALY` / None
- `count_exclude_candidates(names, should_skip=...) -> int` … sdist listing 中の
  `.claude/` 相対で should_skip True の件数
- `find_sdist_control_reason(names, should_skip=...) -> str | None` … `CONTROL_MISSING` / None
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

## 射程外（confirm へ受け渡す観測）

- `WHEEL_NOT_FOUND` の **CLI 層**での実測（`--wheel` に存在しないパスを与えて exit 3＋
  stderr 先頭行）は confirm タスクの契約。本ファイルでは検出器層
  （`select_single_artifact` の 0 件）でのみ観測する
- 実ビルド（`python -m build`）を伴う end-to-end の exit 0・改変 wheel での種別観測も confirm 側
"""

from __future__ import annotations

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

from c3 import _excludes  # noqa: E402
from c3._excludes import KEEP_PATTERNS, should_skip  # noqa: E402
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

# `.claude/` 相対で should_skip False の通常配布ファイル（fixture の素材）
_ORDINARY_RELPATHS = (
    "CLAUDE.md",
    "agents/planner.md",
    "hooks/stop.py",
    "skills/start/SKILL.md",
    "docs/config-policy.md",
)


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


def kinds(violations) -> set[str]:
    """`find_violations` の戻り値から違反種別の集合を取り出す。"""
    return {v[0] for v in violations}


def details(violations) -> list[str]:
    """`find_violations` の戻り値から該当エントリ/パターンを取り出す。"""
    return [v[1] for v in violations]


def always_false(rel_posix: str) -> bool:
    """常に False を返す should_skip（注入対照の独立性検証用・P8(b)）。"""
    return False


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
        """正: sdist listing に対照が在れば検証可能（None）。"""
        assert find_sdist_control_reason(clean_sdist_namelist()) is None

    def test_sdist_without_control_is_control_missing(self):
        """負: 対照が sdist に無ければ CONTROL_MISSING（検証不能・対照喪失）。

        候補件数だけは 1 件以上ある listing を使い、「対照の不在」が理由であることを
        (c) の候補件数 0 と分離する。
        """
        names = clean_sdist_namelist(
            with_control=False,
            extra=(_SDIST_CLAUDE_PREFIX + _EXCLUDED_SAMPLE,),
        )
        assert count_exclude_candidates(names) >= 1
        assert find_sdist_control_reason(names) == CONTROL_MISSING

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
        """
        names = clean_sdist_namelist()
        assert count_exclude_candidates(names, should_skip=always_false) == 0
        assert find_sdist_control_reason(names, should_skip=always_false) == CONTROL_MISSING


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

    def test_violation_kinds_are_exactly_five(self):
        assert set(VIOLATION_KINDS) == {
            "EXCLUDE_VIOLATION",
            "KEEP_MISSING",
            "FR2_VIOLATION",
            "CONTROL_LEAKED",
            "UNEXPECTED_TOPLEVEL",
        }
        assert len(set(VIOLATION_KINDS)) == len(VIOLATION_KINDS)
        assert [EXCLUDE_VIOLATION, KEEP_MISSING, FR2_VIOLATION, CONTROL_LEAKED, UNEXPECTED_TOPLEVEL] == [
            "EXCLUDE_VIOLATION",
            "KEEP_MISSING",
            "FR2_VIOLATION",
            "CONTROL_LEAKED",
            "UNEXPECTED_TOPLEVEL",
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


def _write_sdist(path: Path, names) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for name in names:
            info = tarfile.TarInfo(name)
            info.size = 0
            tf.addfile(info, io.BytesIO(b""))
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
