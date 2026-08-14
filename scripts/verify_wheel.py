"""scripts/verify_wheel.py

リリース前 wheel 実体検証（`/CLAUDE.md` §3・§6 手順 5 の機械化）。

使い方:
  python scripts/verify_wheel.py                既定: 公開等価ビルド（sdist 経由）を実行して検証
  python scripts/verify_wheel.py --wheel PATH   既に手元にある wheel を検証（ビルドしない）

配布対象外（`scripts/` は wheel / sdist 非収録）。CI の `wheel-check` job と
リリース前のローカル実行の両方から同じ経路で呼ばれる。

## 検証する性質（FR-1 / FR-2 / FR-3）

wheel namelist のうち `_template/.claude/` 境界より後ろを `.claude/` 相対パスとして扱い、

1. EXCLUDE 違反: `should_skip()` True のエントリが 1 件でもあれば違反
2. KEEP 欠落: `KEEP_PATTERNS` のパターンに一致するエントリが 1 件も無ければ違反
3. FR-2: `breaking-changes.txt` が在り `state/c3_version.txt` が無いこと
4. 注入対照の不混入: `state/setup_done.flag` が wheel に無いこと
   （**`should_skip` を経由しない名前一致**で判定する。SSOT〔`_excludes.py`〕と
   フィルタ側複製〔`hatch_build.py`〕が同時に劣化しても、この検査だけは生き残る）
5. トップレベル逸脱: wheel のトップレベルが `c3` と `*.dist-info` 以外を含まないこと
   （`pyproject.toml` の force-include を target 非依存セクションに書いた場合の混入を捕まえる）

期待値の正本（SSOT）は `src/c3/_excludes.py`。本スクリプトにパターンを複製しない。

## 正の対照（空の緑の防止）

sdist は VCS ignore を尊重するため、`.claude/` 配下に `should_skip` True の実効候補は
自然には 1 件も存在しない（実測）。そこで `pyproject.toml` の
`[tool.hatch.build.targets.sdist.force-include]` で `.claude/state/setup_done.flag` を
sdist にだけ注入し、

- sdist に対照が**在る**こと（無ければ検証不能 `CONTROL_MISSING`）
- sdist 側の候補件数が 1 件以上あること（0 件なら `CONTROL_MISSING`）
- wheel に対照が**無い**こと（在れば違反 `CONTROL_LEAKED`）

の三点で「実フィルタが毎回 1 件を実際に落とした」ことを実証する。

## exit code

| code | 意味 |
|---|---|
| 0 | 検証 PASS |
| 1 | 違反（配布物の退行）。種別は `VIOLATION_KINDS` の 5 種 |
| 3 | 検証不能（検査インフラの故障）。原因識別子は `UNVERIFIABLE_REASONS` の 7 種 |

2 は使わない（argparse の usage error と衝突するため）。

`state/c3_version.txt` 不在検査が実 wheel 層で恒真である旨など、検査の非対称性は
`tests/test_verify_wheel.py` のモジュール docstring に記録している。
"""

from __future__ import annotations

import sys
from pathlib import Path

# c3-src-bootstrap: 配布元 repo の src/ を site-packages より優先する。
# scripts/check_deletions.py:24-26 と同型。これを欠くと期待値が site-packages 側の
# `_excludes` に解決し「repo の意図と wheel 実体の突合」という検証の意味が壊れる。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import argparse
import fnmatch
import importlib.util
import shutil
import subprocess
import tarfile
import tempfile
import zipfile

from c3 import _excludes

# stdout/stderr reconfigure for Windows CI compatibility (cp1252 environment)
# stdin は未使用なため reconfigure 不要（本スクリプトは stdin を読まない）
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError, OSError):
    pass

try:
    sys.stderr.reconfigure(encoding='utf-8')
except (AttributeError, ValueError, OSError):
    pass


# ---------------------------------------------------------------------------
# 定数（テストが凍結する literal）
# ---------------------------------------------------------------------------

EXIT_PASS = 0
EXIT_VIOLATION = 1
EXIT_UNVERIFIABLE = 3

# exit 1: 違反種別（配布物の退行）
EXCLUDE_VIOLATION = "EXCLUDE_VIOLATION"
KEEP_MISSING = "KEEP_MISSING"
FR2_VIOLATION = "FR2_VIOLATION"
CONTROL_LEAKED = "CONTROL_LEAKED"
UNEXPECTED_TOPLEVEL = "UNEXPECTED_TOPLEVEL"

VIOLATION_KINDS: tuple[str, ...] = (
    EXCLUDE_VIOLATION,
    KEEP_MISSING,
    FR2_VIOLATION,
    CONTROL_LEAKED,
    UNEXPECTED_TOPLEVEL,
)

# exit 3: 原因識別子（検査インフラの故障）
BUILD_TOOL_MISSING = "BUILD_TOOL_MISSING"
BUILD_FAILED = "BUILD_FAILED"
WHEEL_NOT_FOUND = "WHEEL_NOT_FOUND"
ZIP_READ_ERROR = "ZIP_READ_ERROR"
TEMPLATE_EMPTY = "TEMPLATE_EMPTY"
LAYOUT_ANOMALY = "LAYOUT_ANOMALY"
CONTROL_MISSING = "CONTROL_MISSING"

UNVERIFIABLE_REASONS: tuple[str, ...] = (
    BUILD_TOOL_MISSING,
    BUILD_FAILED,
    WHEEL_NOT_FOUND,
    ZIP_READ_ERROR,
    TEMPLATE_EMPTY,
    LAYOUT_ANOMALY,
    CONTROL_MISSING,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent

# wheel 内の `.claude/` 実体の境界。`c3/_template/.claude/` を直書きせず境界で切り出す
# （パッケージ配置が変わっても壊れないようにするため）。
_TEMPLATE_BOUNDARY = "_template/.claude/"

# sdist tarball のルート直下 `.claude/` ディレクトリ名
_SDIST_CLAUDE_DIRNAME = ".claude"

# 注入対照（sdist に在り wheel には無いべきファイル）
_CONTROL_RELPATH = "state/setup_done.flag"

# FR-2 の明示検査対象
_BREAKING_CHANGES_RELPATH = "breaking-changes.txt"
_C3_VERSION_RELPATH = "state/c3_version.txt"

# wheel のトップレベルとして既知のもの
_KNOWN_TOPLEVEL_NAMES: tuple[str, ...] = ("c3",)
_KNOWN_TOPLEVEL_PATTERNS: tuple[str, ...] = ("*.dist-info",)

# 成果物の glob パターン
_WHEEL_GLOB = "*.whl"
_SDIST_GLOB = "*.tar.gz"


# ---------------------------------------------------------------------------
# 入力正規化
# ---------------------------------------------------------------------------


def _normalize(name: str) -> str:
    """アーカイブ内エントリ名を POSIX 形へ正規化する。"""
    return name.replace("\\", "/")


def _template_relpaths(namelist) -> list[str]:
    """wheel namelist から `.claude/` 相対パスを取り出す。

    - ディレクトリエントリ（末尾 `/`）は判定対象外
      （`fnmatch.fnmatchcase("reports/", "reports/*")` は True になるため、
      正規化を欠くと正常な wheel でも EXCLUDE 違反として誤検出される）
    - 境界を含まないエントリ（`c3/cli.py` 等のパッケージ実体）は対象外
    """
    rels: list[str] = []
    for name in namelist:
        posix = _normalize(name)
        if posix.endswith("/"):
            continue
        index = posix.find(_TEMPLATE_BOUNDARY)
        if index < 0:
            continue
        rel = posix[index + len(_TEMPLATE_BOUNDARY):]
        if not rel:
            continue
        rels.append(rel)
    return rels


def _sdist_claude_relpaths(names) -> list[str]:
    """sdist の member 名から、ルート直下 `.claude/` 配下の相対パスを取り出す。

    sdist の member は `<name>-<version>/.claude/<rel>` の形。ディレクトリエントリは
    wheel 側と同じ理由で判定対象外にする。
    """
    rels: list[str] = []
    for name in names:
        posix = _normalize(name)
        if posix.endswith("/"):
            continue
        parts = posix.split("/", 2)
        if len(parts) != 3 or parts[1] != _SDIST_CLAUDE_DIRNAME:
            continue
        rel = parts[2]
        if not rel:
            continue
        rels.append(rel)
    return rels


def _unknown_toplevels(namelist) -> list[str]:
    """wheel namelist の既知集合外のトップレベル名を（重複除去して）返す。"""
    unknown: list[str] = []
    seen: set[str] = set()
    for name in namelist:
        posix = _normalize(name)
        if not posix:
            continue
        top = posix.split("/", 1)[0]
        if not top or top in seen:
            continue
        seen.add(top)
        if top in _KNOWN_TOPLEVEL_NAMES:
            continue
        if any(fnmatch.fnmatchcase(top, p) for p in _KNOWN_TOPLEVEL_PATTERNS):
            continue
        unknown.append(top)
    return unknown


# ---------------------------------------------------------------------------
# 純粋検出器
# ---------------------------------------------------------------------------


def find_violations(namelist, should_skip=_excludes.should_skip) -> list[tuple[str, str]]:
    """wheel namelist から違反（配布物の退行）を列挙する。

    Args:
        namelist: wheel 内エントリ名のリスト
        should_skip: 除外判定関数。既定は SSOT の `c3._excludes.should_skip`。
            テスト・診断からは**引数差し替え**で注入する（monkeypatch を使わない）。

    Returns:
        `(違反種別, 該当エントリ or パターン)` のリスト。違反が無ければ空リスト。

    注: 注入対照の混入検査（`CONTROL_LEAKED`）は `should_skip` 引数を参照しない
    独立した名前一致で判定する。SSOT とフィルタ側複製が同時に劣化しても、
    この検査だけは生き残らせるため。
    """
    violations: list[tuple[str, str]] = []
    rels = _template_relpaths(namelist)
    rel_set = set(rels)

    # 1. EXCLUDE 違反（SSOT の判定関数をそのまま使う）
    for rel in rels:
        if should_skip(rel):
            violations.append((EXCLUDE_VIOLATION, rel))

    # 2. KEEP 欠落（存在検証）
    for pattern in _excludes.KEEP_PATTERNS:
        if not any(fnmatch.fnmatchcase(rel, pattern) for rel in rels):
            violations.append((KEEP_MISSING, pattern))

    # 3. FR-2 明示検査（1・2 と重なるが、退行検知の意図を独立の名前付き検査として残す）
    if _BREAKING_CHANGES_RELPATH not in rel_set:
        violations.append((FR2_VIOLATION, f"{_BREAKING_CHANGES_RELPATH} が wheel に無い"))
    if _C3_VERSION_RELPATH in rel_set:
        violations.append((FR2_VIOLATION, f"{_C3_VERSION_RELPATH} が wheel に混入している"))

    # 4. 注入対照の混入（should_skip 非依存の名前一致）
    if _CONTROL_RELPATH in rel_set:
        violations.append((CONTROL_LEAKED, _CONTROL_RELPATH))

    # 5. トップレベル逸脱
    for top in _unknown_toplevels(namelist):
        violations.append((UNEXPECTED_TOPLEVEL, top))

    return violations


def find_unverifiable(namelist) -> str | None:
    """検証不能（検査インフラの故障）を検出する。違反とは区別する。

    Returns:
        `TEMPLATE_EMPTY` / `LAYOUT_ANOMALY` / None
    """
    for name in namelist:
        posix = _normalize(name)
        if posix.endswith("/"):
            continue
        if posix.count(_TEMPLATE_BOUNDARY) > 1:
            return LAYOUT_ANOMALY

    rels = _template_relpaths(namelist)
    for rel in rels:
        # 境界後がパスとして不正（先頭スラッシュ・空セグメント）
        if rel.startswith("/") or "//" in rel:
            return LAYOUT_ANOMALY

    if not rels:
        # 「違反 0 件」と「1 件も走査していない」を区別する（空の緑の防止）
        return TEMPLATE_EMPTY
    return None


def count_exclude_candidates(names, should_skip=_excludes.should_skip) -> int:
    """sdist の `.claude/` 配下で `should_skip` True になるファイル件数を返す。

    実フィルタが見る入力（sdist）そのものを数えるため、作業ツリーの dirty さに
    依存しない。0 件は注入対照の退行を意味する（`find_sdist_control_reason` 参照）。
    """
    return sum(1 for rel in _sdist_claude_relpaths(names) if should_skip(rel))


def find_sdist_control_reason(names, should_skip=_excludes.should_skip) -> str | None:
    """注入対照が sdist 側で成立しているかを判定する。

    Returns:
        `CONTROL_MISSING`（対照が sdist に不在 / 候補件数 0）または None
    """
    rels = set(_sdist_claude_relpaths(names))
    if _CONTROL_RELPATH not in rels:
        return CONTROL_MISSING
    if count_exclude_candidates(names, should_skip=should_skip) == 0:
        return CONTROL_MISSING
    return None


# ---------------------------------------------------------------------------
# 外殻（成果物の取得・読み取り・ビルド）
# ---------------------------------------------------------------------------


def select_single_artifact(outdir, pattern) -> tuple[str | None, str | None]:
    """outdir から `pattern` に一致する成果物をちょうど 1 件選ぶ。

    0 件・複数件はいずれも fail-loud（検証不能）。原因識別子の割り当ては:

    - `*.whl` が 0 件 → `WHEEL_NOT_FOUND`（ADR-3 改訂 3）
    - それ以外の「ちょうど 1 件でない」→ `BUILD_FAILED`
      （一時 outdir は毎回新規作成するため、期待どおりの成果物が 1 件ずつ出ない状態は
      ビルドが期待どおりに終わっていないことを意味する）

    Returns:
        `(パス, 原因識別子)`。成功時は `(パス, None)`、失敗時は `(None, 識別子)`。
    """
    matches = sorted(str(p) for p in Path(outdir).glob(pattern))
    if len(matches) == 1:
        return matches[0], None
    if not matches and pattern.endswith(".whl"):
        return None, WHEEL_NOT_FOUND
    return None, BUILD_FAILED


def read_namelist(path) -> tuple[list[str] | None, str | None]:
    """wheel（zip）のエントリ名一覧を読む。

    Returns:
        `(namelist, 原因識別子)`。読めなければ `(None, ZIP_READ_ERROR)`。
    """
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.namelist(), None
    except (OSError, ValueError, zipfile.BadZipFile):
        return None, ZIP_READ_ERROR


def read_sdist_names(path) -> tuple[list[str] | None, str | None]:
    """sdist（tar.gz）のファイル member 名一覧を読む。

    Returns:
        `(names, 原因識別子)`。読めなければ `(None, ZIP_READ_ERROR)`
        （アーカイブ読取失敗を wheel 側と同じ識別子で表す）。
    """
    try:
        with tarfile.open(path) as tf:
            return [m.name for m in tf.getmembers() if m.isfile()], None
    except (OSError, ValueError, tarfile.TarError):
        return None, ZIP_READ_ERROR


def run_build(
    outdir,
    runner=subprocess.run,
    find_spec=importlib.util.find_spec,
) -> str | None:
    """公開等価のビルド（sdist 経由）を実行する。

    argv は `[sys.executable, "-m", "build", "--outdir", <outdir>]`。
    `publish.yml` の `python -m build`（引数なし＝sdist を作りその sdist から wheel を作る）
    とビルド入力まで一致させる。`--wheel` を付けるとソースツリー直接ビルドになり、
    公開物と別対象の検証になるため付けない。

    PyPA `build` の導入有無の判定は**この関数の中**に置く。`main` の前段に置くと、
    `build_runner` を差し替えた呼び出しまで実行環境の build 導入有無に依存する
    （CI の pytest matrix は build を入れない）。

    Returns:
        成功時 None / 失敗時は原因識別子（`BUILD_TOOL_MISSING` / `BUILD_FAILED`）
    """
    try:
        spec = find_spec("build")
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return BUILD_TOOL_MISSING

    argv = [sys.executable, "-m", "build", "--outdir", str(outdir)]
    try:
        completed = runner(argv, cwd=str(_REPO_ROOT))
    except OSError:
        return BUILD_FAILED

    if getattr(completed, "returncode", 1) != 0:
        return BUILD_FAILED
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _unverifiable(reason: str, detail: str) -> int:
    """検証不能を報告する（stderr 先頭行に ASCII の原因識別子を必ず出す）。"""
    print(f"{reason}: {detail}", file=sys.stderr)
    if reason == BUILD_TOOL_MISSING:
        print(
            "PyPA build が未導入です。`python -m pip install build` で導入してください。",
            file=sys.stderr,
        )
    return EXIT_UNVERIFIABLE


def _check_wheel(namelist) -> int:
    """wheel namelist を検査して exit code を返す。"""
    reason = find_unverifiable(namelist)
    if reason is not None:
        return _unverifiable(reason, "wheel の `_template/.claude/` 構造が検査できない")

    violations = find_violations(namelist)
    if violations:
        print("配布物の退行を検出しました:", file=sys.stderr)
        for kind, detail in violations:
            print(f"  {kind}: {detail}", file=sys.stderr)
        return EXIT_VIOLATION

    print("検証 PASS: EXCLUDE 混入なし・KEEP 全件あり・FR-2 充足・注入対照は wheel に不在")
    return EXIT_PASS


def _verify_existing_wheel(wheel_path: str) -> int:
    """`--wheel` モード: 既存 wheel を検証する（ビルドしない）。"""
    if not Path(wheel_path).is_file():
        return _unverifiable(WHEEL_NOT_FOUND, f"--wheel に指定されたパスが存在しません: {wheel_path}")

    print(f"検証対象 wheel: {Path(wheel_path).name}")
    print(
        "--wheel モードは sdist を作らないため、注入対照の検証は wheel 側"
        "（`state/setup_done.flag` の不在確認）のみ実施します"
        "（sdist 側の存在確認・候補件数の観測は既定モード限定）。"
    )

    namelist, reason = read_namelist(wheel_path)
    if reason is not None:
        return _unverifiable(reason, f"wheel を zip として読めません: {wheel_path}")
    return _check_wheel(namelist)


def _verify_via_build(build_runner) -> int:
    """既定モード: 公開等価ビルド（sdist 経由）を実行して検証する。"""
    outdir = tempfile.mkdtemp(prefix="c3-verify-wheel-")
    try:
        reason = build_runner(outdir)
        if reason is not None:
            return _unverifiable(reason, "公開等価ビルド（sdist 経由）を完了できませんでした")

        wheel_path, reason = select_single_artifact(outdir, _WHEEL_GLOB)
        if reason is not None:
            return _unverifiable(reason, f"ビルド成果物 {_WHEEL_GLOB} をちょうど 1 件に特定できません")

        sdist_path, reason = select_single_artifact(outdir, _SDIST_GLOB)
        if reason is not None:
            return _unverifiable(reason, f"ビルド成果物 {_SDIST_GLOB} をちょうど 1 件に特定できません")

        namelist, reason = read_namelist(wheel_path)
        if reason is not None:
            return _unverifiable(reason, f"wheel を zip として読めません: {wheel_path}")

        sdist_names, reason = read_sdist_names(sdist_path)
        if reason is not None:
            return _unverifiable(reason, f"sdist を tar として読めません: {sdist_path}")

        print(f"検証対象 wheel: {Path(wheel_path).name}")
        print(f"対照入力 sdist: {Path(sdist_path).name}")
        print(
            "EXCLUDE 実効候補件数（sdist の .claude/ 配下で should_skip True）: "
            f"{count_exclude_candidates(sdist_names)}"
        )

        reason = find_sdist_control_reason(sdist_names)
        if reason is not None:
            return _unverifiable(
                reason,
                "注入対照が成立していません"
                f"（sdist の `{_CONTROL_RELPATH}` の存在と候補件数 1 以上を確認してください）",
            )

        return _check_wheel(namelist)
    finally:
        try:
            shutil.rmtree(outdir)
        except OSError as exc:
            # 後始末の失敗で検証結果を変えない（警告のみ）
            print(f"警告: 一時ディレクトリを削除できませんでした: {outdir}: {exc}", file=sys.stderr)


def main(argv=None, *, build_runner=run_build) -> int:
    """エントリポイント。

    Returns:
        終了コード（0 = PASS / 1 = 違反 / 3 = 検証不能）
    """
    parser = argparse.ArgumentParser(
        description=(
            "wheel 実体が src/c3/_excludes.py の意図と一致するか検証する"
            "（既定: 公開等価ビルド〔sdist 経由〕を実行）"
        ),
    )
    parser.add_argument(
        "--wheel",
        metavar="PATH",
        help="既に手元にある wheel を検証する（ビルドしない）。省略時は公開等価ビルドを実行する",
    )
    args = parser.parse_args(argv)

    if args.wheel:
        return _verify_existing_wheel(args.wheel)
    return _verify_via_build(build_runner)


if __name__ == "__main__":
    sys.exit(main())
