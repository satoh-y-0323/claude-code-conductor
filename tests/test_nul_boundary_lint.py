"""
NUL 境界 lint の機械検査。

architecture-report-20260722-210138.md §2 (ADR-NB-1〜8) / plan-report-20260722-210450.md §2
に基づく。前例 `tests/test_no_bare_python_launcher.py` と同型（検出器＋本検査＋単体テストを同居）。

## 検出ロジックの骨子

走査対象内の `str.join` 呼び出しを **全件、既定で違反**とみなす（封じ込め型）。
`os.path.join` 等の同名別関数は **引数個数による構造判別**（位置引数ちょうど 1 個・キーワードなし・
`Starred` でない）で除外する（ADR-NB-1）。

セパレータは以下の順で解決する（ADR-NB-2）:
  1. `Constant(str)` レシーバ → その値
  2. `Name` レシーバ かつ〔同一モジュールのトップレベルに `NAME = "リテラル"` がちょうど 1 つ **かつ**
     `^[A-Z_][A-Z0-9_]*$` **かつ** 同一関数スコープに同名ローカル束縛が無い〕→ その値
  3. それ以外 → 解決不能（fail-closed）

`"\\x00"` ちょうど 1 文字のみ準拠（ADR-NB-3）。以下は宣言なしで除外（ADR-NB-4）:
  - E-1: 位置引数が 1 個でない / キーワードあり（`str.join` でない）
  - E-2: セパレータが空文字列 `""`
  - E-3: 引数が文字列リテラルのみの list/tuple/set リテラル

宣言マーカーは `# nul-boundary: allow(<理由>)`（語彙は `allow` の 1 種のみ・理由は strip 後 5 文字以上）。
抽出は `tokenize.COMMENT` トークン限定（行テキストへの素当ては文字列リテラルと誤衝突するため不可・ADR-NB-5）。
結合はコメント起点・1 対 1（規則 ①②③）。f-string 内の `join` は包含する文の行範囲で解決する
（f-string 内部式の位置情報が Python バージョン依存のため・サイクル 3 DC-AS-001）。

パース失敗・読み込み失敗はスキップせず、ファイルパスを明示したメッセージで検査自体を失敗させる
（ADR-NB-8）。

## Red フェーズについて

本検査（`test_no_nul_boundary_violations_in_target_files`）は、既存コードに宣言マーカーが
1 つも付与されていないため **失敗する**。これは機能未実装（マーカー付与・是正タスク未着手）
による正しい失敗であり、テスト自体の欠陥ではない。developer によるマーカー付与・是正
（impl-annotate タスク）完了後に Green 化する。

検出器自身の単体テスト（本ファイル内の合成ケース）はすべて成功する。
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths & Glob Patterns
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent

# 走査対象の glob パターン（CR-M-001: DRY 化のためモジュールレベルで共有）
# ADR-NB-6 参照: 全層とも再帰 glob に統一・_template は除外
_GLOB_SRC_C3 = "src/c3/**/*.py"
_GLOB_CLAUDE_HOOKS = ".claude/hooks/**/*.py"
_GLOB_CLAUDE_SKILLS_SCRIPTS = ".claude/skills/*/scripts/**/*.py"
_GLOB_DEV_LOOP = ".dev/loop/**/*.py"

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

NUL_SEP = "\x00"
MIN_REASON_LEN = 5  # ADR-NB-5: strip 後 5 文字以上でなければ宣言として無効

_MARKER_RE = re.compile(r"#\s*nul-boundary:\s*allow\(([^)]*)\)")
_TOPLEVEL_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# Violation = (file, line, sep_repr, snippet)
# snippet は ascii() でエスケープ済み（Windows cp932 stdout 対策・ADR-NB-7）
Violation = tuple[str, int, str, str]


# ---------------------------------------------------------------------------
# 走査対象の解決（ADR-NB-6）
# ---------------------------------------------------------------------------


def iter_target_files(root: Path = REPO_ROOT) -> list[Path]:
    """走査対象の Python ファイル一覧を返す。

    必須層（存在確認は呼び出し側=本検査が別途行う。ここでは単純収集のみ）:
      - src/c3/**/*.py （パス要素に "_template" を含むものを除外）
      - .claude/hooks/**/*.py
      - .claude/skills/*/scripts/**/*.py

    任意層（ディレクトリが存在する場合のみ）:
      - .dev/loop/**/*.py

    全層とも再帰 glob に統一する（DC-GP-006: 将来サブディレクトリが増えても暗黙に
    走査対象から漏れないようにするため）。
    """
    files: list[Path] = []
    files.extend(root.glob(_GLOB_SRC_C3))
    files.extend(root.glob(_GLOB_CLAUDE_HOOKS))
    files.extend(root.glob(_GLOB_CLAUDE_SKILLS_SCRIPTS))
    dev_loop = root / ".dev" / "loop"
    if dev_loop.is_dir():
        files.extend(root.glob(_GLOB_DEV_LOOP))
    # _template はビルド生成物であり走査対象外（パス要素一致で除外・全層に一様適用）
    files = [f for f in files if "_template" not in f.parts]
    return sorted(set(files))


# ---------------------------------------------------------------------------
# AST 補助: 親ノードマップ・スコープ解決
# ---------------------------------------------------------------------------


def _build_parent_map(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node  # type: ignore[attr-defined]


def _enclosing_function(node: ast.AST) -> ast.AST | None:
    n = getattr(node, "parent", None)
    while n is not None:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return n
        n = getattr(n, "parent", None)
    return None


def _is_inside_joinedstr(node: ast.AST) -> bool:
    n = getattr(node, "parent", None)
    while n is not None:
        if isinstance(n, ast.JoinedStr):
            return True
        n = getattr(n, "parent", None)
    return False


def _enclosing_stmt(node: ast.AST) -> ast.stmt | None:
    n = getattr(node, "parent", None)
    while n is not None:
        if isinstance(n, ast.stmt):
            return n
        n = getattr(n, "parent", None)
    return None


# ---------------------------------------------------------------------------
# AST 補助: 関数スコープ内のローカル束縛収集（シャドウイング対策・DC-AS-005）
# ---------------------------------------------------------------------------


def _collect_target_names(target: ast.AST, names: set[str]) -> None:
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _collect_target_names(elt, names)
    elif isinstance(target, ast.Starred):
        _collect_target_names(target.value, names)


def _collect_from_node(node: ast.AST, names: set[str]) -> None:
    if isinstance(node, ast.Assign):
        for t in node.targets:
            _collect_target_names(t, names)
    elif isinstance(node, ast.AnnAssign):
        _collect_target_names(node.target, names)
    elif isinstance(node, ast.AugAssign):
        _collect_target_names(node.target, names)
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        _collect_target_names(node.target, names)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if item.optional_vars is not None:
                _collect_target_names(item.optional_vars, names)
    elif isinstance(node, ast.ExceptHandler):
        if node.name:
            names.add(node.name)
    elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        for gen in node.generators:
            _collect_target_names(gen.target, names)
    elif isinstance(node, ast.NamedExpr):
        _collect_target_names(node.target, names)


def _collect_local_bindings(func_node: ast.AST) -> set[str]:
    """func_node（FunctionDef/AsyncFunctionDef/Lambda）のスコープ内のローカル束縛名を集める。

    関数引数・代入・for ターゲット・with as / except as / 内包表記の束縛が対象（ADR-NB-2）。
    ネストした関数・ラムダ・クラス定義は別スコープのため、その内部の束縛は数えない
    （それらのノード自体には立ち寄るが、配下へは再帰しない）。
    """
    names: set[str] = set()

    args = func_node.args  # type: ignore[union-attr]
    for arglist in (
        getattr(args, "posonlyargs", []),
        args.args,
        args.kwonlyargs,
    ):
        for a in arglist:
            names.add(a.arg)
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue
            _collect_from_node(child, names)
            walk(child)

    if isinstance(func_node, ast.Lambda):
        body_stmts: list[ast.AST] = [func_node.body]
    else:
        body_stmts = list(func_node.body)  # type: ignore[union-attr]

    for stmt in body_stmts:
        _collect_from_node(stmt, names)
        walk(stmt)

    return names


# ---------------------------------------------------------------------------
# E-1 / E-3 判定
# ---------------------------------------------------------------------------


def _passes_arity_filter(call: ast.Call) -> bool:
    """E-1: str.join 候補として通す（os.path.join 等の同名別関数を除外・ADR-NB-1）。"""
    if call.keywords:
        return False
    if len(call.args) != 1:
        return False
    if isinstance(call.args[0], ast.Starred):
        return False
    return True


def _is_literal_str_collection(node: ast.AST) -> bool:
    """E-3: 引数が文字列リテラルのみの list/tuple/set リテラルか。"""
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return False
    return all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts)


# ---------------------------------------------------------------------------
# セパレータ解決（ADR-NB-2）
# ---------------------------------------------------------------------------


def _resolve_separator(
    receiver: ast.AST, module: ast.Module, call: ast.Call
) -> tuple[str, str | None]:
    """('resolved', value) または ('unresolved', None) を返す。"""
    if isinstance(receiver, ast.Constant) and isinstance(receiver.value, str):
        return ("resolved", receiver.value)

    if isinstance(receiver, ast.Name):
        name = receiver.id
        if not _TOPLEVEL_NAME_RE.match(name):
            return ("unresolved", None)

        count_all = 0
        literal_count = 0
        literal_value: str | None = None
        for stmt in module.body:
            targets: list[ast.AST] = []
            value: ast.AST | None = None
            if isinstance(stmt, ast.Assign):
                targets = list(stmt.targets)
                value = stmt.value
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                targets = [stmt.target]
                value = stmt.value
            else:
                continue
            if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                count_all += 1
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    literal_count += 1
                    literal_value = value.value

        if count_all != 1 or literal_count != 1:
            return ("unresolved", None)

        enclosing_func = _enclosing_function(call)
        if enclosing_func is not None:
            local_bindings = _collect_local_bindings(enclosing_func)
            if name in local_bindings:
                return ("unresolved", None)

        return ("resolved", literal_value)

    return ("unresolved", None)


# ---------------------------------------------------------------------------
# マーカー抽出（tokenize.COMMENT 限定・ADR-NB-5）
# ---------------------------------------------------------------------------


def _extract_markers(text: str) -> list[tuple[int, str]]:
    """(line_no, reason) のリストを返す。理由が 5 文字未満のものは含めない。"""
    markers: list[tuple[int, str]] = []
    tokens = tokenize.generate_tokens(io.StringIO(text).readline)
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            m = _MARKER_RE.search(tok.string)
            if m:
                reason = m.group(1).strip()
                if len(reason) >= MIN_REASON_LEN:
                    markers.append((tok.start[0], reason))
    return markers


def _collect_join_targets(tree: ast.Module) -> list[dict]:
    """AST から `join` ターゲットを収集する。

    returns:
        各要素が以下キーを持つ dict のリスト:
        - call: ast.Call ノード
        - sep_repr: セパレータの repr 形式（"resolved" / "<unresolved>"）
        - lineno, end_lineno: 対象の行範囲（f-string の場合は包含する文の範囲）
        - report_line: エラー報告用の行番号（通常は call.lineno）
    """
    _build_parent_map(tree)

    targets: list[dict] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join"
        ):
            continue
        call = node
        if not _passes_arity_filter(call):
            continue  # E-1: str.join ではない
        arg = call.args[0]
        if _is_literal_str_collection(arg):
            continue  # E-3

        receiver = call.func.value
        status, value = _resolve_separator(receiver, tree, call)
        if status == "resolved":
            if value == NUL_SEP:
                continue  # 準拠
            if value == "":
                continue  # E-2
            sep_repr = repr(value)
        else:
            sep_repr = "<unresolved>"

        if _is_inside_joinedstr(call):
            stmt = _enclosing_stmt(call)
            assert stmt is not None
            lineno = stmt.lineno
            end_lineno = stmt.end_lineno or stmt.lineno
        else:
            lineno = call.lineno
            end_lineno = call.end_lineno or call.lineno

        targets.append(
            {
                "call": call,
                "sep_repr": sep_repr,
                "lineno": lineno,
                "end_lineno": end_lineno,
                "report_line": call.lineno,
            }
        )

    return targets


def _suppress_by_markers(targets: list[dict], markers: list[tuple[int, str]]) -> set[int]:
    """マーカーと join ターゲットを結び付け、抑止される join の id セットを返す。

    マーカーは以下の順で 1 個の join に結び付く（fail-closed: 複数に該当する場合は抑止しない）:
    1. マーカーが行 L にあるとき、行範囲 [lineno, end_lineno] が L を含む対象 join
    2. 1 に該当する join が存在しない場合のみ、lineno == L + 1 の対象 join（直前行コメント）
    3. 1 または 2 で結び付く対象が 2 個以上ある場合、そのマーカーはどれも抑止しない
    """
    suppressed_ids: set[int] = set()
    for marker_line, _reason in markers:
        candidates = [t for t in targets if t["lineno"] <= marker_line <= t["end_lineno"]]
        if not candidates:
            candidates = [t for t in targets if t["lineno"] == marker_line + 1]
        if len(candidates) == 1:
            suppressed_ids.add(id(candidates[0]["call"]))
        # 0 個 or 2 個以上ならどれも抑止しない（fail-closed）
    return suppressed_ids


# ---------------------------------------------------------------------------
# 検出器本体
# ---------------------------------------------------------------------------


def find_violations(path: Path) -> list[Violation]:
    """1 ファイルから NUL 境界 lint 違反を検出する。

    - `text = path.read_text(encoding="utf-8")` で 1 回だけ読み、同一文字列から
      `ast.parse` と `tokenize.generate_tokens` の 2 パスを行う（tokenize.open() は使わない）。
    - SyntaxError・読み込み失敗・tokenize 失敗はスキップせず、ファイルパスを明示した
      メッセージで例外を送出する（ADR-NB-8）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        raise AssertionError(f"{path}: 読み込みに失敗しました: {e}") from e

    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as e:
        raise AssertionError(f"{path}: ast.parse に失敗しました: {e}") from e

    try:
        markers = _extract_markers(text)
    except (tokenize.TokenError, IndentationError) as e:
        raise AssertionError(f"{path}: tokenize に失敗しました: {e}") from e

    lines = text.splitlines()

    # join ターゲットを収集
    targets = _collect_join_targets(tree)

    # マーカーと join の結び付け、抑止 ID を計算
    suppressed_ids = _suppress_by_markers(targets, markers)

    # 違反リストを構築
    violations: list[Violation] = []
    for t in targets:
        if id(t["call"]) in suppressed_ids:
            continue
        line_no = t["report_line"]
        raw_snippet = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
        violations.append((str(path), line_no, t["sep_repr"], ascii(raw_snippet)))

    return violations


def _format_violations(violations: list[Violation]) -> str:
    return "\n".join(f"  {f}:{line}: sep={sep} | {snippet}" for f, line, sep, snippet in violations)


# ---------------------------------------------------------------------------
# 本検査（Red: 現状は宣言未実施のため多数の違反を検出して失敗する）
# ---------------------------------------------------------------------------


class TestRequiredLayersAreNotEmpty:
    """パス typo による検査の空回りを防ぐガード（前例の SKILL.md ガードと同型）。"""

    def test_src_c3_layer_has_files(self):
        files = list(REPO_ROOT.glob(_GLOB_SRC_C3))
        files = [f for f in files if "_template" not in f.parts]
        assert files, f"{_GLOB_SRC_C3} が1件も見つかりません（走査対象パスの確認が必要）"

    def test_claude_hooks_layer_has_files(self):
        files = list(REPO_ROOT.glob(_GLOB_CLAUDE_HOOKS))
        assert files, f"{_GLOB_CLAUDE_HOOKS} が1件も見つかりません（走査対象パスの確認が必要）"

    def test_claude_skills_scripts_layer_has_files(self):
        files = list(REPO_ROOT.glob(_GLOB_CLAUDE_SKILLS_SCRIPTS))
        assert files, (
            f"{_GLOB_CLAUDE_SKILLS_SCRIPTS} が1件も見つかりません（走査対象パスの確認が必要）"
        )


class TestNoNulBoundaryViolations:
    """本検査。既存コードに宣言マーカーが無いため現時点では失敗する（Red）。"""

    def test_no_nul_boundary_violations_in_target_files(self):
        files = iter_target_files()
        assert files, "iter_target_files() が1件もファイルを返しませんでした"

        all_violations: list[Violation] = []
        for f in files:
            all_violations.extend(find_violations(f))

        assert not all_violations, (
            f"NUL 境界 lint 違反が {len(all_violations)} 件見つかりました。\n"
            "str.join のセパレータが NUL (\\x00) でない場合、is `# nul-boundary: allow(<理由>)` "
            "の宣言コメントを付与するか、値を NUL 区切りへ是正してください。\n"
            "検出箇所（file:line: sep=<repr> | snippet）:\n" + _format_violations(all_violations)
        )


# ---------------------------------------------------------------------------
# iter_target_files の単体テスト
# ---------------------------------------------------------------------------


class TestIterTargetFiles:
    def test_excludes_template_directory(self, tmp_path):
        (tmp_path / "src" / "c3" / "_template").mkdir(parents=True)
        (tmp_path / "src" / "c3" / "_template" / "excluded.py").write_text(
            "x = 1\n", encoding="utf-8"
        )
        (tmp_path / "src" / "c3").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "c3" / "included.py").write_text("x = 1\n", encoding="utf-8")

        files = iter_target_files(tmp_path)
        names = {f.name for f in files}
        assert "excluded.py" not in names
        assert "included.py" in names

    def test_real_tree_returns_no_template_paths(self):
        """実ツリー検証: _template を含むパスが 0 件であること（DC-GP-005）。"""
        files = iter_target_files(REPO_ROOT)
        template_hits = [f for f in files if "_template" in f.parts]
        assert template_hits == [], f"_template 配下が走査対象に混入しています: {template_hits}"

    def test_missing_dev_loop_does_not_raise_and_returns_required_layers_only(self, tmp_path):
        (tmp_path / "src" / "c3").mkdir(parents=True)
        (tmp_path / "src" / "c3" / "a.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / ".claude" / "hooks").mkdir(parents=True)
        (tmp_path / ".claude" / "hooks" / "b.py").write_text("x = 1\n", encoding="utf-8")

        files = iter_target_files(tmp_path)  # .dev/loop が存在しない

        names = {f.name for f in files}
        assert names == {"a.py", "b.py"}

    def test_skills_scripts_layer_recursive_glob(self, tmp_path):
        scripts_dir = tmp_path / ".claude" / "skills" / "foo" / "scripts" / "nested"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "deep.py").write_text("x = 1\n", encoding="utf-8")

        files = iter_target_files(tmp_path)
        assert any(f.name == "deep.py" for f in files)

    def test_optional_dev_loop_layer_included_when_present(self, tmp_path):
        dev_loop = tmp_path / ".dev" / "loop"
        dev_loop.mkdir(parents=True)
        (dev_loop / "run_loop.py").write_text("x = 1\n", encoding="utf-8")

        files = iter_target_files(tmp_path)
        assert any(f.name == "run_loop.py" for f in files)


# ---------------------------------------------------------------------------
# 検出器単体テスト: 検出されるべきケース
# ---------------------------------------------------------------------------


class TestDetectsViolations:
    def test_detects_unmarked_newline_join(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text('x = "\\n".join(items)\n', encoding="utf-8")
        violations = find_violations(f)
        assert len(violations) == 1
        assert violations[0][1] == 1

    def test_detects_unmarked_comma_space_join(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text('x = ", ".join(items)\n', encoding="utf-8")
        violations = find_violations(f)
        assert len(violations) == 1

    def test_detects_unresolved_name_separator_join(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("def f(sep, items):\n    return sep.join(items)\n", encoding="utf-8")
        violations = find_violations(f)
        assert len(violations) == 1

    def test_reason_too_short_is_invalid_declaration(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(
            'x = "\\n".join(items)  # nul-boundary: allow(ok)\n', encoding="utf-8"
        )
        violations = find_violations(f)
        assert len(violations) == 1

    def test_empty_reason_is_invalid_declaration(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(
            'x = "\\n".join(items)  # nul-boundary: allow(  )\n', encoding="utf-8"
        )
        violations = find_violations(f)
        assert len(violations) == 1

    def test_marker_inside_string_literal_does_not_suppress(self, tmp_path):
        """文字列リテラル内にマーカー文字列があっても抑止として効かない（DC-GP-001）。"""
        f = tmp_path / "mod.py"
        f.write_text(
            'doc = "# nul-boundary: allow(これは文字列リテラルです)"\n'
            'x = "\\n".join(items)\n',
            encoding="utf-8",
        )
        violations = find_violations(f)
        assert len(violations) == 1
        assert violations[0][1] == 2

    def test_two_targets_same_line_one_marker_fail_closed(self, tmp_path):
        """同一行に対象 join が 2 個・マーカー 1 個は fail-closed で両方検出（DC-AM-007）。"""
        f = tmp_path / "mod.py"
        f.write_text(
            'x = "\\n".join(a) + "\\t".join(b)  # nul-boundary: allow(理由文字数十分)\n',
            encoding="utf-8",
        )
        violations = find_violations(f)
        assert len(violations) == 2

    def test_shadowed_toplevel_constant_is_not_treated_as_compliant(self, tmp_path):
        """トップレベル定数と同名のローカル束縛でシャドウした形は検出する（DC-AS-005）。"""
        f = tmp_path / "mod.py"
        f.write_text(
            'SEP = "\\x00"\n'
            "\n"
            "def f(items):\n"
            '    SEP = "\\n"\n'
            "    return SEP.join(items)\n",
            encoding="utf-8",
        )
        violations = find_violations(f)
        assert len(violations) == 1

    def test_comment_line_gap_before_join_does_not_bind(self, tmp_path):
        """マーカーと対象 join の間にコメント行が1行挟まる形は規則②(L+1限定)が空振りする
        （サイクル 3 DC-GP-004）。"""
        f = tmp_path / "mod.py"
        f.write_text(
            "# nul-boundary: allow(理由文字数は十分あります)\n"
            "# unrelated comment\n"
            'x = "\\n".join(items)\n',
            encoding="utf-8",
        )
        violations = find_violations(f)
        assert len(violations) == 1

    def test_marker_after_join_does_not_bind_backward(self, tmp_path):
        """マーカーが対象 join の直後行にある形は後方結合しない（サイクル 3 DC-GP-004）。"""
        f = tmp_path / "mod.py"
        f.write_text(
            'x = "\\n".join(items)\n'
            "# nul-boundary: allow(理由文字数は十分あります)\n",
            encoding="utf-8",
        )
        violations = find_violations(f)
        assert len(violations) == 1

    def test_old_vocabulary_human_does_not_suppress(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(
            'x = "\\n".join(items)  # nul-boundary: human(人間可読な行集合です)\n',
            encoding="utf-8",
        )
        violations = find_violations(f)
        assert len(violations) == 1

    def test_old_vocabulary_safe_does_not_suppress(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(
            'x = "\\n".join(items)  # nul-boundary: safe(外部由来値は入りません)\n',
            encoding="utf-8",
        )
        violations = find_violations(f)
        assert len(violations) == 1

    def test_old_vocabulary_fixed_does_not_suppress(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(
            'x = "\\n".join(items)  # nul-boundary: fixed(区切りは固定です)\n',
            encoding="utf-8",
        )
        violations = find_violations(f)
        assert len(violations) == 1

    def test_old_vocabulary_legacy_does_not_suppress(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(
            'x = "\\n".join(items)  # nul-boundary: legacy(後方互換のためです)\n',
            encoding="utf-8",
        )
        violations = find_violations(f)
        assert len(violations) == 1


# ---------------------------------------------------------------------------
# 検出器単体テスト: 検出されないべきケース
# ---------------------------------------------------------------------------


class TestDoesNotDetectViolations:
    def test_nul_separator_join_is_compliant(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text('x = "\\x00".join(items)\n', encoding="utf-8")
        assert find_violations(f) == []

    def test_marker_on_same_line_suppresses(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(
            'x = "\\n".join(items)  # nul-boundary: allow(理由文字数は十分あります)\n',
            encoding="utf-8",
        )
        assert find_violations(f) == []

    def test_marker_on_previous_line_suppresses(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(
            "# nul-boundary: allow(理由文字数は十分あります)\n"
            'x = "\\n".join(items)\n',
            encoding="utf-8",
        )
        assert find_violations(f) == []

    def test_os_path_join_two_args_is_not_flagged(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("import os\nx = os.path.join(a, b)\n", encoding="utf-8")
        assert find_violations(f) == []

    def test_os_path_join_three_args_is_not_flagged(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("import os\nx = os.path.join(a, b, c)\n", encoding="utf-8")
        assert find_violations(f) == []

    def test_os_path_join_starred_is_not_flagged(self, tmp_path):
        """os.path.join(*parts) は Starred 単一引数のため arity だけでは判別できず、
        Starred 除外により除外される（DC-AS-004）。"""
        f = tmp_path / "mod.py"
        f.write_text("import os\nx = os.path.join(*parts)\n", encoding="utf-8")
        assert find_violations(f) == []

    def test_empty_string_join_is_not_flagged(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text('x = "".join(items)\n', encoding="utf-8")
        assert find_violations(f) == []

    def test_literal_string_list_join_is_not_flagged(self, tmp_path):
        """E-3: 引数が文字列リテラルのみの list リテラルは外部由来値が入り得ないため除外。"""
        f = tmp_path / "mod.py"
        f.write_text('x = " / ".join(["a", "b"])\n', encoding="utf-8")
        assert find_violations(f) == []

    def test_adjacent_lines_each_with_own_marker_both_suppress(self, tmp_path):
        """run_loop.py:1195-1196 の再現: 隣接行に対象 join が1個ずつあり、
        それぞれ行末にマーカーがある形（サイクル 2 DC-AS-001）。"""
        f = tmp_path / "mod.py"
        f.write_text(
            'a = "\\n".join(question_lines)  # nul-boundary: allow(質問行は改行結合のままにする)\n'
            'b = "\\n".join(axis_lines)  # nul-boundary: allow(軸行は改行結合のままにする)\n',
            encoding="utf-8",
        )
        assert find_violations(f) == []

    def test_empty_separator_adjacent_to_marked_join_does_not_invalidate_marker(self, tmp_path):
        """E-2 (空セパレータ) は「対象」に数えないため、隣接・同一行にあっても
        宣言付き join の有効性に影響しない（サイクル 2 DC-AM-003）。"""
        f = tmp_path / "mod.py"
        f.write_text(
            'a = "".join(x)\n'
            'b = "\\n".join(y)  # nul-boundary: allow(理由文字数は十分あります)\n'
            'c = "".join(z)\n',
            encoding="utf-8",
        )
        assert find_violations(f) == []

    def test_fstring_join_single_line_with_trailing_marker_suppresses(self, tmp_path):
        """f-string 内の join（単一行）にマーカーを付けた形は包含文の行範囲で解決され
        抑止される（サイクル 3 DC-AS-001）。"""
        f = tmp_path / "mod.py"
        f.write_text(
            'msg = f"prefix {sep.join(items)} suffix"'
            "  # nul-boundary: allow(理由文字数は十分あります)\n",
            encoding="utf-8",
        )
        assert find_violations(f) == []

    def test_fstring_join_multiline_implicit_concat_with_marker_suppresses(self, tmp_path):
        """f-string 内の join（暗黙連結の複数行）にマーカーを付けた形も、
        包含文の行範囲（末尾行）で解決され抑止される（サイクル 3 DC-AS-001）。"""
        f = tmp_path / "mod.py"
        f.write_text(
            "msg = (\n"
            '    f"prefix {sep.join(items)} "\n'
            '    f"suffix"\n'
            ")  # nul-boundary: allow(理由文字数は十分あります)\n",
            encoding="utf-8",
        )
        assert find_violations(f) == []

    def test_multiline_join_marker_on_last_line_suppresses(self, tmp_path):
        """複数行にまたがる join でマーカーが末尾行にある場合も抑止される。"""
        f = tmp_path / "mod.py"
        f.write_text(
            'x = "\\n".join(\n'
            "    items\n"
            ")  # nul-boundary: allow(理由文字数は十分あります)\n",
            encoding="utf-8",
        )
        assert find_violations(f) == []

    def test_c3_run_style_join_of_shadowing_free_toplevel_constant_is_compliant(self, tmp_path):
        """トップレベル定数がシャドウされず参照される通常ケースは準拠と判定される
        （シャドウ検出の対照系）。"""
        f = tmp_path / "mod.py"
        f.write_text(
            'SEP = "\\x00"\n'
            "\n"
            "def f(items):\n"
            "    return SEP.join(items)\n",
            encoding="utf-8",
        )
        assert find_violations(f) == []


# ---------------------------------------------------------------------------
# 頑健性テスト（Windows / エンコーディング / パース失敗）
# ---------------------------------------------------------------------------


class TestRobustness:
    def test_reads_utf8_file_with_japanese_comments_without_error(self, tmp_path):
        """非 UTF-8 既定環境（Windows cp932）を想定し、日本語コメントを含むファイルを
        例外なく読めること（DC-AS-001）。"""
        f = tmp_path / "mod.py"
        f.write_text(
            "# これは日本語のコメントです。文字化けや UnicodeDecodeError が発生しないこと\n"
            'x = "\\n".join(items)  # nul-boundary: allow(日本語の理由文でも問題ないはずです)\n',
            encoding="utf-8",
        )
        violations = find_violations(f)  # 例外を送出しないこと自体が検証対象
        assert violations == []

    def test_violation_snippet_is_ascii_safe(self, tmp_path):
        """assert メッセージの snippet は ascii() でエスケープされ、日本語を含んでいても
        cp932 stdout への書き出しで UnicodeEncodeError を起こさないこと。"""
        f = tmp_path / "mod.py"
        f.write_text('x = "、".join(items)  # 日本語コメント\n', encoding="utf-8")
        violations = find_violations(f)
        assert len(violations) == 1
        snippet = violations[0][3]
        # ascii() の出力は常に ASCII のみで構成される
        snippet.encode("ascii")

    def test_syntax_error_raises_with_file_path_in_message(self, tmp_path):
        f = tmp_path / "broken.py"
        f.write_text("def f(:\n    pass\n", encoding="utf-8")
        with pytest.raises(AssertionError, match=re.escape(str(f))):
            find_violations(f)

    def test_missing_file_raises_with_file_path_in_message(self, tmp_path):
        f = tmp_path / "does_not_exist.py"
        with pytest.raises(AssertionError, match=re.escape(str(f))):
            find_violations(f)


# ---------------------------------------------------------------------------
# 理由の長さ境界テスト
# ---------------------------------------------------------------------------


class TestReasonLengthBoundary:
    def test_reason_exactly_min_length_is_valid(self, tmp_path):
        reason = "a" * MIN_REASON_LEN
        f = tmp_path / "mod.py"
        f.write_text(f'x = "\\n".join(items)  # nul-boundary: allow({reason})\n', encoding="utf-8")
        assert find_violations(f) == []

    def test_reason_one_below_min_length_is_invalid(self, tmp_path):
        reason = "a" * (MIN_REASON_LEN - 1)
        f = tmp_path / "mod.py"
        f.write_text(f'x = "\\n".join(items)  # nul-boundary: allow({reason})\n', encoding="utf-8")
        violations = find_violations(f)
        assert len(violations) == 1
