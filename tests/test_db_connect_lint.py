"""
`sqlite3.connect()` の直接呼び出しを検出する lint。

architecture-report-20260804-235224.md ADR-11 に基づく。

## 検出ロジックの骨子

`import sqlite3 [as X]` のエイリアスを解決し、`X.connect(...)` の呼び出しを全件検出。
**`from sqlite3 import connect` は無条件で違反**（マーカーによる免除も認めない）。

マーカー語彙:
- `# c3-db-connect: allow(<理由>)` — 恒久的な例外。理由は strip 後 5 文字以上
- `# c3-db-connect: pending(<参照>)` — 移行待ち。同じく 5 文字以上

マーカー抽出は **`tokenize.COMMENT` トークン限定**（行テキストへの素当ては
文字列リテラルと誤衝突するため不可・前例 test_nul_boundary_lint.py ADR-NB-5）。

## マーカー帰属の曖昧性（E-0 差し戻し・2026-08-05）

行単位のマーカー帰属には穴があった: 同一行に `X.connect(...)` が 2 件以上あると、
マーカー 1 個が両方を黙らせてしまう（`x, y = connect(a), connect(b)  # allow(...)` → 検出 0 件）。
列位置を持たないタプルが `set()` で重複排除される問題（過小報告）も併発していた。

対処（fail-closed。前例: test_nul_boundary_lint.py の `_suppress_by_markers` / DC-AM-007
`test_two_targets_same_line_one_marker_fail_closed`）:

- (A) 違反タプルに列位置（`col_offset`）を含める → 同一行の複数呼び出しが別々に数えられる
- (B) 同一行に 2 件以上の `connect` があり、その行（または直前行）にマーカーがある場合は
  **マーカーの有無によらず無条件で違反とする**。行に 1 個しか書けないマーカーが
  どの呼び出しを指すかは原理的に決められないため、曖昧な帰属そのものを許さない。
  書き手は行を分けるしかなくなり、分ければマーカーは一意になる

## 走査範囲

| 層 | 扱い |
|---|---|
| `src/c3/**/*.py`（`_template` / `db.py` 除外） | 対象。`db.py` は別カウンタで管理 |
| `.claude/hooks/**/*.py` | 対象 |
| `.claude/skills/*/scripts/**/*.py` | 対象 |
| `scripts/**/*.py` | 対象（ADR-10・ブートストラップ層） |
| `.dev/**/*.py`（存在時・`.dev/tests/**` 除外） | 対象。ただし `.dev/tests/` は除外 |
| `tests/**/*.py` | **対象外**（テスト層は 98 箇所・趣旨が異なる） |

## マニフェスト（3 つの assert）

1. **未マーカーの `sqlite3.connect` が 0 件**（新規複製はここで落ちる）
2. **`pending` マーカーの分布がマニフェストと厳密一致**（`==` で厳密照合）
3. **`src/c3/db.py` 内の `sqlite3.connect` 件数 == `_DB_PY_CONNECT_SITES`**
   - Red 時点: 17（未実装）
   - Green 時点: 18（`connect()` が 1 つ増える）
   - **この assert は Red 群に入る** (DC-AS-001)
   - S-C 完了時の期待値: 1（`connect()` 自身のみ残る）
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

# 走査対象の glob パターン
_GLOB_SRC_C3 = "src/c3/**/*.py"
_GLOB_CLAUDE_HOOKS = ".claude/hooks/**/*.py"
_GLOB_CLAUDE_SKILLS_SCRIPTS = ".claude/skills/*/scripts/**/*.py"
_GLOB_SCRIPTS = "scripts/**/*.py"
_GLOB_DEV = ".dev/**/*.py"

# ---------------------------------------------------------------------------
# マーカー正規表現
# ---------------------------------------------------------------------------

MIN_REASON_LEN = 5  # strip 後 5 文字以上でなければ無効
_MARKER_RE = re.compile(r"#\s*c3-db-connect:\s*(allow|pending)\(([^)]*)\)")

# ---------------------------------------------------------------------------
# 走査対象の解決
# ---------------------------------------------------------------------------


def iter_target_files(root: Path = REPO_ROOT) -> list[Path]:
    """walk査対象の Python ファイル一覧を返す。

    必須層（存在確認は呼び出し側）:
      - src/c3/**/*.py （`_template` と `db.py` を除外）
      - .claude/hooks/**/*.py
      - .claude/skills/*/scripts/**/*.py
      - scripts/**/*.py

    任意層（ディレクトリが存在する場合のみ）:
      - .dev/**/*.py （`.dev/tests/` を除外）

    全層とも再帰 glob に統一する。
    """
    files: list[Path] = []
    files.extend(root.glob(_GLOB_SRC_C3))
    files.extend(root.glob(_GLOB_CLAUDE_HOOKS))
    files.extend(root.glob(_GLOB_CLAUDE_SKILLS_SCRIPTS))
    files.extend(root.glob(_GLOB_SCRIPTS))

    dev = root / ".dev"
    if dev.is_dir():
        files.extend(root.glob(_GLOB_DEV))

    # _template と db.py を除外、.dev/tests/ 配下も除外（テスト層は趣旨が異なるため）。
    # パス区切り文字に依存しないよう、str(path) の直比較ではなく relative_to().parts の
    # タプル比較で判定する（Windows は "\\"、CI の Linux/macOS は "/" のため）。
    def _is_dev_tests(f: Path) -> bool:
        rel_parts = f.relative_to(root).parts
        return len(rel_parts) >= 2 and rel_parts[0] == ".dev" and rel_parts[1] == "tests"

    files = [
        f for f in files
        if "_template" not in f.parts
        and f.name != "db.py"
        and not _is_dev_tests(f)
    ]
    return sorted(set(files))


# ---------------------------------------------------------------------------
# AST ベースの検出
# ---------------------------------------------------------------------------


def _get_sqlite3_alias(tree: ast.Module) -> dict[str, str]:
    """モジュール内の `import sqlite3 [as X]` からエイリアスマップを構築。

    returns: {エイリアス名: 元の import 文} のマッピング
    """
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlite3":
                    name = alias.asname if alias.asname else alias.name
                    aliases[name] = "sqlite3"
    return aliases


def _find_connect_calls(tree: ast.Module, aliases: dict[str, str]) -> list[dict]:
    """AST から `X.connect(...)` 呼び出しを収集。

    - `X` はエイリアスで解決
    - 位置情報（行番号・列位置）を保持。列位置は同一行に複数の呼び出しがある場合に
      違反タプルを一意にするために使う（(A) の根拠。DC-AM-007 の前例に倣う）
    - `from sqlite3 import connect` は AST で直接判定不可なため、
      実装側で文字列スキャンで検出（後述）

    returns: {'line': <lineno>, 'col': <col_offset>, 'type': ..., 'receiver': ...} のリスト
    """
    calls: list[dict] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"):
            continue

        # receiver が Name で、エイリアスに登録されているか
        if isinstance(node.func.value, ast.Name):
            name = node.func.value.id
            if name in aliases:
                calls.append({
                    "line": node.lineno,
                    "col": node.col_offset,
                    "type": "attribute_call",
                    "receiver": name,
                })

    return calls


def _detect_from_sqlite3_import_connect(text: str) -> list[int]:
    """文字列スキャンで `from sqlite3 import connect` を検出。

    returns: 該当行番号のリスト
    """
    lines = text.splitlines()
    violations: list[int] = []

    for lineno, line in enumerate(lines, 1):
        # `from sqlite3 import connect` パターンを素当て
        if re.search(r'from\s+sqlite3\s+import\s+.*\bconnect\b', line):
            violations.append(lineno)

    return violations


def _extract_markers(text: str) -> dict[int, tuple[str, str]]:
    """マーカーを tokenize.COMMENT トークンから抽出。

    returns: {行番号: (タイプ, 理由)} のマッピング
    """
    markers: dict[int, tuple[str, str]] = {}
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for tok in tokens:
            if tok.type == tokenize.COMMENT:
                m = _MARKER_RE.search(tok.string)
                if m:
                    marker_type = m.group(1)  # 'allow' or 'pending'
                    reason = m.group(2).strip()
                    if len(reason) >= MIN_REASON_LEN:
                        markers[tok.start[0]] = (marker_type, reason)
    except tokenize.TokenError:
        pass  # tokenize 失敗は検査失敗ではなく処理スキップ

    return markers


#: 同一行に複数の connect があり、マーカーの帰属先が決まらないために
#: fail-closed で違反にした場合の理由文言。「行を分ける」対処が読み取れることを
#: plan-report fix-marker-scope 完了条件 7 で要求されている。
_AMBIGUOUS_MARKER_REASON = (
    "sqlite3.connect: 同一行に複数の接続がありマーカーの帰属先が決まらないため"
    "無条件で違反。行を分けること"
)


def find_db_connect_violations(path: Path) -> list[tuple[int, int, str, str]]:
    """1 ファイルから `sqlite3.connect` 呼び出しの違反を検出。

    returns: (行番号, 列位置, 理由, タイプ) のリスト

    列位置を持たせる理由 (A): 同一行に複数の `X.connect(...)` があると、列位置なしでは
    完全に同一のタプルになり、末尾の `set()` による重複排除で件数が過小報告される
    （E-0 差し戻し [E0-2]）。

    同一行に 2 件以上の `connect` がある場合の扱い (B): その行（または直前行）の
    マーカーは「どちらの呼び出しを指すか」原理的に決められないため、マーカーの
    有無によらず無条件で違反とする（fail-closed）。1 行に 1 つの `connect` であれば
    従来どおりマーカーで抑止される。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        raise AssertionError(f"{path}: 読み込み失敗: {e}") from e

    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as e:
        raise AssertionError(f"{path}: ast.parse 失敗: {e}") from e

    violations: list[tuple[int, int, str, str]] = []

    # マーカーを抽出
    markers = _extract_markers(text)

    # `from sqlite3 import connect` は無条件で違反（列位置の曖昧性は対象外のため 0 固定）
    for lineno in _detect_from_sqlite3_import_connect(text):
        violations.append((lineno, 0, "from sqlite3 import connect", "from_import"))

    # `X.connect(...)` を検出
    aliases = _get_sqlite3_alias(tree)
    calls = _find_connect_calls(tree, aliases)

    # 同一行の connect 呼び出し数（マーカー帰属の曖昧性判定に使う）
    calls_per_line: dict[int, int] = {}
    for call_info in calls:
        calls_per_line[call_info["line"]] = calls_per_line.get(call_info["line"], 0) + 1

    for call_info in calls:
        lineno = call_info["line"]
        col = call_info["col"]
        ambiguous_line = calls_per_line[lineno] >= 2
        marked = (lineno in markers) or (lineno - 1 in markers)

        if marked and not ambiguous_line:
            # 通常の抑止: 1 行 1 呼び出し + マーカー（従来どおり）
            continue

        if marked and ambiguous_line:
            # (B) 帰属が曖昧なマーカーは効かせない。fail-closed で違反にする
            violations.append((lineno, col, _AMBIGUOUS_MARKER_REASON, "ambiguous_marker_scope"))
            continue

        # マーカーなし → 違反（同一行に複数あっても (A) の col により個別に残る）
        violations.append((lineno, col, "sqlite3.connect", "unmarked_call"))

    return sorted(set(violations), key=lambda x: (x[0], x[1]))


# ---------------------------------------------------------------------------
# 本検査
# ---------------------------------------------------------------------------

# 期待マニフェスト（Green 時点の期待値）
_PENDING_MANIFEST_DEV_PRESENT = {
    "src/c3/migrate.py": 1,
    ".claude/hooks/tier_gap_check.py": 1,
    ".claude/skills/dev-workflow/scripts/record_agent_outcome.py": 1,
    ".dev/smoke/run_smoke.py": 1,
}

_PENDING_MANIFEST_DEV_ABSENT = {
    "src/c3/migrate.py": 1,
    ".claude/hooks/tier_gap_check.py": 1,
    ".claude/skills/dev-workflow/scripts/record_agent_outcome.py": 1,
}


class TestRequiredLayersAreNotEmpty:
    """パス typo による検査の空回りを防ぐガード。"""

    def test_src_c3_layer_exists(self):
        files = list(REPO_ROOT.glob(_GLOB_SRC_C3))
        files = [f for f in files if "_template" not in f.parts and f.name != "db.py"]
        assert files, f"{_GLOB_SRC_C3} が 1 件も見つかりません"

    def test_claude_hooks_layer_exists(self):
        files = list(REPO_ROOT.glob(_GLOB_CLAUDE_HOOKS))
        assert files, f"{_GLOB_CLAUDE_HOOKS} が 1 件も見つかりません"

    def test_claude_skills_scripts_layer_exists(self):
        files = list(REPO_ROOT.glob(_GLOB_CLAUDE_SKILLS_SCRIPTS))
        assert files, f"{_GLOB_CLAUDE_SKILLS_SCRIPTS} が 1 件も見つかりません"

    def test_scripts_layer_exists(self):
        files = list(REPO_ROOT.glob(_GLOB_SCRIPTS))
        assert files, f"{_GLOB_SCRIPTS} が 1 件も見つかりません"


class TestNoDbConnectViolations:
    """sqlite3.connect 呼び出しの検出。

    Red 時点: 未マーカーの呼び出しが複数件（主に `scripts/audit_review_decisions.py`）。
    Green 時点: 全てマーカーで抑止または `c3.db.connect()` へ移行。
    """

    def test_no_unmarked_sqlite3_connect_in_target_files(self):
        """未マーカーの `sqlite3.connect` が 0 件であること。"""
        files = iter_target_files()
        assert files, "iter_target_files() が 1 件もファイルを返しませんでした"

        all_violations: list[tuple[Path, int, int, str, str]] = []
        for f in files:
            violations = find_db_connect_violations(f)
            for lineno, col, reason, vtype in violations:
                all_violations.append((f, lineno, col, reason, vtype))

        assert not all_violations, (
            f"未マーカーの sqlite3.connect が {len(all_violations)} 件見つかりました:\n"
            + "\n".join(
                f"  {f.relative_to(REPO_ROOT)}:{lineno}:{col} - {reason}"
                for f, lineno, col, reason, _ in all_violations
            )
        )

    def test_pending_manifest_consistency(self):
        """pending マーカーの分布がマニフェストと厳密一致。

        .dev が存在するかで期待値が変わる（.dev/tests は除外のため、
        .dev/smoke/run_smoke.py のみが対象）。
        """
        dev_exists = (REPO_ROOT / ".dev").is_dir()
        expected_manifest = (
            _PENDING_MANIFEST_DEV_PRESENT if dev_exists
            else _PENDING_MANIFEST_DEV_ABSENT
        )

        files = iter_target_files()
        actual_pending: dict[str, int] = {}

        for f in files:
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue

            markers = _extract_markers(text)
            for lineno, (marker_type, _) in markers.items():
                if marker_type == "pending":
                    # マニフェストのキーは POSIX 区切りで書かれているため as_posix() で正規化する。
                    # str() のままだと Windows で "src\\c3\\migrate.py" になり、Linux / macOS CI では
                    # 通るのに Windows でだけ落ちる（2026-08-05 に実測）。
                    rel_path = f.relative_to(REPO_ROOT).as_posix()
                    actual_pending[rel_path] = actual_pending.get(rel_path, 0) + 1

        # 厳密一致チェック
        assert actual_pending == expected_manifest, (
            f"pending マニフェスト不一致。\n"
            f"期待: {expected_manifest}\n"
            f"実測: {actual_pending}"
        )

    def test_db_py_connect_sites_count(self):
        """src/c3/db.py 内の `sqlite3.connect` 件数が _DB_PY_CONNECT_SITES と一致。

        Red 時点: 17（未実装）
        Green 時点: 18（`connect()` が 1 つ増える）
        **この assert は Red 群に入る**（DC-AS-001）
        """
        db_py = REPO_ROOT / "src" / "c3" / "db.py"
        assert db_py.exists(), f"{db_py} が見つかりません"

        text = db_py.read_text(encoding="utf-8")
        count = text.count("sqlite3.connect")

        _DB_PY_CONNECT_SITES = 18
        assert count == _DB_PY_CONNECT_SITES, (
            f"src/c3/db.py の sqlite3.connect 件数が不一致。\n"
            f"期待: {_DB_PY_CONNECT_SITES}\n"
            f"実測: {count}"
        )


class TestSameLineMarkerScopeAmbiguity:
    """E-0 差し戻し（欠陥 [E0-2]・plan-report fix-marker-scope）の回帰テスト。

    合成ソースで `find_db_connect_violations()` を直接検証する
    （前例: test_nul_boundary_lint.py の TestDetectsViolations /
    TestDoesNotDetectViolations・DC-AM-007 `_suppress_by_markers`）。
    """

    def test_same_line_multiple_connects_with_marker_is_violation(self, tmp_path):
        """(B) 同一行に 2 件の connect + allow マーカーは、マーカーが効かず違反になる。

        修正前は行単位のマーカー帰属により検出 0 件だった（完了条件 1）。
        """
        f = tmp_path / "mod.py"
        f.write_text(
            "import sqlite3\n"
            "x, y = sqlite3.connect(a), sqlite3.connect(b)"
            "  # c3-db-connect: allow(片方だけ許可したい)\n",
            encoding="utf-8",
        )
        violations = find_db_connect_violations(f)
        assert len(violations) == 2, violations
        assert all(vtype == "ambiguous_marker_scope" for _, _, _, vtype in violations)
        # (A): 列位置が異なるため 2 件とも別々のタプルとして残っている
        cols = {col for _, col, _, _ in violations}
        assert len(cols) == 2, violations

    def test_same_line_multiple_connects_without_marker_is_two_violations(self, tmp_path):
        """(A) 同一行に未マーカーの connect が 2 件あれば、違反も 2 件になる。

        修正前は `sorted(set(violations))` の重複排除で 1 件に潰れていた
        （完了条件 2・E-0 実測の [E0-2]）。
        """
        f = tmp_path / "mod.py"
        f.write_text(
            "import sqlite3\n"
            "x, y = sqlite3.connect(a), sqlite3.connect(b)\n",
            encoding="utf-8",
        )
        violations = find_db_connect_violations(f)
        assert len(violations) == 2, violations
        assert all(vtype == "unmarked_call" for _, _, _, vtype in violations)
        cols = {col for _, col, _, _ in violations}
        assert len(cols) == 2, violations

    def test_single_connect_per_line_with_marker_is_still_suppressed(self, tmp_path):
        """過剰対応の検出（完了条件 3）: 1 行 1 connect + マーカーは従来どおり抑止される。"""
        f = tmp_path / "mod.py"
        f.write_text(
            "import sqlite3\n"
            "conn = sqlite3.connect(a)  # c3-db-connect: allow(十分な理由文字列)\n",
            encoding="utf-8",
        )
        violations = find_db_connect_violations(f)
        assert violations == []

    def test_single_connect_per_line_without_marker_is_still_a_violation(self, tmp_path):
        """非回帰: 1 行 1 connect・マーカーなしは引き続き通常どおり違反 1 件。"""
        f = tmp_path / "mod.py"
        f.write_text(
            "import sqlite3\n"
            "conn = sqlite3.connect(a)\n",
            encoding="utf-8",
        )
        violations = find_db_connect_violations(f)
        assert len(violations) == 1
        assert violations[0][3] == "unmarked_call"

    def test_ambiguous_violation_message_tells_reader_to_split_the_line(self, tmp_path):
        """違反メッセージに「行を分ける」対処が読み取れること（完了条件 7 の裏取り）。"""
        f = tmp_path / "mod.py"
        f.write_text(
            "import sqlite3\n"
            "x, y = sqlite3.connect(a), sqlite3.connect(b)"
            "  # c3-db-connect: allow(片方だけ許可したい)\n",
            encoding="utf-8",
        )
        violations = find_db_connect_violations(f)
        assert violations, "曖昧な帰属が検出されていません"
        assert all("行を分け" in reason for _, _, reason, _ in violations), violations
