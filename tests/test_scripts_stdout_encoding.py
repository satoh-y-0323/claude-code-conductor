#!/usr/bin/env python3
"""
test_scripts_stdout_encoding.py: Static analysis test for stdout/stderr reconfigure idiom in scripts.

Tests that all in-scope *.py files that print non-ASCII characters to stdout or stderr
contain sys.stdout.reconfigure(encoding='utf-8') and sys.stderr.reconfigure(encoding='utf-8') calls.

This ensures Windows CI environments (cp1252) can handle UTF-8 output without UnicodeEncodeError.

## Scan Scope (architecture-report-20260802-190003.md ADR-11 / 要件 F-6)

- `scripts/*.py`                        — repo 直下の dev tool（従来からの対象）
- `.claude/skills/*/scripts/**/*.py`     — skill 付属スクリプト（配布物。ADR-11 で追加）

ADR-11 の追加以前は repo 直下 `scripts/` のみが対象で、`.claude/skills/*/scripts/` 配下の
スクリプトには reconfigure を検査する者が 0 人＝「空の緑」だった。
glob は `tests/test_nul_boundary_lint.py` の `_GLOB_CLAUDE_SKILLS_SCRIPTS` と同一形にしてある。

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


REPO_ROOT = Path(__file__).resolve().parent.parent

# 走査対象の glob パターン（tests/test_nul_boundary_lint.py と同一形）
_GLOB_REPO_SCRIPTS = "scripts/*.py"
_GLOB_CLAUDE_SKILLS_SCRIPTS = ".claude/skills/*/scripts/**/*.py"


def get_scripts_dir() -> Path:
    """Return the repo-root scripts directory path."""
    project_root = Path(__file__).parent.parent
    return project_root / "scripts"


def get_scan_targets() -> list[Path]:
    """Return every *.py file in scope, sorted and de-duplicated.

    Scope = repo 直下 `scripts/` ∪ `.claude/skills/*/scripts/` 配下（再帰）。
    `__pycache__` 配下は除外する。
    """
    targets: set[Path] = set()
    for pattern in (_GLOB_REPO_SCRIPTS, _GLOB_CLAUDE_SKILLS_SCRIPTS):
        for path in REPO_ROOT.glob(pattern):
            if "__pycache__" in path.parts:
                continue
            targets.add(path)
    return sorted(targets)


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


def test_scan_targets_include_claude_skills_scripts():
    """S-10 回帰ガード: 走査対象一覧に `.claude/skills/*/scripts/` 由来が含まれること。

    ADR-11 の射程拡張が将来 `scripts/` のみへ戻されると、skill 付属スクリプトの
    reconfigure を検査する者が再び 0 人（空の緑）になる。それを検出するためのテスト。

    NOTE: 本ケースは是正の有無に関わらず緑であり、`tester.md:49`
    （最初から Pass するテストは修正する）の適用対象外とする（plan-report 20260802-204515 の
    test-detector (B) に明記された裁定）。
    """
    targets = get_scan_targets()
    rel = [p.relative_to(REPO_ROOT).as_posix() for p in targets]

    assert rel, "走査対象が 1 件も無い"
    assert any(
        r.startswith(".claude/skills/") and "/scripts/" in r for r in rel
    ), f".claude/skills/*/scripts/ 由来が走査対象に含まれていない: {rel}"
    assert any(r.startswith("scripts/") for r in rel), f"repo 直下 scripts/ が落ちている: {rel}"
    # 射程拡張で初めて検査対象になった実在ファイル（ADR-11 / 要件 F-7 の是正対象）
    assert ".claude/skills/dev-workflow/scripts/record_review_decision.py" in rel


def test_scripts_stdout_encoding():
    """
    Test that all in-scope scripts printing non-ASCII characters have stdout/stderr reconfigure.
    """
    scripts_dir = get_scripts_dir()

    if not scripts_dir.exists():
        raise RuntimeError(f"Scripts directory not found: {scripts_dir}")

    script_files = get_scan_targets()

    if not script_files:
        raise RuntimeError(f"No .py files found under {REPO_ROOT}")

    print(f"\n=== Scanning {len(script_files)} script files ===")
    print(f"Repo root: {REPO_ROOT}\n")

    failures = []
    passes = []

    for script_path in script_files:
        passed, message = check_script_file(script_path)

        rel_name = script_path.relative_to(REPO_ROOT).as_posix()
        status = "PASS" if passed else "FAIL"
        print(f"[{status}] {rel_name}: {message}")

        if not passed:
            failures.append((rel_name, message))
        else:
            passes.append(rel_name)

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
