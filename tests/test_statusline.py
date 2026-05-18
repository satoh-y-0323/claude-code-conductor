"""Tests for .claude/hooks/statusline.py (AST 静的解析特化)。

本ファイルは AST ベースの静的検査（型アノテーション・バイトカウント実装確認）と
基礎的な関数単体テストを担当する。subprocess / in-process の振る舞いテストは
``tests/hooks/test_statusline.py`` を参照すること。
"""

import ast
import io
import sys
import time
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

# pytest の DontReadFromInput との非互換を回避
# statusline.py はモジュールレベルで sys.stdin/stdout/stderr.reconfigure を呼ぶ
sys.stdin = MagicMock()
sys.stdin.reconfigure = MagicMock()
sys.stdout.reconfigure = MagicMock()
sys.stderr.reconfigure = MagicMock()

# importlib で .claude/hooks/statusline.py を直接ロード
_spec = importlib.util.spec_from_file_location(
    "statusline",
    Path(__file__).parent.parent / ".claude" / "hooks" / "statusline.py",
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

HOOK_PATH = Path(__file__).parent.parent / ".claude" / "hooks" / "statusline.py"


# ---------------------------------------------------------------------------
# pct_color (4 件)
# ---------------------------------------------------------------------------

def test_pct_color_above_90_returns_red():
    """pct_color(91) は RED を返す（90 超 → RED）"""
    assert mod.pct_color(91) == mod.RED


def test_pct_color_above_75_returns_orange():
    """pct_color(76) は ORANGE を返す（75 超 → ORANGE）"""
    assert mod.pct_color(76) == mod.ORANGE


def test_pct_color_above_60_returns_yellow():
    """pct_color(61) は YELLOW を返す（60 超 → YELLOW）"""
    assert mod.pct_color(61) == mod.YELLOW


def test_pct_color_60_or_below_returns_green():
    """pct_color(50) は GREEN を返す（60 以下 → GREEN）"""
    assert mod.pct_color(50) == mod.GREEN


# ---------------------------------------------------------------------------
# format_reset_time (2 件)
# ---------------------------------------------------------------------------

def test_format_reset_time_unix_future_returns_time_string():
    """未来の unix タイムスタンプを渡すと "Xm" / "Xh Ym" / "Xd Yh" 形式の文字列を返す"""
    future_unix = time.time() + 300  # 5 分後
    result = mod.format_reset_time(future_unix)
    assert any(unit in result for unit in ("m", "h", "d")), (
        f"Expected time string containing 'm', 'h', or 'd', got: {result!r}"
    )


def test_format_reset_time_iso_future_returns_time_string():
    """未来の ISO 8601 文字列を渡すと "Xm" / "Xh Ym" / "Xd Yh" 形式の文字列を返す"""
    iso_future = "2099-01-01T00:00:00+00:00"
    result = mod.format_reset_time(iso_future)
    assert any(unit in result for unit in ("m", "h", "d")), (
        f"Expected time string containing 'm', 'h', or 'd', got: {result!r}"
    )


# ---------------------------------------------------------------------------
# render_output (2 件)
# ---------------------------------------------------------------------------

def test_render_output_context_usage_contains_expected_text(capsys):
    """render_output がコンテキスト使用率を含む出力を書き出す"""
    import json
    payload = json.dumps({"context_window": {"used_percentage": 50}})
    mod.render_output(payload)
    captured = capsys.readouterr()
    output = captured.out
    assert "context" in output or "%" in output, (
        f"Expected 'context' or '%' in output, got: {output!r}"
    )


def test_render_output_rate_limit_contains_expected_text(capsys):
    """render_output が rate limit 情報を含む出力を書き出す（省スペース UI: '5h lim'）。"""
    import json
    payload = json.dumps({
        "context_window": {"used_percentage": 20},
        "rate_limits": {
            "five_hour": {
                "used_percentage": 40,
                "resets_at": None,
            }
        },
    })
    mod.render_output(payload)
    captured = capsys.readouterr()
    output = captured.out
    assert "5h" in output and "lim" in output, (
        f"Expected '5h' and 'lim' in output (compact label '5h lim'), got: {output!r}"
    )


# ---------------------------------------------------------------------------
# main() バイトカウント (Low-3 修正済み回帰検証テスト)
# ---------------------------------------------------------------------------

def test_total_size_uses_byte_count_not_char_count():
    """main() の total_size がバイト数でカウントされていることを AST で検証する（Green 回帰防止）。

    UTF-8 マルチバイト文字（例: "あ" = 3バイト）を含む行を処理する際、
    total_size はバイト数でカウントされる必要がある。実装は
    `total_size += len(line.encode('utf-8'))` で対応済みであり、本テストは
    将来の改修で `len(line)` に戻されないかを AST で守る。
    """
    tree = ast.parse(HOOK_PATH.read_text(encoding="utf-8"))

    # main 関数ノードを探す
    fn_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            fn_node = node
            break

    assert fn_node is not None, "main 関数が statusline.py に見つからない"

    # main 関数内で total_size への加算が len(line.encode(...)) 形式かを確認する
    # `total_size += len(line)` は NG（文字数カウント）
    # `total_size += len(line.encode('utf-8'))` が OK（バイト数カウント）
    has_byte_count = False
    for node in ast.walk(fn_node):
        # AugAssign: total_size += ...
        if not isinstance(node, ast.AugAssign):
            continue
        target = node.target
        if not (isinstance(target, ast.Name) and target.id == "total_size"):
            continue
        # 右辺が len(...) の呼び出しか確認
        value = node.value
        if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "len"):
            continue
        # len() の引数が encode() 呼び出しかを確認
        if not value.args:
            continue
        arg = value.args[0]
        if (
            isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Attribute)
            and arg.func.attr == "encode"
        ):
            has_byte_count = True
            break

    assert has_byte_count, (
        "main() の total_size はバイト数でカウントされていない。\n"
        "現在は `total_size += len(line)` で文字数カウントになっている。\n"
        "UTF-8 マルチバイト文字（例: 'あ' = 3バイト）では文字数とバイト数が異なる。\n"
        "修正: `total_size += len(line.encode('utf-8'))` に変更すること。"
    )


# ---------------------------------------------------------------------------
# main() 切り詰め: バイト/文字境界不一致 (Low 第3ラウンド指摘2)
# ---------------------------------------------------------------------------

def test_truncation_uses_byte_aware_index():
    """main() の切り詰め処理がバイト境界を考慮していることを AST で検証する（Green 回帰防止）。

    `overflow` バイト数を文字列スライスにそのまま渡すとマルチバイト文字（例: "あ" = 3バイト）
    で切り詰め位置がずれるため、実装は以下のいずれかを採用する必要がある:
      - encode してバイト列で切り詰めた後 `decode(errors='replace')` で復元する
      - `sys.stdin.read(MAX_INPUT)` で一括読み込みする

    本テストは AST でいずれかのパターンが存在することを確認し、将来の改修で
    文字数インデックスのスライスに戻されないかを守る。
    """
    tree = ast.parse(HOOK_PATH.read_text(encoding="utf-8"))

    # main 関数ノードを探す
    fn_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            fn_node = node
            break

    assert fn_node is not None, "main 関数が statusline.py に見つからない"

    # パターンA: chunks[-1] への代入で encode/decode を経由しているか確認
    # chunks[-1] = <expr involving encode>
    has_byte_aware_truncation = False

    for node in ast.walk(fn_node):
        # Assign: chunks[-1] = ...
        if not isinstance(node, ast.Assign):
            continue
        # ターゲットが chunks[-1] であるか確認
        for target in node.targets:
            if not (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "chunks"
            ):
                continue
            # 右辺に .encode または .decode が含まれるか確認
            rhs_src = ast.dump(node.value)
            if "encode" in rhs_src or "decode" in rhs_src:
                has_byte_aware_truncation = True
                break
        if has_byte_aware_truncation:
            break

    # パターンB: sys.stdin.read() による一括読み込みに変更されているか確認
    # for line in sys.stdin のループが消えて stdin.read() になっているか
    has_stdin_read_pattern = False
    for node in ast.walk(fn_node):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "read"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "stdin"
        ):
            has_stdin_read_pattern = True
            break

    assert has_byte_aware_truncation or has_stdin_read_pattern, (
        "main() の切り詰め処理がバイト境界を考慮していない。\n"
        "現在の実装: `chunks[-1] = chunks[-1][: len(chunks[-1]) - overflow]`\n"
        "  - `overflow` はバイト数だが、文字列スライスは文字数インデックスを使っている。\n"
        "  - マルチバイト文字（例: 'あ' = 3バイト）では切り詰め位置が誤る。\n"
        "修正例A: encode してバイト列で切り詰め後 decode する。\n"
        "修正例B: `sys.stdin.read(MAX_INPUT)` で一括読み込みに変更する。"
    )


# ---------------------------------------------------------------------------
# render_output の data 変数型アノテーション (Low-1 Round 5)
# ---------------------------------------------------------------------------

def test_render_output_data_has_typed_annotation():
    """render_output 内の data 変数が `dict[str, Any]` 形式の詳細型を持つことを AST で検証する（Green 回帰防止）。

    実装は `data: dict[str, Any] = {}` を採用済み。本テストは将来の改修で
    `data: dict = {}` のような subscript なしアノテーションに退行しないかを守る。

    検出対象: `dict[str, Any]` （Python 3.9+ の組込み generic）または
    `Dict[str, Any]` (`typing.Dict`) のどちらかであれば PASS。
    """
    source = HOOK_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # render_output 関数ノードを探す
    fn_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "render_output":
            fn_node = node
            break

    assert fn_node is not None, "render_output 関数が statusline.py に見つからない"

    # render_output 内の AnnAssign (型アノテーション付き代入) を探す
    # 対象: data: dict[str, Any] = {} や data: Dict[str, Any] = {}
    has_typed_data_annotation = False
    for node in ast.walk(fn_node):
        if not isinstance(node, ast.AnnAssign):
            continue
        # ターゲットが 'data' という名前か
        target = node.target
        if not (isinstance(target, ast.Name) and target.id == "data"):
            continue
        # アノテーションが Subscript 形式（dict[str, Any] や Dict[str, Any]）か確認
        annotation = node.annotation
        if isinstance(annotation, ast.Subscript):
            # dict[str, Any] または Dict[str, Any]
            ann_value = annotation.value
            if isinstance(ann_value, ast.Name) and ann_value.id.lower() == "dict":
                has_typed_data_annotation = True
                break
        # ast.Attribute 形式の場合（typing.Dict など）
        if isinstance(annotation, ast.Attribute) and annotation.attr.lower() == "dict":
            has_typed_data_annotation = True
            break

    assert has_typed_data_annotation, (
        "[Low-1] render_output() の `data` 変数に詳細型アノテーションがない。\n"
        "現在の実装: `data: dict = {}`\n"
        "  - `dict` は型引数なしで不完全なアノテーション。\n"
        "  - 型チェッカーは値の型を Any として扱い、型安全性の恩恵が得られない。\n"
        "期待する修正: `data: dict[str, Any] = {}` または `data: Dict[str, Any] = {}`\n"
        "  - `from typing import Any` または Python 3.9+ では `from __future__ import annotations` が必要。\n"
        "AST チェック: render_output 内の `data` に subscript 付きの dict アノテーションが見つからない。"
    )
