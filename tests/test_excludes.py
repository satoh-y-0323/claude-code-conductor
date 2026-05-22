"""Tests for the shared exclusion list (``c3._excludes``)."""

from __future__ import annotations

from c3._excludes import EXCLUDE_PATTERNS, KEEP_PATTERNS, should_skip


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
    assert should_skip("docs/game-studios-research.md")
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
