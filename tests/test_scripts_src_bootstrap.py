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

## 対象ファイルの動的列挙（E 周回 2 CR-NEW Medium 是正・2026-08-05）

修正前は `has_bootstrap_before_c3_import()`（AST ベースの本格実装）が定義されているのに
どのテストからも呼ばれておらず、実テストは弱い部分文字列一致
（`"c3-src-bootstrap" in text` / `"sys.path.insert" in text`）＋
ハードコードされた 2 ファイル名（`required_files`）のみを対象にしていた（空の緑）。
将来 `scripts/` 配下に第 3 のファイルが追加され `c3` を import しても
`required_files` に追記しない限り黙って検査を免れる。

修正後は `_iter_scripts_importing_c3()` で `scripts/*.py` を毎回 AST 走査し、
`c3` を import しているファイルを動的に列挙したうえで、その全件に対し
`has_bootstrap_before_c3_import()` を実際に呼び出す。
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

        # 第 2 引数に `"src"` を含む文字列リテラルが含まれるか。
        #
        # 【E 周回 2 CR-NEW Medium 是正で判明した defect】: 実運用の bootstrap 行は
        # `sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))`
        # のように `str(Path(...) / "src")` という Call 式でラップされている
        # （scripts/audit_review_decisions.py:32 / check_deletions.py:26）。
        # 修正前は引数が直接 ast.Constant / ast.JoinedStr である場合しか見ておらず、
        # この実運用パターンを恒久的に見逃していた。`has_bootstrap_before_c3_import()`
        # がどのテストからも呼ばれていなかった（元の CR 指摘）ためこの defect も
        # 未発見のまま残っていた。第 2 引数の部分木全体を ast.walk で走査し、
        # 文字列 Constant のどこかに "src" が含まれていれば検出する。
        arg = call.args[1]
        has_src = any(
            isinstance(sub, ast.Constant) and isinstance(sub.value, str) and "src" in sub.value
            for sub in ast.walk(arg)
        )

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
# 対象ファイルの動的列挙
# ---------------------------------------------------------------------------


def _iter_scripts_importing_c3() -> list[Path]:
    """`scripts/*.py` のうち `c3` を import しているファイルを動的に列挙する。

    ハードコードされたファイル名リストに依存すると、将来 `scripts/` 配下に
    第 3 のファイルが追加され `c3` を import しても、リストへの追記漏れで
    この検査を黙ってすり抜ける（CR-NEW Medium 実測）。走査結果を毎回 AST で
    判定することでこれを防ぐ。
    """
    found: list[Path] = []
    for f in sorted(SCRIPTS_DIR.glob("*.py")):
        try:
            text = f.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(f))
        except (UnicodeDecodeError, OSError, SyntaxError):
            continue
        if _find_c3_imports(tree):
            found.append(f)
    return found


# ---------------------------------------------------------------------------
# 本検査（追加時点で緑）
# ---------------------------------------------------------------------------


class TestScriptsSrcBootstrap:
    """scripts/ 層の sys.path ブートストラップを検証。

    `has_bootstrap_before_c3_import()` を実際に呼び出し、対象ファイルは
    `scripts/*.py` のうち `c3` を import しているものを動的に列挙する
    （CR-NEW Medium 是正: 修正前はこの関数がどのテストからも呼ばれておらず
    「空の緑」だった）。前タスク `bootstrap-scripts-src` が実装済みのため、
    本検査は追加時点で緑。
    """

    def test_target_set_is_not_empty(self):
        """走査対象の空集合ガード（typo による検査の空回りを防ぐ）。"""
        targets = _iter_scripts_importing_c3()
        assert targets, f"{SCRIPTS_DIR} 配下に c3 を import するファイルが 1 件も見つかりません"

    def test_target_set_includes_known_sentinel_files(self):
        """番兵: 既知の 2 ファイルが動的列挙結果に含まれること
        （動的列挙そのものが壊れていないことの裏取り）。
        """
        targets = {f.name for f in _iter_scripts_importing_c3()}
        required_files = {"audit_review_decisions.py", "check_deletions.py"}
        missing = required_files - targets
        assert not missing, (
            f"動的列挙結果に含まれていません: {missing}\n実測: {targets}"
        )

    def test_scripts_importing_c3_have_valid_bootstrap(self):
        """`c3` を import する `scripts/*.py` 全件に対して
        `has_bootstrap_before_c3_import()` を実際に呼び出し検証する。

        修正前はこの関数が定義されているだけで使われておらず「空の緑」だった
        （CR-NEW Medium・親 Claude 実測: grep で使用箇所ゼロを確認）。
        """
        targets = _iter_scripts_importing_c3()
        assert targets, "走査対象が空です（test_target_set_is_not_empty で先に落ちるはず）"

        failures: list[str] = []
        for f in targets:
            ok, message = has_bootstrap_before_c3_import(f)
            if not ok:
                failures.append(f"{f.relative_to(REPO_ROOT)}: {message}")

        assert not failures, "ブートストラップ欠落:\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# has_bootstrap_before_c3_import() 単体の検出力（合成ソース）
# ---------------------------------------------------------------------------


class TestHasBootstrapBeforeC3ImportDetectsViolations:
    """`has_bootstrap_before_c3_import()` 自体が実際に違反を検出できることの
    合成ソースでの裏取り。実運用ファイル（現状 2 件とも正しい）だけでは
    この関数の検出力そのものは裏取りできないため、合成ソースで直接検証する。
    """

    def test_missing_sys_path_insert_is_detected(self, tmp_path):
        """c3 を import しているのに sys.path.insert が無ければ検出される。"""
        f = tmp_path / "no_bootstrap.py"
        f.write_text("import c3\n", encoding="utf-8")
        ok, message = has_bootstrap_before_c3_import(f)
        assert ok is False
        assert "sys.path.insert" in message

    def test_sys_path_insert_after_import_is_detected(self, tmp_path):
        """sys.path.insert が c3 import の後にある（順序違反）は検出される。"""
        f = tmp_path / "wrong_order.py"
        f.write_text(
            "import sys\n"
            "import c3\n"
            "sys.path.insert(0, 'src')\n",
            encoding="utf-8",
        )
        ok, message = has_bootstrap_before_c3_import(f)
        assert ok is False
        assert "c3 import" in message

    def test_valid_bootstrap_is_accepted(self, tmp_path):
        """順序が正しいブートストラップは検出されない（過剰検出の非回帰）。"""
        f = tmp_path / "ok.py"
        f.write_text(
            "import sys\n"
            "sys.path.insert(0, 'src')\n"
            "import c3\n",
            encoding="utf-8",
        )
        ok, message = has_bootstrap_before_c3_import(f)
        assert ok is True
        assert message == ""

    def test_file_without_c3_import_is_exempt(self, tmp_path):
        """c3 を import しないファイルはブートストラップ不要で対象外になる。"""
        f = tmp_path / "unrelated.py"
        f.write_text("import os\n", encoding="utf-8")
        ok, message = has_bootstrap_before_c3_import(f)
        assert ok is True

    def test_src_path_check_rejects_insert_without_src(self, tmp_path):
        """sys.path.insert はあるが引数に 'src' を含まない場合は検出される。"""
        f = tmp_path / "no_src_arg.py"
        f.write_text(
            "import sys\n"
            "sys.path.insert(0, 'other')\n"
            "import c3\n",
            encoding="utf-8",
        )
        ok, message = has_bootstrap_before_c3_import(f)
        assert ok is False
