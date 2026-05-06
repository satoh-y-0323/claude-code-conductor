"""Tests for docstring consistency across test files.

古いコメント・誤ったドキュメントが残存していないことを確認する。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path


TESTS_ROOT = Path(__file__).parent
SESSION_UTILS_TEST_PATH = TESTS_ROOT / "hooks" / "test_session_utils.py"


def test_toctou_docstring_does_not_say_fail():
    """test_toctou_uses_open_x_mode の docstring に "FAIL" が含まれていないこと（Low-M4-残）。

    実装が open('x') に修正済みであるにもかかわらず、
    test_toctou_uses_open_x_mode の docstring に
    「現在の実装は open('w') を使っているため、このテストは意図的に FAIL する」
    という古いコメントが残存している。

    実装修正後は docstring を更新してこのコメントを削除すると Green になる。
    """
    source = SESSION_UTILS_TEST_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # test_toctou_uses_open_x_mode 関数ノードを探す
    fn_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "test_toctou_uses_open_x_mode":
            fn_node = node
            break

    assert fn_node is not None, (
        f"test_toctou_uses_open_x_mode 関数が {SESSION_UTILS_TEST_PATH} に見つからない"
    )

    # 関数の docstring を取得する
    docstring = ast.get_docstring(fn_node) or ""

    assert "FAIL" not in docstring, (
        "test_toctou_uses_open_x_mode の docstring に 'FAIL' という文字列が含まれている。\n"
        "実装は open('x') に修正済みのため、このコメントは誤りである。\n"
        f"現在の docstring:\n{docstring}\n"
        "修正: docstring から 'FAIL' に言及している行を削除すること。"
    )
