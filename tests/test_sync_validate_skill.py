"""Red-phase sync test: template and body of validate_skill_change.py must be identical.

Tests verify that:
  src/c3/_template/.claude/hooks/validate_skill_change.py
is byte-for-byte identical to:
  .claude/hooks/validate_skill_change.py

These tests FAIL (Red phase) until the developer syncs the template file.
"""

from __future__ import annotations

from pathlib import Path

WORKTREE_ROOT = Path(__file__).parent.parent

BODY_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "validate_skill_change.py"
TEMPLATE_PATH = (
    WORKTREE_ROOT
    / "src"
    / "c3"
    / "_template"
    / ".claude"
    / "hooks"
    / "validate_skill_change.py"
)


class TestTemplateSyncWithBody:
    """Template file must be kept in sync with the canonical body file."""

    def test_template_file_exists(self):
        """The template file must exist at the expected path."""
        assert TEMPLATE_PATH.exists(), (
            f"Template file not found: {TEMPLATE_PATH}\n"
            "Expected the developer to copy/sync the body file to this location."
        )

    def test_body_file_exists(self):
        """The body (canonical) hook file must exist."""
        assert BODY_PATH.exists(), (
            f"Body file not found: {BODY_PATH}\n"
            "This is the reference/canonical file."
        )

    def test_template_identical_to_body(self):
        """Template and body files must have identical byte content."""
        assert BODY_PATH.exists(), f"Body file missing: {BODY_PATH}"
        assert TEMPLATE_PATH.exists(), f"Template file missing: {TEMPLATE_PATH}"

        body_bytes = BODY_PATH.read_bytes()
        template_bytes = TEMPLATE_PATH.read_bytes()

        assert body_bytes == template_bytes, (
            "Template file content differs from body file.\n"
            f"  body:     {BODY_PATH}\n"
            f"  template: {TEMPLATE_PATH}\n\n"
            "Diff (body vs template):\n"
            + _unified_diff(
                BODY_PATH.read_text(encoding="utf-8", errors="replace"),
                TEMPLATE_PATH.read_text(encoding="utf-8", errors="replace"),
            )
        )

    def test_template_uses_return_not_sys_exit_in_main(self):
        """Template main() must use return (not sys.exit) for pass-through paths."""
        assert TEMPLATE_PATH.exists(), f"Template file missing: {TEMPLATE_PATH}"
        source = TEMPLATE_PATH.read_text(encoding="utf-8")
        lines = source.splitlines()

        # Collect lines inside the main() function body (before __main__ block)
        in_main = False
        main_body_lines = []
        for line in lines:
            if line.startswith("def main("):
                in_main = True
                continue
            if in_main and line.startswith("if __name__"):
                break
            if in_main:
                main_body_lines.append(line)

        sys_exit_in_main = [l for l in main_body_lines if "sys.exit" in l]
        assert not sys_exit_in_main, (
            "Template main() body still contains sys.exit() calls:\n"
            + "\n".join(sys_exit_in_main)
            + "\nExpected plain 'return' statements instead."
        )

    def test_template_dunder_main_uses_sys_exit_pattern(self):
        """Template __main__ block must use sys.exit(main() or 0) pattern."""
        assert TEMPLATE_PATH.exists(), f"Template file missing: {TEMPLATE_PATH}"
        source = TEMPLATE_PATH.read_text(encoding="utf-8")
        normalized = " ".join(source.split())
        pattern = "sys.exit(main() or 0)"
        assert pattern in normalized, (
            f"Expected '{pattern}' in the template __main__ block, but it was not found.\n"
            "The template may still use a bare 'main()' call or an incorrect pattern."
        )


def _unified_diff(a: str, b: str) -> str:
    """Return a simple unified diff string between two texts."""
    import difflib
    a_lines = a.splitlines(keepends=True)
    b_lines = b.splitlines(keepends=True)
    diff = difflib.unified_diff(a_lines, b_lines, fromfile="body", tofile="template")
    return "".join(diff) or "(no textual diff — possible BOM/encoding difference)"
