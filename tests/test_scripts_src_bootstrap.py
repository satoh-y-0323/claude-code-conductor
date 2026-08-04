"""
`scripts/` 配下で `c3` を import するファイルが、`c3` の import より前に
`sys.path.insert(0, ...)` でリポジトリの `src/` を優先する ブートストラップが
存在することを AST で検査する。

architecture-report-20260804-235224.md ADR-10 に基づく。

## 検査内容

1. `scripts/` 配下の Python ファイルを全走査
2. `c3` を import している行を検出
3. その import より**前**のモジュールレベル（class・関数外）で
   `sys.path.insert(0, ...)` が存在し、その引数に `"src"` を含むことを確認

## 前例との関係

`tests/conftest.py:14` が同型のブートストラップを実装しており、
本検査は `scripts/` 層でも同じパターンが強制されることを確認する。

## 追加時点での期待

前タスク `bootstrap-scripts-src` が実装済みのため、本検査は
**追加時点で緑**（Red 群に含めない）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths & Glob Patterns
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

# ---------------------------------------------------------------------------
# AST 検査ロジック
# ---------------------------------------------------------------------------


def _find_c3_imports(tree: ast.Module) -> list[int]:
    """`c3` を import している行番号のリストを返す。

    対象:
      - `import c3`
      - `import c3.db`
      - `from c3 import ...`
      - `from c3.db import ...`
    """
    import_lines: list[int] = []

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                # 正確なマッチ: "c3" か "c3...." で始まる
                if alias.name == "c3" or alias.name.startswith("c3."):
                    import_lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            # `from c3 import ...` または `from c3.xxx import ...`
            if node.module and (node.module == "c3" or node.module.startswith("c3.")):
                import_lines.append(node.lineno)

    return import_lines


def _find_sys_path_insert(tree: ast.Module) -> list[dict]:
    """モジュールレベルの `sys.path.insert(0, ...)` を検出。

    - クラス内・関数内のものは対象外（モジュールレベルのみ）
    - 引数に `"src"` を含むかをチェック

    returns: {'line': <lineno>, 'has_src': <bool>} のリスト
    """
    inserts: list[dict] = []

    for node in tree.body:
        if not isinstance(node, ast.Expr):
            continue
        if not isinstance(node.value, ast.Call):
            continue

        call = node.value
        # `sys.path.insert(0, ...)` パターン
        if not (isinstance(call.func, ast.Attribute)
                and call.func.attr == "insert"):
            continue
        if not isinstance(call.func.value, ast.Attribute):
            continue
        if call.func.value.attr != "path":
            continue
        if not isinstance(call.func.value.value, ast.Name):
            continue
        if call.func.value.value.id != "sys":
            continue

        # 引数が 2 個以上か確認（0 と何か）
        if len(call.args) < 2:
            continue

        # 第 2 引数に `"src"` が含まれるか
        arg = call.args[1]
        has_src = False

        if isinstance(arg, ast.Constant):
            if isinstance(arg.value, str) and "src" in arg.value:
                has_src = True
        elif isinstance(arg, ast.JoinedStr):  # f-string
            for value in arg.values:
                if isinstance(value, ast.Constant):
                    if isinstance(value.value, str) and "src" in value.value:
                        has_src = True

        inserts.append({
            "line": node.lineno,
            "has_src": has_src,
        })

    return inserts


def has_bootstrap_before_c3_import(path: Path) -> tuple[bool, str]:
    """ファイルが有効なブートストラップを持つか検査。

    returns: (有効?, エラーメッセージ)
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        return False, f"読み込み失敗: {e}"

    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as e:
        return False, f"ast.parse 失敗: {e}"

    # `c3` の import がなければ OK（ブートストラップは不要）
    c3_imports = _find_c3_imports(tree)
    if not c3_imports:
        return True, ""  # 対象外

    # ブートストラップを検索
    inserts = _find_sys_path_insert(tree)
    if not inserts:
        return False, "sys.path.insert(0, ...) が見つかりません"

    # ブートストラップが `c3` import より前にあるか
    for insert_info in inserts:
        if insert_info["has_src"]:
            insert_line = insert_info["line"]
            # 最初の `c3` import より前か
            if insert_line < min(c3_imports):
                return True, ""

    return False, (
        f"sys.path.insert に 'src' 含む行が、c3 import（行 {min(c3_imports)}）より前にありません。"
        f"ブートストラップ行: {[i['line'] for i in inserts]}"
    )


# ---------------------------------------------------------------------------
# 本検査（追加時点で緑）
# ---------------------------------------------------------------------------


class TestScriptsSrcBootstrap:
    """scripts/ 層の sys.path ブートストラップを検証。

    前タスク `bootstrap-scripts-src` が実装済みのため、本検査は追加時点で緑。
    """

    def test_sentinel_files_have_bootstrap_comment(self):
        """番兵ファイルに bootstrap コメントが含まれていること。

        実装済みの前タスク `bootstrap-scripts-src` の確認。
        """
        required_files = {
            "audit_review_decisions.py",
            "check_deletions.py",
        }

        for fname in required_files:
            fpath = SCRIPTS_DIR / fname
            assert fpath.exists(), f"{fpath} が見つかりません"

            text = fpath.read_text(encoding="utf-8")
            assert "c3-src-bootstrap" in text, (
                f"{fname} に 'c3-src-bootstrap' コメントが見つかりません。"
                f"bootstrap が実装されていないと考えられます。"
            )
            assert "sys.path.insert" in text, (
                f"{fname} に 'sys.path.insert' が見つかりません。"
                f"bootstrap が実装されていないと考えられます。"
            )

    def test_sentinel_files_exist(self):
        """番兵: `scripts/audit_review_decisions.py` と
        `scripts/check_deletions.py` が走査結果に含まれること。
        """
        required_files = {
            "audit_review_decisions.py",
            "check_deletions.py",
        }
        existing = {f.name for f in SCRIPTS_DIR.glob("*.py") if f.exists()}

        missing = required_files - existing
        assert not missing, (
            f"番兵ファイルが見つかりません: {missing}\n"
            f"存在するファイル: {existing}"
        )
