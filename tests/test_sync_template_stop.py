"""
Red-phase tests for sync-template-stop task.

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
import importlib.util
import json
import os
import types
import unittest.mock
from datetime import date
from pathlib import Path
from typing import Any

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

TODAY_STR = date.today().strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assert_file_exists() -> None:
    """Fail (not skip) if the template stop.py does not exist."""
    assert TEMPLATE_STOP_PY.exists(), (
        f"Template stop.py not found at {TEMPLATE_STOP_PY}. "
        "The file must be created as part of the sync-template-stop task."
    )


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
        mod = _load_module_from_path(TEMPLATE_STOP_PY, "_tmpl_stop_test_const_id")
        assert hasattr(mod, "MAX_ID_LENGTH"), (
            "template stop.py must define MAX_ID_LENGTH at module level. "
            "Add: MAX_ID_LENGTH = 64"
        )
        assert mod.MAX_ID_LENGTH == 64, (
            f"Expected MAX_ID_LENGTH == 64, got {mod.MAX_ID_LENGTH!r}"
        )

    def test_max_description_length_constant_value(self):
        """MAX_DESCRIPTION_LENGTH must equal 500."""
        _assert_file_exists()
        mod = _load_module_from_path(TEMPLATE_STOP_PY, "_tmpl_stop_test_const_desc")
        assert hasattr(mod, "MAX_DESCRIPTION_LENGTH"), (
            "template stop.py must define MAX_DESCRIPTION_LENGTH at module level. "
            "Add: MAX_DESCRIPTION_LENGTH = 500"
        )
        assert mod.MAX_DESCRIPTION_LENGTH == 500, (
            f"Expected MAX_DESCRIPTION_LENGTH == 500, got {mod.MAX_DESCRIPTION_LENGTH!r}"
        )


# ---------------------------------------------------------------------------
# [Sec Medium-1T] update_patterns enforces input length limits
# ---------------------------------------------------------------------------

class TestUpdatePatternsLengthValidation:
    """[Sec Medium-1T] update_patterns must reject patterns exceeding length limits."""

    def _setup_mod(self, tmp_path: Path):
        _assert_file_exists()
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        patterns_file = tmp_path / "patterns.json"
        mod = _load_module_from_path(
            TEMPLATE_STOP_PY,
            f"_tmpl_stop_test_validation_{tmp_path.name}",
        )
        mod.SESSIONS_DIR = str(sessions_dir)
        mod.PATTERNS_FILE = str(patterns_file)
        patterns_data: dict[str, Any] = {"patterns": []}
        patterns_file.write_text(json.dumps(patterns_data), encoding="utf-8")
        return mod, sessions_dir, patterns_file

    def _write_session_with_patterns(
        self, sessions_dir: Path, date_str: str, patterns: list
    ) -> None:
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

    def test_id_exceeding_max_length_is_rejected(self, tmp_path):
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
            "Pattern with id of length 65 (> MAX_ID_LENGTH=64) must be rejected. "
            "Add: if not pid or len(pid) > MAX_ID_LENGTH: continue"
        )

    def test_id_at_max_length_is_accepted(self, tmp_path):
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
            "The check should be len(pid) > MAX_ID_LENGTH, not >=."
        )

    def test_description_exceeding_max_length_is_rejected(self, tmp_path):
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
            "Add: if len(description) > MAX_DESCRIPTION_LENGTH: continue"
        )

    def test_description_at_max_length_is_accepted(self, tmp_path):
        """Patterns with description exactly 500 characters long must be accepted."""
        mod, sessions_dir, patterns_file = self._setup_mod(tmp_path)
        exact_desc = "y" * 500
        self._write_session_with_patterns(sessions_dir, TODAY_STR, [
            {"id": "pat-exact-desc", "description": exact_desc}
        ])

        mod.update_patterns(TODAY_STR)

        data = json.loads(patterns_file.read_text(encoding="utf-8"))
        stored = next(
            (p for p in data["patterns"] if p["id"] == "pat-exact-desc"), None
        )
        assert stored is not None, (
            "Pattern with description of exactly 500 characters must be accepted. "
            "The check should be len(description) > MAX_DESCRIPTION_LENGTH, not >=."
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
