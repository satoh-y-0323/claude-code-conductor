#!/usr/bin/env python3
"""
test_hooks_stdin_idiom.py: Static analysis test for stdin reconfigure idiom in hooks.

Tests that all .claude/hooks/*.py files that use sys.stdin also contain a
sys.stdin.reconfigure(encoding='utf-8') call (or equivalent reconfigure with
encoding parameter), following the idiom established in CLAUDE.md §9-3.

## Detection Strategy (AST-based)

1. **sys.stdin Usage Detection (AST)**:
   - Walks the AST for ast.Attribute nodes matching sys -> stdin
     (Name "sys" / Attribute "stdin").
   - Detects patterns: sys.stdin.read(), sys.stdin.readline(), json.load(sys.stdin), etc.
   - Excludes the single sys.stdin node that is the receiver of a
     sys.stdin.reconfigure(...) call (i.e. the func chain of an ast.Call):
     configuring the stream is setup, not "usage". Every other sys.stdin node
     counts as usage.
   - No raw-source regex is involved at any stage, so comments and docstrings
     cannot influence the result (see section 3).

2. **Reconfigure Detection (AST)**:
   - Walks the AST for an ast.Call whose func is the attribute chain
     sys -> stdin -> reconfigure (Name "sys" / Attribute "stdin" /
     Attribute "reconfigure") and which carries an `encoding=` keyword argument.
   - Does NOT require try/except context (though production idiom uses it):
     the visitor walks the whole tree regardless of control flow, so both the
     `try/except AttributeError` idiom and the `if sys.stdin and hasattr(...)`
     idiom are detected.
   - Quote style of the encoding value is irrelevant, since the check inspects
     the parsed keyword argument rather than raw source text.

3. **Comments and Docstrings**:
   - Both detections are AST-based, so comments and docstrings are excluded from
     the detection surface automatically: comments never enter the AST at all,
     and a docstring is a plain string constant, not an ast.Call. A file that
     merely *mentions* sys.stdin.reconfigure(encoding='utf-8') in a comment or
     docstring is therefore correctly reported as "reconfigure missing".

## Known Limitations

- Does not validate that reconfigure is actually reached at runtime
  (e.g., if it's in an if-block that never runs). Static detection is
  best-effort; behavioral verification is in ADR-3 subprocess tests.
- A guard expression such as `if sys.stdin and hasattr(sys.stdin, 'reconfigure')`
  counts as usage, because only the reconfigure call's own receiver node is
  excluded. This is conservative-safe: a file carrying that guard also calls
  reconfigure, so the idiom check still passes.
- Does not check if stdin.buffer or other alternatives are used
  (acceptable per ADR-2: stdin.reconfigure is the standard idiom).
"""

import ast
from pathlib import Path

import pytest


def get_hooks_dir() -> Path:
    """Return the .claude/hooks directory path."""
    project_root = Path(__file__).parent.parent
    return project_root / ".claude" / "hooks"


def _is_sys_stdin(node: ast.AST) -> bool:
    """Return True if node is the attribute expression ``sys.stdin``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "stdin"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def extract_stdin_refs(source: str, filename: str) -> bool:
    """
    Check if source code uses sys.stdin.

    Returns True if sys.stdin is referenced in a way that requires reading
    (not just in reconfigure calls).

    Detection is purely AST-based: comments and docstrings never contribute to
    the result, because comments do not enter the AST at all and a docstring is
    a plain string constant rather than an attribute expression.

    Args:
        source: Python source code as string
        filename: Name of file (for error reporting)

    Returns:
        bool: True if sys.stdin usage is detected
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        print(f"  [SYNTAX ERROR] {filename}: {e}")
        return False

    # sys.stdin nodes acting as the receiver of a sys.stdin.reconfigure(...)
    # call. Configuring the stream is setup, not usage, so they are excluded.
    # `tree` stays alive for the whole function, so node identities are stable.
    reconfigure_receivers = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "reconfigure"
            and _is_sys_stdin(node.func.value)
        ):
            reconfigure_receivers.add(id(node.func.value))

    for node in ast.walk(tree):
        if _is_sys_stdin(node) and id(node) not in reconfigure_receivers:
            return True

    return False


def has_stdin_reconfigure(source: str) -> bool:
    """
    Check if source code contains sys.stdin.reconfigure(encoding=...).

    Uses AST-based detection to avoid false positives from comments/docstrings.

    Args:
        source: Python source code as string

    Returns:
        bool: True if reconfigure call with encoding parameter found
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    class ReconfigureVisitor(ast.NodeVisitor):
        def __init__(self):
            self.found_reconfigure = False

        def visit_Call(self, node: ast.Call) -> None:
            # Check for sys.stdin.reconfigure(...) pattern
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "reconfigure"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "stdin"
                and isinstance(node.func.value.value, ast.Name)
                and node.func.value.value.id == "sys"
            ):
                # Found sys.stdin.reconfigure call. Check for encoding keyword argument.
                for keyword in node.keywords:
                    if keyword.arg == "encoding":
                        self.found_reconfigure = True
                        return
            self.generic_visit(node)

    visitor = ReconfigureVisitor()
    visitor.visit(tree)
    return visitor.found_reconfigure


def check_hook_file(hook_path: Path) -> tuple[bool, str]:
    """
    Check a single hook file for stdin reconfigure idiom.

    Args:
        hook_path: Path to hook .py file

    Returns:
        (passed, message): bool indicating pass/fail and explanatory message
    """
    try:
        source = hook_path.read_text(encoding="utf-8")
    except Exception as e:
        return False, f"Failed to read: {e}"

    uses_stdin = extract_stdin_refs(source, hook_path.name)
    has_reconfigure = has_stdin_reconfigure(source)

    if uses_stdin and not has_reconfigure:
        return False, "Uses sys.stdin but missing reconfigure(encoding=...)"
    elif uses_stdin and has_reconfigure:
        return True, "OK: uses sys.stdin and has reconfigure"
    else:
        return True, "OK: does not use sys.stdin"


def test_hooks_stdin_idiom():
    """
    Test that all hooks using sys.stdin have stdin.reconfigure(encoding='utf-8').
    """
    hooks_dir = get_hooks_dir()

    if not hooks_dir.exists():
        raise RuntimeError(f"Hooks directory not found: {hooks_dir}")

    hook_files = sorted(hooks_dir.glob("*.py"))

    if not hook_files:
        raise RuntimeError(f"No .py files found in {hooks_dir}")

    print(f"\n=== Scanning {len(hook_files)} hook files ===")
    print(f"Directory: {hooks_dir}\n")

    failures = []
    passes = []

    for hook_path in hook_files:
        # Check all hook files including private modules (_hook_utils.py, etc.)
        # using the same reconfigure detection logic.
        passed, message = check_hook_file(hook_path)

        status = "PASS" if passed else "FAIL"
        print(f"[{status}] {hook_path.name}: {message}")

        if not passed:
            failures.append((hook_path.name, message))
        else:
            passes.append(hook_path.name)

    print("\n=== Summary ===")
    print(f"Passed: {len(passes)}")
    print(f"Failed: {len(failures)}")

    if failures:
        print("\n=== Failed hooks ===")
        for name, msg in failures:
            print(f"  {name}: {msg}")

        # Construct assertion message with failing file names
        failing_files = ", ".join(name for name, _ in failures)
        raise AssertionError(
            f"stdin idiom check failed for {len(failures)} hook(s): {failing_files}"
        )


# 以下は has_stdin_reconfigure() / extract_stdin_refs() の AST ベース判定そのものを
# 対象にした単体テスト。上の test_hooks_stdin_idiom() は実 hook ファイルを走査するため、
# 「コメント/docstring 内の文字列を検出しない」という偽陽性排除の効果は（そのような hook が
# 存在しないため）検証されない。正規表現ベースへの回帰を検知する回帰防止テストとして分離する。

# コメント内にのみ reconfigure が現れる負例（実コードでは stdin を読むだけ）
_SRC_RECONFIGURE_IN_COMMENT_ONLY = """\
import sys

# sys.stdin.reconfigure(encoding='utf-8')
data = sys.stdin.read()
"""

# docstring 内にのみ reconfigure が現れる負例
_SRC_RECONFIGURE_IN_DOCSTRING_ONLY = '''\
"""Hook that should call sys.stdin.reconfigure(encoding='utf-8') but does not."""

import sys

data = sys.stdin.read()
'''

# 本番 idiom その 1: try/except AttributeError
_SRC_RECONFIGURE_TRY_EXCEPT = """\
import sys

try:
    sys.stdin.reconfigure(encoding='utf-8')
except AttributeError:
    pass

data = sys.stdin.read()
"""

# 本番 idiom その 2: if hasattr（recall_autorebuild.py 系）
_SRC_RECONFIGURE_IF_HASATTR = """\
import sys

if sys.stdin and hasattr(sys.stdin, 'reconfigure'):
    sys.stdin.reconfigure(encoding='utf-8')

data = sys.stdin.read()
"""

# stdin を一切使わないソース
_SRC_NO_STDIN = """\
import sys

print(sys.argv)
"""

# extract_stdin_refs の「実使用検出」用の正例。実コードで sys.stdin.read() を呼ぶ。
_SRC_REAL_STDIN_READ = """\
import sys

data = sys.stdin.read()
print(data)
"""

# CR-r3 finding #1 の反証シナリオ: 実コードは reconfigure 呼び出しのみで stdin を読まず、
# コメントに「.reconfigure で終わらない」sys.stdin 言及がある負例。
# 旧実装（AST ゲート後に生ソース全体へ regex フォールバック）では usage あり(True)と
# 誤検出していた。純粋 AST 判定への回帰防止テスト。
_SRC_RECONFIGURE_ONLY_WITH_STDIN_COMMENT = """\
import sys

try:
    sys.stdin.reconfigure(encoding='utf-8')
except AttributeError:
    pass

# NOTE: some tools call sys.stdin.read() directly instead
print("done")
"""


@pytest.mark.parametrize(
    "source, expected",
    [
        pytest.param(
            _SRC_RECONFIGURE_IN_COMMENT_ONLY, False, id="comment-only-is-not-detected"
        ),
        pytest.param(
            _SRC_RECONFIGURE_IN_DOCSTRING_ONLY,
            False,
            id="docstring-only-is-not-detected",
        ),
        pytest.param(_SRC_RECONFIGURE_TRY_EXCEPT, True, id="try-except-idiom"),
        pytest.param(_SRC_RECONFIGURE_IF_HASATTR, True, id="if-hasattr-idiom"),
    ],
)
def test_has_stdin_reconfigure_ignores_comments_and_docstrings(source, expected):
    """AST ベース判定がコメント/docstring 内の文字列を検出しないことを検証する。"""
    assert has_stdin_reconfigure(source) is expected


@pytest.mark.parametrize(
    "source, expected",
    [
        pytest.param(_SRC_NO_STDIN, False, id="no-stdin-usage"),
        pytest.param(_SRC_REAL_STDIN_READ, True, id="real-stdin-read-usage"),
        pytest.param(
            _SRC_RECONFIGURE_ONLY_WITH_STDIN_COMMENT,
            False,
            id="reconfigure-only-with-stdin-comment-is-not-usage",
        ),
    ],
)
def test_extract_stdin_refs_detects_only_real_usage(source, expected):
    """extract_stdin_refs() が実コード上の stdin 使用のみを検出することを検証する。

    - stdin を使わないソースでは False
    - 実際に sys.stdin.read() を呼ぶソースでは True
    - reconfigure 呼び出しのみ＋コメント内の sys.stdin 言及では False
      （純粋 AST 判定であり、コメントは検出面に入らない）
    """
    assert extract_stdin_refs(source, "synthetic.py") is expected


if __name__ == "__main__":
    test_hooks_stdin_idiom()
