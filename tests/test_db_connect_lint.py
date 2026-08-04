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
    - 位置情報（行番号）を保持
    - `from sqlite3 import connect` は AST で直接判定不可なため、
      実装側で文字列スキャンで検出（後述）

    returns: {'line': <lineno>, 'snippet': <コード断片>} のリスト
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


def find_db_connect_violations(path: Path) -> list[tuple[int, str, str]]:
    """1 ファイルから `sqlite3.connect` 呼び出しの違反を検出。

    returns: (行番号, 理由, タイプ) のリスト
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        raise AssertionError(f"{path}: 読み込み失敗: {e}") from e

    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as e:
        raise AssertionError(f"{path}: ast.parse 失敗: {e}") from e

    violations: list[tuple[int, str, str]] = []

    # マーカーを抽出
    markers = _extract_markers(text)

    # `from sqlite3 import connect` は無条件で違反
    for lineno in _detect_from_sqlite3_import_connect(text):
        violations.append((lineno, "from sqlite3 import connect", "from_import"))

    # `X.connect(...)` を検出
    aliases = _get_sqlite3_alias(tree)
    calls = _find_connect_calls(tree, aliases)

    for call_info in calls:
        lineno = call_info["line"]
        # マーカーがあれば抑止（同一行またはその直前行）
        if lineno in markers:
            continue
        if lineno - 1 in markers:
            continue
        # マーカーなし → 違反
        violations.append((lineno, "sqlite3.connect", "unmarked_call"))

    return sorted(set(violations), key=lambda x: x[0])


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

        all_violations: list[tuple[Path, int, str, str]] = []
        for f in files:
            violations = find_db_connect_violations(f)
            for lineno, reason, vtype in violations:
                all_violations.append((f, lineno, reason, vtype))

        assert not all_violations, (
            f"未マーカーの sqlite3.connect が {len(all_violations)} 件見つかりました:\n"
            + "\n".join(
                f"  {f.relative_to(REPO_ROOT)}:{lineno} - {reason}"
                for f, lineno, reason, _ in all_violations
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
