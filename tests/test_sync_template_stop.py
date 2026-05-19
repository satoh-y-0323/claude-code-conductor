"""
Regression guard for the sync-template-stop task.

These tests verify that the template-side stop.py
(src/c3/_template/.claude/hooks/stop.py) contains the same
security and quality fixes as the body-side stop.py.

Fixes under test:
  [Code High-N1]   _build_sessions_by_date function exists
  [Sec Medium-1T]  MAX_ID_LENGTH = 64 / MAX_DESCRIPTION_LENGTH = 500 constants exist
  [Sec Medium-1T]  update_patterns enforces id > 64 and description > 500 length limits
  [Code Low-N4]    No function uses `yyyymmdd` as a parameter name

If the template file does not exist, ALL tests fail with AssertionError
(not pytest.skip) because file absence is itself a test failure.
"""

from __future__ import annotations

import ast
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parent.parent
TEMPLATE_STOP_PY = (
    WORKTREE_ROOT
    / "src"
    / "c3"
    / "_template"
    / ".claude"
    / "hooks"
    / "stop.py"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assert_file_exists() -> None:
    """Fail (not skip) if the template stop.py does not exist."""
    assert TEMPLATE_STOP_PY.exists(), (
        f"Template stop.py not found at {TEMPLATE_STOP_PY}. "
        "The file must be created as part of the sync-template-stop task."
    )


def _read_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_ast(path: Path) -> ast.Module:
    return ast.parse(_read_source(path))


def _get_module_constant(tree: ast.Module, name: str):
    """Return the value of a module-level constant assignment, or None if not found."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    if isinstance(node.value, ast.Constant):
                        return node.value.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                if node.value and isinstance(node.value, ast.Constant):
                    return node.value.value
    return None


# ---------------------------------------------------------------------------
# [Code High-N1] _build_sessions_by_date function must exist
# ---------------------------------------------------------------------------

class TestBuildSessionsByDateExists:
    """[Code High-N1] template stop.py must define _build_sessions_by_date."""

    def test_function_defined_in_ast(self):
        """_build_sessions_by_date must be defined as a top-level function."""
        _assert_file_exists()
        tree = _parse_ast(TEMPLATE_STOP_PY)
        func_names = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        ]
        assert "_build_sessions_by_date" in func_names, (
            "template stop.py must define a function named '_build_sessions_by_date'. "
            "This function should build a {date_str: count} dict from the sessions dir "
            "so that update_patterns can call os.listdir only once (O(N+M) fix)."
        )


# ---------------------------------------------------------------------------
# [Sec Medium-1T] MAX_ID_LENGTH and MAX_DESCRIPTION_LENGTH constants
# ---------------------------------------------------------------------------

class TestLengthLimitConstants:
    """[Sec Medium-1T] template stop.py must define MAX_ID_LENGTH and MAX_DESCRIPTION_LENGTH."""

    def test_max_id_length_constant_value(self):
        """MAX_ID_LENGTH must equal 64."""
        _assert_file_exists()
        tree = _parse_ast(TEMPLATE_STOP_PY)
        value = _get_module_constant(tree, "MAX_ID_LENGTH")
        assert value is not None, (
            "template stop.py must define MAX_ID_LENGTH at module level. "
            "Add: MAX_ID_LENGTH = 64"
        )
        assert value == 64, (
            f"Expected MAX_ID_LENGTH == 64, got {value!r}"
        )

    def test_max_description_length_constant_value(self):
        """MAX_DESCRIPTION_LENGTH must equal 500."""
        _assert_file_exists()
        tree = _parse_ast(TEMPLATE_STOP_PY)
        value = _get_module_constant(tree, "MAX_DESCRIPTION_LENGTH")
        assert value is not None, (
            "template stop.py must define MAX_DESCRIPTION_LENGTH at module level. "
            "Add: MAX_DESCRIPTION_LENGTH = 500"
        )
        assert value == 500, (
            f"Expected MAX_DESCRIPTION_LENGTH == 500, got {value!r}"
        )


# ---------------------------------------------------------------------------
# [Sec Medium-1T] update_patterns enforces input length limits
# ---------------------------------------------------------------------------

class TestUpdatePatternsLengthValidation:
    """[Sec Medium-1T] update_patterns must enforce id and description length limits."""

    def _get_update_patterns_nodes(self, tree: ast.Module) -> list:
        """Return all AST nodes within the update_patterns function, or empty list."""
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "update_patterns":
                return list(ast.walk(node))
        return []

    def _has_len_gt_check(self, nodes: list, constant_name: str, constant_value: int) -> bool:
        """Return True if nodes contain: len(...) > constant_name (or > constant_value)."""
        for node in nodes:
            if (
                isinstance(node, ast.Compare)
                and len(node.ops) >= 1
                and isinstance(node.ops[0], ast.Gt)
                and isinstance(node.left, ast.Call)
                and isinstance(node.left.func, ast.Name)
                and node.left.func.id == "len"
            ):
                for comp in node.comparators:
                    if (isinstance(comp, ast.Name) and comp.id == constant_name) or (
                        isinstance(comp, ast.Constant) and comp.value == constant_value
                    ):
                        return True
        return False

    def test_id_exceeding_max_length_is_rejected(self):
        """update_patterns must contain len(pid) > MAX_ID_LENGTH to reject oversized ids."""
        _assert_file_exists()
        tree = _parse_ast(TEMPLATE_STOP_PY)
        nodes = self._get_update_patterns_nodes(tree)
        assert nodes, "update_patterns function not found in template stop.py"
        assert self._has_len_gt_check(nodes, "MAX_ID_LENGTH", 64), (
            "update_patterns must contain `len(pid) > MAX_ID_LENGTH` (or > 64) "
            "to reject patterns with id longer than MAX_ID_LENGTH characters. "
            "Add: if not pid or len(pid) > MAX_ID_LENGTH: continue"
        )

    def test_id_at_max_length_is_accepted(self):
        """update_patterns must use > (not >=) for id length, so exact max is accepted."""
        _assert_file_exists()
        tree = _parse_ast(TEMPLATE_STOP_PY)
        nodes = self._get_update_patterns_nodes(tree)
        assert nodes, "update_patterns function not found in template stop.py"
        assert self._has_len_gt_check(nodes, "MAX_ID_LENGTH", 64), (
            "update_patterns must use strict `>` (not `>=`) for id length check. "
            "Patterns with id of exactly MAX_ID_LENGTH characters must be accepted. "
            "Correct: len(pid) > MAX_ID_LENGTH"
        )

    def test_description_exceeding_max_length_is_rejected(self):
        """update_patterns must contain len(description) > MAX_DESCRIPTION_LENGTH."""
        _assert_file_exists()
        tree = _parse_ast(TEMPLATE_STOP_PY)
        nodes = self._get_update_patterns_nodes(tree)
        assert nodes, "update_patterns function not found in template stop.py"
        assert self._has_len_gt_check(nodes, "MAX_DESCRIPTION_LENGTH", 500), (
            "update_patterns must contain `len(description) > MAX_DESCRIPTION_LENGTH` "
            "(or > 500) to reject oversized descriptions. "
            "Add: if len(description) > MAX_DESCRIPTION_LENGTH: continue"
        )

    def test_description_at_max_length_is_accepted(self):
        """update_patterns must use > (not >=) for description length, so exact max is accepted."""
        _assert_file_exists()
        tree = _parse_ast(TEMPLATE_STOP_PY)
        nodes = self._get_update_patterns_nodes(tree)
        assert nodes, "update_patterns function not found in template stop.py"
        assert self._has_len_gt_check(nodes, "MAX_DESCRIPTION_LENGTH", 500), (
            "update_patterns must use strict `>` (not `>=`) for description length check. "
            "Patterns with description of exactly MAX_DESCRIPTION_LENGTH characters must be accepted. "
            "Correct: len(description) > MAX_DESCRIPTION_LENGTH"
        )


# ---------------------------------------------------------------------------
# [Code Low-N4] No function uses `yyyymmdd` as a parameter name
# ---------------------------------------------------------------------------

class TestNoYyyymmddParameterName:
    """[Code Low-N4] All functions must use `date_str`, not `yyyymmdd`, as parameter name."""

    def test_no_yyyymmdd_in_any_function_signature(self):
        """No function in template stop.py may use `yyyymmdd` as a parameter name."""
        _assert_file_exists()
        tree = _parse_ast(TEMPLATE_STOP_PY)
        violations: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for arg in node.args.args:
                    if arg.arg == "yyyymmdd":
                        violations.append(
                            f"  function '{node.name}' at line {node.lineno} "
                            f"uses 'yyyymmdd' as a parameter name"
                        )
        assert not violations, (
            "template stop.py must not use 'yyyymmdd' as a parameter name "
            "in any function. Rename to 'date_str'.\nViolations:\n"
            + "\n".join(violations)
        )
