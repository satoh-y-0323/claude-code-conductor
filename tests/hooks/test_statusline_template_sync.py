"""Tests verifying that src/c3/_template/.claude/hooks/statusline.py is in sync
with the canonical body at .claude/hooks/statusline.py.

役割分担:
- `.dev/hooks/_sync_check.py` (PostToolUse): 配布元の作業中に同期不一致をリアルタイム警告
- 本テスト (CI 検証): pytest 経由でリリース前に最終同期を担保する

両者は冗長ではなく、開発フローの異なる時点（編集時 / CI 時）で同期不一致を検出する。

Covers:
1. Template file exists at the expected path.
2. Template file contains the MAX_INPUT trimming fix
   (overflow = total_size - MAX_INPUT).
3. Template and body files have identical content (full-sync check).
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parents[2]
BODY_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "statusline.py"
TEMPLATE_PATH = (
    WORKTREE_ROOT / "src" / "c3" / "_template" / ".claude" / "hooks" / "statusline.py"
)

OVERFLOW_LINE = "overflow = total_size - MAX_INPUT"


# ---------------------------------------------------------------------------
# Test 1: template file exists
# ---------------------------------------------------------------------------

def test_template_file_exists():
    """Template statusline.py must exist at src/c3/_template/.claude/hooks/statusline.py."""
    assert TEMPLATE_PATH.exists(), (
        f"Template file not found: {TEMPLATE_PATH}\n"
        "The template must be created/synced with the body."
    )


# ---------------------------------------------------------------------------
# Test 2: template contains the MAX_INPUT overflow trim logic
# ---------------------------------------------------------------------------

def test_template_contains_overflow_trim():
    """Template must contain the overflow trimming line: 'overflow = total_size - MAX_INPUT'."""
    if not TEMPLATE_PATH.exists():
        pytest.skip(f"Template file missing, skipped by test_template_file_exists: {TEMPLATE_PATH}")

    content = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert OVERFLOW_LINE in content, (
        f"Expected trimming line not found in template.\n"
        f"Missing: {OVERFLOW_LINE!r}\n"
        f"Template path: {TEMPLATE_PATH}"
    )


# ---------------------------------------------------------------------------
# Test 3: template and body have identical content
# ---------------------------------------------------------------------------

def test_template_and_body_are_identical():
    """Template and body statusline.py must have identical file contents."""
    assert BODY_PATH.exists(), f"Body file not found: {BODY_PATH}"

    if not TEMPLATE_PATH.exists():
        pytest.skip(f"Template file missing, skipped by test_template_file_exists: {TEMPLATE_PATH}")

    body_content = BODY_PATH.read_text(encoding="utf-8")
    template_content = TEMPLATE_PATH.read_text(encoding="utf-8")

    assert template_content == body_content, (
        "Template and body statusline.py have diverged.\n"
        f"Body:     {BODY_PATH}\n"
        f"Template: {TEMPLATE_PATH}\n"
        "The template must be updated to exactly match the body."
    )
