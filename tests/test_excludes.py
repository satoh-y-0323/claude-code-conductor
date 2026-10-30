"""Tests for the shared exclusion list (``c3._excludes``)."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from c3._excludes import EXCLUDE_PATTERNS, KEEP_PATTERNS, should_skip

REPO_ROOT = Path(__file__).resolve().parent.parent
HATCH_BUILD_PATH = REPO_ROOT / "hatch_build.py"


def test_keeps_framework_files():
    assert not should_skip("agents/architect.md")
    assert not should_skip("skills/dev-workflow.md")
    assert not should_skip("commands/develop.md")
    assert not should_skip("hooks/pre_tool.py")
    assert not should_skip("skills/dev-workflow/references/code-review-checklist.md")
    assert not should_skip("settings.json")
    assert not should_skip("CLAUDE.md")
    assert not should_skip("docs/settings.json.md")


def test_excludes_personal_files():
    assert should_skip("reports/plan-report-20260427-232152.md")
    assert should_skip("reports/test-report-20260429-203045.md")
    assert should_skip("memory/sessions/20260427.tmp")
    assert should_skip("memory/sessions/20260501.tmp")
    assert should_skip("memory/patterns.json")
    assert should_skip("memory/agent-audit.log")
    assert should_skip("tmp/scratch.txt")
    assert should_skip("docs/decisions.md")
    assert should_skip("docs/taxonomy.md")
    assert should_skip("docs/c3追加予定機能リスト.md")
    assert should_skip("settings.local.json")


def test_excludes_pycache_at_any_depth():
    """``__pycache__/*.pyc`` artefacts must never ship in the wheel or be
    copied by ``c3 init`` / ``c3 update``. They appear when the dev or
    user runs hooks locally; the build hook reads from the filesystem
    so without an explicit rule they sneak into the bundle.
    """
    assert should_skip("hooks/__pycache__/pre_tool.cpython-311.pyc")
    assert should_skip("__pycache__/foo.pyc")
    assert should_skip("hooks/__pycache__/stop.cpython-311.pyo")
    assert should_skip("agents/sub/__pycache__/x.pyc")
    # bare .pyc/.pyo (legacy layout) are also filtered defensively
    assert should_skip("hooks/legacy.pyc")
    assert should_skip("hooks/legacy.pyo")
    # but .py source files are framework files and stay
    assert not should_skip("hooks/pre_tool.py")


def test_keep_overrides_exclude_for_gitkeep():
    assert not should_skip("reports/.gitkeep")
    assert not should_skip("memory/.gitkeep")
    assert not should_skip("memory/sessions/.gitkeep")
    assert not should_skip("tmp/.gitkeep")


def test_keep_patterns_actually_protect_against_excludes():
    """KEEP_PATTERNS exist to defend specific paths. They are still useful
    even when not strictly needed today (defense against future EXCLUDE
    additions). This test just confirms the KEEP list is non-empty and
    every entry passes the should_skip filter.
    """
    assert KEEP_PATTERNS, "KEEP_PATTERNS should not be empty"
    for keep in KEEP_PATTERNS:
        assert not should_skip(keep), f"{keep!r} should be retained"


# ---------------------------------------------------------------------------
# T4-1: 3 ファイル同期の同一性検査（architecture-report-20260714-213000.md §6）
#
# hatch_build.py はリポジトリルート直下にあり package 化されていないため
# importlib.util でファイルパスから直接ロードする。hatch_build.py は
# ``hatchling.builders.hooks.plugin.interface`` に依存する import 文を持つため、
# hatchling 未インストール環境では exec_module が ImportError で失敗し得る。
# その場合は正規表現でタプル本文を抽出して比較するテキスト比較にフォールバックする
# （どちらの経路でも同期漏れを検知できることが受け入れ条件）。
# ---------------------------------------------------------------------------

def _load_hatch_build_patterns_via_importlib() -> tuple[tuple[str, ...], tuple[str, ...]]:
    spec = importlib.util.spec_from_file_location(
        "_hatch_build_under_test_excludes", HATCH_BUILD_PATH
    )
    assert spec and spec.loader, f"could not build import spec for {HATCH_BUILD_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EXCLUDE_PATTERNS, module.KEEP_PATTERNS


def _strip_comment_lines(body: str) -> str:
    """タプル本文から行頭コメント行を除去する。

    ``# v2.14.1: ... isolation:"worktree" で生成する ...`` のような日本語コメント行は
    引用符付き文字列（``"worktree"``）を含むため、コメントを除去せず素朴に
    ``"..."`` を正規表現抽出すると誤ってコメント内の引用符もパターンとして
    拾ってしまう（実機で確認済みの落とし穴）。行頭が ``#`` の行は丸ごと除外する。
    """
    return "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )


def _extract_pattern_tuple_via_regex(text: str, const_name: str) -> tuple[str, ...]:
    """``NAME: tuple[str, ...] = (\n    "a",\n    "b",\n)`` 形式のタプル本文から
    文字列リテラルだけを正規表現で抽出する（フォールバック比較用）。
    """
    match = re.search(
        rf"{const_name}\s*:\s*tuple\[str,\s*\.\.\.\]\s*=\s*\((.*?)\n\)",
        text,
        re.DOTALL,
    )
    assert match, f"{const_name} definition not found via regex fallback"
    body = _strip_comment_lines(match.group(1))
    return tuple(re.findall(r'"((?:[^"\\]|\\.)*)"', body))


def _load_hatch_build_patterns_via_regex_fallback() -> tuple[tuple[str, ...], tuple[str, ...]]:
    text = HATCH_BUILD_PATH.read_text(encoding="utf-8")
    exclude = _extract_pattern_tuple_via_regex(text, "EXCLUDE_PATTERNS")
    keep = _extract_pattern_tuple_via_regex(text, "KEEP_PATTERNS")
    return exclude, keep


def _load_hatch_build_patterns() -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        return _load_hatch_build_patterns_via_importlib()
    except ImportError:
        # hatchling 未インストール環境（BuildHookInterface が import できない）
        return _load_hatch_build_patterns_via_regex_fallback()


def test_hatch_build_exclude_patterns_match_c3_excludes():
    """hatch_build.py の EXCLUDE_PATTERNS は c3._excludes.EXCLUDE_PATTERNS と
    タプル完全一致でなければならない（CLAUDE.md §2 の 3 ファイル同期グループ）。
    過去 defect: v1.1.0 で state/tier_selection.json が wheel に混入した
    （3 ファイル間の手動同期漏れ）。
    """
    hatch_exclude, _hatch_keep = _load_hatch_build_patterns()
    assert hatch_exclude == EXCLUDE_PATTERNS


def test_hatch_build_keep_patterns_match_c3_excludes():
    """KEEP_PATTERNS も同様にタプル完全一致でなければならない。"""
    _hatch_exclude, hatch_keep = _load_hatch_build_patterns()
    assert hatch_keep == KEEP_PATTERNS


# ---------------------------------------------------------------------------
# T4-2: autonomous-mode skill 配布対象検査（architecture-report-20260714-213000.md §9-1）
#
# v2.53.0 配布切替で autonomous-mode skill の除外定義を削除し配布対象化した
# （旧仕様: 配布除外での熟成対象）。SKILL.md 本体もサブパスも配布対象になる。
# dev-workflow は巻き込み事故がないことも併せて検査する。
# ---------------------------------------------------------------------------

def test_autonomous_mode_skill_is_included_in_distribution():
    # v2.53.0 配布切替で除外解除された（旧: should_skip(...) is True）
    assert should_skip("skills/autonomous-mode/SKILL.md") is False


def test_dev_workflow_skill_not_caught_up_in_autonomous_mode_exclusion():
    assert should_skip("skills/dev-workflow/SKILL.md") is False


# ---------------------------------------------------------------------------
# T4-4: Dead pattern removal verification (test-excludes task)
#
# Three dead EXCLUDE_PATTERNS entries must be removed:
#   - "hooks/subagent_log.py" (v?.?.?廃止)
#   - "agents/tdd-develop.md" (v2.1.0 廃止、planner 3-wave 統一に伴い)
#   - "skills/worktree-tdd-workflow/*" (v2.1.0 廃止、planner 3-wave 統一に伴い)
#
# These tests verify:
# (a) Negative control: Dead patterns are NOT in EXCLUDE_PATTERNS (Red before removal)
# (b) Positive twins: the *live* patterns that remain still exclude, observed
#     through the real behaviour of should_skip(). Each twin mirrors the shape
#     of a dead pattern it replaces, so the deletion cannot silently take the
#     exclusion mechanism down with it:
#       - exact-file twin  <-> "hooks/subagent_log.py" / "agents/tdd-develop.md"
#       - glob-directory twin <-> "skills/worktree-tdd-workflow/*"
#
# Every twin must assert should_skip(...) is True. Asserting `is False` would be
# vacuous here: with EXCLUDE_PATTERNS emptied, should_skip returns False for
# everything, so a `is False` assertion stays green against a destroyed
# implementation and measures nothing.
# ---------------------------------------------------------------------------


def test_dead_pattern_hooks_subagent_log_not_excluded():
    """Negative control (a): 'hooks/subagent_log.py' must NOT be in EXCLUDE_PATTERNS."""
    # Read module dynamically to avoid binding EXCLUDE_PATTERNS at import time
    import c3._excludes
    patterns = c3._excludes.EXCLUDE_PATTERNS
    assert "hooks/subagent_log.py" not in patterns, (
        "Dead pattern 'hooks/subagent_log.py' must be removed from EXCLUDE_PATTERNS"
    )


def test_dead_pattern_agents_tdd_develop_not_excluded():
    """Negative control (a): 'agents/tdd-develop.md' must NOT be in EXCLUDE_PATTERNS."""
    import c3._excludes
    patterns = c3._excludes.EXCLUDE_PATTERNS
    assert "agents/tdd-develop.md" not in patterns, (
        "Dead pattern 'agents/tdd-develop.md' must be removed from EXCLUDE_PATTERNS"
    )


def test_dead_pattern_skills_worktree_tdd_workflow_not_excluded():
    """Negative control (a): 'skills/worktree-tdd-workflow/*' must NOT be in EXCLUDE_PATTERNS."""
    import c3._excludes
    patterns = c3._excludes.EXCLUDE_PATTERNS
    assert "skills/worktree-tdd-workflow/*" not in patterns, (
        "Dead pattern 'skills/worktree-tdd-workflow/*' must be removed from EXCLUDE_PATTERNS"
    )


def test_positive_twin_exact_file_patterns_still_excluded():
    """Positive twin (b): live *exact-file* exclusions still fire.

    Twin of the two deleted exact-file patterns ("hooks/subagent_log.py" and
    "agents/tdd-develop.md"): removing them must not break exact-file matching
    for the entries that are still alive.
    """
    for rel in (
        "memory/patterns.json",
        "memory/agent-audit.log",
        "settings.local.json",
        "pytest_temp.ini",
        "docs/taxonomy.md",
    ):
        assert should_skip(rel) is True, (
            f"live exact-file exclusion must still fire for {rel!r}"
        )


def test_positive_twin_glob_directory_patterns_still_excluded():
    """Positive twin (b): live *glob-directory* exclusions still fire.

    Twin of the deleted "skills/worktree-tdd-workflow/*" pattern: removing it
    must not break ``dir/*`` matching for the entries that are still alive,
    including nested paths (fnmatch's ``*`` spans ``/``).
    """
    for rel in (
        "agent-memory/wt_tester/MEMORY.md",
        "worktrees/agent-abc123/reports/code-review-report.md",
        "memory/sessions/20260809.md",
        "logs/hook.log",
        "tmp/scratch.txt",
    ):
        assert should_skip(rel) is True, (
            f"live glob-directory exclusion must still fire for {rel!r}"
        )


def test_positive_twin_reports_state_excluded():
    """Positive twin (b): Verify that legitimate exclusions still work.

    Reports and state directories must be excluded (EXCLUDE_PATTERNS intact).
    """
    assert should_skip("reports/test-report-20260809-120000.md") is True
    assert should_skip("state/tier_selection.json") is True
