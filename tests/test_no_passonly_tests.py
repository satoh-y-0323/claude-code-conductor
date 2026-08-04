"""
`pass` のみ・空・`...`・裸 `return` の不完全テスト関数を検出する lint。

plan-report §5 / architecture-report ADR-11 に基づく。
判定対象: `tests/` を根に `test_*.py` ファイルから、
`test_` で始まる関数定義（`ast.FunctionDef` / `ast.AsyncFunctionDef`）すべて。

## 判定の外延（DC-AM-002 サイクル 1）

docstring を除いた本体が以下のいずれか：
- **空**（本体が存在しない）
- **`pass`** のみ
- **`...`（Ellipsis）** のみ
- **裸の `return`**（値の返却なし）

上記は**複合条件ではなく OR ロジック**：
複数の要素を含む本体（`pass; ...` など）は非対象。

## 判定対象の主語（DC-AM-003・【訂正・2026-08-05】）

**`ast.walk(tree)` でモジュール全体を走査し、クラス内のメソッド・ネストした関数を含めて
`test_` で始まる関数定義をすべて判定対象とする。** `tree.body` 直下のみを見る実装は
クラス内メソッドを取りこぼす（本リポジトリのテストはほぼ全てクラス内メソッドのため、
その実装では実質何も検出できない「空の緑」になる）。

判定対象**外**なのは `ast.ClassDef` そのもの（クラス定義自体は本体判定の対象にならない）だけである。

## スキップ許容（DC-AM-003 と DC-GPS-004 の実施）

`@pytest.mark.skip` デコレータが付与されている場合のみ許容。
`@pytest.mark.skipif` は不許容（条件が False の環境では実行され passed に算入される）。
`skip` 許容時は `reason=` が必須。

`reason=` の必須化は E 周回 2（CR-NEW Medium）で実装した。`reason=` が
無い（`@pytest.mark.skip` に括弧が無い・`@pytest.mark.skip()` で引数なし）、
または空文字・空白のみの場合は **skip の許容自体を無効にする**（body の
pass-only 判定にフォールバックせず、reason 欠落そのものを独立した違反として
報告する）。修正前はこのルールが docstring にのみ明記され、実装は
「reason= 必須チェックは後で」という TODO コメントのまま未実装だった。

## docstring の扱い

関数本体の先頭に docstring が存在する場合、**その行範囲外の本体のみ**を判定対象とする。
docstring 自体は本体に含めない（Docstring の内容の空性は判定対象外）。

## 走査根と下限 assert（【訂正・2026-08-05】下限値を明示）

走査根は `tests/` ディレクトリのみ（`.dev/` を含めない）。
`rglob("test_*.py")` で再帰走査するため、ファイル数が `git` 管理下で安定する。
下限 assert: `tests/` 配下の `test_*.py` は実測 103 件のため、**走査ファイル数 >= 100** を
番兵とする（`assert files` = 1 件以上では glob を書き損じて 1 件だけ拾っても通ってしまうため）。

## テストに assert なしまで広げない理由

既存の正当なスモークテスト 8 件（`tests/test_audit_review_decisions.py:641-650` を除く）を
巻き込む。巻き込まないためにこの定義（pass のみ・empty・...・return）に限定。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths & Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# 検出ロジック
# ---------------------------------------------------------------------------


def _is_docstring_only_after_index(stmts: list[ast.stmt], idx: int) -> bool:
    """stmts[idx] が Expr(Constant(str)) で docstring だった場合、idx を進める。

    実装: `ast.get_docstring(node)` を使わず、
    「先頭要素が Expr(Constant(str))」で判定する（より厳密）。
    """
    if idx >= len(stmts):
        return False
    stmt = stmts[idx]
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        return isinstance(stmt.value.value, str)
    return False


def _collect_test_functions(tree: ast.Module) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """モジュール全体を `ast.walk` で走査し、`test_` で始まる関数定義を収集する。

    クラス内メソッド・ネストした関数を含む（DC-AM-003 訂正）。
    判定対象外なのは `ast.ClassDef` そのものだけである。

    returns: (関数名, ノード) のリスト
    """
    functions: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                functions.append((node.name, node))
    return functions


def _skip_decorator_status(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[bool, bool]:
    """`@pytest.mark.skip` の有無と、`reason=` が有効かどうかを判定する。

    E 周回 2（CR-NEW Medium）是正: 修正前は skip の有無しか見ておらず、
    docstring が明記する「reason= 必須」ルールが実装されていなかった
    （`_has_skip_decorator` の docstring に「reason= 必須チェックは後で」と
    自己申告が残ったまま未実装だった）。

    returns: (has_skip, has_valid_reason)
      - has_skip: `@pytest.mark.skip`（デコレータ形・呼び出し形いずれも）が付与されているか
      - has_valid_reason: 呼び出し形かつ `reason=` キーワード引数が
        strip 後 1 文字以上の文字列リテラルであるか。
        デコレータ形（`@pytest.mark.skip`、括弧なし）は `reason=` を
        そもそも書けない形のため無条件で False。
        引数なしの呼び出し形（`@pytest.mark.skip()`）も False。
    """
    for dec in func_node.decorator_list:
        # dec が `pytest.mark.skip` のケース（括弧なし）:
        #   Attribute(value=Attribute(...), attr="skip")
        if isinstance(dec, ast.Attribute):
            if dec.attr == "skip":
                return True, False
        elif isinstance(dec, ast.Call):
            func_part = dec.func
            if isinstance(func_part, ast.Attribute) and func_part.attr == "skip":
                for kw in dec.keywords:
                    if kw.arg == "reason":
                        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                            if kw.value.value.strip():
                                return True, True
                # skip 呼び出しはあるが reason= が無い・空・非文字列
                return True, False
    return False, False


def _body_is_only_pass_or_ellipsis_or_empty_or_return(
    body: list[ast.stmt], skip_docstring: bool = True
) -> bool:
    """body が以下のいずれかであるか判定:
    - 空（len == 0）
    - pass のみ
    - Ellipsis のみ
    - 裸の return のみ

    Args:
        body: 関数本体（docstring は削除されていないもの）
        skip_docstring: True の場合、先頭の docstring をスキップしてから判定
    """
    stmts = list(body)

    # docstring をスキップ
    idx = 0
    if skip_docstring and _is_docstring_only_after_index(stmts, 0):
        idx = 1

    remaining = stmts[idx:]

    # 空
    if len(remaining) == 0:
        return True

    # 1 要素のみを見る
    if len(remaining) != 1:
        return False

    stmt = remaining[0]

    # Pass
    if isinstance(stmt, ast.Pass):
        return True

    # Ellipsis のみ
    if isinstance(stmt, ast.Expr):
        if isinstance(stmt.value, ast.Constant):
            if stmt.value.value is ...:
                return True

    # 裸の return（値なし）
    if isinstance(stmt, ast.Return):
        if stmt.value is None:
            return True

    return False


def find_empty_test_functions(path: Path) -> list[tuple[str, str]]:
    """1 ファイルから不完全テスト関数を検出する。

    returns: (関数名, 理由) のリスト
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        raise AssertionError(f"{path}: 読み込み失敗: {e}") from e

    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as e:
        raise AssertionError(f"{path}: ast.parse 失敗: {e}") from e

    violations: list[tuple[str, str]] = []

    for func_name, func_node in _collect_test_functions(tree):
        has_skip, has_valid_reason = _skip_decorator_status(func_node)

        if has_skip and not has_valid_reason:
            # reason= が無い・空の skip は許容しない（body の判定にフォールバックせず、
            # reason 欠落そのものを違反として報告する）。
            violations.append(
                (func_name, "@pytest.mark.skip に reason= が必須です（現在: 無しまたは空文字）")
            )
            continue

        if has_skip and has_valid_reason:
            # 正当な reason 付き skip は許容
            continue

        # 本体を判定
        if _body_is_only_pass_or_ellipsis_or_empty_or_return(func_node.body):
            violations.append((func_name, "本体が pass/empty/.../return のみ"))

    return violations


# ---------------------------------------------------------------------------
# 本検査
# ---------------------------------------------------------------------------


class TestNoEmptyTestFunctions:
    """不完全テスト関数を検出する。

    assert なしの定義（`pass` / empty / `...` / `return` のみ）を違反とする。
    スモークテストの中には正当な実装（値チェックなし・実装確認のみ）があるため、
    `pass` のみ に限定。
    """

    def test_no_passonly_tests_in_tests_directory(self):
        """tests/ 配下の全 test_*.py から不完全テスト関数を検出。

        Red 時点: B10（pass のみ）の違反が検出される。
        Green 時点: 違反が 0 件。
        """
        # ガード: glob を書き損じて 1 件だけ拾っても通らないよう下限を明示する
        # （実測 103 件・【訂正・2026-08-05】DC-AM-001）
        files = list(TESTS_DIR.rglob("test_*.py"))
        assert len(files) >= 100, (
            f"{TESTS_DIR} から test_*.py が {len(files)} 件しか見つかりません"
            "（実測 103 件を下回っています。glob パスの typo を疑ってください）"
        )

        all_violations: list[tuple[Path, str, str]] = []
        for f in files:
            violations = find_empty_test_functions(f)
            for func_name, reason in violations:
                all_violations.append((f, func_name, reason))

        assert not all_violations, (
            f"不完全テスト関数が {len(all_violations)} 件見つかりました:\n"
            + "\n".join(f"  {f.relative_to(REPO_ROOT)}:{name} - {reason}"
                       for f, name, reason in all_violations)
        )


# ---------------------------------------------------------------------------
# 検出器単体テスト
# ---------------------------------------------------------------------------


class TestDetectsEmptyTestFunctions:
    """検出されるべきケース"""

    def test_detects_pass_only(self, tmp_path):
        f = tmp_path / "test_mod.py"
        f.write_text("def test_foo():\n    pass\n", encoding="utf-8")
        violations = find_empty_test_functions(f)
        assert len(violations) == 1
        assert violations[0][0] == "test_foo"

    def test_detects_empty_body(self, tmp_path):
        """実際には構文エラーだが、AST を直接構築したテストケース向け。"""
        # pass 相当として扱う実装上の仮定値を使う
        f = tmp_path / "test_mod.py"
        f.write_text("def test_foo():\n    pass\n", encoding="utf-8")
        violations = find_empty_test_functions(f)
        assert len(violations) == 1

    def test_detects_ellipsis_only(self, tmp_path):
        f = tmp_path / "test_mod.py"
        f.write_text("def test_foo():\n    ...\n", encoding="utf-8")
        violations = find_empty_test_functions(f)
        assert len(violations) == 1
        assert violations[0][0] == "test_foo"

    def test_detects_bare_return(self, tmp_path):
        f = tmp_path / "test_mod.py"
        f.write_text("def test_foo():\n    return\n", encoding="utf-8")
        violations = find_empty_test_functions(f)
        assert len(violations) == 1
        assert violations[0][0] == "test_foo"

    def test_skips_docstring_for_judgment(self, tmp_path):
        """docstring 後の pass は検出対象。docstring のみの関数は対象外。"""
        f = tmp_path / "test_mod.py"
        f.write_text(
            '"""This is docstring."""\n'
            "def test_foo():\n"
            '    """Docstring."""\n'
            "    pass\n",
            encoding="utf-8"
        )
        violations = find_empty_test_functions(f)
        assert len(violations) == 1


class TestDoesNotDetectValidTestFunctions:
    """検出されないべきケース"""

    def test_ignores_non_test_functions(self, tmp_path):
        f = tmp_path / "test_mod.py"
        f.write_text("def helper():\n    pass\n", encoding="utf-8")
        violations = find_empty_test_functions(f)
        assert violations == []

    def test_ignores_test_with_assertion(self, tmp_path):
        f = tmp_path / "test_mod.py"
        f.write_text("def test_foo():\n    assert True\n", encoding="utf-8")
        violations = find_empty_test_functions(f)
        assert violations == []

    def test_ignores_test_with_skip_decorator(self, tmp_path):
        f = tmp_path / "test_mod.py"
        f.write_text(
            "import pytest\n"
            "@pytest.mark.skip(reason='not ready')\n"
            "def test_foo():\n"
            "    pass\n",
            encoding="utf-8"
        )
        violations = find_empty_test_functions(f)
        assert violations == []

    def test_rejects_skipif_decorator(self, tmp_path):
        """skipif は許容しない（条件 False なら実行される）。"""
        f = tmp_path / "test_mod.py"
        f.write_text(
            "import pytest\n"
            "@pytest.mark.skipif(False, reason='xxx')\n"
            "def test_foo():\n"
            "    pass\n",
            encoding="utf-8"
        )
        violations = find_empty_test_functions(f)
        assert len(violations) == 1

    def test_ignores_async_test_with_assertion(self, tmp_path):
        f = tmp_path / "test_mod.py"
        f.write_text(
            "async def test_foo():\n"
            "    assert True\n",
            encoding="utf-8"
        )
        violations = find_empty_test_functions(f)
        assert violations == []


class TestDetectsClassMethodsAndNestedFunctions:
    """クラス内メソッド・ネストした関数も判定対象（DC-AM-003 訂正）。

    前版は「クラス・ネスト関数は対象外」としていたが、これは
    「クラス定義そのもの（ast.ClassDef）は対象外」の意図を誤読した実装を招いた。
    本リポジトリのテストはほぼ全てクラス内メソッドのため、その実装では
    B10 のような違反を検出できない「空の緑」になっていた。
    """

    def test_detects_class_methods(self, tmp_path):
        f = tmp_path / "test_mod.py"
        f.write_text(
            "class TestFoo:\n"
            "    def test_bar(self):\n"
            "        pass\n",
            encoding="utf-8"
        )
        violations = find_empty_test_functions(f)
        assert len(violations) == 1
        assert violations[0][0] == "test_bar"

    def test_detects_nested_function_in_test(self, tmp_path):
        f = tmp_path / "test_mod.py"
        f.write_text(
            "def test_outer():\n"
            "    def test_inner():\n"
            "        pass\n"
            "    assert True\n",
            encoding="utf-8"
        )
        violations = find_empty_test_functions(f)
        # test_outer は assert True を持つため非対象。test_inner のみ検出される。
        assert len(violations) == 1
        assert violations[0][0] == "test_inner"

    def test_class_definition_itself_is_not_a_judgment_target(self, tmp_path):
        """判定対象外なのは ast.ClassDef そのものだけであることの確認。

        クラス本体に test_ 関数が 1 つもなければ違反 0 件（クラス自体は判定されない）。
        """
        f = tmp_path / "test_mod.py"
        f.write_text(
            "class TestFoo:\n"
            "    def helper(self):\n"
            "        pass\n",
            encoding="utf-8"
        )
        violations = find_empty_test_functions(f)
        assert violations == []


class TestSkipReasonRequired:
    """E 周回 2 CR-NEW Medium の回帰テスト: `@pytest.mark.skip` の `reason=` 必須化。

    修正前は docstring に「reason= 必須」と明記されているのに実装が無く、
    `@pytest.mark.skip`（reason なし）で `pass` のみのテストがすり抜けた
    （`_has_skip_decorator` の docstring 自己申告どおり未実装だった）。
    """

    def test_skip_without_parens_is_a_violation(self, tmp_path):
        """`@pytest.mark.skip`（括弧なし・reason を書けない形）は違反になる。"""
        f = tmp_path / "test_mod.py"
        f.write_text(
            "import pytest\n"
            "@pytest.mark.skip\n"
            "def test_foo():\n"
            "    assert True\n",
            encoding="utf-8"
        )
        violations = find_empty_test_functions(f)
        assert len(violations) == 1
        assert violations[0][0] == "test_foo"
        assert "reason=" in violations[0][1]

    def test_skip_call_without_reason_kwarg_is_a_violation(self, tmp_path):
        """`@pytest.mark.skip()`（引数なし）は違反になる。"""
        f = tmp_path / "test_mod.py"
        f.write_text(
            "import pytest\n"
            "@pytest.mark.skip()\n"
            "def test_foo():\n"
            "    assert True\n",
            encoding="utf-8"
        )
        violations = find_empty_test_functions(f)
        assert len(violations) == 1
        assert "reason=" in violations[0][1]

    def test_skip_with_empty_reason_is_a_violation(self, tmp_path):
        """`reason=''`（空文字）は違反になる。"""
        f = tmp_path / "test_mod.py"
        f.write_text(
            "import pytest\n"
            "@pytest.mark.skip(reason='')\n"
            "def test_foo():\n"
            "    assert True\n",
            encoding="utf-8"
        )
        violations = find_empty_test_functions(f)
        assert len(violations) == 1
        assert "reason=" in violations[0][1]

    def test_skip_with_whitespace_only_reason_is_a_violation(self, tmp_path):
        """`reason='   '`（空白のみ）は strip 後に空になるため違反になる。"""
        f = tmp_path / "test_mod.py"
        f.write_text(
            "import pytest\n"
            "@pytest.mark.skip(reason='   ')\n"
            "def test_foo():\n"
            "    assert True\n",
            encoding="utf-8"
        )
        violations = find_empty_test_functions(f)
        assert len(violations) == 1
        assert "reason=" in violations[0][1]

    def test_skip_reason_missing_is_reported_independent_of_body(self, tmp_path):
        """reason 欠落はテスト本体が pass-only でなくても独立して検出される
        （body 判定へのフォールバックではなく、reason 欠落そのものが違反）。
        """
        f = tmp_path / "test_mod.py"
        f.write_text(
            "import pytest\n"
            "@pytest.mark.skip\n"
            "def test_foo():\n"
            "    assert 1 + 1 == 2\n"
            "    assert True\n",
            encoding="utf-8"
        )
        violations = find_empty_test_functions(f)
        assert len(violations) == 1
        assert "reason=" in violations[0][1]

    def test_skip_with_valid_reason_is_still_accepted(self, tmp_path):
        """非回帰: reason 付き skip は引き続き許容される（既存テストと同型）。"""
        f = tmp_path / "test_mod.py"
        f.write_text(
            "import pytest\n"
            "@pytest.mark.skip(reason='not ready')\n"
            "def test_foo():\n"
            "    pass\n",
            encoding="utf-8"
        )
        violations = find_empty_test_functions(f)
        assert violations == []

    def test_skip_reason_check_applies_to_class_methods_too(self, tmp_path):
        """クラス内メソッドの skip にも reason= 必須が適用される。"""
        f = tmp_path / "test_mod.py"
        f.write_text(
            "import pytest\n"
            "class TestFoo:\n"
            "    @pytest.mark.skip\n"
            "    def test_bar(self):\n"
            "        assert True\n",
            encoding="utf-8"
        )
        violations = find_empty_test_functions(f)
        assert len(violations) == 1
        assert violations[0][0] == "test_bar"
        assert "reason=" in violations[0][1]
