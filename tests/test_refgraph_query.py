"""参照抽出器のクエリ層（`scripts/refgraph_query.py`）のテスト。

`docs/refgraph-contract.md` §5-1「クエリ層」に基づく **Red フェーズ**のテスト。
本ファイルを書いた時点で `scripts/refgraph_query.py` は存在しない。

抽出器（`src/c3/refgraph.py`）は網羅的に関係を採る（実リポジトリで 36,498 辺）。
そのままでは読めないので、**読む側で絞る道具**がクエリ層である。絞る軸は 2 つ:

- **軸 1: 出所のカテゴリで外す** — `live` / `history` / `derived` / `dev` / `tests`
- **軸 2: 派生の参照先を原本へ畳む** — `src/c3/_template/<X>` → `<X>`（= `.claude/...`）

カバーする完成条件（契約 §5-1「クエリ層の完成条件」）:

- 条件 1（カテゴリ判定が純粋関数）: `TestCategorizeIsPure`
- 条件 2（実測値の再現）  : `TestMeasuredCountsAreReproduced`（環境ゲートあり）
- 条件 3（畳むと ambiguous が減る・正の対照つき）: `TestFoldLinksCollapsesDerivedTwins`
- 条件 4（抽出器を変更しない）: `TestExtractorIsNotModified`
- 条件 5（do-nothing スタブ検査）: `TestApiShape` と `TestExtractorIsNotModified`
  **のみ**がスタブで緑になってよい（合計 9 件）。理由は各クラスの docstring を参照。

--------------------------------------------------------------------------
本ファイルが確定させるクエリ層の API（契約 §5-1 は名前を定めていないので tester が決めた）
--------------------------------------------------------------------------

    CATEGORIES: tuple[str, ...]                    # 契約 §5-1 表の 5 カテゴリ
    categorize(path: str) -> str                   # 純粋関数・パス文字列だけを取る
    filter_links(links, categories) -> tuple[Link, ...]   # **source** のカテゴリで絞る
    fold_target(target: str) -> str                # 純粋関数・派生 target を原本へ
    fold_links(links) -> tuple[Link, ...]          # target を畳んで重複除去する

設計判断（契約に書かれていないので、ここで決めて宣言する）:

1. **分類はルート相対 POSIX パスの前方一致で行い、成分境界で切る。**
   `.claude/tmp-scratch.md` は `history` ではない（`.claude/tmp/` で始まらない）。
   `CHANGELOG.md` だけは**完全一致**（`docs/CHANGELOG.md` は `live`）
2. **多重一致は起こらない。** ルート相対の前方一致で見る限り 5 カテゴリの条件は
   互いに素になる（`.dev/tests/x.py` は `dev`、`src/c3/_template/.claude/reports/x`
   は `derived`）。「含む」で書いた実装だけが取り違える。これを反例 1 で殺す
3. **`filter_links` は `source` のカテゴリだけで絞る**（契約 §5-1「出所のカテゴリ」）。
   `target` のカテゴリは見ない。存在しないカテゴリ名を渡したら `ValueError`
   （黙って空を返すと、綴り間違いが「そのカテゴリは 0 件」に化ける）
4. **`fold_links` は target の書き換えと重複除去だけを行い、`resolution` は書き換えない。**
   曖昧だったという事実は出所の記録なので消さない。`ambiguous` の**本数**は
   重複除去だけで減る（契約 §5-1 条件 3 はそれで満たされる）
5. **`fold_links` は `source` を畳まない。** 畳むと「どこから参照されたか」が失われ、
   契約 §2 原則 2（出所を残す）を壊す

テスト作法（契約 §7）:

1. 不在は `assert xs == []` で直接 assert する（空ループ内で assert しない）
2. 合成入力では参照先のファイルを実際に作る
3. パスはルート相対 POSIX
4. 「無いこと」の検査は「在ること」の対照と同じ fixture に置く
5. assert のないテストを書かない
6. 機構を足したら同じ周回でその機構を検査するテストを足す

合成ツリーの題材について（今日実際に踏んだ落とし穴の回避）:

- skill / agent ディレクトリを**作らない**。作ると `md_bare_skill_name` /
  `md_bare_agent_name` が発火し、パス解決を測っているつもりで名前解決を測る
- 参照元を対象の**祖先ディレクトリ配下に置かない**（source 相対で解決してしまう）。
  `.claude/rules/` から `.claude/docs/dup/note.md` を狙うのはそのため
- `write_text()` には必ず `encoding="utf-8"` を付ける（既定 cp932 の事故）
"""

from __future__ import annotations

import ast
import os
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

# 抽出器はモジュール属性経由で参照する（型と Link の生成にだけ使う）。
import c3.refgraph as refgraph  # noqa: E402

# ---------------------------------------------------------------------------
# `scripts/` は sys.path に無いので追加する（既存の tests/test_check_deletions.py と同じ流儀）。
# ただし **module レベルでは import しない**。未実装時に collection error になると
# 全ケースが実行されず、「スタブで緑になるのは何件か」（契約 §5-1 条件 5）を
# 確かめられなくなるため、テスト本体から遅延ロードする。
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
QUERY_MODULE_PATH = SCRIPTS_DIR / "refgraph_query.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def query():
    """`scripts/refgraph_query.py` をモジュールとして返す（実行時に解決する）."""
    import refgraph_query

    return refgraph_query


# ---------------------------------------------------------------------------
# 契約 §5-1 の表を**テスト側で独立に**書き下したもの（実装から import しない）。
# 実装が定義を減らしたり分類を変えたら赤になる。
# ---------------------------------------------------------------------------
CATEGORY_NAMES = ("live", "history", "derived", "dev", "tests")

TEMPLATE_PREFIX = "src/c3/_template/"
DEV_PREFIX = ".dev/"
TESTS_PREFIX = "tests/"
HISTORY_PREFIXES = (
    ".claude/reports/",
    ".claude/memory/",
    ".claude/agent-memory/",
    ".claude/tmp/",
)
HISTORY_EXACT = ("CHANGELOG.md",)


def _oracle_category(path: str) -> str:
    """契約 §5-1 の表から独立に組んだ参照実装（実装との直積突合に使う）."""
    if path.startswith(TEMPLATE_PREFIX):
        return "derived"
    if path.startswith(DEV_PREFIX):
        return "dev"
    if path.startswith(TESTS_PREFIX):
        return "tests"
    if path in HISTORY_EXACT or path.startswith(HISTORY_PREFIXES):
        return "history"
    return "live"


def _oracle_fold_target(target: str) -> str:
    if target.startswith(TEMPLATE_PREFIX):
        return target[len(TEMPLATE_PREFIX):]
    return target


def _key(link) -> tuple:
    return (
        link.relation,
        link.source,
        link.source_line,
        link.context,
        link.target,
        link.target_exists,
        link.resolution,
    )


def _oracle_fold(links) -> tuple:
    """`fold_links` の参照実装: target を畳み、7 フィールド一致の重複を出現順で除去."""
    seen = set()
    out = []
    for link in links:
        folded = replace(link, target=_oracle_fold_target(link.target))
        key = _key(folded)
        if key in seen:
            continue
        seen.add(key)
        out.append(folded)
    return tuple(out)


# ---------------------------------------------------------------------------
# 実測値（2026-08-05・契約 §5-1 の表と本文。**再測定しない**）
# ---------------------------------------------------------------------------
MEASURED_AT = "2026-08-05"
MEASURED_TOTAL_LINKS = 36_498
MEASURED_COUNTS = {
    "live": 2_992,
    "history": 31_352,
    "derived": 864,
    "dev": 1_048,
    "tests": 242,
}
MEASURED_LIVE_SOURCE_FILES = 128
MEASURED_LIVE_MISSING = 300
MEASURED_LIVE_AMBIGUOUS = 2_005
MEASURED_LIVE_AMBIGUOUS_WITH_TEMPLATE_TARGET = 1_007

# 許容幅。根拠はレポート（test-report）§実測値の縛り方に記載。要旨:
#   - 文書を 1 行足すだけで数字は動く（`.claude/reports/` へレポートを 1 本書くと
#     history が数十〜百数十増える）。完全一致にすると自分の作業で赤になる
#   - 一方カテゴリを丸ごと取り違える事故は、**小さい側**のカテゴリで必ず
#     ±5% を大きく超える（最小の `tests` 242 が他へ流れれば -100%）
MEASURED_TOLERANCE = 0.05

# 本タスク（クエリ層の追加）が新しく作るファイル。実測時には存在しなかったので
# 件数比較から外す。契約 §5-1 の注意「クエリ層の出力ファイルを走査ツリー内に置くと
# 次の抽出でそれ自身が参照元になる」の一般化。
SELF_ADDED_SOURCES = frozenset(
    {
        "tests/test_refgraph_query.py",
        "scripts/refgraph_query.py",
    }
)

# 契約 §5 の抽出器公開 API（AST で数えた top-level の非アンダースコア定義）。
# クエリ層のために抽出器へ関数・定数を足したらここで赤になる。
EXTRACTOR_PUBLIC_DEFINITIONS = (
    "CONTEXT_MAX_CHARS",
    "Graph",
    "Link",
    "Node",
    "SCHEMA_VERSION",
    "Skipped",
    "build_graph",
    "read_graph",
    "write_graph",
)
EXTRACTOR_DATACLASS_FIELDS = {
    "Node": ("id", "kind", "exists"),
    "Link": (
        "relation",
        "source",
        "source_line",
        "context",
        "target",
        "target_exists",
        "resolution",
    ),
    "Skipped": ("path", "reason"),
}


# ---------------------------------------------------------------------------
# ヘルパー / fixture
# ---------------------------------------------------------------------------
def _mkfile(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _link(
    *,
    source: str,
    target: str,
    relation: str = "md_code_span_path",
    source_line: int = 1,
    context: str = "ctx",
    target_exists: bool = True,
    resolution: str = "exact",
):
    """テスト用の合成 `Link`（抽出器の型をそのまま使う）."""
    return refgraph.Link(
        relation=relation,
        source=source,
        source_line=source_line,
        context=context,
        target=target,
        target_exists=target_exists,
        resolution=resolution,
    )


def _build_synthetic_tree(root: Path) -> None:
    """5 カテゴリのうち live / history / derived を含む最小ツリー。

    - `.claude/docs/dup/note.md` と `src/c3/_template/.claude/docs/dup/note.md`
      の 2 候補があるので、部分パス `dup/note.md` は `ambiguous` になる
    - 参照元は `.claude/rules/`（live）と `.claude/reports/`（history）の 2 か所。
      どちらも対象の祖先ディレクトリではないので source 相対では解決しない
    - skills / agents ディレクトリは**作らない**（bare name 解決の発火を避ける）
    """
    _mkfile(root, ".claude/docs/dup/note.md", "# original\n")
    _mkfile(root, ".claude/hooks/alive.py", "# alive\n")
    _mkfile(
        root,
        "src/c3/_template/.claude/docs/dup/note.md",
        "# build-time copy\n\n- exact path: `.claude/hooks/alive.py`\n",
    )

    memo = (
        "# memo\n"
        "\n"
        "- ambiguous partial path: `dup/note.md`\n"
        "- exact path: `.claude/hooks/alive.py`\n"
    )
    _mkfile(root, ".claude/rules/memo-live.md", memo)
    _mkfile(root, ".claude/reports/memo-history.md", memo)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def repo_graph(repo_root):
    """実リポジトリ全体の抽出結果（session スコープ・1 回だけ構築する）."""
    return refgraph.build_graph(repo_root)


@pytest.fixture(scope="session")
def repo_links(repo_graph):
    """実測時に存在しなかった 2 ファイル（本タスクの成果物）を除いた辺."""
    return tuple(
        link for link in repo_graph.links if link.source not in SELF_ADDED_SOURCES
    )


@pytest.fixture
def synthetic_graph(tmp_path):
    _build_synthetic_tree(tmp_path)
    return refgraph.build_graph(tmp_path)


class _FilesystemTouched(AssertionError):
    """純粋であるべき関数がファイルシステムを見たときに投げる."""


# 落とす API は「存在確認・列挙」系に限る。`builtins.open` / `os.stat` は
# pytest 自身が失敗レポートの描画に使うため触らない（限界はレポートに記載）。
_FILESYSTEM_APIS = (
    (os.path, "exists"),
    (os.path, "isdir"),
    (os.path, "isfile"),
    (os, "listdir"),
    (os, "scandir"),
    (os, "walk"),
    (Path, "exists"),
    (Path, "is_file"),
    (Path, "is_dir"),
    (Path, "iterdir"),
    (Path, "glob"),
    (Path, "rglob"),
)


def _call_with_filesystem_blocked(func, values) -> dict:
    """`func` を FS API を落とした状態で呼び、`{入力: 返り値}` を返す。

    差し替えは `with` の中だけに閉じる。**assert は差し替えを戻してから行う**こと
    （差し替えたまま失敗すると、pytest が traceback を描画するときに
    `Path.exists()` を呼んで INTERNALERROR になる。実際に踏んだ）。
    """

    def boom(*args, **kwargs):
        raise _FilesystemTouched(
            "this function must not touch the filesystem (契約 §5-1 条件 1)"
        )

    with pytest.MonkeyPatch.context() as patch:
        for owner, name in _FILESYSTEM_APIS:
            patch.setattr(owner, name, boom)
        return {value: func(value) for value in values}


def _measurement_environment_gaps(root: Path) -> list:
    """実測（2026-08-05）が行われた作業ツリーに揃っていたものが揃っているか。

    `.dev/` `src/c3/_template/` `.claude/reports/` `.claude/agent-memory/` は
    すべて `.gitignore` されている。CI のクリーンチェックアウトには存在しないので、
    そこで実測値を比較しても「分類が壊れた」ことにはならない。
    """
    gaps = []
    if not (root / ".dev").is_dir():
        gaps.append(".dev/")
    if not (root / "src" / "c3" / "_template" / ".claude").is_dir():
        gaps.append("src/c3/_template/.claude/")
    if not (root / ".claude" / "agent-memory").is_dir():
        gaps.append(".claude/agent-memory/")
    reports = root / ".claude" / "reports"
    if not reports.is_dir() or not any(reports.glob("*.md")):
        gaps.append(".claude/reports/*.md")
    return gaps


def _within_tolerance(observed: int, expected: int) -> bool:
    return abs(observed - expected) <= expected * MEASURED_TOLERANCE


# ===========================================================================
# 契約 §5-1 条件 5: do-nothing スタブで緑になってよいのは本クラスだけ（その 1）
# ===========================================================================
class TestApiShape:
    """API の型検査。**何も絞らないスタブで緑になってよい**（宣言済み・5 件）."""

    def test_categories_constant_lists_the_five_contract_categories(self):
        """`CATEGORIES` が契約 §5-1 表の 5 カテゴリであること（順不同）."""
        assert sorted(query().CATEGORIES) == sorted(CATEGORY_NAMES)

    def test_categorize_returns_one_of_the_categories(self):
        """`categorize(path)` が文字列を返し、5 カテゴリのいずれかであること."""
        got = query().categorize(".claude/hooks/stop.py")

        assert isinstance(got, str)
        assert got in CATEGORY_NAMES

    def test_filter_links_returns_a_tuple_of_links(self):
        """`filter_links(links, categories)` が `Link` の tuple を返すこと."""
        links = [_link(source=".claude/hooks/a.py", target=".claude/hooks/b.py")]

        got = query().filter_links(links, CATEGORY_NAMES)

        assert isinstance(got, tuple)
        assert [type(item) for item in got if type(item) is not refgraph.Link] == []

    def test_fold_target_returns_a_string(self):
        """`fold_target(target)` が文字列を返すこと."""
        got = query().fold_target(".claude/hooks/stop.py")

        assert isinstance(got, str)

    def test_fold_links_returns_a_tuple_of_links(self):
        """`fold_links(links)` が `Link` の tuple を返すこと."""
        links = [_link(source=".claude/hooks/a.py", target=".claude/hooks/b.py")]

        got = query().fold_links(links)

        assert isinstance(got, tuple)
        assert [type(item) for item in got if type(item) is not refgraph.Link] == []


# ===========================================================================
# 契約 §5-1 条件 4: 抽出器を変更しないこと
#
# ★ do-nothing スタブで緑になってよい（宣言済み・4 件）。
#   理由: 本クラスが測るのは**抽出器側**の不変性であり、クエリ層が何もしない実装で
#   あっても「抽出器を変更していない」は正しく成立する。ここに positive control を
#   置いてスタブを赤にしようとすると、クエリ層の機能テストの重複になる。
# ===========================================================================
class TestExtractorIsNotModified:
    def test_extractor_public_definitions_are_unchanged(self):
        """`src/c3/refgraph.py` の top-level 公開定義が 9 個のまま増減しないこと.

        クエリ層のために抽出器へ分類関数・カテゴリ定数を足したらここで赤になる
        （契約 §5-1 条件 4「クエリ層は `read_graph` / `build_graph` の結果を読むだけ」）。
        """
        tree = ast.parse(Path(refgraph.__file__).read_text(encoding="utf-8"))

        defined = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.append(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        defined.append(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                defined.append(node.target.id)

        public = sorted(name for name in defined if not name.startswith("_"))
        assert public == sorted(EXTRACTOR_PUBLIC_DEFINITIONS), (
            "the extractor's public surface changed while adding the query layer"
        )

    def test_extractor_dataclass_fields_are_unchanged(self):
        """`Node` / `Link` / `Skipped` のフィールドが増減しないこと.

        クエリ層のために `Link.category` のような欄を抽出器へ足すと、判定が
        抽出器へ再混入する（契約 §6 条件 5 / §9）。
        """
        got = {}
        for name in EXTRACTOR_DATACLASS_FIELDS:
            cls = getattr(refgraph, name)
            got[name] = tuple(cls.__dataclass_fields__)

        assert got == {key: tuple(val) for key, val in EXTRACTOR_DATACLASS_FIELDS.items()}

    def test_query_layer_lives_outside_the_distributed_package(self):
        """クエリ層が `scripts/` にあり、配布物 `src/c3/` に無いこと（契約 §5-1 置き場所）."""
        assert QUERY_MODULE_PATH.is_file(), (
            f"the query layer must live at scripts/refgraph_query.py; {QUERY_MODULE_PATH} は無い"
        )

        leaked = sorted(
            str(path.relative_to(REPO_ROOT)).replace("\\", "/")
            for path in (REPO_ROOT / "src").rglob("refgraph_query.py")
        )
        assert leaked == [], f"判定を配布物へ置いてはいけない（契約 §1-2 / §5-1）: {leaked}"

    def test_query_module_never_writes_into_the_extractor(self):
        """クエリ層が抽出器の属性へ書き込まない（monkeypatch しない）こと.

        `refgraph.build_graph = ...` のような差し替えは「抽出器を変更しない」の
        抜け道になる。AST で属性代入・`setattr` / `delattr` を禁じる。
        """
        assert QUERY_MODULE_PATH.is_file(), f"{QUERY_MODULE_PATH} が無い（未実装）"
        source = QUERY_MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)

        aliases = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "c3.refgraph" or alias.name.endswith("refgraph"):
                        aliases.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "refgraph":
                        aliases.add(alias.asname or alias.name)

        offending = []
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            elif isinstance(node, ast.Delete):
                targets = list(node.targets)
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in aliases
                ):
                    offending.append((getattr(node, "lineno", 0), target.attr))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"setattr", "delattr"} and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Name) and first.id in aliases:
                        offending.append((node.lineno, node.func.id))

        assert offending == [], (
            f"the query layer must not mutate the extractor module: {offending}"
        )


# ===========================================================================
# 契約 §5-1 条件 1: カテゴリ判定が純粋関数であること
# ===========================================================================
class TestCategorizeIsPure:
    # 合成入力で 5 カテゴリを網羅する。境界（成分区切り）のケースを同じ表に置く。
    CASES = (
        # --- live（表の「上記以外」） -------------------------------------
        (".claude/settings.json", "live"),
        (".claude/hooks/stop.py", "live"),
        (".claude/skills/dev-workflow/SKILL.md", "live"),
        (".claude/agents/tester.md", "live"),
        (".claude/docs/taxonomy.md", "live"),
        (".claude/rules/promoted/index.md", "live"),
        (".claude/CLAUDE.md", "live"),
        ("src/c3/cli.py", "live"),
        ("src/c3/refgraph.py", "live"),
        ("scripts/check_deletions.py", "live"),
        ("hatch_build.py", "live"),
        ("README.md", "live"),
        ("docs/refgraph-contract.md", "live"),
        # --- history ------------------------------------------------------
        ("CHANGELOG.md", "history"),
        (".claude/reports/plan-report-20260805-000000.md", "history"),
        (".claude/memory/patterns.json", "history"),
        (".claude/memory/sessions/session.md", "history"),
        (".claude/agent-memory/tester/MEMORY.md", "history"),
        (".claude/tmp/po-manifest.md", "history"),
        # --- derived ------------------------------------------------------
        ("src/c3/_template/.claude/agents/tester.md", "derived"),
        ("src/c3/_template/.claude/hooks/stop.py", "derived"),
        # derived が history に勝つ（複製ツリーの中の reports/ は「過去の記録」ではない）
        ("src/c3/_template/.claude/reports/.gitkeep", "derived"),
        ("src/c3/_template/.claude/memory/archive/.gitkeep", "derived"),
        # --- dev ----------------------------------------------------------
        (".dev/hooks/_template_guard.py", "dev"),
        (".dev/changelog-evals.md", "dev"),
        # dev が tests に勝つ（`.dev/tests/` は配布元専用の作業ツリー）
        (".dev/tests/test_run_loop_pure.py", "dev"),
        # --- tests --------------------------------------------------------
        ("tests/test_refgraph.py", "tests"),
        ("tests/conftest.py", "tests"),
        ("tests/hooks/test_record_agent_outcome.py", "tests"),
    )

    def test_categorize_classifies_every_contract_path_shape(self):
        """契約 §5-1 表の 5 カテゴリを合成入力で網羅する（条件 1）.

        **`parametrize` にしない。** 1 行 1 テストにすると「常に `live` を返す実装」で
        `live` 行だけが個別に緑になり、契約 §5-1 条件 5 の「スタブで緑になるテストが
        無いこと」を件数で言えなくなる（実測: 13 件が空回りで緑になった）。
        表全体を 1 つの比較にすれば、どの行が外れても 1 件の赤になる。
        """
        expected = dict(self.CASES)
        assert len(expected) == len(self.CASES), "表に重複したパスがある"

        got = {path: query().categorize(path) for path in expected}

        assert got == expected

    def test_categorize_is_pure_and_needs_no_filesystem(self):
        """存在確認・列挙 API を落とした状態でも正しく分類できること（条件 1）.

        負の対照（FS を触ったら `_FilesystemTouched`）だけでは「常に `live` を返す実装」が
        緑になるので、**同じテストの中で** 5 カテゴリすべての正解を要求する
        （契約 §7 規律 4）。パスは実在しないものを使う（存在判定に頼れない）。
        """
        expected = {
            ".claude/docs/does-not-exist.md": "live",
            ".claude/reports/does-not-exist.md": "history",
            "src/c3/_template/.claude/does-not-exist.md": "derived",
            ".dev/does-not-exist.md": "dev",
            "tests/does_not_exist.py": "tests",
        }

        got = _call_with_filesystem_blocked(query().categorize, tuple(expected))

        assert got == expected

    def test_categorize_is_deterministic_for_the_same_string(self):
        """同じ文字列には同じ答えを返すこと（状態を持たない・呼ぶ順に依らない）.

        正の対照として「答えが 5 種類そろっていること」を同じテストで要求する。
        これが無いと定数を返す実装が「決定的だ」というだけで緑になる（§7 規律 4）。
        """
        module = query()
        paths = [path for path, _ in self.CASES]

        first = [module.categorize(path) for path in paths]
        second = [module.categorize(path) for path in reversed(paths)]

        assert first == list(reversed(second))
        assert sorted(set(first)) == sorted(CATEGORY_NAMES), (
            f"positive control: 表は 5 カテゴリを網羅するはずが {sorted(set(first))}"
        )

    def test_every_real_source_falls_into_exactly_one_category(self, repo_links):
        """実リポジトリの全辺が 5 カテゴリに過不足なく分割されること（条件 1）.

        「常に `live` を返す実装」でも分割自体は成立してしまうので、
        **どのチェックアウトにも tracked で存在する** 3 カテゴリが実際に現れることを
        同じテストで要求する（契約 §7 規律 4）。
        """
        module = query()
        assert len(repo_links) > 0, "positive control: 辺が 1 本も無い"

        counts = Counter(module.categorize(link.source) for link in repo_links)

        unknown = sorted(set(counts) - set(CATEGORY_NAMES))
        assert unknown == [], f"category outside contract §5-1: {unknown}"
        assert sum(counts.values()) == len(repo_links)

        tracked_everywhere = {"live", "history", "tests"}
        absent = sorted(tracked_everywhere - {name for name, n in counts.items() if n > 0})
        assert absent == [], (
            f"these categories must exist in any checkout but were empty: {absent}"
        )

    def test_categorize_agrees_with_the_independent_oracle_on_every_real_source(
        self, repo_links
    ):
        """実リポジトリの全参照元で、契約表から独立に組んだ分類器と一致すること.

        合成表が想定しなかった実パス形状（深い階層・拡張子・記号）を通す。
        **リポジトリが変わっても赤にならず、分類が壊れたときだけ赤になる**ので、
        実測値の再現（条件 2）が環境で skip される場所でも分類の正しさは守られる。
        """
        module = query()
        assert len(repo_links) > 0, "positive control: 辺が 1 本も無い"

        disagreements = sorted(
            {
                (link.source, module.categorize(link.source), _oracle_category(link.source))
                for link in repo_links
                if module.categorize(link.source) != _oracle_category(link.source)
            }
        )
        assert disagreements[:10] == [], (
            f"categorize disagrees with the contract §5-1 table: {disagreements[:10]}"
        )


# ===========================================================================
# 契約 §5-1 条件 2: 実測値（2026-08-05）の再現
#
# 縛り方: **完全一致ではなく ±5% の許容幅**。かつ実測が行われた作業ツリー
# （gitignore された 4 つの木）が揃っているときだけ比較する。根拠はレポート参照。
# ===========================================================================
class TestMeasuredCountsAreReproduced:
    @pytest.fixture(autouse=True)
    def _require_measurement_environment(self, repo_root):
        gaps = _measurement_environment_gaps(repo_root)
        if gaps:
            pytest.skip(
                f"{MEASURED_AT} の実測は gitignore された作業ツリーを含む環境で行われた。"
                f"このチェックアウトには {gaps} が無いので件数比較は成立しない"
                "（分類の正しさは TestCategorizeIsPure が環境非依存で守る）"
            )

    def test_category_counts_match_the_measurement(self, repo_links):
        """`live` 2,992 / `history` 31,352 / `derived` 864 / `dev` 1,048 / `tests` 242.

        ずれたときに「分類が壊れた」のか「リポジトリが変わった」のかを切り分けられる
        よう、総辺数もあわせて測り、判定文を assert メッセージへ入れる。
        """
        module = query()
        counts = Counter(module.categorize(link.source) for link in repo_links)
        total = len(repo_links)

        off = sorted(
            (name, counts.get(name, 0), expected)
            for name, expected in MEASURED_COUNTS.items()
            if not _within_tolerance(counts.get(name, 0), expected)
        )

        if _within_tolerance(total, MEASURED_TOTAL_LINKS):
            verdict = (
                f"総辺数は実測どおり（{total} vs {MEASURED_TOTAL_LINKS}）なのに分布がずれている"
                "＝**分類が壊れた**可能性が高い"
            )
        else:
            verdict = (
                f"総辺数が動いている（{total} vs {MEASURED_TOTAL_LINKS}）"
                "＝**リポジトリが変わった**可能性が高い。抽出器側の退行も疑うこと"
            )

        assert off == [], (
            f"category counts drifted beyond ±{MEASURED_TOLERANCE:.0%} "
            f"(measured {MEASURED_AT}): {off}. {verdict}"
        )

    def test_live_source_file_count_matches_the_measurement(self, repo_links):
        """`live` の参照元ファイル数が 128 であること（契約 §5-1 表）."""
        module = query()
        live = query().filter_links(repo_links, ("live",))
        assert len(live) > 0, "positive control: live が空"

        files = {link.source for link in live}
        assert [name for name in files if module.categorize(name) != "live"] == []
        assert _within_tolerance(len(files), MEASURED_LIVE_SOURCE_FILES), (
            f"live source files: {len(files)} vs {MEASURED_LIVE_SOURCE_FILES}"
        )

    def test_live_missing_count_matches_the_measurement(self, repo_links):
        """`live` に絞ると `missing` が 300 になること（契約 §5-1 の本文）.

        契約は「3,226 → 300」と書くが、**縛るのは 300 だけ**にする。
        全体の `missing` は `.dev/` へメモを 1 本書くだけで動く（契約 §5-1 の注意に
        ある 149 本がまさにそれ）。動かないのは絞った後の数字のほうであり、
        それこそがこの絞り込みの価値である。
        """
        module = query()
        live = module.filter_links(repo_links, ("live",))
        assert len(live) > 0, "positive control: live が空"

        live_missing = [link for link in live if link.resolution == "missing"]
        all_missing = [link for link in repo_links if link.resolution == "missing"]

        assert len(all_missing) > len(live_missing), (
            "positive control: 絞る前後で missing が同数なら絞れていない"
        )
        assert _within_tolerance(len(live_missing), MEASURED_LIVE_MISSING), (
            f"live missing: {len(live_missing)} vs {MEASURED_LIVE_MISSING} "
            f"(all missing = {len(all_missing)})"
        )

    def test_live_ambiguous_share_of_template_targets_matches_the_measurement(
        self, repo_links
    ):
        """`live` の `ambiguous` 2,005 件のうち 1,007 件の target が `_template/`.

        契約 §5-1 軸 2 の根拠そのもの。畳み込みで曖昧さが半減する原資がここにある。
        """
        module = query()
        live = module.filter_links(repo_links, ("live",))
        ambiguous = [link for link in live if link.resolution == "ambiguous"]
        template_targets = [
            link for link in ambiguous if link.target.startswith(TEMPLATE_PREFIX)
        ]

        assert _within_tolerance(len(ambiguous), MEASURED_LIVE_AMBIGUOUS), (
            f"live ambiguous: {len(ambiguous)} vs {MEASURED_LIVE_AMBIGUOUS}"
        )
        assert _within_tolerance(
            len(template_targets), MEASURED_LIVE_AMBIGUOUS_WITH_TEMPLATE_TARGET
        ), (
            f"live ambiguous with _template target: {len(template_targets)} vs "
            f"{MEASURED_LIVE_AMBIGUOUS_WITH_TEMPLATE_TARGET}"
        )


# ===========================================================================
# 軸 1 の道具: `filter_links`
# ===========================================================================
class TestFilterLinksNarrowsBySourceCategory:
    def test_live_keeps_the_live_reference_and_drops_its_history_twin(
        self, synthetic_graph
    ):
        """同じ本文を live と history の 2 か所に置き、live だけが残ること.

        「在ること」と「無いこと」を**同じ fixture** に置く（契約 §7 規律 4）。
        何も返さない実装は positive 側で、何も絞らない実装は negative 側で赤になる。
        """
        module = query()
        links = synthetic_graph.links

        live = module.filter_links(links, ("live",))

        kept = sorted({link.source for link in live})
        assert kept == [".claude/rules/memo-live.md"], f"live sources: {kept}"

        dropped = [link for link in live if link.source.startswith(".claude/reports/")]
        assert dropped == [], f"history sources must be excluded from live: {dropped}"

        # positive: live 側の中身が失われていないこと
        assert sorted({link.target for link in live}) == [
            ".claude/docs/dup/note.md",
            ".claude/hooks/alive.py",
            "src/c3/_template/.claude/docs/dup/note.md",
        ]

    def test_history_and_derived_are_selectable_too(self, synthetic_graph):
        """`history` / `derived` も同じ道具で取り出せること（軸 1 の網羅）."""
        module = query()
        links = synthetic_graph.links

        history = module.filter_links(links, ("history",))
        derived = module.filter_links(links, ("derived",))

        assert sorted({link.source for link in history}) == [
            ".claude/reports/memo-history.md"
        ]
        assert sorted({link.source for link in derived}) == [
            "src/c3/_template/.claude/docs/dup/note.md"
        ]

    def test_selecting_all_categories_is_the_identity_and_none_is_empty(
        self, synthetic_graph
    ):
        """全カテゴリ指定は恒等、空指定は空（分割の両端）.

        さらに 5 カテゴリの部分和が全体に一致すること＝重複も取りこぼしも無いこと。
        """
        module = query()
        links = synthetic_graph.links
        assert len(links) > 0, "positive control: 合成ツリーの辺が 0"

        assert list(module.filter_links(links, CATEGORY_NAMES)) == list(links)
        assert list(module.filter_links(links, ())) == []

        partition = sum(
            len(module.filter_links(links, (name,))) for name in CATEGORY_NAMES
        )
        assert partition == len(links)

    def test_filter_preserves_input_order(self):
        """絞っても入力の順序を保つこと（出所を追う読み方が壊れる）."""
        module = query()
        links = [
            _link(source=".claude/hooks/a.py", target="x.md", source_line=1),
            _link(source="CHANGELOG.md", target="x.md", source_line=2),
            _link(source=".claude/hooks/b.py", target="x.md", source_line=3),
            _link(source=".dev/notes.md", target="x.md", source_line=4),
            _link(source=".claude/hooks/c.py", target="x.md", source_line=5),
        ]

        got = module.filter_links(links, ("live",))

        assert [link.source_line for link in got] == [1, 3, 5]

    def test_unknown_category_name_fails_loudly(self):
        """存在しないカテゴリ名は `ValueError`（黙って空を返さない）.

        綴り間違いが「そのカテゴリは 0 件」に化けると、読む側は絞り込みの結果を
        信じてしまう。正の対照として正しい名前は通ることを同じテストで確かめる。
        """
        module = query()
        links = [_link(source=".claude/hooks/a.py", target="x.md")]

        assert len(module.filter_links(links, ("live",))) == 1

        with pytest.raises(ValueError):
            module.filter_links(links, ("lives",))

    def test_filtering_the_real_repo_shrinks_the_missing_set(self, repo_links):
        """実リポジトリで `live` に絞ると `missing` が減ること（環境非依存）.

        件数そのものは環境で動くので、ここでは**減ること**と
        **live に既知の辺が残っていること**だけを見る。何も返さない実装は
        positive 側で、何も絞らない実装は「減ること」で赤になる。
        """
        module = query()
        live = module.filter_links(repo_links, ("live",))

        assert len(live) > 0, "positive control: live が空"
        assert len(live) < len(repo_links), "何も絞れていない"

        live_missing = sum(1 for link in live if link.resolution == "missing")
        all_missing = sum(1 for link in repo_links if link.resolution == "missing")
        assert live_missing < all_missing

        # positive control: 生きている登録（settings.json → hook）が live に残ること
        kept = [
            link
            for link in live
            if link.relation == "settings_hook"
            and link.target == ".claude/hooks/permission_handler.py"
        ]
        assert len(kept) >= 1, "a live hook registration disappeared from the live view"

        # negative control: CHANGELOG.md 由来の辺は live に無い
        leaked = [link for link in live if link.source == "CHANGELOG.md"]
        assert leaked == [], f"CHANGELOG.md is history, not live: {leaked[:3]}"


# ===========================================================================
# 軸 2 の道具: `fold_target`（純粋関数）
# ===========================================================================
class TestFoldTargetIsPure:
    CASES = (
        # 畳む
        ("src/c3/_template/.claude/hooks/stop.py", ".claude/hooks/stop.py"),
        ("src/c3/_template/.claude/agents/tester.md", ".claude/agents/tester.md"),
        ("src/c3/_template/.claude/CLAUDE.md", ".claude/CLAUDE.md"),
        # 畳まない（原本・別ツリー・成分境界・テーブルノード）
        (".claude/hooks/stop.py", ".claude/hooks/stop.py"),
        ("src/c3/cli.py", "src/c3/cli.py"),
        ("src/c3/_templates/x.md", "src/c3/_templates/x.md"),
        ("src/c3/_template.py", "src/c3/_template.py"),
        ("docs/src/c3/_template/x.md", "docs/src/c3/_template/x.md"),
        ("sqltable:agent_outcomes", "sqltable:agent_outcomes"),
        ("CHANGELOG.md", "CHANGELOG.md"),
    )

    def test_fold_target_maps_derived_paths_to_their_original(self):
        """`src/c3/_template/<X>` を `<X>` へ畳み、それ以外は素通しすること.

        `categorize` の表と同じ理由で `parametrize` にしない（素通しスタブでは
        「畳まない」行だけが個別に緑になり、空回りが件数に紛れる）。
        """
        expected = dict(self.CASES)
        assert len(expected) == len(self.CASES), "表に重複した target がある"

        got = {target: query().fold_target(target) for target in expected}

        assert got == expected

    def test_fold_target_is_pure_and_needs_no_filesystem(self):
        """存在確認 API を落としても畳めること（原本が無くても畳む）.

        正の対照（畳む）と負の対照（畳まない）を同じテストに置くので、
        素通しスタブも「常に畳む」実装も赤になる。
        """
        expected = {
            "src/c3/_template/.claude/no-such-file.md": ".claude/no-such-file.md",
            ".claude/no-such-file.md": ".claude/no-such-file.md",
            "sqltable:no_such_table": "sqltable:no_such_table",
        }

        got = _call_with_filesystem_blocked(query().fold_target, tuple(expected))

        assert got == expected


# ===========================================================================
# 契約 §5-1 条件 3: 畳んだ後に `ambiguous` が減ること（正の対照つき）
# ===========================================================================
class TestFoldLinksCollapsesDerivedTwins:
    @staticmethod
    def _twin_pair_and_controls():
        """畳んで潰れるペア／潰れない対照／畳まれない辺を 1 つの入力にまとめる."""
        return [
            # (1)(2) 同じ 1 行から出た 2 候補。畳むと同一になるので 1 本へ潰れる
            _link(
                source=".claude/docs/a.md",
                target=".claude/docs/dup/note.md",
                source_line=3,
                context="- `dup/note.md`",
                resolution="ambiguous",
            ),
            _link(
                source=".claude/docs/a.md",
                target="src/c3/_template/.claude/docs/dup/note.md",
                source_line=3,
                context="- `dup/note.md`",
                resolution="ambiguous",
            ),
            # (3)(4) 両方とも派生でない ambiguous ペア。畳んでも潰れてはいけない
            _link(
                source=".claude/docs/b.md",
                target=".claude/docs/x/note.md",
                source_line=5,
                context="- `note.md`",
                resolution="ambiguous",
            ),
            _link(
                source=".claude/docs/b.md",
                target=".claude/docs/y/note.md",
                source_line=5,
                context="- `note.md`",
                resolution="ambiguous",
            ),
            # (5) 派生を指す exact。相方がいないので潰れず、target だけ畳まれる
            _link(
                source=".claude/docs/c.md",
                target="src/c3/_template/.claude/hooks/only-here.py",
                source_line=7,
                context="- `src/c3/_template/.claude/hooks/only-here.py`",
                resolution="exact",
            ),
            # (6) 派生と無関係な exact。素通し
            _link(
                source=".claude/docs/c.md",
                target=".claude/hooks/alive.py",
                source_line=8,
                context="- `.claude/hooks/alive.py`",
                resolution="exact",
            ),
        ]

    def test_derived_twin_collapses_while_the_others_survive(self):
        """畳むと派生の双子だけが 1 本に潰れること（条件 3・正の対照つき）.

        - positive: 潰れるのは (1)(2) の 1 組だけ → 6 本が 5 本になる
        - negative: 派生でない ambiguous ペア (3)(4) は 2 本のまま
        - positive: `exact` の 2 本は失われない（うち 1 本は target が畳まれる）

        「何も返さない実装」は本数で、「何もしない実装」は target と ambiguous 数で
        赤になる。
        """
        module = query()
        links = self._twin_pair_and_controls()

        folded = module.fold_links(links)

        assert len(links) == 6
        assert len(folded) == 5, f"exactly one twin pair must collapse; got {len(folded)}"

        before = sum(1 for link in links if link.resolution == "ambiguous")
        after = sum(1 for link in folded if link.resolution == "ambiguous")
        assert (before, after) == (4, 3), (
            f"ambiguous must drop by exactly the collapsed twin; got {before} -> {after}"
        )

        targets = [link.target for link in folded]
        assert targets.count(".claude/docs/dup/note.md") == 1
        assert [t for t in targets if t.startswith(TEMPLATE_PREFIX)] == []
        assert ".claude/hooks/only-here.py" in targets
        assert ".claude/hooks/alive.py" in targets

        # negative: 派生でない ambiguous ペアは畳まれない
        assert targets.count(".claude/docs/x/note.md") == 1
        assert targets.count(".claude/docs/y/note.md") == 1

    def test_fold_never_touches_the_source(self):
        """`source` は畳まないこと（出所が失われる・契約 §2 原則 2）.

        派生ツリーの中の文書が原本を参照している辺で確かめる。正の対照として
        同じ入力の `target` は畳まれることを見る。
        """
        module = query()
        links = [
            _link(
                source="src/c3/_template/.claude/docs/note.md",
                target="src/c3/_template/.claude/hooks/stop.py",
                source_line=2,
            )
        ]

        folded = module.fold_links(links)

        assert len(folded) == 1
        assert folded[0].source == "src/c3/_template/.claude/docs/note.md"
        assert folded[0].target == ".claude/hooks/stop.py"

    def test_fold_does_not_mutate_the_input(self):
        """入力の `Link` を書き換えないこと（呼び出し側が元データを失う）.

        正の対照として、返り値のほうは実際に畳まれていることを同じテストで見る。
        これが無いと「何もしない実装」が「壊していない」というだけで緑になる。
        """
        module = query()
        links = self._twin_pair_and_controls()
        before = [_key(link) for link in links]

        folded = module.fold_links(links)

        assert [_key(link) for link in links] == before
        assert len(folded) == len(links) - 1, "positive control: 双子が畳まれていない"
        assert [
            link for link in folded if link.target.startswith(TEMPLATE_PREFIX)
        ] == []

    def test_end_to_end_ambiguous_pair_from_build_graph_is_folded(self, synthetic_graph):
        """`build_graph` の実出力に対して畳めること（合成ツリー・実ファイルつき）.

        手で組んだ `Link` だけで測ると、抽出器が実際に出す形（context / source_line が
        同一の 2 候補）と食い違っても気づけない。契約 §7 規律 2 に従い参照先の
        ファイルを実際に作ったツリーで確かめる。
        """
        module = query()
        live = module.filter_links(synthetic_graph.links, ("live",))
        assert len(live) == 3, f"fixture drifted: {[_key(link) for link in live]}"

        folded = module.fold_links(live)

        assert len(folded) == 2
        assert sorted(link.target for link in folded) == [
            ".claude/docs/dup/note.md",
            ".claude/hooks/alive.py",
        ]

        ambiguous = [link for link in folded if link.resolution == "ambiguous"]
        assert len(ambiguous) == 1
        assert ambiguous[0].target == ".claude/docs/dup/note.md"
        assert ambiguous[0].source == ".claude/rules/memo-live.md"

    def test_fold_matches_the_oracle_on_the_real_repo(self, repo_links):
        """実リポジトリ全体で、契約から独立に組んだ参照実装と完全一致すること.

        「target を畳んで、7 フィールドが同一になった辺を出現順で重複除去する」
        以上でも以下でもないことを固定する（`resolution` の書き換え・並べ替え・
        取りこぼしを禁じる）。環境非依存。
        """
        module = query()
        assert len(repo_links) > 0, "positive control: 辺が 1 本も無い"

        got = module.fold_links(repo_links)
        expected = _oracle_fold(repo_links)

        assert len(got) == len(expected)
        assert [_key(link) for link in got] == [_key(link) for link in expected]

    def test_fold_removes_every_template_target_on_the_real_repo(self, repo_links):
        """実リポジトリで畳んだ後、`_template/` を指す target が 1 つも残らないこと.

        正の対照として、畳む前には存在していたことを同じテストで確かめる
        （契約 §7 規律 4）。
        """
        module = query()

        before = [link for link in repo_links if link.target.startswith(TEMPLATE_PREFIX)]
        assert len(before) > 0, "positive control: 畳む対象が 1 本も無い"

        folded = module.fold_links(repo_links)
        after = [link for link in folded if link.target.startswith(TEMPLATE_PREFIX)]
        assert after[:5] == [], f"derived targets survived the fold: {after[:5]}"

        # positive control: 「全部捨てる」実装も target を 0 本にできてしまうので、
        # 落とせるのは畳んで重複になった辺までであることを同じテストで縛る。
        assert len(folded) > 0, "positive control: 畳んだ結果が空"
        assert len(repo_links) - len(folded) <= len(before), (
            f"fold dropped more than the derived twins: "
            f"{len(repo_links)} -> {len(folded)} (derived targets = {len(before)})"
        )

    def test_fold_reduces_ambiguous_without_losing_the_rest(self, repo_links):
        """実リポジトリで `ambiguous` が減り、かつ全部消えてはいないこと（条件 3）.

        件数そのものは環境で動くので、ここでは向きだけを見る。
        「何も返さない実装」は残存本数で、「何もしない実装」は減少で赤になる。
        """
        module = query()
        live = module.filter_links(repo_links, ("live",))
        folded = module.fold_links(live)

        before = sum(1 for link in live if link.resolution == "ambiguous")
        after = sum(1 for link in folded if link.resolution == "ambiguous")

        assert before > 0, "positive control: 畳む前に ambiguous が無い"
        assert after > 0, "ambiguous が全滅するのは畳みすぎ（別の参照まで潰している）"
        assert after < before, f"folding must reduce ambiguous edges; got {before} -> {after}"
        assert len(folded) < len(live)


# ===========================================================================
# tester 自作の反例
# ===========================================================================
class TestCounterexamples:
    def test_counterexample_1_prefixes_must_stop_at_a_path_component(self):
        """反例 1: 前方一致を成分境界で切らない実装を殺す.

        2 通りの誤りを同じ表で殺す。

        (a) 区切り無しの前方一致 — `startswith(".dev")` `startswith("tests")`
            `startswith(".claude/tmp")` `startswith("src/c3/_template")` は、
            いずれも実在しうる別名（`.devcontainer/` 等）を巻き込む
        (b) 前方一致でなく**部分一致** — `"tests/" in path` `".claude/reports/" in path`
            は、成分の途中に同じ並びが現れるだけのパスを巻き込む。
            この行が無いと `startswith` を `in` に書き換える変異が素通りする
            （実測: 変異 M5 / M6 が 39 件全緑で通過した）

        分類は**ルート相対パスの先頭**を見る。正の対照（本物）を同じテーブルに置く。
        """
        module = query()
        cases = {
            # (a) 区切りが無いだけの別名。巻き込まれてはいけない
            ".devcontainer/devcontainer.json": "live",
            ".claude/tmp-scratch.md": "live",
            ".claude/reports-archive/old.md": "live",
            ".claude/memory-notes.md": "live",
            ".claude/agent-memory-policy.md": "live",
            "tests-fixtures/sample.py": "live",
            "src/c3/_templates/x.md": "live",
            "src/c3/_template.py": "live",
            # (b) 同じ並びが**成分の途中**に現れるだけ。先頭ではないので live
            "src/c3/tests/helper.py": "live",
            ".claude/docs/tests/fixture.md": "live",
            "docs/.dev/notes.md": "live",
            "docs/.claude/reports/example.md": "live",
            "docs/.claude/tmp/example.md": "live",
            "docs/src/c3/_template/example.md": "live",
            # 正の対照（本物）
            ".dev/notes.md": "dev",
            ".claude/tmp/manifest.md": "history",
            ".claude/reports/r.md": "history",
            ".claude/memory/patterns.json": "history",
            ".claude/agent-memory/tester/MEMORY.md": "history",
            "tests/sample.py": "tests",
            "src/c3/_template/.claude/x.md": "derived",
        }

        got = {path: module.categorize(path) for path in cases}

        assert got == cases

    def test_counterexample_2_changelog_is_matched_exactly(self):
        """反例 2: `CHANGELOG.md` を「含む」で判定する実装を殺す.

        表の `history` は**ルートの** `CHANGELOG.md` を指す。派生ツリーや docs 配下に
        同名ファイルが現れても history にはならない（`src/c3/_template/` 配下なら
        `derived` が勝つ）。
        """
        module = query()
        cases = {
            "CHANGELOG.md": "history",
            "docs/CHANGELOG.md": "live",
            "CHANGELOG.md.bak": "live",
            "OLD_CHANGELOG.md": "live",
            ".dev/CHANGELOG.md": "dev",
            "src/c3/_template/CHANGELOG.md": "derived",
        }

        got = {path: module.categorize(path) for path in cases}

        assert got == cases

    def test_counterexample_3_filter_looks_at_the_source_not_the_target(self):
        """反例 3: `target` のカテゴリで絞る実装を殺す（軸 1 は「出所」）.

        `live` な文書が `_template/` のファイルを参照している辺は **live**。
        これを落とすと「原本を直せば消える参照」が視界から消え、軸 2 の畳み込みで
        減らすべき曖昧さそのものが見えなくなる。
        逆向き（`derived` な文書が `live` を参照）も同じ fixture に置く。
        """
        module = query()
        links = [
            _link(
                source=".claude/docs/live.md",
                target="src/c3/_template/.claude/hooks/stop.py",
                source_line=1,
            ),
            _link(
                source="src/c3/_template/.claude/docs/copy.md",
                target=".claude/hooks/stop.py",
                source_line=2,
            ),
        ]

        live = module.filter_links(links, ("live",))
        derived = module.filter_links(links, ("derived",))

        assert [link.source_line for link in live] == [1]
        assert [link.source_line for link in derived] == [2]

    def test_counterexample_4_fold_keeps_genuinely_distinct_edges(self):
        """反例 4: 畳んだ後に target だけで重複除去する実装を殺す.

        同じ target を指していても、**別の行・別のファイル・別の relation** から出た
        参照は別の関係である。潰すと出所（契約 §2 原則 2）が消える。
        正の対照として、本当に同一の 2 本は潰れることを同じテストで確かめる。
        """
        module = query()
        links = [
            _link(source=".claude/docs/a.md", target="src/c3/_template/.claude/x.md",
                  source_line=1, context="line 1"),
            _link(source=".claude/docs/a.md", target=".claude/x.md",
                  source_line=2, context="line 2"),
            _link(source=".claude/docs/b.md", target=".claude/x.md",
                  source_line=1, context="line 1"),
            _link(source=".claude/docs/a.md", target=".claude/x.md",
                  source_line=1, context="line 1", relation="md_link"),
            # 本当に同一（畳むと 1 本目と 7 フィールド一致）→ 潰れる
            _link(source=".claude/docs/a.md", target=".claude/x.md",
                  source_line=1, context="line 1"),
        ]

        folded = module.fold_links(links)

        assert len(folded) == 4, (
            f"only the truly identical pair may collapse; got {[_key(l) for l in folded]}"
        )
        assert [_key(link) for link in folded] == [
            _key(link) for link in _oracle_fold(links)
        ]

    def test_counterexample_5_query_layer_reads_a_written_graph_file(self, tmp_path):
        """反例 5: `read_graph` で読み戻したグラフでも同じ結果になること.

        契約 §5-1 条件 4 は「クエリ層は `read_graph` / `build_graph` の**結果**を
        読むだけ」と書く。つまりクエリ層はファイル経由の `Link`（JSON 往復後）でも
        同じ答えを返さなければならない。`build_graph` の返り値だけを想定した実装
        （抽出器の内部状態に触る実装）はここで赤になる。
        """
        module = query()
        _build_synthetic_tree(tmp_path)
        graph = refgraph.build_graph(tmp_path)

        out = tmp_path / "out" / "graph.json"
        refgraph.write_graph(graph, out)
        restored = refgraph.read_graph(out)

        direct = module.fold_links(module.filter_links(graph.links, ("live",)))
        via_file = module.fold_links(module.filter_links(restored.links, ("live",)))

        assert len(direct) == 2, "positive control: 畳んだ結果が 2 本にならない"
        assert [_key(link) for link in via_file] == [_key(link) for link in direct]

    def test_counterexample_6_filter_and_fold_accept_any_link_sequence(self):
        """反例 6: 入力を `Graph` 前提にせず、辺の並びを受け取ること.

        `list` / `tuple` / generator のどれで渡しても同じ結果になること。
        `graph.links`（tuple）だけを想定した実装は、絞った結果を再度渡す
        （= 実際の使い方）と壊れる。
        """
        module = query()
        base = [
            _link(source=".claude/docs/a.md", target="src/c3/_template/.claude/x.md"),
            _link(source="CHANGELOG.md", target=".claude/x.md"),
        ]

        from_list = module.fold_links(module.filter_links(base, ("live",)))
        from_tuple = module.fold_links(module.filter_links(tuple(base), ("live",)))
        from_iter = module.fold_links(module.filter_links(iter(base), ("live",)))

        assert [_key(link) for link in from_list] == [
            ("md_code_span_path", ".claude/docs/a.md", 1, "ctx", ".claude/x.md", True, "exact")
        ]
        assert [_key(link) for link in from_tuple] == [_key(link) for link in from_list]
        assert [_key(link) for link in from_iter] == [_key(link) for link in from_list]
