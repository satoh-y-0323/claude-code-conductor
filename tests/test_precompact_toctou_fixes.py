"""
Red-phase tests for fix-precompact-toctou task.

These tests verify the planned fixes are in place. They are written
BEFORE the implementation, so they are expected to FAIL initially.

Fixes under test:
  [Fix 1] TOCTOU in .claude/hooks/pre_compact.py (main)
           open(session_file, 'x') with FileExistsError catch
  [Fix 2] TOCTOU in src/c3/_template/.claude/hooks/pre_compact.py (template)
           same open(..., 'x') fix in the template file
  [Fix 3] SESSION_JSON_MARKER constant defined at module level in the template file
           and used (not hardcoded) inside create_session_template
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open, call

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parent.parent
MAIN_PRE_COMPACT = WORKTREE_ROOT / ".claude" / "hooks" / "pre_compact.py"
TEMPLATE_PRE_COMPACT = (
    WORKTREE_ROOT / "src" / "c3" / "_template" / ".claude" / "hooks" / "pre_compact.py"
)
SESSION_UTILS_PY = WORKTREE_ROOT / ".claude" / "hooks" / "session_utils.py"
TEMPLATE_SESSION_UTILS_PY = (
    WORKTREE_ROOT / "src" / "c3" / "_template" / ".claude" / "hooks" / "session_utils.py"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_ast(path: Path) -> ast.Module:
    return ast.parse(_read_source(path))


def _load_module_from_path(path: Path, module_name: str) -> types.ModuleType:
    """Load a standalone script as a module without executing __main__."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _find_open_calls_in_function(fn_node: ast.FunctionDef) -> list[ast.Call]:
    """Return all ast.Call nodes inside fn_node where the function is `open`."""
    calls = []
    for node in ast.walk(fn_node):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "open":
                calls.append(node)
    return calls


def _get_open_mode_arg(call_node: ast.Call) -> str | None:
    """Extract the mode string from an open() call (2nd positional or keyword 'mode')."""
    # Positional: open(path, mode, ...)
    if len(call_node.args) >= 2:
        mode_node = call_node.args[1]
        if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
            return mode_node.value
    # Keyword: open(path, mode='x', ...)
    for kw in call_node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def _get_main_function(tree: ast.Module) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    return None


def _get_append_checkpoint_function(tree: ast.Module) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "append_checkpoint":
            return node
    return None


def _try_except_has_file_exists_error(try_node: ast.Try) -> bool:
    """Return True if any handler in the try block catches FileExistsError."""
    for handler in try_node.handlers:
        exc = handler.type
        if exc is None:
            # bare except — counts as catching FileExistsError
            return True
        if isinstance(exc, ast.Name) and exc.id == "FileExistsError":
            return True
        if isinstance(exc, ast.Tuple):
            for elt in exc.elts:
                if isinstance(elt, ast.Name) and elt.id == "FileExistsError":
                    return True
    return False


def _find_toctou_fix_in_function(fn_node: ast.FunctionDef) -> tuple[bool, str]:
    """
    Check whether the function uses the TOCTOU-safe pattern:
        try:
            with open(..., 'x') as f:
                ...
        except FileExistsError:
            pass

    Returns (passed, reason).
    """
    # Walk top-level statements looking for a Try block that:
    #  1. Contains an open() call with mode 'x' in its body
    #  2. Has a FileExistsError handler
    for node in ast.walk(fn_node):
        if not isinstance(node, ast.Try):
            continue
        # Check body for open(..., 'x')
        has_exclusive_open = False
        for child in ast.walk(ast.Module(body=node.body, type_ignores=[])):
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Name) and func.id == "open":
                    mode = _get_open_mode_arg(child)
                    if mode is not None and "x" in mode:
                        has_exclusive_open = True
                        break
        if has_exclusive_open and _try_except_has_file_exists_error(node):
            return True, "OK"

    # Also reject if old TOCTOU pattern (os.path.exists check + open 'w') is present
    has_exists_guard = False
    for node in ast.walk(fn_node):
        if isinstance(node, ast.If):
            # Check for `if not os.path.exists(...)` or `if os.path.exists(...)` patterns
            test = node.test
            # Unwrap `not ...`
            inner = test
            if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                inner = test.operand
            if isinstance(inner, ast.Call):
                func = inner.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "exists"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "path"
                ):
                    # Check body for open(..., 'w')
                    for body_node in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                        if isinstance(body_node, ast.Call):
                            bfunc = body_node.func
                            if isinstance(bfunc, ast.Name) and bfunc.id == "open":
                                mode = _get_open_mode_arg(body_node)
                                if mode is not None and "w" in mode:
                                    has_exists_guard = True
                                    break

    if has_exists_guard:
        return False, (
            "Found old TOCTOU pattern: `if not os.path.exists(session_file): open(..., 'w')`. "
            "Replace with: try: open(..., 'x') except FileExistsError: pass"
        )

    return False, (
        "Could not find TOCTOU-safe pattern: "
        "try: open(..., 'x') ... except FileExistsError: pass"
    )


# ---------------------------------------------------------------------------
# [Fix 1] TOCTOU fix in session_utils.py::append_checkpoint
# ---------------------------------------------------------------------------

class TestAppendCheckpointToctouFix:
    """[Fix 1] .claude/hooks/session_utils.py::append_checkpoint must use open(..., 'x') with FileExistsError guard."""

    def test_main_file_exists(self):
        """The session_utils.py file must exist."""
        assert SESSION_UTILS_PY.exists(), (
            f"session_utils.py not found at {SESSION_UTILS_PY}"
        )

    def test_main_no_toctou_exists_guard(self):
        """append_checkpoint must NOT use the TOCTOU-prone `if not os.path.exists(...): open(w)` pattern."""
        tree = _parse_ast(SESSION_UTILS_PY)
        fn = _get_append_checkpoint_function(tree)
        assert fn is not None, "append_checkpoint function not found in session_utils.py"

        for node in ast.walk(fn):
            if isinstance(node, ast.If):
                test = node.test
                inner = test
                if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                    inner = test.operand
                if isinstance(inner, ast.Call):
                    func = inner.func
                    if (
                        isinstance(func, ast.Attribute)
                        and func.attr == "exists"
                        and isinstance(func.value, ast.Attribute)
                        and func.value.attr == "path"
                    ):
                        for body_node in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                            if isinstance(body_node, ast.Call):
                                bfunc = body_node.func
                                if isinstance(bfunc, ast.Name) and bfunc.id == "open":
                                    mode = _get_open_mode_arg(body_node)
                                    if mode is not None and "w" in mode:
                                        raise AssertionError(
                                            "Found TOCTOU bug in session_utils.py::append_checkpoint: "
                                            "`if not os.path.exists(session_file): open(..., 'w')`. "
                                            "Replace with: try: open(..., 'x') except FileExistsError: pass"
                                        )

    def test_main_uses_exclusive_open(self):
        """append_checkpoint must use open(session_file, 'x') for TOCTOU-safe file creation."""
        tree = _parse_ast(SESSION_UTILS_PY)
        fn = _get_append_checkpoint_function(tree)
        assert fn is not None, "append_checkpoint function not found in session_utils.py"

        passed, reason = _find_toctou_fix_in_function(fn)
        assert passed, (
            f"TOCTOU fix not found in session_utils.py::append_checkpoint: {reason}"
        )

    def test_main_file_exists_error_caught(self):
        """The try block around open(..., 'x') must catch FileExistsError."""
        tree = _parse_ast(SESSION_UTILS_PY)
        fn = _get_append_checkpoint_function(tree)
        assert fn is not None, "append_checkpoint function not found in session_utils.py"

        found_try = False
        for node in ast.walk(fn):
            if not isinstance(node, ast.Try):
                continue
            has_x_open = False
            for child in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if isinstance(child, ast.Call):
                    func = child.func
                    if isinstance(func, ast.Name) and func.id == "open":
                        mode = _get_open_mode_arg(child)
                        if mode is not None and "x" in mode:
                            has_x_open = True
                            break
            if has_x_open:
                found_try = True
                assert _try_except_has_file_exists_error(node), (
                    "The try block wrapping open(..., 'x') in session_utils.py::append_checkpoint "
                    "must catch FileExistsError. "
                    "Add: except FileExistsError: pass"
                )
                break

        assert found_try, (
            "Could not find a try block containing open(..., 'x') in session_utils.py::append_checkpoint. "
            "Expected: try: open(session_file, 'x') ... except FileExistsError: pass"
        )


# ---------------------------------------------------------------------------
# [Fix 2] TOCTOU fix in TEMPLATE session_utils.py::append_checkpoint
# ---------------------------------------------------------------------------

class TestTemplatePreCompactToctouFix:
    """[Fix 2] src/c3/_template/.claude/hooks/session_utils.py::append_checkpoint must use open(..., 'x') pattern."""

    def test_template_file_exists(self):
        """The template session_utils.py file must exist."""
        assert TEMPLATE_SESSION_UTILS_PY.exists(), (
            f"Template session_utils.py not found at {TEMPLATE_SESSION_UTILS_PY}. "
            "Developer must ensure this file exists."
        )

    def test_template_no_toctou_exists_guard(self):
        """Template append_checkpoint must NOT use the TOCTOU-prone `if not os.path.exists(...): open(w)` pattern."""
        assert TEMPLATE_SESSION_UTILS_PY.exists(), (
            f"Template file not found: {TEMPLATE_SESSION_UTILS_PY}"
        )
        tree = _parse_ast(TEMPLATE_SESSION_UTILS_PY)
        fn = _get_append_checkpoint_function(tree)
        assert fn is not None, "append_checkpoint function not found in template session_utils.py"

        for node in ast.walk(fn):
            if isinstance(node, ast.If):
                test = node.test
                inner = test
                if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                    inner = test.operand
                if isinstance(inner, ast.Call):
                    func = inner.func
                    if (
                        isinstance(func, ast.Attribute)
                        and func.attr == "exists"
                        and isinstance(func.value, ast.Attribute)
                        and func.value.attr == "path"
                    ):
                        for body_node in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                            if isinstance(body_node, ast.Call):
                                bfunc = body_node.func
                                if isinstance(bfunc, ast.Name) and bfunc.id == "open":
                                    mode = _get_open_mode_arg(body_node)
                                    if mode is not None and "w" in mode:
                                        raise AssertionError(
                                            "Found TOCTOU bug in template session_utils.py::append_checkpoint: "
                                            "`if not os.path.exists(session_file): open(..., 'w')`. "
                                            "Replace with: try: open(..., 'x') except FileExistsError: pass"
                                        )

    def test_template_uses_exclusive_open(self):
        """Template append_checkpoint must use open(session_file, 'x') for TOCTOU-safe file creation."""
        assert TEMPLATE_SESSION_UTILS_PY.exists(), (
            f"Template file not found: {TEMPLATE_SESSION_UTILS_PY}"
        )
        tree = _parse_ast(TEMPLATE_SESSION_UTILS_PY)
        fn = _get_append_checkpoint_function(tree)
        assert fn is not None, "append_checkpoint function not found in template session_utils.py"

        passed, reason = _find_toctou_fix_in_function(fn)
        assert passed, (
            f"TOCTOU fix not found in template session_utils.py::append_checkpoint: {reason}"
        )

    def test_template_file_exists_error_caught(self):
        """The try block around open(..., 'x') in template must catch FileExistsError."""
        assert TEMPLATE_SESSION_UTILS_PY.exists(), (
            f"Template file not found: {TEMPLATE_SESSION_UTILS_PY}"
        )
        tree = _parse_ast(TEMPLATE_SESSION_UTILS_PY)
        fn = _get_append_checkpoint_function(tree)
        assert fn is not None, "append_checkpoint function not found in template session_utils.py"

        found_try = False
        for node in ast.walk(fn):
            if not isinstance(node, ast.Try):
                continue
            has_x_open = False
            for child in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if isinstance(child, ast.Call):
                    func = child.func
                    if isinstance(func, ast.Name) and func.id == "open":
                        mode = _get_open_mode_arg(child)
                        if mode is not None and "x" in mode:
                            has_x_open = True
                            break
            if has_x_open:
                found_try = True
                assert _try_except_has_file_exists_error(node), (
                    "The try block wrapping open(..., 'x') in template session_utils.py::append_checkpoint "
                    "must catch FileExistsError. "
                    "Add: except FileExistsError: pass"
                )
                break

        assert found_try, (
            "Could not find a try block containing open(..., 'x') in template session_utils.py::append_checkpoint. "
            "Expected: try: open(session_file, 'x') ... except FileExistsError: pass"
        )


# ---------------------------------------------------------------------------
# [Fix 3] SESSION_JSON_MARKER constant in TEMPLATE session_utils.py
# ---------------------------------------------------------------------------

class TestTemplateSessionJsonMarker:
    """[Fix 3] Template session_utils.py must define SESSION_JSON_MARKER at module level
    and use it (not a raw string) inside create_session_template."""

    def test_template_session_json_marker_constant_defined(self):
        """Template must export SESSION_JSON_MARKER = 'C3:SESSION:JSON' at module level."""
        assert TEMPLATE_SESSION_UTILS_PY.exists(), (
            f"Template file not found: {TEMPLATE_SESSION_UTILS_PY}"
        )
        mod = _load_module_from_path(TEMPLATE_SESSION_UTILS_PY, "_template_session_utils_test_marker")
        assert hasattr(mod, "SESSION_JSON_MARKER"), (
            "Template session_utils.py must define SESSION_JSON_MARKER at module level. "
            "Add: SESSION_JSON_MARKER = 'C3:SESSION:JSON'"
        )
        assert mod.SESSION_JSON_MARKER == "C3:SESSION:JSON", (
            f"Expected SESSION_JSON_MARKER == 'C3:SESSION:JSON', got {mod.SESSION_JSON_MARKER!r}"
        )

    def test_template_no_hardcoded_marker_in_create_session_template(self):
        """Template create_session_template must NOT contain the raw 'C3:SESSION:JSON' string literal."""
        assert TEMPLATE_SESSION_UTILS_PY.exists(), (
            f"Template file not found: {TEMPLATE_SESSION_UTILS_PY}"
        )
        tree = _parse_ast(TEMPLATE_SESSION_UTILS_PY)

        fn_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "create_session_template":
                fn_node = node
                break

        assert fn_node is not None, (
            "create_session_template function not found in template session_utils.py"
        )

        for node in ast.walk(fn_node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "C3:SESSION:JSON" not in node.value, (
                    "Template create_session_template contains the literal string "
                    "'C3:SESSION:JSON' in a string constant. "
                    "It must use the SESSION_JSON_MARKER constant instead."
                )

    def test_template_create_session_template_references_marker(self):
        """Template create_session_template must reference SESSION_JSON_MARKER as a variable."""
        assert TEMPLATE_SESSION_UTILS_PY.exists(), (
            f"Template file not found: {TEMPLATE_SESSION_UTILS_PY}"
        )
        tree = _parse_ast(TEMPLATE_SESSION_UTILS_PY)

        fn_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "create_session_template":
                fn_node = node
                break

        assert fn_node is not None, (
            "create_session_template function not found in template session_utils.py"
        )

        name_refs = [
            node.id
            for node in ast.walk(fn_node)
            if isinstance(node, ast.Name)
        ]
        assert "SESSION_JSON_MARKER" in name_refs, (
            "Template create_session_template must reference SESSION_JSON_MARKER as a variable "
            "(not embed the literal string 'C3:SESSION:JSON')."
        )

    def test_template_marker_defined_at_module_level(self):
        """SESSION_JSON_MARKER must be a module-level assignment (not inside a function)."""
        assert TEMPLATE_SESSION_UTILS_PY.exists(), (
            f"Template file not found: {TEMPLATE_SESSION_UTILS_PY}"
        )
        tree = _parse_ast(TEMPLATE_SESSION_UTILS_PY)

        # Only check top-level assignments (tree.body, not inside functions)
        found = False
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "SESSION_JSON_MARKER":
                    if isinstance(node.value, ast.Constant) and node.value.value == "C3:SESSION:JSON":
                        found = True
                        break

        assert found, (
            "Template session_utils.py must have a top-level assignment: "
            "SESSION_JSON_MARKER = 'C3:SESSION:JSON'. "
            "This constant must not be inside a function."
        )
