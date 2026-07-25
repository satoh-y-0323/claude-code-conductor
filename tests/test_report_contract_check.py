"""Tests for .claude/hooks/report_contract_check.py (P2 hook)

plan-report-20260725-180252.md T1(2) / architecture-report-20260725-175915.md §2 D-2 の
仕様に基づく。

## Red フェーズの経緯（T2 developer 実装前）

T2 developer 実装前の時点では `.claude/hooks/report_contract_check.py` が存在せず、
`_run_hook()` ヘルパーは対象ファイルが無ければ `FileNotFoundError` を送出する設計に
していた（`pytestmark skipif` は使わない。SKIP では Red の証跡が残らないため）。
当時は構文エラー・タイポによる失敗ではなく、hook 不在という「機能未実装」による
失敗だった（実装後の現在も、本ヘルパーは hook 不在時のフォールバックとして機能する）。

## 擬似リポジトリ方式について

P2 の判定はファイルパスの構造（`.claude/reports/` 直下か・prefix/suffix パターン）のみに
依存し、`Path(__file__)` からのプロジェクトルート導出は不要と判断した（D-2 の記述には
protected path の絶対アンカーに関する言及がないため）。したがって patterns_guard.py の
ようなコピー配置は行わず、`tmp_path` 配下に作った任意のパス文字列を直接 tool_input に
渡す。実ファイルの実在は前提にしない（P2 はファイル内容ではなく命名規約のみを検査する
設計のため）。

## strict-4（対象 prefix）

D-2 により警告対象は以下 4 prefix のみ（strict-4。CR/SR は 2 レジーム問題のため対象外）:
    - requirements-report-
    - architecture-report-
    - plan-report-
    - design-review-report-

suffix は `\\d{8}-\\d{6}\\.md`（YYYYMMDD-HHMMSS.md）に一致しなければ警告。

## ケース仕様（plan T1(2) 準拠）

    1. Write 以外（Edit・Bash 等）→ 沈黙（stderr・stdout とも空）・exit 0
    2. `.claude/reports/` 直下 かつ strict-4 prefix かつ suffix 不一致 → 警告
    3. 対象外: CR/SR prefix・自由域 prefix・archive/ 配下・reports 外 → 沈黙
    4. 壊れた JSON → 沈黙・exit 0
    5. 2 経路の検証: stderr の `[ReportContract WARN]` と stdout JSON
       (`hookSpecificOutput.hookEventName == "PostToolUse"`)。additionalContext に
       対象 basename・期待形式・report-timestamp skill への言及を含む

## E 周回 1 修正サイクル追加分（T5 — plan-report §2-B）

以下は F1 / F2 / F5 の未実装により追加時点で Red だった。いずれも独立した module-level の
test 関数として追加した（既存の未使用定数への追記では実行されないため・rework2
DC-AM-001。同 defect の再発防止として未使用定数 `_BAD_SUFFIXES` / `_STRICT4_PREFIXES` /
`_EXEMPT_PREFIXES` は削除した）。

    (a) F1: 判定を basename の `^{prefix}\\d{8}-\\d{6}\\.md$` **フルマッチ**へ変更した。
        追加当時の実装は `re.search(r'\\d{8}-\\d{6}\\.md$')`（suffix 一致）だったため
        `plan-report-badname-extra-20260725-180252.md` がすり抜けて Red だった。
        対の沈黙ケース（正規名 `plan-report-20260725-180252.md` が引き続き沈黙）も
        併置し、フルマッチ化で正常系が反転しないことを固定する。
    (b) F2: reports ディレクトリ判定を構造判定
        （`parent.name == 'reports' and parent.parent.name == '.claude'`）へ変更した。
        追加当時の実装は `parts.index('reports')` の**最初一致**だったため、上流に同名
        `reports/` ディレクトリがあると `.claude/reports/` 直下を検知できず Red だった。
        対の沈黙ケース（`.claude` 直下でない `reports/` 直下は沈黙）も併置する。
    (e) F5: stderr / additionalContext のサニタイズを共有
        `_hook_utils.sanitize_for_terminal()`（C0+DEL+**C1**）へ置換した。
        追加当時のローカル `_sanitize` は `[\\x00-\\x1f\\x7f]`（C0+DEL のみ）で
        C1 制御文字（例 CSI = `\\x9b`）が素通りしていたため Red だった。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

WORKTREE_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "report_contract_check.py"

_GOOD_SUFFIX = "20260725-180252.md"

# C1 制御文字（CSI）。一部端末でエスケープシーケンスのプリフィクスとして解釈される。
_C1_CSI = "\x9b"


def _run_hook(
    payload: dict | None,
    *,
    raw_stdin: str | None = None,
) -> subprocess.CompletedProcess:
    if not HOOK_PATH.is_file():
        raise FileNotFoundError(
            f"{HOOK_PATH} が存在しません（P2 hook 未実装のため Red）"
        )
    env: dict[str, str] = {"PYTHONIOENCODING": "utf-8"}
    for key in ("SYSTEMROOT", "PATH"):
        if key in os.environ:
            env[key] = os.environ[key]
    stdin_data = raw_stdin if raw_stdin is not None else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=stdin_data,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _payload(tool_name: str, file_path: str) -> dict:
    return {"tool_name": tool_name, "tool_input": {"file_path": file_path}}


def _reports_path(tmp_path: Path, basename: str) -> str:
    return str(tmp_path / ".claude" / "reports" / basename)


def _archive_path(tmp_path: Path, basename: str) -> str:
    return str(tmp_path / ".claude" / "reports" / "archive" / basename)


# ---------------------------------------------------------------------------
# 1. Write 以外 → 沈黙
# ---------------------------------------------------------------------------


class TestNonWriteToolIsSilent:
    def test_edit_tool_is_silent(self, tmp_path: Path) -> None:
        """新規命名は Write でのみ発生するため Edit は対象外."""
        path = _reports_path(tmp_path, "plan-report-final.md")
        result = _run_hook(_payload("Edit", path))
        assert result.returncode == 0
        assert not result.stdout.strip(), f"Edit で stdout に出力があった: {result.stdout!r}"
        assert not result.stderr.strip(), f"Edit で stderr に出力があった: {result.stderr!r}"

    def test_bash_tool_is_silent(self, tmp_path: Path) -> None:
        path = _reports_path(tmp_path, "plan-report-final.md")
        result = _run_hook(_payload("Bash", path))
        assert result.returncode == 0
        assert not result.stdout.strip()
        assert not result.stderr.strip()


# ---------------------------------------------------------------------------
# 2. strict-4 prefix + suffix 不一致 → 警告
# ---------------------------------------------------------------------------


class TestStrict4BadSuffixWarns:
    def test_requirements_report_bad_suffix_warns(self, tmp_path: Path) -> None:
        path = _reports_path(tmp_path, "requirements-report-final.md")
        result = _run_hook(_payload("Write", path))
        assert result.returncode == 0
        assert "[ReportContract WARN]" in result.stderr, (
            f"strict-4 prefix + suffix 不一致で警告が出なかった: stderr={result.stderr!r}"
        )

    def test_architecture_report_bad_suffix_warns(self, tmp_path: Path) -> None:
        path = _reports_path(tmp_path, "architecture-report-2026-07-25.md")
        result = _run_hook(_payload("Write", path))
        assert "[ReportContract WARN]" in result.stderr

    def test_plan_report_bad_suffix_warns(self, tmp_path: Path) -> None:
        path = _reports_path(tmp_path, "plan-report-20260725.md")
        result = _run_hook(_payload("Write", path))
        assert "[ReportContract WARN]" in result.stderr

    def test_design_review_report_bad_suffix_warns(self, tmp_path: Path) -> None:
        path = _reports_path(tmp_path, "design-review-report-draft.md")
        result = _run_hook(_payload("Write", path))
        assert "[ReportContract WARN]" in result.stderr


class TestStrict4GoodSuffixIsSilent:
    def test_requirements_report_good_suffix_is_silent(self, tmp_path: Path) -> None:
        path = _reports_path(tmp_path, f"requirements-report-{_GOOD_SUFFIX}")
        result = _run_hook(_payload("Write", path))
        assert result.returncode == 0
        assert not result.stderr.strip(), (
            f"正しい命名なのに警告が出た: {result.stderr!r}"
        )
        assert not result.stdout.strip()

    def test_architecture_report_good_suffix_is_silent(self, tmp_path: Path) -> None:
        path = _reports_path(tmp_path, f"architecture-report-{_GOOD_SUFFIX}")
        result = _run_hook(_payload("Write", path))
        assert not result.stderr.strip()

    def test_plan_report_good_suffix_is_silent(self, tmp_path: Path) -> None:
        path = _reports_path(tmp_path, f"plan-report-{_GOOD_SUFFIX}")
        result = _run_hook(_payload("Write", path))
        assert not result.stderr.strip()

    def test_design_review_report_good_suffix_is_silent(self, tmp_path: Path) -> None:
        path = _reports_path(tmp_path, f"design-review-report-{_GOOD_SUFFIX}")
        result = _run_hook(_payload("Write", path))
        assert not result.stderr.strip()


# ---------------------------------------------------------------------------
# 3. 対象外（CR/SR prefix・自由域 prefix・archive/ 配下・reports 外） → 沈黙
# ---------------------------------------------------------------------------


class TestExemptPrefixesAreSilent:
    def test_code_review_report_bad_name_is_silent(self, tmp_path: Path) -> None:
        """CR は R2(task_id 契約)レジームのため strict-4 対象外."""
        path = _reports_path(tmp_path, "code-review-report-final-draft.md")
        result = _run_hook(_payload("Write", path))
        assert result.returncode == 0
        assert not result.stderr.strip(), (
            f"code-review-report- は対象外のはずが警告が出た: {result.stderr!r}"
        )

    def test_security_review_report_bad_name_is_silent(self, tmp_path: Path) -> None:
        path = _reports_path(tmp_path, "security-review-report-final-draft.md")
        result = _run_hook(_payload("Write", path))
        assert not result.stderr.strip()

    def test_free_form_prefix_is_silent(self, tmp_path: Path) -> None:
        """test-report- 等の自由域 prefix はタイムスタンプ契約対象外."""
        path = _reports_path(tmp_path, "test-report-summary.md")
        result = _run_hook(_payload("Write", path))
        assert result.returncode == 0
        assert not result.stderr.strip(), (
            f"自由域 prefix は対象外のはずが警告が出た: {result.stderr!r}"
        )

    def test_honor_system_inventory_prefix_is_silent(self, tmp_path: Path) -> None:
        path = _reports_path(tmp_path, "honor-system-inventory-notes.md")
        result = _run_hook(_payload("Write", path))
        assert not result.stderr.strip()

    def test_archive_subdirectory_is_silent_even_with_strict4_prefix(
        self, tmp_path: Path
    ) -> None:
        """archive/ 配下は strict-4 prefix + suffix 不一致でも対象外（直下限定）。"""
        path = _archive_path(tmp_path, "plan-report-final.md")
        result = _run_hook(_payload("Write", path))
        assert result.returncode == 0
        assert not result.stderr.strip(), (
            f"archive/ 配下は対象外のはずが警告が出た: {result.stderr!r}"
        )

    def test_outside_reports_directory_is_silent(self, tmp_path: Path) -> None:
        """`.claude/reports/` 以外への Write（同名でも）は対象外。"""
        path = str(tmp_path / ".claude" / "other" / "plan-report-final.md")
        result = _run_hook(_payload("Write", path))
        assert result.returncode == 0
        assert not result.stderr.strip(), (
            f"reports/ 外は対象外のはずが警告が出た: {result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# 4. fail-open
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_malformed_json_stdin_is_silent_exit_zero(self) -> None:
        result = _run_hook(None, raw_stdin="{not valid json")
        assert result.returncode == 0
        assert not result.stderr.strip()

    def test_missing_tool_input_is_silent_exit_zero(self) -> None:
        result = _run_hook({"tool_name": "Write"})
        assert result.returncode == 0

    def test_empty_stdin_is_silent_exit_zero(self) -> None:
        result = _run_hook(None, raw_stdin="")
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# 5. 2 経路の検証（stderr + stdout JSON）
# ---------------------------------------------------------------------------


class TestTwoChannelOutput:
    def test_warn_emits_stdout_json_with_hook_event_name(self, tmp_path: Path) -> None:
        path = _reports_path(tmp_path, "plan-report-bad-name.md")
        result = _run_hook(_payload("Write", path))
        assert result.returncode == 0
        parsed = json.loads(result.stdout)
        assert "hookSpecificOutput" in parsed, f"stdout に hookSpecificOutput が無い: {parsed}"
        assert parsed["hookSpecificOutput"]["hookEventName"] == "PostToolUse", (
            f"hookEventName が PostToolUse でない: {parsed}"
        )

    def test_additional_context_mentions_basename_and_expected_format(
        self, tmp_path: Path
    ) -> None:
        path = _reports_path(tmp_path, "plan-report-bad-name.md")
        result = _run_hook(_payload("Write", path))
        parsed = json.loads(result.stdout)
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        assert "plan-report-bad-name.md" in ctx, (
            f"additionalContext に対象 basename が含まれていない: {ctx!r}"
        )
        assert "YYYYMMDD-HHMMSS" in ctx, (
            f"additionalContext に期待形式の言及が無い: {ctx!r}"
        )
        assert "report-timestamp" in ctx, (
            f"additionalContext に report-timestamp skill への言及が無い: {ctx!r}"
        )

    def test_no_violation_produces_no_stdout(self, tmp_path: Path) -> None:
        path = _reports_path(tmp_path, f"plan-report-{_GOOD_SUFFIX}")
        result = _run_hook(_payload("Write", path))
        assert not result.stdout.strip(), (
            f"違反がないのに stdout に出力があった: {result.stdout!r}"
        )


# ===========================================================================
# T5 追加分（E 周回 1 修正サイクル・Red）— すべて独立した module-level test 関数
# ===========================================================================


# ---------------------------------------------------------------------------
# (a) F1: フルマッチ判定
# ---------------------------------------------------------------------------


def test_fullmatch_violation_with_extra_segment_warns(tmp_path: Path) -> None:
    """[F1・Red] prefix と timestamp の間に任意文字列を挟む名前は逸脱として警告される。

    `plan-report-badname-extra-20260725-180252.md` は末尾が `\\d{8}-\\d{6}\\.md` に
    一致するため追加当時の suffix `re.search` 判定ではすり抜けていた。判定を
    `^{prefix}\\d{8}-\\d{6}\\.md$` のフルマッチへ変更して警告するようにした。
    """
    basename = f"plan-report-badname-extra-{_GOOD_SUFFIX}"
    result = _run_hook(_payload("Write", _reports_path(tmp_path, basename)))
    assert result.returncode == 0
    assert "[ReportContract WARN]" in result.stderr, (
        "フルマッチ違反（prefix と timestamp の間に余分な文字列）で警告が出なかった: "
        f"stderr={result.stderr!r}"
    )


def test_fullmatch_canonical_name_remains_silent(tmp_path: Path) -> None:
    """[F1・対の沈黙ケース] 正規名はフルマッチ化後も引き続き沈黙する（正常系の非反転）。"""
    basename = f"plan-report-{_GOOD_SUFFIX}"
    result = _run_hook(_payload("Write", _reports_path(tmp_path, basename)))
    assert result.returncode == 0
    assert not result.stderr.strip(), (
        f"正規名 {basename} が警告された（フルマッチ化で正常系が反転している）: "
        f"{result.stderr!r}"
    )
    assert not result.stdout.strip(), (
        f"正規名 {basename} で stdout に出力があった: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# (b) F2: reports ディレクトリの構造判定
# ---------------------------------------------------------------------------


def test_upstream_same_named_reports_directory_still_detects(tmp_path: Path) -> None:
    """[F2・Red] 上流に同名 `reports/` があっても `.claude/reports/` 直下を検知する。

    `tmp_path/reports/.claude/reports/plan-report-final.md` は
    `parts.index('reports')` の最初一致方式では上流の `reports/` に誤アンカーし
    沈黙してしまう。構造判定（`parent.name == 'reports'` かつ
    `parent.parent.name == '.claude'`）なら正しく検知する。
    """
    path = str(
        tmp_path / "reports" / ".claude" / "reports" / "plan-report-final.md"
    )
    result = _run_hook(_payload("Write", path))
    assert result.returncode == 0
    assert "[ReportContract WARN]" in result.stderr, (
        "上流同名ディレクトリ構成で `.claude/reports/` 直下が検知されなかった: "
        f"stderr={result.stderr!r}"
    )


def test_reports_directory_not_under_claude_is_silent(tmp_path: Path) -> None:
    """[F2・対の沈黙ケース] `.claude` 直下でない `reports/` 直下への Write は沈黙する。"""
    path = str(tmp_path / "reports" / "plan-report-final.md")
    result = _run_hook(_payload("Write", path))
    assert result.returncode == 0
    assert not result.stderr.strip(), (
        f"`.claude/reports/` ではない reports/ 直下が警告された: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# (e) F5: C1 制御文字のサニタイズ（共有 sanitize_for_terminal）
# ---------------------------------------------------------------------------


def test_c1_control_char_is_stripped_from_both_channels(tmp_path: Path) -> None:
    """[F5・Red] basename の C1 制御文字が stderr / additionalContext に現れない。

    追加当時のローカル `_sanitize` は `[\\x00-\\x1f\\x7f]`（C0+DEL）のみを除去していたため
    C1 (`\\x9b` = CSI) が素通りしていた。共有 `_hook_utils.sanitize_for_terminal()`
    （C0+DEL+C1）へ置換して除去するようになっている。
    """
    basename = f"plan-report-{_C1_CSI}badname.md"
    result = _run_hook(_payload("Write", _reports_path(tmp_path, basename)))
    assert result.returncode == 0
    assert "[ReportContract WARN]" in result.stderr, (
        f"前提となる警告自体が出ていない: stderr={result.stderr!r}"
    )
    assert _C1_CSI not in result.stderr, (
        f"C1 制御文字が stderr に残っている: {result.stderr!r}"
    )
    ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert _C1_CSI not in ctx, (
        f"C1 制御文字が additionalContext に残っている: {ctx!r}"
    )


# ===========================================================================
# T8 追加分（E 周回 2 修正サイクル・plan-report §2-C）— 独立した module-level 関数
# ===========================================================================
#
# 【Red ケース】追加時点の実装で失敗していた（機能未実装）
#   (a) F7: ディレクトリ成分のケース非依存化（`.lower()`）。
#       追加当時は `parent.name != 'reports' or parent.parent.name != '.claude'` の
#       case-sensitive 比較だったため `.CLAUDE/reports/` / `.claude/REPORTS/` を沈黙させていた。
#   (b) F8: タイムスタンプ正規表現へ `re.ASCII` を指定。
#       追加当時は `\d` が Unicode 十進数字にマッチしていたため全角数字のタイムスタンプが
#       フルマッチしてしまい沈黙していた（全角数字による契約偽装）。
#
# 【非反転固定ケース】現行実装で既に合格する（回帰ガードとして固定）
#   (h) F7 の射程限定。ケース非依存化はディレクトリ成分のみに閉じ、
#       basename（prefix・タイムスタンプ・`.md`）は厳密維持であること、および
#       `.claude` 直下でない `reports/` は構造条件で沈黙のままであることを固定する。
#
# なお「既存の正規名沈黙が不変」（(h) の 1 項目）は
# `TestStrict4GoodSuffixIsSilent` / `test_fullmatch_canonical_name_remains_silent`
# が既に固定済みのため、ここではケース違いディレクトリ下での正規名沈黙
# （F7 適用後に新たな誤検知を作らないこと）を追加で固定する。

# 全角数字（U+FF10-U+FF19）へ変換する対応表。ハイフンと `.md` は ASCII のまま残す。
_FULLWIDTH_DIGITS = str.maketrans(
    "0123456789",
    "０１２３４５６７８９",
)


def _custom_reports_path(
    tmp_path: Path, claude_dir: str, reports_dir: str, basename: str
) -> str:
    """`.claude` / `reports` 相当のディレクトリ名を任意に差し替えたパスを作る。"""
    return str(tmp_path / claude_dir / reports_dir / basename)


# ---------------------------------------------------------------------------
# (a) F7: ディレクトリ成分のケース非依存化 — Red
# ---------------------------------------------------------------------------


def test_uppercase_claude_directory_still_warns(tmp_path: Path) -> None:
    """[T8 (a)・F7・Red] `.CLAUDE/reports/` 直下の逸脱名でも警告される。

    ケース非依存 FS（Windows・macOS 既定）では `.CLAUDE` は `.claude` と同一実体の
    別名であり、追加当時の case-sensitive 比較では検知漏れになっていた。
    F7（`parent.parent.name.lower() == '.claude'`）で閉じる。
    """
    path = _custom_reports_path(tmp_path, ".CLAUDE", "reports", "plan-report-final.md")
    result = _run_hook(_payload("Write", path))
    assert result.returncode == 0
    assert "[ReportContract WARN]" in result.stderr, (
        f"`.CLAUDE/reports/` 直下の逸脱名が検知されなかった: stderr={result.stderr!r}"
    )


def test_uppercase_reports_directory_still_warns(tmp_path: Path) -> None:
    """[T8 (a)・F7・Red] `.claude/REPORTS/` 直下の逸脱名でも警告される。

    F7（`parent.name.lower() == 'reports'`）で閉じる。
    """
    path = _custom_reports_path(tmp_path, ".claude", "REPORTS", "plan-report-final.md")
    result = _run_hook(_payload("Write", path))
    assert result.returncode == 0
    assert "[ReportContract WARN]" in result.stderr, (
        f"`.claude/REPORTS/` 直下の逸脱名が検知されなかった: stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# (b) F8: タイムスタンプは ASCII 数字限定（re.ASCII） — Red
# ---------------------------------------------------------------------------


def test_fullwidth_digit_timestamp_warns(tmp_path: Path) -> None:
    """[T8 (b)・F8・Red] 全角数字のタイムスタンプは契約逸脱として警告される。

    `\\d` は既定で Unicode 十進数字（全角数字 U+FF10-U+FF19 を含む）にマッチするため、
    追加当時の実装では全角数字だけで構成された名前がフルマッチして沈黙していた。
    `re.ASCII` を指定して ASCII 数字のみになり逸脱として検知するようにした。
    """
    basename = f"plan-report-{'20260725-180252'.translate(_FULLWIDTH_DIGITS)}.md"
    # 前提の裏取り: 数字部が全角へ変換され、区切りと拡張子は ASCII のままである。
    assert "２" in basename, f"全角数字が生成されていない: {basename!r}"
    assert basename.endswith("-１８０２５２.md"), (
        f"ハイフン / 拡張子が ASCII でない: {basename!r}"
    )

    result = _run_hook(_payload("Write", _reports_path(tmp_path, basename)))
    assert result.returncode == 0
    assert "[ReportContract WARN]" in result.stderr, (
        f"全角数字タイムスタンプが逸脱として検知されなかった: stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# (h) 非反転固定ケース（F7 の射程限定）
# ---------------------------------------------------------------------------


def test_uppercase_md_extension_still_warns(tmp_path: Path) -> None:
    """[T8 (h)・非反転固定] `.MD` 拡張子は正規タイムスタンプでも引き続き警告される。

    F7 のケース非依存化はディレクトリ成分に限定し、P2 の basename
    （prefix・タイムスタンプ・`.md`）は厳密維持する（`re.IGNORECASE` 不使用）。
    現行実装でも合格するため、F7 実装で basename 判定が緩まないことの回帰ガード。
    """
    basename = "plan-report-20260725-180252.MD"
    result = _run_hook(_payload("Write", _reports_path(tmp_path, basename)))
    assert result.returncode == 0
    assert "[ReportContract WARN]" in result.stderr, (
        f"`.MD` 拡張子が逸脱として扱われなくなった（basename 厳密維持の破れ）: "
        f"stderr={result.stderr!r}"
    )


def test_canonical_name_under_case_variant_dirs_remains_silent(
    tmp_path: Path,
) -> None:
    """[T8 (h)・非反転固定] ケース違いディレクトリ下でも正規名は沈黙する。

    F7 適用後にディレクトリ判定が通るようになっても、正規名（フルマッチ成功）は
    警告されない。ケース非依存化が正常系の沈黙を反転させないことの固定。
    追加当時（F7 実装前）はディレクトリ判定の時点で沈黙していたためいずれにせよ
    合格していた（F7 実装後の現在はディレクトリ判定を通過したうえで、basename の
    正規形一致によって沈黙する。理由は変わったが結果は不変）。
    """
    basename = f"plan-report-{_GOOD_SUFFIX}"
    path = _custom_reports_path(tmp_path, ".CLAUDE", "Reports", basename)
    result = _run_hook(_payload("Write", path))
    assert result.returncode == 0
    assert not result.stderr.strip(), (
        f"ケース違いディレクトリ下の正規名が警告された: {result.stderr!r}"
    )
    assert not result.stdout.strip(), (
        f"ケース違いディレクトリ下の正規名で stdout に出力があった: {result.stdout!r}"
    )


def test_uppercase_reports_not_under_claude_remains_silent(tmp_path: Path) -> None:
    """[T8 (h)・非反転固定] `.claude` 直下でない `REPORTS/` 直下は引き続き沈黙する。

    ケース非依存化しても構造条件（親が reports・祖父が `.claude`）は維持される。
    `tmp_path/REPORTS/` は祖父が `.claude` でないため対象外のまま。
    """
    path = str(tmp_path / "REPORTS" / "plan-report-final.md")
    result = _run_hook(_payload("Write", path))
    assert result.returncode == 0
    assert not result.stderr.strip(), (
        f"`.claude` 直下でない REPORTS/ 直下が警告された（構造条件の破れ）: "
        f"{result.stderr!r}"
    )


# ===========================================================================
# T11 追加分（E 周回 3 修正サイクル・plan-report §2-D）— 独立した module-level 関数
# ===========================================================================
#
# 【Red ケース】追加時点の実装で失敗していた（機能未実装）
#   (i) F11: パス成分比較の末尾ドット・スペース除去。共有ヘルパー
#       `_hook_utils.norm_component()`（`.lower().rstrip('. ')`）を導入する前は、
#       P2 の parent / parent.parent 比較のいずれも Windows の Win32 パス正規化
#       （末尾ドット・スペースの暗黙除去）によるバイパスを閉じられていなかった。
#
# 【対の非反転】末尾ドット・スペース混入ディレクトリ配下でも正規名は沈黙する
#   (j) 正規化ヘルパー導入後、`.claude.` 配下でも正規名（フルマッチ成功）は
#       誤って警告されない。


def test_claude_directory_trailing_dot_still_detects_violation(tmp_path: Path) -> None:
    """[T11 (i)・F11・Red] `.claude.` （末尾ドット）配下の逸脱名でも検知される。

    Windows の Win32 パス正規化は末尾ドットを暗黙に除去するため、`.claude.` は
    実ファイルシステム上 `.claude` と同一実体になり得る。共有ヘルパー
    `norm_component()` 導入前は `parent.parent.name.lower() == '.claude'` の
    厳密比較のため `.claude.` は一致せず、契約逸脱名でも沈黙していた。
    """
    path = _custom_reports_path(
        tmp_path, ".claude.", "reports", "plan-report-badname.md"
    )
    result = _run_hook(_payload("Write", path))
    assert result.returncode == 0
    assert "[ReportContract WARN]" in result.stderr, (
        f"`.claude.` （末尾ドット）配下の逸脱名が検知されなかった: "
        f"stderr={result.stderr!r}"
    )


def test_claude_directory_trailing_dot_with_canonical_name_remains_silent(
    tmp_path: Path,
) -> None:
    """[T11 (j)・対の非反転] `.claude.` 配下でも正規名（フルマッチ成功）は沈黙する。

    正規化ヘルパー導入によりディレクトリ判定が通るようになっても、basename が
    正規のタイムスタンプ契約に一致する場合は引き続き沈黙する
    （正規化がディレクトリ側の検知を広げても、basename 側の判定基準は変えないことの固定）。
    """
    basename = f"plan-report-{_GOOD_SUFFIX}"
    path = _custom_reports_path(tmp_path, ".claude.", "reports", basename)
    result = _run_hook(_payload("Write", path))
    assert result.returncode == 0
    assert not result.stderr.strip(), (
        f"`.claude.` 配下の正規名が警告された: {result.stderr!r}"
    )
    assert not result.stdout.strip(), (
        f"`.claude.` 配下の正規名で stdout に出力があった: {result.stdout!r}"
    )
