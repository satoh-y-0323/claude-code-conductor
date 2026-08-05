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

## 直前行マーカーの相関検証（E 周回 2 CR-NEW High・2026-08-05）

(B) の対処後も「直前行」という帰属経路そのものの曖昧さが残っていた:
`(lineno - 1 in markers)` は直前行の**中身**を一切見ないため、
`sqlite3.connect` と無関係な行にマーカーを貼っただけで次行の `connect` が抑止される
（`n = 1  # allow(...)` の次行に無警告の `sqlite3.connect(b)` を書ける・CR 実測）。

対処（fail-closed。同型の考え方を直前行にも適用）: 直前行マーカーを有効とみなすのは
**直前行がマーカー以外のトークンを持たない「マーカー専用のコメント行」であり、
かつ対象行の `connect` が 1 件だけ**の場合に限る。実運用の正しいパターン
（`migrate.py:84` 等: マーカーだけの行を `connect()` の直前に独立して置く）はこの形になっている。
判定は `tokenize` で「コメント・改行・インデント制御以外のトークンを持つ行」の集合
（`_find_code_line_numbers`）を作り、直前行がこの集合に含まれるかで行う。
含まれる場合（トレーリングマーカー付きの無関係なコード行・別の `connect` 呼び出し行など）は
**マーカーの有無によらず無条件で違反とする**（`unrelated_prev_line_marker`）。

## 分類優先順位の是正（E 周回 3 CR-NEW Medium・2026-08-05）

上記 (B) と直前行相関検証の組み合わせに分類ずれがあった: 「対象行に `connect` が
2 件以上（ambiguous_line）」かつ「直前行が相関の取れたマーカー専用行」の場合、
修正前は `unrelated_prev_line_marker`（「マーカーは対象行に直接置くか、マーカー
だけの独立したコメント行を直前に置け」）を返していたが、このケースは**まさに
その対処を既に実行済み**であり、それでもなお解消しない（唯一の解決策は
「行を分ける」）。読んだ人が既に満たしている対処を提示されて詰まる問題があった
（CR 実測）。

対処: `ambiguous_line` が真で、かつ何らかのマーカー（対象行直接 or 相関の取れた
直前行）が適用され得る場合は、常に `ambiguous_marker_scope`（「行を分けること」）
を優先する。マーカーが一切存在しない曖昧行（`unmarked_call`）や、マーカーはある
が相関しない単一 connect 行（`unrelated_prev_line_marker`）の分類は変更しない
（非回帰。`TestAmbiguousMarkerScopePriorityBothSides` で両側を検証）。

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

#: 直前行にマーカーはあるが、対象行の connect() との相関が確認できないために
#: fail-closed で違反にした場合の理由文言。直し方（対象行に直接置く／独立した
#: コメント専用行にする）が読み取れることを完了条件 7 で要求されている。
_UNRELATED_PREV_LINE_MARKER_REASON = (
    "sqlite3.connect: 直前行にマーカーがあるが対象行との相関が確認できないため無効"
    "（fail-closed）。マーカーは対象行に直接置くか、マーカーだけの独立したコメント行を"
    "connect() の直前に置くこと"
)

_NON_CODE_TOKEN_TYPES = frozenset((
    tokenize.COMMENT,
    tokenize.NL,
    tokenize.NEWLINE,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENCODING,
    tokenize.ENDMARKER,
))


def _find_code_line_numbers(text: str) -> set[int]:
    """『コメント・改行・インデント制御以外のトークンを持つ行』の集合を返す。

    直前行マーカーの相関検証に使う。この集合に含まれない行は「マーカー専用の
    コメント行」であり、対象行の connect() を指していると判断できる。含まれる行
    （コードと同居するトレーリングマーカー・別の connect() 呼び出し行など）は
    相関が確認できないため、直前行マーカーとしては無効にする。
    """
    code_lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in _NON_CODE_TOKEN_TYPES:
                continue
            # 複数行にまたがるトークン（三重引用符文字列等）は開始行から終了行まで
            # すべてコード行として扱う。
            code_lines.update(range(tok.start[0], tok.end[0] + 1))
    except tokenize.TokenError:
        pass  # tokenize 失敗は検査失敗ではなく処理スキップ（_extract_markers と同方針）

    return code_lines


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

    直前行マーカーの相関検証 (C・E 周回 2 CR-NEW High): 直前行にマーカーがあっても、
    その行がマーカー専用のコメント行でない（＝対象行の connect() と無関係な可能性がある）
    場合は無条件で違反とする（fail-closed）。

    分類優先順位 (E 周回 3 CR-NEW Medium): `ambiguous_line` が真で、かつ何らかの
    マーカー（対象行直接 or 相関の取れた直前行）が適用され得る場合は、
    `unrelated_prev_line_marker` ではなく常に `ambiguous_marker_scope`
    （「行を分けること」）を優先する。既に満たしている対処を提示して読み手を
    惑わせないための分類是正（モジュール docstring 参照）。
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
    code_lines = _find_code_line_numbers(text)

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

        has_line_marker = lineno in markers
        prev_line_has_marker = (lineno - 1) in markers
        # (C) 直前行がマーカー専用のコメント行（コードを一切持たない）でなければ、
        # そのマーカーはこの connect() を指しているとは判断できない。
        prev_marker_is_correlated = prev_line_has_marker and (lineno - 1) not in code_lines
        # ambiguous_line の真偽によらず、まず「何らかのマーカーがこの呼び出しに
        # 適用され得るか」を判定する（E 周回 3 CR-NEW Medium 是正・2026-08-05）。
        # 修正前は `valid_prev_marker = prev_marker_is_correlated and not ambiguous_line`
        # としており、対象行が曖昧（ambiguous_line=True）な時点で相関の取れた
        # 直前行マーカーが「非相関」扱いに落とされ、unrelated_prev_line_marker
        # （「マーカーだけの独立したコメント行を直前に置け」という、まさに
        # このケースで既に満たされている対処）に誤分類されていた（CR 実測）。
        marker_applies = has_line_marker or prev_marker_is_correlated

        if ambiguous_line and marker_applies:
            # (B) 同一行に 2 件以上の connect があり、かつ何らかのマーカーが
            # 存在する場合は、そのマーカーがどちらの呼び出しを指すか原理的に
            # 決められないため、常に「行を分ける」対処を示す
            # ambiguous_marker_scope を優先する。
            violations.append((lineno, col, _AMBIGUOUS_MARKER_REASON, "ambiguous_marker_scope"))
            continue

        if not ambiguous_line and marker_applies:
            # 通常の抑止: 1 行 1 呼び出し + マーカー（従来どおり）
            continue

        if prev_line_has_marker and not prev_marker_is_correlated:
            # (C) 直前行にマーカーはあるが、相関が確認できない
            # （無関係なコード行・別の connect() 行との同居等）。
            # ambiguous_line が真でもマーカーが一切適用され得ない
            # （has_line_marker=False かつ prev_marker_is_correlated=False）
            # ケースはここに落ちるが、それは「マーカーはあるが相関しない」
            # という診断のほうが「行を分けろ」より的確なため、あえて
            # ambiguous_marker_scope に統合しない。
            violations.append(
                (lineno, col, _UNRELATED_PREV_LINE_MARKER_REASON, "unrelated_prev_line_marker")
            )
            continue

        # マーカーなし（直前行にもマーカーが一切ない）→ 違反。
        # ambiguous_line であってもマーカーが存在しなければ、これは
        # 「行を分けろ」という診断ではなく単純な未マーカー呼び出しであるため
        # unmarked_call のままにする（同一行に複数あっても (A) の col により
        # 個別に残る・非回帰）。
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

        # 内訳（E 周回 2 CR-NEW Low 是正）:
        #   既存 17 箇所（fix-connect-dedup 以前からある呼び出し。migrate.py 等の
        #   `# c3-db-connect: pending(...)` マーカー付き呼び出しは対象外＝db.py 内のみ数える）
        # + connect() 自身の 1 箇所（ADR-9・`src/c3/db.py:111` の `sqlite3.connect(str(db_path))`）
        # = 18（Green 時点の現在値）。
        # S-C（pending 移行完了）時点の期待値は 1（connect() 自身のみが残り、
        # 17 箇所は c3.db.connect() 経由に置き換わる想定）。
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


class TestPrevLineMarkerCorrelation:
    """E 周回 2 CR-NEW High の回帰テスト: 直前行マーカーの相関未検証。

    修正前は `(lineno - 1 in markers)` のみで判定しており、直前行の中身と
    マーカーの相関を一切確認していなかった。無関係な行にマーカーを貼るだけで
    次行の未マーカー `sqlite3.connect` を無警告で通せた（CR 実測の再現・plan §2）。
    """

    def test_marker_on_unrelated_code_line_does_not_suppress_next_line(self, tmp_path):
        """直前行が connect と無関係なコード（トレーリングマーカー付き）の場合、
        次の行の connect は抑止されず違反になる。

        修正前は検出 0 件だった（plan 完了条件 1・親 Claude 実測の再現:
        `n = 1  # allow(...)` の直後の `sqlite3.connect(b)`）。
        """
        f = tmp_path / "mod.py"
        f.write_text(
            "import sqlite3\n"
            "n = 1  # c3-db-connect: allow(connect と無関係な行に貼ったマーカー)\n"
            "conn = sqlite3.connect(b)\n",
            encoding="utf-8",
        )
        violations = find_db_connect_violations(f)
        assert len(violations) == 1, violations
        assert violations[0][3] == "unrelated_prev_line_marker"
        assert violations[0][0] == 3  # conn の行

    def test_marker_trailing_on_other_connect_line_does_not_suppress_next_line(self, tmp_path):
        """直前行が『別の connect + トレーリングマーカー』の場合も、
        次の行の connect は抑止されず違反になる（CR 実測ケース 1 の再現）。
        """
        f = tmp_path / "mod.py"
        f.write_text(
            "import sqlite3\n"
            "conn1 = sqlite3.connect(a)  # c3-db-connect: allow(reason for conn1 only)\n"
            "conn2 = sqlite3.connect(b)\n",
            encoding="utf-8",
        )
        violations = find_db_connect_violations(f)
        # conn1 自身は同一行の直接マーカーで従来どおり抑止される。
        # conn2 は無関係な直前行マーカーのため違反になる。
        assert len(violations) == 1, violations
        assert violations[0][3] == "unrelated_prev_line_marker"
        assert violations[0][0] == 3  # conn2 の行

    def test_marker_only_line_immediately_before_connect_is_still_suppressed(self, tmp_path):
        """過剰対応の検出（plan 完了条件 2）: マーカーだけの独立したコメント行を
        connect() の直前に置く実運用パターン（migrate.py:84 等）は、
        引き続き抑止される。これが崩れたら不合格。
        """
        f = tmp_path / "mod.py"
        f.write_text(
            "import sqlite3\n"
            "# c3-db-connect: pending(c3.db.connect への移行待ち)\n"
            "conn = sqlite3.connect(a)\n",
            encoding="utf-8",
        )
        violations = find_db_connect_violations(f)
        assert violations == []

    def test_marker_only_line_before_ambiguous_target_line_is_still_a_violation(self, tmp_path):
        """直前行がマーカー専用のコメント行でも、対象行自体に connect が 2 件あれば
        どちらを指すか決められないため、引き続き違反になる（(B) との整合）。

        【E 周回 3 CR-NEW Medium 是正・2026-08-05】分類は `unrelated_prev_line_marker`
        ではなく `ambiguous_marker_scope`（「行を分ける」対処）になる。修正前は
        「マーカーだけの独立したコメント行を直前に置く」という、まさにこのケースで
        既に満たされている対処を示してしまい、読んだ人を惑わせていた（CR 実測）。
        """
        f = tmp_path / "mod.py"
        f.write_text(
            "import sqlite3\n"
            "# c3-db-connect: allow(十分な理由文字列)\n"
            "x, y = sqlite3.connect(a), sqlite3.connect(b)\n",
            encoding="utf-8",
        )
        violations = find_db_connect_violations(f)
        assert len(violations) == 2, violations
        assert all(vtype == "ambiguous_marker_scope" for _, _, _, vtype in violations)
        assert all("行を分け" in reason for _, _, reason, _ in violations), violations

    def test_violation_message_tells_reader_how_to_fix(self, tmp_path):
        """違反メッセージに直し方（対象行に置く／独立したコメント行にする）が
        読み取れること（完了条件 1 の裏取り: plan §2 の要求）。
        """
        f = tmp_path / "mod.py"
        f.write_text(
            "import sqlite3\n"
            "n = 1  # c3-db-connect: allow(connect と無関係な行に貼ったマーカー)\n"
            "conn = sqlite3.connect(a)\n",
            encoding="utf-8",
        )
        violations = find_db_connect_violations(f)
        assert violations, "無関係な直前行マーカーが検出されていません"
        assert all("対象行" in reason for _, _, reason, _ in violations), violations


class TestAmbiguousMarkerScopePriorityBothSides:
    """E 周回 3 CR-NEW Medium 是正の両側テスト（plan §5・完了条件 2）。

    - 見逃さない: 同一行 2 件 + 直前行に正しい（相関の取れた）マーカー →
      ambiguous_marker_scope になること（既存の
      test_marker_only_line_before_ambiguous_target_line_is_still_a_violation
      で検証済みだが、ここでは境界ケースを追加で押さえる）
    - 誤検出しない: 分類優先順位の変更が既存の他分類（unmarked_call /
      unrelated_prev_line_marker）を巻き込んでいないこと
    """

    def test_ambiguous_line_without_any_marker_stays_unmarked_call(self, tmp_path):
        """誤検出しない側: 同一行 2 件だがマーカーが一切無い場合は
        ambiguous_marker_scope ではなく従来どおり unmarked_call のまま
        （分類優先順位の変更が「マーカー無し」ケースを巻き込んでいないことの非回帰）。
        """
        f = tmp_path / "mod.py"
        f.write_text(
            "import sqlite3\n"
            "x, y = sqlite3.connect(a), sqlite3.connect(b)\n",
            encoding="utf-8",
        )
        violations = find_db_connect_violations(f)
        assert len(violations) == 2, violations
        assert all(vtype == "unmarked_call" for _, _, _, vtype in violations), violations

    def test_ambiguous_line_with_uncorrelated_prev_marker_stays_unrelated(self, tmp_path):
        """誤検出しない側: 対象行が曖昧（2 件）でも、直前行マーカーが
        コードと同居する非相関マーカーの場合は unrelated_prev_line_marker のまま
        （ambiguous_marker_scope に無条件で丸め込まない）。
        """
        f = tmp_path / "mod.py"
        f.write_text(
            "import sqlite3\n"
            "n = 1  # c3-db-connect: allow(connect と無関係な行に貼ったマーカー)\n"
            "x, y = sqlite3.connect(a), sqlite3.connect(b)\n",
            encoding="utf-8",
        )
        violations = find_db_connect_violations(f)
        assert len(violations) == 2, violations
        assert all(vtype == "unrelated_prev_line_marker" for _, _, _, vtype in violations), violations

    def test_fixing_by_splitting_the_line_resolves_the_violation(self, tmp_path):
        """見逃さない側の実効性検証（plan confirm タスク §5 の要求と同型）:
        `ambiguous_marker_scope` が示す「行を分ける」対処を実際に適用すると
        違反が解消することを実測する。
        """
        f = tmp_path / "mod.py"
        f.write_text(
            "import sqlite3\n"
            "# c3-db-connect: allow(十分な理由文字列)\n"
            "x, y = sqlite3.connect(a), sqlite3.connect(b)\n",
            encoding="utf-8",
        )
        before = find_db_connect_violations(f)
        assert before, "修正前は違反が検出されているはず"
        assert all(vtype == "ambiguous_marker_scope" for _, _, _, vtype in before)

        # 「行を分ける」対処を適用する
        f.write_text(
            "import sqlite3\n"
            "x = sqlite3.connect(a)  # c3-db-connect: allow(十分な理由文字列)\n"
            "y = sqlite3.connect(b)  # c3-db-connect: allow(十分な理由文字列)\n",
            encoding="utf-8",
        )
        after = find_db_connect_violations(f)
        assert after == [], f"行を分けても違反が解消していません: {after}"
