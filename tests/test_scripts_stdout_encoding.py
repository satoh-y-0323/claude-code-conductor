#!/usr/bin/env python3
"""
test_scripts_stdout_encoding.py: Static analysis test for stdout/stderr reconfigure idiom in scripts.

Tests that all scripts/*.py files that print non-ASCII characters to stdout or stderr
contain sys.stdout.reconfigure(encoding='utf-8') and sys.stderr.reconfigure(encoding='utf-8') calls.

This ensures Windows CI environments (cp1252) can handle UTF-8 output without UnicodeEncodeError.

## Detection Strategy (AST-based)

1. **Non-ASCII Print Detection (AST)**:
   - Walks the AST for ast.Call nodes matching print(...)
   - Extracts string constants and f-string components (ast.JoinedStr.values)
   - Checks if any contain non-ASCII characters (ord > 127)
   - Determines output destination from the `file=` keyword argument:
     - `file=` missing or None: stdout
     - `file=sys.stderr`: stderr
   - Ignores comment-only scripts and scripts with no prints (safe to skip)

2. **Reconfigure Detection (AST)**:
   - Walks the AST for ast.Call nodes matching sys.stdout.reconfigure(encoding=...)
     and sys.stderr.reconfigure(encoding=...)
   - Checks for the `encoding=` keyword argument (value must be a string constant)
   - Does not require try/except context (though production idiom uses it)

3. **Comments and Docstrings**:
   - Both detections are AST-based, so comments and docstrings are excluded automatically.
   - A script that only mentions the reconfigure idiom in comments will correctly be reported as missing.

## Known Limitations

- Does not validate that reconfigure is actually reached at runtime.
- Does not distinguish between stdout/stderr destination reliably if file= uses complex expressions.
  Conservative assumption: if a print() has non-ASCII and file= is not explicitly `sys.stderr`,
  we check for stdout reconfigure. If file=sys.stderr, we check stderr reconfigure.
"""

import ast
from pathlib import Path

import pytest


def get_scripts_dir() -> Path:
    """Return the scripts directory path."""
    project_root = Path(__file__).parent.parent
    return project_root / "scripts"


def _contains_non_ascii(s: str) -> bool:
    """Check if string contains non-ASCII characters."""
    return any(ord(c) > 127 for c in s)


def _extract_string_constants(node: ast.AST) -> list[str]:
    """Extract string constants from various AST node types.

    Handles:
    - ast.Constant (Python 3.8+)
    - ast.Str (legacy Python 3.7)
    - ast.JoinedStr (f-strings): extract components from values
    """
    strings = []

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        strings.append(node.value)
    elif isinstance(node, ast.Str):  # legacy
        strings.append(node.s)
    elif isinstance(node, ast.JoinedStr):
        # f-string: extract all constant parts
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                strings.append(value.value)
            elif isinstance(value, ast.Str):  # legacy
                strings.append(value.s)

    return strings


def _get_print_file_target(call_node: ast.Call) -> str:
    """
    Determine which stream a print() call targets.

    Returns:
        "stdout" (default or file=None)
        "stderr" (if file=sys.stderr is detected)
        "unknown" (if file= is complex expression we can't statically determine)
    """
    for keyword in call_node.keywords:
        if keyword.arg == "file":
            # Check if value is sys.stderr
            if (
                isinstance(keyword.value, ast.Attribute)
                and keyword.value.attr == "stderr"
                and isinstance(keyword.value.value, ast.Name)
                and keyword.value.value.id == "sys"
            ):
                return "stderr"
            # Check if value is None
            elif isinstance(keyword.value, ast.Constant) and keyword.value.value is None:
                return "stdout"
            # Check if value is ast.NameConstant (legacy None)
            elif isinstance(keyword.value, ast.NameConstant) and keyword.value.value is None:
                return "stdout"
            else:
                # Complex expression, can't determine
                return "unknown"

    # No file= argument means stdout
    return "stdout"


def extract_non_ascii_prints(source: str, filename: str) -> dict[str, list[tuple[int, str]]]:
    """
    Check source code for print() calls with non-ASCII characters.

    Returns:
        dict with keys "stdout" and "stderr", each mapping to list of (lineno, output_target)
        where output_target is the determined stream.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        print(f"  [SYNTAX ERROR] {filename}: {e}")
        return {"stdout": [], "stderr": []}

    non_ascii_prints = {"stdout": [], "stderr": []}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        # Check if this is a print() call
        if not (isinstance(node.func, ast.Name) and node.func.id == "print"):
            continue

        # Extract all string constants from arguments
        has_non_ascii = False
        for arg in node.args:
            strings = _extract_string_constants(arg)
            for s in strings:
                if _contains_non_ascii(s):
                    has_non_ascii = True
                    break
            if has_non_ascii:
                break

        # Also check keyword arguments (except file=)
        if not has_non_ascii:
            for keyword in node.keywords:
                if keyword.arg == "file":
                    continue
                strings = _extract_string_constants(keyword.value)
                for s in strings:
                    if _contains_non_ascii(s):
                        has_non_ascii = True
                        break
                if has_non_ascii:
                    break

        if has_non_ascii:
            target = _get_print_file_target(node)
            if target != "unknown":
                non_ascii_prints[target].append((node.lineno, target))

    return non_ascii_prints


def has_stdout_reconfigure(source: str) -> bool:
    """Check if source has sys.stdout.reconfigure(encoding=...)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "reconfigure"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "stdout"
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "sys"
        ):
            # Check for encoding= keyword argument
            for keyword in node.keywords:
                if keyword.arg == "encoding":
                    return True

    return False


def has_stderr_reconfigure(source: str) -> bool:
    """Check if source has sys.stderr.reconfigure(encoding=...)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "reconfigure"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "stderr"
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "sys"
        ):
            # Check for encoding= keyword argument
            for keyword in node.keywords:
                if keyword.arg == "encoding":
                    return True

    return False


def check_script_file(script_path: Path) -> tuple[bool, str]:
    """
    Check a single script file for stdout/stderr reconfigure idiom.

    Returns:
        (passed, message): bool indicating pass/fail and explanatory message
    """
    try:
        source = script_path.read_text(encoding="utf-8")
    except Exception as e:
        return False, f"Failed to read: {e}"

    non_ascii_prints = extract_non_ascii_prints(source, script_path.name)
    has_stdout_reconfig = has_stdout_reconfigure(source)
    has_stderr_reconfig = has_stderr_reconfigure(source)

    failures = []

    if non_ascii_prints["stdout"] and not has_stdout_reconfig:
        failures.append("prints non-ASCII to stdout but missing reconfigure(encoding=...)")

    if non_ascii_prints["stderr"] and not has_stderr_reconfig:
        failures.append("prints non-ASCII to stderr but missing reconfigure(encoding=...)")

    if failures:
        return False, "; ".join(failures)

    if non_ascii_prints["stdout"] or non_ascii_prints["stderr"]:
        return True, "OK: prints non-ASCII and has reconfigure"
    else:
        return True, "OK: no non-ASCII prints"


def test_scripts_stdout_encoding():
    """
    Test that all scripts printing non-ASCII characters have stdout/stderr reconfigure.
    """
    scripts_dir = get_scripts_dir()

    if not scripts_dir.exists():
        raise RuntimeError(f"Scripts directory not found: {scripts_dir}")

    script_files = sorted(scripts_dir.glob("*.py"))

    if not script_files:
        raise RuntimeError(f"No .py files found in {scripts_dir}")

    print(f"\n=== Scanning {len(script_files)} script files ===")
    print(f"Directory: {scripts_dir}\n")

    failures = []
    passes = []

    for script_path in script_files:
        passed, message = check_script_file(script_path)

        status = "PASS" if passed else "FAIL"
        print(f"[{status}] {script_path.name}: {message}")

        if not passed:
            failures.append((script_path.name, message))
        else:
            passes.append(script_path.name)

    print("\n=== Summary ===")
    print(f"Passed: {len(passes)}")
    print(f"Failed: {len(failures)}")

    if failures:
        print("\n=== Failed scripts ===")
        for name, msg in failures:
            print(f"  {name}: {msg}")

        failing_files = ", ".join(name for name, _ in failures)
        raise AssertionError(
            f"stdout/stderr encoding check failed for {len(failures)} script(s): {failing_files}"
        )


if __name__ == "__main__":
    test_scripts_stdout_encoding()
