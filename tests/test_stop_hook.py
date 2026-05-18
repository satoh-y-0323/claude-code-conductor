"""
Tests for .claude/hooks/stop.py (Round 5 - Red phase)

  TestSavePatternsAtomicWrite (Medium-1)
    - save_patterns() must use os.replace (or tempfile + rename) for atomic write

  TestBuildSessionsByDateReturnsSet (Medium-3)
    - _build_sessions_by_date() must return a set, not a dict

Tests for .claude/hooks/stop.py (Round 6 - Red phase)

  TestApplySessionUpdatesAtomicWrite (Medium-1 Round 6)
    - _apply_session_updates() must use os.replace or tempfile for atomic write
"""

from __future__ import annotations

import ast
import importlib.util
import json
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parent.parent
HOOKS_DIR = WORKTREE_ROOT / ".claude" / "hooks"
STOP_PY = HOOKS_DIR / "stop.py"


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

def _load_stop_module(module_name: str) -> types.ModuleType:
    """Load stop.py as a fresh module instance without registering in sys.modules."""
    spec = importlib.util.spec_from_file_location(module_name, STOP_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# TestSavePatternsAtomicWrite (Medium-1)
# ---------------------------------------------------------------------------


class TestSavePatternsAtomicWrite:
    """[Red] save_patterns() must use atomic write via os.replace / tempfile.

    Current implementation:
        def save_patterns(data: dict) -> None:
            os.makedirs(...)
            with open(PATTERNS_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ...)

    Problem: Direct overwrite with open(..., 'w') is not atomic.
    If the process is interrupted mid-write, patterns.json will be corrupted.
    enable_sandbox.py already uses tempfile.mkstemp + os.replace for atomic writes.

    Expected after fix:
        Use tempfile.mkstemp or a NamedTemporaryFile, write to temp file,
        then rename/replace to PATTERNS_FILE atomically using os.replace.

    実装側で修正済み（os.replace not used）。本テストは Green 回帰防止テスト。
    """

    def test_save_patterns_uses_atomic_write(self):
        """[Medium-1] save_patterns() must use os.replace or os.rename for atomic write.

        Verification: AST analysis of stop.py to check that save_patterns()
        uses os.replace (or os.rename) for the final write operation, OR
        uses tempfile module (mkstemp / NamedTemporaryFile).

        本テストは Green 回帰防止テスト（実装側修正済み）。修正前は save_patterns()
        uses direct open(..., 'w') without atomic replace.
        """
        source = STOP_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Find save_patterns function node
        fn_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "save_patterns":
                fn_node = node
                break

        assert fn_node is not None, "save_patterns function not found in stop.py"

        # Check for os.replace or os.rename call within save_patterns
        has_os_replace = False
        for node in ast.walk(fn_node):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                # os.replace(src, dst) or os.rename(src, dst)
                if func.attr in ("replace", "rename") and isinstance(func.value, ast.Name) and func.value.id == "os":
                    has_os_replace = True
                    break
                # os.path.replace は存在しないが念のため os.replace を含む
                if func.attr == "replace" and isinstance(func.value, ast.Attribute) and func.value.attr == "path":
                    has_os_replace = True
                    break

        # Check for tempfile module usage within save_patterns
        has_tempfile = False
        fn_src = ast.dump(fn_node)
        if "mkstemp" in fn_src or "NamedTemporaryFile" in fn_src or "tempfile" in fn_src:
            has_tempfile = True

        assert has_os_replace or has_tempfile, (
            "[Medium-1] save_patterns() must use atomic write.\n"
            "Current implementation: open(PATTERNS_FILE, 'w') directly overwrites the file.\n"
            "This is NOT atomic: if interrupted mid-write, patterns.json will be corrupted.\n"
            "enable_sandbox.py already uses tempfile.mkstemp + os.replace as a reference.\n"
            "Expected fix: use tempfile.mkstemp + os.replace, or os.rename after writing to a temp file.\n"
            "AST check: os.replace / os.rename / tempfile.mkstemp / NamedTemporaryFile not found "
            "inside save_patterns()."
        )


# ---------------------------------------------------------------------------
# TestBuildSessionsByDateReturnsSet (Medium-3)
# ---------------------------------------------------------------------------


class TestBuildSessionsByDateReturnsSet:
    """[Red] _build_sessions_by_date() must return a set[str], not a dict.

    Current implementation:
        def _build_sessions_by_date(sessions_dir: str) -> dict:
            ...
            result = {}
            for fname in os.listdir(sessions_dir):
                if fname.endswith('.tmp'):
                    result[fname[:-4]] = True   # dict with sentinel True values
            return result

    Problem: The return value is used only for key membership checks (``key in result``).
    The dict is effectively a set, but the type annotation and implementation say dict.
    This is misleading and wastes memory storing True sentinel values.

    Expected after fix:
        - Return type annotation is set or set[str]
        - Return statement returns set(...) or uses a set literal / set comprehension

    本テストは Green 回帰防止テスト（実装側修正済み）。修正前は:
        1. Type annotation is 'dict' not 'set'
        2. Return statement builds a dict, not a set
    """

    def test_build_sessions_by_date_returns_set(self):
        """[Medium-3] _build_sessions_by_date() must be annotated as -> set and return a set.

        Verification: AST analysis of stop.py to check:
          1. The return type annotation of _build_sessions_by_date is 'set' (or set[str])
          2. OR the return statement returns a set literal / set() / set comprehension

        本テストは Green 回帰防止テスト（実装側修正済み）。修正前は both checks fail:
          - annotation is 'dict'
          - return statement builds and returns a dict {}
        """
        source = STOP_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Find _build_sessions_by_date function node
        fn_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_build_sessions_by_date":
                fn_node = node
                break

        assert fn_node is not None, "_build_sessions_by_date function not found in stop.py"

        # Check 1: return type annotation is set or set[str]
        has_set_annotation = False
        returns = fn_node.returns
        if returns is not None:
            # -> set
            if isinstance(returns, ast.Name) and returns.id == "set":
                has_set_annotation = True
            # -> set[str] (subscript form)
            elif isinstance(returns, ast.Subscript):
                subscript_value = returns.value
                if isinstance(subscript_value, ast.Name) and subscript_value.id == "set":
                    has_set_annotation = True
            # -> Set[str] from typing (capital S)
            elif isinstance(returns, ast.Name) and returns.id == "Set":
                has_set_annotation = True

        # Check 2: any Return statement inside fn returns a set literal, set(), or set comprehension
        has_set_return = False
        for node in ast.walk(fn_node):
            if not isinstance(node, ast.Return):
                continue
            val = node.value
            if val is None:
                continue
            # set() call: return set(...)
            if isinstance(val, ast.Call) and isinstance(val.func, ast.Name) and val.func.id == "set":
                has_set_return = True
                break
            # set literal: return {expr, ...} — ast.Set node (not ast.Dict)
            if isinstance(val, ast.Set):
                has_set_return = True
                break
            # set comprehension: return {expr for ...}
            if isinstance(val, ast.SetComp):
                has_set_return = True
                break

        assert has_set_annotation or has_set_return, (
            "[Medium-3] _build_sessions_by_date() must return a set, not a dict.\n"
            "Current implementation:\n"
            "  - Return annotation: -> dict  (should be -> set or -> set[str])\n"
            "  - Return statement: returns a dict {} with True sentinel values\n"
            "The return value is only used for key membership checks (``key in result``).\n"
            "Expected fix:\n"
            "  - Change annotation to -> set[str]\n"
            "  - Change implementation to return a set comprehension or set()\n"
            "  Example: return {fname[:-4] for fname in os.listdir(sessions_dir) if fname.endswith('.tmp')}\n"
            "AST check: set annotation or set/SetComp/set-literal return not found."
        )


# ---------------------------------------------------------------------------
# TestApplySessionUpdatesAtomicWrite (Round 6 Medium-1)
# ---------------------------------------------------------------------------


class TestApplySessionUpdatesAtomicWrite:
    """[Red Round 6] _apply_session_updates() must use atomic write via os.replace / tempfile.

    Current implementation:
        def _apply_session_updates(path: str, content: str, message: str = '') -> None:
            ...
            if updated != content:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(updated)

    Problem: Direct overwrite with open(path, 'w') is not atomic.
    If the process is interrupted mid-write, the session file will be corrupted.
    save_patterns() in the same file already uses tempfile.mkstemp + os.replace
    as the correct atomic write pattern.

    Expected after fix:
        Use tempfile.mkstemp (or NamedTemporaryFile) in the same directory,
        write updated content to temp file, then atomically replace with os.replace().

    本テストは Green 回帰防止テスト（実装側修正済み）。修正前は _apply_session_updates()
    uses direct open(path, 'w') without any atomic replace.
    """

    def test_apply_session_updates_uses_atomic_write(self):
        """[Round 6 Medium-1] _apply_session_updates() must use os.replace or tempfile for atomic write.

        Verification: AST analysis of stop.py to check that _apply_session_updates()
        uses os.replace (or os.rename) for the final write operation, OR
        uses tempfile module (mkstemp / NamedTemporaryFile).

        本テストは Green 回帰防止テスト（実装側修正済み）。修正前は _apply_session_updates()
        uses direct open(path, 'w') without atomic replace.
        """
        source = STOP_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Find _apply_session_updates function node
        fn_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_apply_session_updates":
                fn_node = node
                break

        assert fn_node is not None, "_apply_session_updates function not found in stop.py"

        # Check for os.replace or os.rename call within _apply_session_updates
        has_os_replace = False
        for node in ast.walk(fn_node):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                # os.replace(src, dst) or os.rename(src, dst)
                if (
                    func.attr in ("replace", "rename")
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                ):
                    has_os_replace = True
                    break

        # Check for tempfile module usage within _apply_session_updates
        has_tempfile = False
        fn_src = ast.dump(fn_node)
        if "mkstemp" in fn_src or "NamedTemporaryFile" in fn_src or "tempfile" in fn_src:
            has_tempfile = True

        assert has_os_replace or has_tempfile, (
            "[Round 6 Medium-1] _apply_session_updates() must use atomic write.\n"
            "Current implementation: open(path, 'w') directly overwrites the session file.\n"
            "This is NOT atomic: if interrupted mid-write, the session file will be corrupted.\n"
            "save_patterns() in stop.py already uses tempfile.mkstemp + os.replace as a reference.\n"
            "Expected fix: use tempfile.mkstemp + os.replace, or os.rename after writing to a temp file.\n"
            "Example:\n"
            "    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path))\n"
            "    try:\n"
            "        with os.fdopen(fd, 'w', encoding='utf-8') as tmp_f:\n"
            "            tmp_f.write(updated)\n"
            "    except Exception:\n"
            "        os.close(fd)\n"
            "        raise\n"
            "    os.replace(tmp_path, path)\n"
            "AST check: os.replace / os.rename / tempfile.mkstemp / NamedTemporaryFile not found "
            "inside _apply_session_updates()."
        )
