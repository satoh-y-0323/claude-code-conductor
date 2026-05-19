"""
Regression guard for the fix-stop-precompact task.

These tests pin the planned fixes in place. They were originally written
BEFORE the implementation as TDD Red tests; the implementation is now in
place and all tests pass.

Fixes under test:
  [Code High-2]   update_patterns caches os.listdir result (O(N+M) instead of O(N×M))
  [Code Medium-3] SESSION_JSON_MARKER constant in pre_compact.py; create_session_template
                  argument name unified to `date_str` in both files
  [Code Low-1]    stop.py import order follows PEP8 (reconfigure after all imports)
  [Sec Medium-1]  update_patterns whitelists allowed fields and enforces length limits
                  (id: 64 chars, description: 500 chars; `promoted` field NOT set)
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import os
import sys
import textwrap
import types
import unittest
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parent.parent
HOOKS_DIR = WORKTREE_ROOT / ".claude" / "hooks"
STOP_PY = HOOKS_DIR / "stop.py"
PRE_COMPACT_PY = HOOKS_DIR / "pre_compact.py"
SESSION_UTILS_PY = HOOKS_DIR / "session_utils.py"

TODAY_STR = date.today().strftime("%Y%m%d")


def _load_module_from_path(path: Path, module_name: str) -> types.ModuleType:
    """Load a standalone script as a module without executing __main__."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_ast(path: Path) -> ast.Module:
    return ast.parse(_read_source(path))


# ---------------------------------------------------------------------------
# [Code Low-1] PEP8 import order in stop.py
#   sys.stdout.reconfigure / sys.stderr.reconfigure must come AFTER all imports
# ---------------------------------------------------------------------------

class TestStopPyImportOrder:
    """[Code Low-1] sys.stdout/stderr.reconfigure must follow all import statements."""

    def test_reconfigure_after_all_imports(self):
        """All import statements must appear before sys.stdout.reconfigure calls."""
        tree = _parse_ast(STOP_PY)
        body = tree.body

        last_import_line = 0
        first_reconfigure_line = None

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                last_import_line = max(last_import_line, node.lineno)

        # Find first reconfigure call (at module top level, possibly inside a try block)
        def _find_reconfigure_line(stmts):
            """Return the line number of the first stdout/stderr.reconfigure call, or None."""
            for node in stmts:
                # Direct Expr statement: sys.stdout.reconfigure(...)
                if isinstance(node, ast.Expr):
                    call = node.value
                    if isinstance(call, ast.Call):
                        func = call.func
                        if (
                            isinstance(func, ast.Attribute)
                            and func.attr == "reconfigure"
                            and isinstance(func.value, ast.Attribute)
                            and func.value.attr in ("stdout", "stderr")
                        ):
                            return node.lineno
                # Try block: allow reconfigure to be wrapped in try/except AttributeError
                if isinstance(node, ast.Try):
                    result = _find_reconfigure_line(node.body)
                    if result is not None:
                        return node.lineno  # report the try block's line
            return None

        first_reconfigure_line = _find_reconfigure_line(body)

        assert first_reconfigure_line is not None, (
            "No sys.stdout.reconfigure/sys.stderr.reconfigure call found in stop.py"
        )
        assert first_reconfigure_line > last_import_line, (
            f"sys.stdout/stderr.reconfigure (line {first_reconfigure_line}) "
            f"must come AFTER all imports (last import at line {last_import_line}). "
            "Fix: move reconfigure calls below all import statements."
        )


# ---------------------------------------------------------------------------
# [Code Medium-3a] SESSION_JSON_MARKER constant in pre_compact.py
# ---------------------------------------------------------------------------

class TestPreCompactSessionJsonMarker:
    """[Code Medium-3a] pre_compact.py must define SESSION_JSON_MARKER as a module-level constant."""

    def test_session_json_marker_constant_defined(self):
        """pre_compact.py must export SESSION_JSON_MARKER = 'C3:SESSION:JSON'."""
        mod = _load_module_from_path(PRE_COMPACT_PY, "_pre_compact_test_marker")
        assert hasattr(mod, "SESSION_JSON_MARKER"), (
            "pre_compact.py must define SESSION_JSON_MARKER at module level. "
            "Add: SESSION_JSON_MARKER = 'C3:SESSION:JSON'"
        )
        assert mod.SESSION_JSON_MARKER == "C3:SESSION:JSON", (
            f"Expected SESSION_JSON_MARKER == 'C3:SESSION:JSON', "
            f"got {mod.SESSION_JSON_MARKER!r}"
        )

    def test_hardcoded_marker_string_not_in_create_session_template(self):
        """create_session_template in session_utils.py must use SESSION_JSON_MARKER, not a raw string.

        After the fix, the function body must reference the SESSION_JSON_MARKER name;
        the substring 'C3:SESSION:JSON' must NOT appear in any string literal within
        the function body.
        """
        tree = _parse_ast(SESSION_UTILS_PY)
        fn_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "create_session_template":
                fn_node = node
                break

        assert fn_node is not None, "create_session_template function not found in session_utils.py"

        # Collect all string constants (including substrings) inside the function
        for node in ast.walk(fn_node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "C3:SESSION:JSON" not in node.value, (
                    f"create_session_template in session_utils.py contains the literal "
                    f"string 'C3:SESSION:JSON' embedded in a string constant. "
                    "It must use the SESSION_JSON_MARKER constant instead."
                )

        # Also verify the function references SESSION_JSON_MARKER as a Name node
        name_refs = [
            node.id
            for node in ast.walk(fn_node)
            if isinstance(node, ast.Name)
        ]
        assert "SESSION_JSON_MARKER" in name_refs, (
            "create_session_template in session_utils.py must reference SESSION_JSON_MARKER "
            "as a variable (not embed the literal string)."
        )


# ---------------------------------------------------------------------------
# [Code Medium-3b] create_session_template argument name unified to `date_str`
# ---------------------------------------------------------------------------

class TestCreateSessionTemplateSignature:
    """[Code Medium-3b] Both files must use `date_str` (not `yyyymmdd`) as the argument name."""

    def _get_param_names(self, path: Path) -> list[str]:
        tree = _parse_ast(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "create_session_template":
                return [arg.arg for arg in node.args.args]
        return []

    def test_stop_py_uses_date_str(self):
        """session_utils.py: create_session_template must use `date_str` as argument name."""
        params = self._get_param_names(SESSION_UTILS_PY)
        assert params, "create_session_template not found in session_utils.py"
        assert "date_str" in params, (
            f"session_utils.py create_session_template uses {params!r}; "
            "expected 'date_str'. Rename the argument from 'yyyymmdd' to 'date_str'."
        )
        assert "yyyymmdd" not in params, (
            f"session_utils.py create_session_template still uses old name 'yyyymmdd'; "
            "rename it to 'date_str'."
        )

    def test_pre_compact_py_uses_date_str(self):
        """session_utils.py: create_session_template must use `date_str` as argument name."""
        params = self._get_param_names(SESSION_UTILS_PY)
        assert params, "create_session_template not found in session_utils.py"
        assert "date_str" in params, (
            f"session_utils.py create_session_template uses {params!r}; expected 'date_str'."
        )


# ---------------------------------------------------------------------------
# [Code High-2] update_patterns caches os.listdir (called only once per invocation)
# ---------------------------------------------------------------------------

class TestUpdatePatternsCachesListdir:
    """[Code High-2] os.listdir must be called at most once inside update_patterns."""

    def _make_temp_env(self, tmp_path: Path) -> dict[str, Path]:
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        patterns_file = tmp_path / "patterns.json"
        # Create a few session files so the loop has real work to do
        for d in [TODAY_STR]:
            (sessions_dir / f"{d}.tmp").write_text("session", encoding="utf-8")
        return {"sessions_dir": sessions_dir, "patterns_file": patterns_file}

    def test_listdir_called_at_most_once(self, tmp_path):
        """When processing N patterns, os.listdir must be called at most once.

        Current behavior (pre-fix): count_sessions_since is called once per pattern
        in the active-patterns loop, each time calling os.listdir. With N patterns,
        os.listdir is called N times → O(N×M).

        Fixed behavior: sessions_by_date dict is built once before the loop,
        so os.listdir is called only once → O(N+M).
        """
        env = self._make_temp_env(tmp_path)

        mod = _load_module_from_path(STOP_PY, "_stop_test_listdir")
        # Override module-level path constants
        mod.SESSIONS_DIR = str(env["sessions_dir"])
        mod.PATTERNS_FILE = str(env["patterns_file"])

        # Prepare a patterns.json with multiple patterns, all using TODAY_STR
        patterns_data = {
            "patterns": [
                {
                    "id": f"pat-{i}",
                    "description": f"pattern {i}",
                    "registered_date": TODAY_STR,
                    "trust_score": 0.1,
                    "promotion_candidate": False,
                    "observations": [{"date": TODAY_STR}],
                    "last_updated": TODAY_STR,
                }
                for i in range(5)
            ]
        }
        env["patterns_file"].write_text(
            json.dumps(patterns_data), encoding="utf-8"
        )

        session_files = [f"{TODAY_STR}.tmp"]
        call_count = {"n": 0}
        original_listdir = os.listdir

        def counting_listdir(path="."):
            # Only count calls to sessions_dir
            try:
                normalized_path = str(Path(str(path)).resolve())
                normalized_sessions = str(env["sessions_dir"].resolve())
                if normalized_path == normalized_sessions:
                    call_count["n"] += 1
                    return session_files
            except Exception:
                pass
            return original_listdir(path)

        # Patch os.listdir at the OS module level that the loaded module references
        import unittest.mock
        with unittest.mock.patch.object(mod.os, "listdir", side_effect=counting_listdir):
            mod.update_patterns(TODAY_STR)

        assert call_count["n"] <= 1, (
            f"os.listdir (for sessions dir) was called {call_count['n']} times during "
            "update_patterns with 5 patterns. Expected at most 1 call (cache the result "
            "before the active-patterns loop). This is the O(N×M) → O(N+M) fix.\n"
            "Hint: build a sessions_by_date dict once before the loop and use it in "
            "count_sessions_since instead of calling os.listdir each iteration."
        )


# ---------------------------------------------------------------------------
# [Sec Medium-1] update_patterns whitelist: allowed fields, length limits, no `promoted`
# ---------------------------------------------------------------------------

class TestUpdatePatternsWhitelist:
    """[Sec Medium-1] update_patterns must whitelist fields and enforce length limits."""

    def _setup_mod(self, tmp_path: Path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        patterns_file = tmp_path / "patterns.json"
        mod = _load_module_from_path(STOP_PY, f"_stop_test_whitelist_{tmp_path.name}")
        mod.SESSIONS_DIR = str(sessions_dir)
        mod.PATTERNS_FILE = str(patterns_file)
        patterns_data: dict[str, Any] = {"patterns": []}
        patterns_file.write_text(json.dumps(patterns_data), encoding="utf-8")
        return mod, sessions_dir, patterns_file

    def _write_session_with_patterns(self, sessions_dir: Path, date_str: str, patterns: list):
        """Write a session .tmp file containing the given patterns in the JSON block."""
        content = (
            f"SESSION: {date_str}\n"
            f"AGENT: \n"
            f"DURATION: \n"
            f"\n"
            f"<!-- C3:SESSION:JSON\n"
            f"{{\n"
            f'  "session": "{date_str}",\n'
            f'  "patterns": {json.dumps(patterns)},\n'
            f'  "successes": [],\n'
            f'  "failures": [],\n'
            f'  "todos": []\n'
            f"}}\n"
            f"-->\n"
        )
        (sessions_dir / f"{date_str}.tmp").write_text(content, encoding="utf-8")

    def test_extra_fields_not_stored(self, tmp_path):
        """Fields beyond id/description/registered_date must not be stored by update_patterns."""
        mod, sessions_dir, patterns_file = self._setup_mod(tmp_path)
        self._write_session_with_patterns(sessions_dir, TODAY_STR, [
            {"id": "pat-extra", "description": "test", "evil_field": "injected"}
        ])

        mod.update_patterns(TODAY_STR)

        data = json.loads(patterns_file.read_text(encoding="utf-8"))
        stored = next((p for p in data["patterns"] if p["id"] == "pat-extra"), None)
        assert stored is not None, "Pattern pat-extra should have been stored"
        assert "evil_field" not in stored, (
            "'evil_field' must not be copied into patterns.json. "
            "update_patterns must only extract whitelisted fields (id, description) "
            "from the observation data."
        )

    def test_promoted_field_not_set_on_new_pattern(self, tmp_path):
        """update_patterns must NOT set the `promoted` field when adding new patterns."""
        mod, sessions_dir, patterns_file = self._setup_mod(tmp_path)
        self._write_session_with_patterns(sessions_dir, TODAY_STR, [
            {"id": "pat-promo", "description": "test", "promoted": True}
        ])

        mod.update_patterns(TODAY_STR)

        data = json.loads(patterns_file.read_text(encoding="utf-8"))
        stored = next((p for p in data["patterns"] if p["id"] == "pat-promo"), None)
        assert stored is not None, "Pattern pat-promo should have been stored"
        assert stored.get("promoted") is not True, (
            "update_patterns must not set `promoted: true` when adding new patterns. "
            "The `promoted` field is set only by the promote-pattern command. "
            "Do not copy `promoted` from the observation payload into the stored pattern."
        )

    def test_id_length_limit_enforced(self, tmp_path):
        """Patterns with id longer than 64 characters must be rejected."""
        mod, sessions_dir, patterns_file = self._setup_mod(tmp_path)
        long_id = "a" * 65
        self._write_session_with_patterns(sessions_dir, TODAY_STR, [
            {"id": long_id, "description": "test"}
        ])

        mod.update_patterns(TODAY_STR)

        data = json.loads(patterns_file.read_text(encoding="utf-8"))
        ids = [p["id"] for p in data["patterns"]]
        assert long_id not in ids, (
            "Pattern with id of length 65 (> 64) must be rejected by update_patterns. "
            "Add: if not pid or len(pid) > 64: continue"
        )

    def test_id_at_max_length_accepted(self, tmp_path):
        """Patterns with id exactly 64 characters long must be accepted."""
        mod, sessions_dir, patterns_file = self._setup_mod(tmp_path)
        exact_id = "b" * 64
        self._write_session_with_patterns(sessions_dir, TODAY_STR, [
            {"id": exact_id, "description": "test"}
        ])

        mod.update_patterns(TODAY_STR)

        data = json.loads(patterns_file.read_text(encoding="utf-8"))
        ids = [p["id"] for p in data["patterns"]]
        assert exact_id in ids, (
            "Pattern with id of exactly 64 characters must be accepted. "
            "The length check should be: len(pid) > 64, not len(pid) >= 64."
        )

    def test_description_length_limit_enforced(self, tmp_path):
        """Patterns with description longer than 500 characters must be rejected."""
        mod, sessions_dir, patterns_file = self._setup_mod(tmp_path)
        long_desc = "x" * 501
        self._write_session_with_patterns(sessions_dir, TODAY_STR, [
            {"id": "pat-long-desc", "description": long_desc}
        ])

        mod.update_patterns(TODAY_STR)

        data = json.loads(patterns_file.read_text(encoding="utf-8"))
        stored = next((p for p in data["patterns"] if p["id"] == "pat-long-desc"), None)
        assert stored is None, (
            "Pattern with description longer than 500 characters must be rejected. "
            "Add: if len(description) > 500: continue"
        )

    def test_description_at_max_length_accepted(self, tmp_path):
        """Patterns with description exactly 500 characters long must be accepted."""
        mod, sessions_dir, patterns_file = self._setup_mod(tmp_path)
        exact_desc = "y" * 500
        self._write_session_with_patterns(sessions_dir, TODAY_STR, [
            {"id": "pat-exact-desc", "description": exact_desc}
        ])

        mod.update_patterns(TODAY_STR)

        data = json.loads(patterns_file.read_text(encoding="utf-8"))
        stored = next((p for p in data["patterns"] if p["id"] == "pat-exact-desc"), None)
        assert stored is not None, (
            "Pattern with description of exactly 500 characters must be accepted. "
            "The length check should be: len(description) > 500, not len(description) >= 500."
        )
