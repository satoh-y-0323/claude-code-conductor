"""参照抽出器（refgraph）の番人テスト — 契約 §6（＋§5-1 完成条件 5）の常設テスト化。

`docs/refgraph-contract.md` に基づく。`plan-report-20260808-010336.md` タスク
`test-structural` の Red フェーズ。**実装（`src/c3/refgraph.py` /
`scripts/refgraph_query.py`）は編集しない。**

`tests/test_refgraph.py`（54 件）・`tests/test_refgraph_query.py`（47 件）は
それぞれの完成条件を個別に検証済み。本ファイルは契約 §6 の完成条件そのものを
条文の言葉のまま常設テスト化し、以下を追加で担う:

1. 形式カバレッジの両側非空 assert（§6 条件 1）
2. 既知の関係 8 行 + 題材の実在 assert（§6 条件 2）
3. A-3 判定材料（source 名固定・live 判定）
4. 出力の往復（8 フィールド込み・§6 条件 4）
5. 取りこぼしの可視化（§6 条件 3）
6. 判定非混入の静的検査（11 リテラル・§6 条件 5・C-21）
7. do-nothing 抽出器スタブ検査（§6 条件 7・C-23）
8. do-nothing クエリ層スタブ検査（§5-1 完成条件 5・C-24 修正版）
9. `_SKIP_DIR_NAMES` の出力ベース検査
10. settings 系の既存解決の回帰（source 名固定）

テスト作法（契約 §7 / plan-report 共通）:

1. 不在は `assert xs == []` で直接 assert する
2. 合成入力では参照先のファイルを実際に作る
3. ノード ID / `skipped` のパスはルート相対 POSIX
4. 「無いこと」の検査は「在ること」の対照と同じ fixture に置く
5. assert のないテストを書かない
6. 機構を足したら同じ周回でその機構を検査するテストを足す

実リポジトリ題材について: 各テストは題材（該当ファイル・該当行）の実在を
**先に** assert する。題材が将来のリファクタで消えた場合、そのテストは
「題材消失」の前提 assert で赤になる（抽出器の欠陥ではない）。その場合は
docstring に書いた代替題材（同じ形の別 source）へ差し替えてよい。
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

import c3.refgraph as refgraph  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
QUERY_MODULE_PATH = SCRIPTS_DIR / "refgraph_query.py"
TEST_REFGRAPH_PATH = REPO_ROOT / "tests" / "test_refgraph.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def query():
    """`scripts/refgraph_query.py` をモジュールとして返す（実行時に遅延ロード）."""
    import refgraph_query

    return refgraph_query


# ---------------------------------------------------------------------------
# 契約 §4 の relation 一覧（**実装から import しない**。実装が減らしたら赤になる）。
# ---------------------------------------------------------------------------
CONTRACT_RELATIONS = (
    "settings_hook",
    "settings_statusline",
    "settings_permission",
    "md_code_span_path",
    "md_link",
    "md_c3_run",
    "md_agent_variant_map",
    "md_subagent_type",
    "md_bare_agent_name",
    "md_bare_skill_name",
    "py_import",
    "py_importlib",
    "py_subprocess_path",
    "py_sql_table",
    "md_prose_path",
    "md_fence_path",
    "py_string",
    "py_comment",
    "text_path",
)

# 契約 §5-1 表の出所カテゴリ接頭辞リテラル 11 件（**実装から import しない**）。
CATEGORY_PREFIX_LITERALS = (
    ".claude/reports/",
    ".claude/memory/",
    ".claude/agent-memory/",
    ".claude/tmp/",
    "src/c3/_template/",
    ".dev/",
    "tests/",
    "CHANGELOG.md",
    ".claude/state/",
    ".claude/logs/",
    "site/",
)


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------
def _mkfile(root: Path, rel: str, text: str) -> Path:
    """合成ツリーへ UTF-8 でファイルを作る（参照先も必ず実体を作る・§7 規律 2）."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _links(graph, *, relation=None, source=None, target=None):
    """条件に合う辺を list で返す（`== []` で不在を直接 assert するため）."""
    out = []
    for link in graph.links:
        if relation is not None and link.relation != relation:
            continue
        if source is not None and link.source != source:
            continue
        if target is not None and link.target != target:
            continue
        out.append(link)
    return out


def _link(
    *,
    source: str = ".claude/docs/source.md",
    target: str = ".claude/docs/target.md",
    relation: str = "md_code_span_path",
    source_line: int = 1,
    context: str = "ctx",
    target_exists: bool = True,
    resolution: str = "exact",
    reference: str = "",
):
    """テスト用の合成 `Link`（全フィールドにデフォルトを持つ・呼び出し漏れ事故対策）."""
    return refgraph.Link(
        relation=relation,
        source=source,
        source_line=source_line,
        context=context,
        target=target,
        target_exists=target_exists,
        resolution=resolution,
        reference=reference,
    )


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def repo_graph(repo_root):
    """実リポジトリ全体の抽出結果（session スコープ・1 回だけ構築する）.

    do-nothing スタブ検査（項目 7・8）はこの fixture を再利用しない
    （別プロセス／別モジュール参照で完結させる）。
    """
    return refgraph.build_graph(repo_root)


# ===========================================================================
# 1. 形式カバレッジ（契約 §6 条件 1）— 両側の非空を先に assert する
# ===========================================================================
class TestFormatCoverageBothSidesNonempty:
    """空集合同士の一致で緑になる穴を塞ぐ（契約表側・実装側それぞれの非空を先に見る）."""

    def test_contract_table_is_nonempty(self):
        """契約 §4 の表から読んだ relation 集合が空でないこと（テスト側の記載漏れガード）."""
        assert len(CONTRACT_RELATIONS) > 0
        assert len(set(CONTRACT_RELATIONS)) == len(CONTRACT_RELATIONS), (
            "CONTRACT_RELATIONS に重複がある"
        )

    def test_implementation_output_is_nonempty(self, repo_graph):
        """実装の出力から集めた relation 集合が空でないこと（辺が 1 本も無い実装を先に殺す）."""
        actual = {link.relation for link in repo_graph.links}
        assert len(actual) > 0, "positive control: 実リポジトリで辺が 1 本も出ていない"

    def test_contract_and_implementation_relation_sets_match_both_directions(self, repo_graph):
        """両側非空を確認した上で、契約表と実装値域が双方向一致すること."""
        expected = set(CONTRACT_RELATIONS)
        actual = {link.relation for link in repo_graph.links}

        # 両側非空（上の 2 テストと重複しても、本テスト単体でも意味が成立するように置く）
        assert len(expected) > 0 and len(actual) > 0

        missing_in_impl = sorted(expected - actual)
        assert missing_in_impl == [], f"契約にあるが実装が 0 本の relation: {missing_in_impl}"

        unknown_in_impl = sorted(actual - expected)
        assert unknown_in_impl == [], f"実装にあるが契約に無い relation: {unknown_in_impl}"


# ===========================================================================
# 2. 既知の関係（契約 §6 条件 2 の表・8 行）— 題材の実在を先に assert する
# ===========================================================================
class TestKnownRelationsWithPremiseChecks:
    """各行は「題材が実在するか」を先に見てから「辺が出るか」を見る.

    題材が消えた場合の代替: 各テストの docstring に、同じ形の代替 source 候補を書く。
    """

    def test_row1_settings_hooks_section_to_permission_handler(self, repo_root, repo_graph):
        """行 1: `settings.json` の hooks 登録 → `permission_handler.py`（`settings_hook`）.

        代替題材: hooks 節に登録されている他の hook（例: `session_stop.py`）でもよい。
        """
        settings_text = (repo_root / ".claude" / "settings.json").read_text(encoding="utf-8")
        assert '"${CLAUDE_PROJECT_DIR}/.claude/hooks/permission_handler.py"' in settings_text, (
            "premise gone: settings.json の hooks 節から permission_handler.py の登録が消えた"
        )

        hits = _links(
            repo_graph,
            relation="settings_hook",
            target=".claude/hooks/permission_handler.py",
        )
        assert len(hits) >= 1, "settings_hook edge to permission_handler.py is missing"

    def test_row2_session_stop_importlib_to_four_modules(self, repo_root, repo_graph):
        """行 2: `session_stop.py` の importlib → 4 モジュール（`py_importlib`）."""
        text = (repo_root / ".claude" / "hooks" / "session_stop.py").read_text(encoding="utf-8")
        expected_calls = (
            '_load_module("stop")',
            '_load_module("consolidate_memory")',
            '_load_module("session_utils")',
            '_load_module("tier_gap_check")',
        )
        missing_calls = [call for call in expected_calls if call not in text]
        assert missing_calls == [], f"premise gone: session_stop.py から消えた呼び出し {missing_calls}"

        expected = {
            ".claude/hooks/stop.py",
            ".claude/hooks/consolidate_memory.py",
            ".claude/hooks/session_utils.py",
            ".claude/hooks/tier_gap_check.py",
        }
        got = {
            link.target
            for link in _links(
                repo_graph, relation="py_importlib", source=".claude/hooks/session_stop.py"
            )
        }
        missing = sorted(expected - got)
        assert missing == [], f"py_importlib edges missing from session_stop.py: {missing}"

    def test_row3_permission_handler_subprocess_to_toast(self, repo_root, repo_graph):
        """行 3: `permission_handler.py` の subprocess → `permission_handler_toast.py`."""
        text = (repo_root / ".claude" / "hooks" / "permission_handler.py").read_text(
            encoding="utf-8"
        )
        assert "os.path.join(_HOOKS_DIR, 'permission_handler_toast.py')" in text, (
            "premise gone: permission_handler.py から toast script の組み立てが消えた"
        )

        hits = _links(
            repo_graph,
            relation="py_subprocess_path",
            source=".claude/hooks/permission_handler.py",
            target=".claude/hooks/permission_handler_toast.py",
        )
        assert len(hits) >= 1, "py_subprocess_path edge to permission_handler_toast.py is missing"

    def test_row4_parallel_agents_variant_table_to_wt_systematic_debugger(
        self, repo_root, repo_graph
    ):
        """行 4: `parallel-agents/SKILL.md` の写像表 → `wt_systematic-debugger.md`."""
        text = (repo_root / ".claude" / "skills" / "parallel-agents" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "| `systematic-debugger` | `wt_systematic-debugger` |" in text, (
            "premise gone: parallel-agents/SKILL.md の写像表行が消えた"
        )

        hits = _links(
            repo_graph,
            relation="md_agent_variant_map",
            source=".claude/skills/parallel-agents/SKILL.md",
            target=".claude/agents/wt_systematic-debugger.md",
        )
        assert len(hits) >= 1, "md_agent_variant_map edge to wt_systematic-debugger.md is missing"

    def test_row5_c3_run_reaches_all_seven_skill_scripts(self, repo_root, repo_graph):
        """行 5: SKILL.md の `c3 run` → skill scripts 7 本（`md_c3_run`）."""
        script_ids = sorted(
            path.relative_to(repo_root).as_posix()
            for path in repo_root.glob(".claude/skills/*/scripts/*.py")
        )
        assert len(script_ids) == 7, f"premise changed: skill scripts count is {len(script_ids)}"

        targets = {link.target for link in _links(repo_graph, relation="md_c3_run")}
        missing = [node_id for node_id in script_ids if node_id not in targets]
        assert missing == [], f"md_c3_run edges missing for skill scripts: {missing}"

    def test_row6_cli_multiline_import_resolves_submodules(self, repo_root, repo_graph):
        """行 6: `cli.py` の複数行括弧付き import → `cli_*.py`（`py_import`）."""
        text = (repo_root / "src" / "c3" / "cli.py").read_text(encoding="utf-8")
        assert "from c3 import (" in text, "premise gone: cli.py の複数行 import が消えた"
        for name in ("cli_ask", "cli_init", "cli_update"):
            assert f"    {name},\n" in text, f"premise gone: cli.py から {name} の import 行が消えた"

        expected = {
            f"src/c3/{name}.py"
            for name in (
                "cli_ask",
                "cli_doctor",
                "cli_init",
                "cli_list",
                "cli_metrics",
                "cli_plan",
                "cli_recall",
                "cli_run",
                "cli_tier",
                "cli_update",
            )
        }
        got = {
            link.target for link in _links(repo_graph, relation="py_import", source="src/c3/cli.py")
        }
        missing = sorted(expected - got)
        assert missing == [], f"py_import edges missing from cli.py (AST 解析の欠落?): {missing}"

    def test_row7_prose_mention_to_security_reviewer(self, repo_root, repo_graph):
        """行 7: SKILL.md の散文 → `security-reviewer.md`（`md_bare_agent_name`）.

        代替題材: `.claude/agents/security-reviewer.md` を素の名前で言及する
        他の SKILL.md でもよい（本テストは特定の 1 ファイルを固定しない）。
        """
        assert (repo_root / ".claude" / "agents" / "security-reviewer.md").is_file(), (
            "premise gone: security-reviewer.md 自体が無くなった"
        )
        mentioning_files = [
            path
            for path in repo_root.glob(".claude/skills/*/SKILL.md")
            if "security-reviewer" in path.read_text(encoding="utf-8")
        ]
        assert len(mentioning_files) >= 1, (
            "premise gone: security-reviewer を散文で言及する SKILL.md が無くなった"
        )

        hits = _links(
            repo_graph,
            relation="md_bare_agent_name",
            target=".claude/agents/security-reviewer.md",
        )
        assert len(hits) >= 1, "md_bare_agent_name edge to security-reviewer.md is missing"

    def test_row8_settings_permission_allow_to_stop_py_not_settings_hook(
        self, repo_root, repo_graph
    ):
        """行 8: `settings.json` の `permissions.allow` → `stop.py`（`settings_permission`・
        **`settings_hook` ではない**）."""
        text = (repo_root / ".claude" / "settings.json").read_text(encoding="utf-8")
        assert "Bash(c3 run .claude/hooks/stop.py*)" in text, (
            "premise gone: permissions.allow の stop.py エントリが消えた"
        )

        perm_hits = _links(
            repo_graph, relation="settings_permission", target=".claude/hooks/stop.py"
        )
        assert len(perm_hits) >= 1, "settings_permission edge to stop.py is missing"

        hook_hits = _links(repo_graph, relation="settings_hook", target=".claude/hooks/stop.py")
        assert hook_hits == [], (
            "stop.py は hooks 節に登録されていない。settings_hook 辺が出るのは誤り: "
            f"{[(l.source, l.source_line, l.context) for l in hook_hits]}"
        )


# ===========================================================================
# 3. A-3 判定材料（source 名固定）
# ===========================================================================
class TestA3JudgmentMaterialSourceNamesFixed:
    """`agents/tdd-develop.md` を target とする辺の source に `_excludes.py` と
    `hatch_build.py` がいずれも含まれ、どちらも `categorize` で `live` になること.

    **本数条件にしない**（`DC-AS-2702`: 走査ツリーに live 文書が増えるたびに緩むため）。
    """

    def test_premise_both_files_literally_mention_tdd_develop_md(self, repo_root):
        """題材の実在: 2 ファイルとも `agents/tdd-develop.md` という文字列を持つこと."""
        excludes_text = (repo_root / "src" / "c3" / "_excludes.py").read_text(encoding="utf-8")
        hatch_text = (repo_root / "hatch_build.py").read_text(encoding="utf-8")
        assert "agents/tdd-develop.md" in excludes_text, (
            "premise gone: src/c3/_excludes.py から agents/tdd-develop.md の記載が消えた"
        )
        assert "agents/tdd-develop.md" in hatch_text, (
            "premise gone: hatch_build.py から agents/tdd-develop.md の記載が消えた"
        )

    def test_both_sources_produce_an_edge_and_are_categorized_live(self, repo_graph):
        """AC-42（`docs/refgraph-acceptance.md:498`）: A-3 削除判定の裏取りに使う実測材料.

        target は `agents/tdd-develop.md`（`.claude/` プレフィックス無し）。
        `src/c3/_excludes.py` / `hatch_build.py` が持つリテラルは `"agents/tdd-develop.md"`
        というプレーンな文字列で、`.claude/agents/tdd-develop.md` という形では
        どこにも書かれていない（削除済みファイルのため §3 の 4 段解決のどこにも
        当たらず `missing` になる。原文正規化形がそのまま target になる）。
        このテストが赤になる場合、それは「py ファイル内の文字列リテラルのパス参照が
        辺になっていない」という本スライスの中核欠落であり、テスト設計の誤りではない
        （plan-report 明記）。
        """
        target_links = [
            link for link in repo_graph.links if link.target == "agents/tdd-develop.md"
        ]

        by_source: dict = {"src/c3/_excludes.py": [], "hatch_build.py": []}
        for link in target_links:
            if link.source in by_source:
                by_source[link.source].append(link)

        missing = sorted(source for source, links in by_source.items() if not links)
        assert missing == [], (
            "A-3 判定材料の source が欠けている（py ファイル内の文字列リテラルの"
            f"パス参照が辺になっていない）: {missing}"
        )

        wrong_resolution = sorted(
            f"{link.source}:{link.resolution}"
            for links in by_source.values()
            for link in links
            if link.resolution != "missing"
        )
        assert wrong_resolution == [], (
            "agents/tdd-develop.md への辺の resolution が missing でない"
            f"（削除済みファイルへの参照のはず）: {wrong_resolution}"
        )

        module = query()
        live_sources = {
            link.source for link in target_links if module.categorize(link.source) == "live"
        }
        assert len(live_sources) >= 2, (
            "agents/tdd-develop.md を target とする全辺の source の categorize に"
            f"live が 2 件未満: {sorted(live_sources)}"
        )


# ===========================================================================
# 4. 往復（契約 §6 条件 4）— 8 フィールド込み
# ===========================================================================
class TestFileRoundTripAllEightFields:
    def test_write_then_read_restores_all_eight_link_fields(self, tmp_path):
        """`write_graph` → `read_graph` で `Link` の 8 フィールドが全て復元されること."""
        _mkfile(tmp_path, ".claude/hooks/target.py", "# target\n")
        _mkfile(tmp_path, ".claude/agents/wt_good.md", "# ok\n")
        _mkfile(
            tmp_path,
            ".claude/skills/good-skill/SKILL.md",
            "# Good Skill\n"
            "\n"
            "参照: `.claude/hooks/target.py`\n"
            "\n"
            "強調形（グロブでない）: **stop.py**\n"
            "\n"
            "| base | variant |\n"
            "| --- | --- |\n"
            "| `good` | `wt_good` |\n",
        )

        out = tmp_path / "out" / "graph.json"
        graph = refgraph.build_graph(tmp_path)
        assert len(graph.links) > 0, "positive control: 往復させる辺が 1 本も無い"

        refgraph.write_graph(graph, out)
        restored = refgraph.read_graph(out)

        assert len(restored.links) == len(graph.links)
        fields = (
            "relation",
            "source",
            "source_line",
            "context",
            "target",
            "target_exists",
            "resolution",
            "reference",
        )
        for original, restored_link in zip(graph.links, restored.links):
            mismatches = [
                field
                for field in fields
                if getattr(original, field) != getattr(restored_link, field)
            ]
            assert mismatches == [], (
                f"round-trip lost fields {mismatches} for link {original}"
            )

        assert restored.to_dict() == graph.to_dict()


# ===========================================================================
# 5. 取りこぼしの可視化（契約 §6 条件 3）
# ===========================================================================
class TestSkippedAndMissingTargetVisualization:
    def test_undecodable_file_appears_in_skipped(self, tmp_path):
        """デコード不能ファイルが `skipped` に出ること（正の対照つき）."""
        _mkfile(tmp_path, ".claude/hooks/alive.py", "# alive\n")
        _mkfile(
            tmp_path,
            ".claude/skills/good-skill/SKILL.md",
            "# ok\n\n参照: `.claude/hooks/alive.py`\n",
        )
        bad = tmp_path / ".claude" / "skills" / "bad-skill" / "SKILL.md"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes("# 壊れた見出し\n".encode("cp932"))

        graph = refgraph.build_graph(tmp_path)

        skipped_paths = [entry.path for entry in graph.skipped]
        assert ".claude/skills/bad-skill/SKILL.md" in skipped_paths, (
            f"undecodable file must appear in skipped; got {skipped_paths!r}"
        )

        # positive control: 壊れていないファイルの辺は失われない
        alive = _links(graph, target=".claude/hooks/alive.py")
        assert len(alive) >= 1, "an unrelated readable file must still produce its edges"

    def test_nonexistent_target_is_an_edge_with_target_exists_false(self, tmp_path):
        """実在しない参照先が `target_exists: false` の辺として出ること（正の対照つき）."""
        _mkfile(tmp_path, ".claude/hooks/alive.py", "# alive\n")
        _mkfile(
            tmp_path,
            "CHANGELOG.md",
            "# changelog\n\n"
            "生きている参照: `.claude/hooks/alive.py`\n"
            "消えた参照: `.claude/agents/tdd-develop.md`\n",
        )

        graph = refgraph.build_graph(tmp_path)

        dead = _links(graph, target=".claude/agents/tdd-develop.md")
        assert len(dead) >= 1, "an edge to a nonexistent target must still be emitted"
        assert [link for link in dead if link.target_exists is not False] == []

        alive = _links(graph, target=".claude/hooks/alive.py")
        assert len(alive) >= 1, "positive control: existing target must also produce an edge"
        assert [link for link in alive if link.target_exists is not True] == []


# ===========================================================================
# 6. 判定非混入の静的検査（契約 §6 条件 5 / C-21）
#
# 検出器（純粋関数）・本検査・検出器の単体テストの 3 分割。
# ===========================================================================
def _string_constants_excluding_docstrings(tree: ast.Module):
    """モジュール内の文字列定数のうち docstring を除いたものを (行番号, 値) で返す.

    docstring = モジュール / 関数 / クラス本体の**先頭文**が文字列定数の場合のそれ。
    コメント（`#`）はそもそも ast に現れないため、この関数だけで
    「docstring / コメント内の出現を誤検出しない」が満たせる。
    """
    docstring_node_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_node_ids.add(id(body[0].value))

    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstring_node_ids:
                continue
            out.append((node.lineno, node.value))
    return out


def find_literal_leaks(source_text: str, literals):
    """ソース中の非 docstring 文字列定数に `literals` のいずれかが含まれていたら
    `(行番号, リテラル, 定数値)` の list を返す（検出器・純粋関数）.

    docstring / コメント内の出現は誤検出しない
    （`_string_constants_excluding_docstrings` が両方とも除く）。
    """
    tree = ast.parse(source_text)
    hits = []
    for lineno, value in _string_constants_excluding_docstrings(tree):
        for literal in literals:
            if literal in value:
                hits.append((lineno, literal, value))
    return hits


class TestLiteralLeakDetector:
    """検出器 `find_literal_leaks` の単体テスト（純粋関数・ファイル非依存）."""

    def test_detects_literal_inside_a_regular_string_constant(self):
        src = 'PREFIX = ".claude/reports/"\n'
        hits = find_literal_leaks(src, (".claude/reports/",))
        assert [lineno for lineno, _, _ in hits] == [1]

    def test_ignores_the_module_docstring(self):
        src = '"""this mentions .claude/reports/ in prose."""\nX = 1\n'
        hits = find_literal_leaks(src, (".claude/reports/",))
        assert hits == []

    def test_ignores_a_function_docstring(self):
        src = 'def f():\n    """mentions .claude/reports/ here."""\n    return 1\n'
        hits = find_literal_leaks(src, (".claude/reports/",))
        assert hits == []

    def test_does_not_ignore_a_later_string_equal_in_content_to_a_docstring(self):
        """docstring 位置（先頭文）でなければ、同じ文字列内容でも検出する（正の対照）."""
        src = 'def f():\n    x = 1\n    return ".claude/reports/"\n'
        hits = find_literal_leaks(src, (".claude/reports/",))
        assert [lineno for lineno, _, _ in hits] == [3]

    def test_no_hit_when_literal_is_absent(self):
        src = 'X = "unrelated string"\n'
        hits = find_literal_leaks(src, (".claude/reports/",))
        assert hits == []


class TestJudgmentPrefixesDoNotLeakIntoExtractor:
    """契約 §6 条件 5 / C-21: 出所カテゴリの接頭辞リテラル 11 件が
    `src/c3/refgraph.py` のコードに 1 つも現れない（負の対照）＋
    同じ 11 件が `scripts/refgraph_query.py` に全て現れる（正の対照）."""

    def test_extractor_source_contains_none_of_the_11_category_prefixes(self):
        assert len(CATEGORY_PREFIX_LITERALS) == 11
        source = (REPO_ROOT / "src" / "c3" / "refgraph.py").read_text(encoding="utf-8")
        hits = find_literal_leaks(source, CATEGORY_PREFIX_LITERALS)
        assert hits == [], f"judgment prefixes leaked into the extractor: {hits}"

    def test_query_layer_source_contains_all_11_category_prefixes(self):
        """正の対照: query 層はこれらのリテラルを使って `generated` 含む分類を実装している."""
        assert QUERY_MODULE_PATH.is_file(), f"{QUERY_MODULE_PATH} が無い（未実装）"
        source = QUERY_MODULE_PATH.read_text(encoding="utf-8")
        hits = find_literal_leaks(source, CATEGORY_PREFIX_LITERALS)

        covered = {literal for _, literal, _ in hits}
        missing = sorted(set(CATEGORY_PREFIX_LITERALS) - covered)
        assert missing == [], (
            f"expected all 11 category literals to appear in the query layer; missing: {missing}"
        )


# ===========================================================================
# 7. do-nothing 抽出器スタブ検査（契約 §6 条件 7 / C-23）
#
# 機構: pytest をサブプロセスで二重起動する（session スコープの repo_graph
# fixture がスタブを迂回しないよう、フレッシュなインタプリタで検査する）。
# ===========================================================================
_STUB_BUILD_GRAPH_RUNNER = """\
import sys
sys.path.insert(0, {src_dir!r})

import c3.refgraph as refgraph


def _empty_graph(root):
    return refgraph.Graph(root=str(root), file_count=0, nodes=(), links=(), skipped=())


refgraph.build_graph = _empty_graph

import pytest

raise SystemExit(pytest.main([
    "-q", "--no-header", "-p", "no:cacheprovider",
    "--junitxml", {junit_path!r},
    {test_file!r},
]))
"""


def _run_junit(runner_source: str, tmp_path: Path) -> dict:
    """`runner_source` を子プロセスで実行し `{テスト名: 'red'|'green'}` を返す."""
    junit_path = tmp_path / "junit.xml"
    runner = tmp_path / "run_stubbed_suite.py"
    runner.write_text(runner_source, encoding="utf-8")

    subprocess.run(
        [sys.executable, str(runner)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )

    assert junit_path.is_file(), (
        f"junit report was not produced (runner crashed before pytest ran?); "
        f"runner={runner}"
    )
    tree = ElementTree.parse(junit_path)
    outcomes = {}
    for case in tree.iter("testcase"):
        name = f"{case.get('classname')}::{case.get('name')}"
        failed = case.find("failure") is not None or case.find("error") is not None
        outcomes[name] = "red" if failed else "green"
    return outcomes


class TestExtractorDoNothingStubDetection:
    """`build_graph` を何も抽出しない実装に差し替え、`tests/test_refgraph.py` の
    うち `TestApiShape`（API の型検査）＋ `_STUB_SAFE_CLASS_NAME_FRAGMENTS`
    （§2-1 が「∀ 型でない」と明記した AC-15 / AC-17 相当のクラス）だけが
    緑に残ることを検証する.

    それ以外の全クラスは「辺が 1 本以上出ている」という positive
    control を持つ設計（`tests/test_refgraph.py` 自身の docstring・契約 §7 規律 4）
    なので、何も抽出しないスタブでは必ず赤になる。

    `_STUB_SAFE_CLASS_NAME_FRAGMENTS` の根拠（`docs/refgraph-acceptance.md` §2-1）:
    - AC-15（`TestOldSchemaBackwardCompat`）: 「∀ 型でない（`Link.reference == ""`
      を assert する）」。`read_graph` だけを当てるテストで `build_graph` の出力を
      見ないため、`build_graph` を空スタブに差し替えても影響を受けない
    - AC-17（`TestNoExceptionOnPathologicalInputs`）: 「∀ 型でない（明示した入力で
      例外が出ないことを assert する）」。出力を見ず「クラッシュしないこと」だけを
      見るため、スタブの下でも真になる（`build_graph` が例外を投げない実装なら
      何であれ成立する）
    """

    _STUB_SAFE_CLASS_NAME_FRAGMENTS = (
        "TestApiShape",
        "TestOldSchemaBackwardCompat",
        "TestNoExceptionOnPathologicalInputs",
    )

    def test_build_graph_stub_leaves_only_api_shape_green(self, tmp_path):
        outcomes = _run_junit(
            _STUB_BUILD_GRAPH_RUNNER.format(
                src_dir=str(REPO_ROOT / "src"),
                junit_path=str(tmp_path / "junit.xml"),
                test_file=str(TEST_REFGRAPH_PATH),
            ),
            tmp_path,
        )
        assert outcomes, "junit report produced no test cases (runner failed?)"

        red = [name for name, status in outcomes.items() if status == "red"]
        green = [name for name, status in outcomes.items() if status == "green"]

        assert len(red) >= 1, "positive control: スタブ下で 1 件も赤にならない"

        unexpected_green = sorted(
            name
            for name in green
            if not any(fragment in name for fragment in self._STUB_SAFE_CLASS_NAME_FRAGMENTS)
        )
        assert unexpected_green == [], (
            "these tests stayed green under a do-nothing build_graph stub "
            f"(V-15 個別確認対象・出力を assert していない疑い): {unexpected_green}"
        )


# ===========================================================================
# 8. do-nothing クエリ層スタブ検査（契約 §5-1 完成条件 5・C-24 修正版）
#
# 設計判断（tester・architecture / 契約に機構の明記が無いため決めた）:
#   `scripts/refgraph_query.py` の全公開関数を inspect で機械導出し、関数ごとに
#   「その関数の戻り値を assert する最小プローブ」を 1 本ずつ用意する。
#   プローブ表のキー集合と機械導出した関数集合の一致を別テストで縛るため、
#   新しい公開関数が増えてもプローブの追加漏れに気づける（列挙のハードコード化を防ぐ）。
# ===========================================================================
def _public_module_functions(module):
    """`module` の非アンダースコア始まりの module レベル関数を inspect で機械導出する.

    `obj.__module__ == module.__name__` で、import で持ち込んだ他モジュールの
    関数（`dataclasses.replace` 等）を巻き込まない。
    """
    return sorted(
        name
        for name, obj in inspect.getmembers(module, inspect.isfunction)
        if not name.startswith("_") and obj.__module__ == module.__name__
    )


def _identity_stub(*args, **kwargs):
    """契約 §5-1 完成条件 5 の「何も絞らない・畳まない」実装.

    第 1 位置引数をそのまま返す（narrowing 系関数はこれで「何もしない」相当になる）。
    """
    if args:
        return args[0]
    return None


class TestQueryLayerStubDetection:
    def _probes(self, tmp_path):
        """公開関数ごとの (位置引数, 戻り値検査) の表. 実装の下では全て真になる."""
        return {
            "categorize": (
                (".claude/reports/x.md",),
                lambda r: r == "history",
            ),
            "filter_links": (
                (
                    (
                        _link(source=".claude/docs/a.md", target=".claude/x.md"),
                        _link(source="CHANGELOG.md", target=".claude/y.md"),
                    ),
                    ("live",),
                ),
                lambda r: len(r) == 1 and r[0].source == ".claude/docs/a.md",
            ),
            "fold_target": (
                ("src/c3/_template/.claude/hooks/stop.py",),
                lambda r: r == ".claude/hooks/stop.py",
            ),
            "fold_links": (
                (
                    (
                        _link(
                            source=".claude/docs/a.md",
                            target="src/c3/_template/.claude/hooks/stop.py",
                        ),
                    ),
                ),
                lambda r: len(r) == 1 and r[0].target == ".claude/hooks/stop.py",
            ),
            "settled_links": (
                (
                    (
                        _link(
                            source=".claude/docs/a.md",
                            target=".claude/x.md",
                            resolution="ambiguous",
                        ),
                        _link(
                            source=".claude/docs/a.md",
                            target=".claude/y.md",
                            resolution="ambiguous",
                        ),
                    ),
                ),
                lambda r: tuple(r) == (),
            ),
            "glob_matches": (
                ("src/**/*.py", "src/c3/refgraph.py"),
                lambda r: r is True,
            ),
            "by_relation": (
                (
                    (
                        _link(source=".claude/docs/a.md", target=".claude/x.md", relation="md_link"),
                        _link(source=".claude/docs/b.md", target=".claude/y.md", relation="py_import"),
                    ),
                    "md_link",
                ),
                lambda r: len(r) == 1 and r[0].relation == "md_link",
            ),
            "by_resolution": (
                (
                    (
                        _link(source=".claude/docs/a.md", target=".claude/x.md", resolution="exact"),
                        _link(
                            source=".claude/docs/b.md",
                            target=".claude/y.md",
                            resolution="ambiguous",
                        ),
                    ),
                    "ambiguous",
                ),
                lambda r: len(r) == 1 and r[0].resolution == "ambiguous",
            ),
            "by_target_kind": (
                (
                    (
                        _link(source=".claude/docs/a.md", target=".claude/x.md"),
                        _link(source=".claude/docs/b.md", target="sqltable:t"),
                    ),
                    "table",
                ),
                lambda r: len(r) == 1 and r[0].target == "sqltable:t",
            ),
            "to_targets": (
                (
                    (
                        _link(source=".claude/docs/a.md", target=".claude/x.md"),
                        _link(source=".claude/docs/b.md", target=".claude/y.md"),
                    ),
                ),
                lambda r: isinstance(r, (set, frozenset))
                and r == {".claude/x.md", ".claude/y.md"},
            ),
            "main": (
                (
                    [
                        "--target",
                        "nonexistent-target-xyz",
                        "--graph",
                        str(tmp_path / "no-such-graph.json"),
                    ],
                ),
                lambda r: r == 2,
            ),
        }

    def test_discovered_public_functions_match_the_probe_table(self, tmp_path):
        """機械導出した公開関数の集合とプローブ表のキー集合が一致すること.

        `scripts/refgraph_query.py` に新しい公開関数が増えたら、プローブの
        追加漏れとしてここが先に赤くなる（列挙のハードコード化を防ぐ）。
        """
        module = query()
        discovered = set(_public_module_functions(module))
        probed = set(self._probes(tmp_path))
        assert discovered == probed, (
            f"discovered={sorted(discovered)} probed={sorted(probed)} "
            f"(差分: discovered-probed={sorted(discovered - probed)}, "
            f"probed-discovered={sorted(probed - discovered)})"
        )

    def test_real_implementation_satisfies_every_probe(self, tmp_path):
        """正の対照: 実装のもとでは全プローブが真になること（プローブ自体の裏取り）."""
        module = query()
        probes = self._probes(tmp_path)

        failed = []
        for name, (args, check) in probes.items():
            fn = getattr(module, name)
            result = fn(*args)
            if not check(result):
                failed.append((name, result))
        assert failed == [], f"real implementation failed its own probe: {failed}"

    def test_identity_stub_turns_every_probe_red(self, tmp_path, monkeypatch):
        """本体: 各公開関数を identity スタブへ個別に差し替えると、
        その関数のプローブが赤になること（契約 §5-1 完成条件 5・C-24 修正版）."""
        module = query()
        probes = self._probes(tmp_path)
        assert len(probes) > 0, "positive control: プローブが 1 本も無い"

        still_green = []
        for name, (args, check) in probes.items():
            with monkeypatch.context() as patch:
                patch.setattr(module, name, _identity_stub)
                try:
                    result = getattr(module, name)(*args)
                    passed = check(result)
                except Exception:
                    passed = False
            if passed:
                still_green.append(name)

        assert still_green == [], (
            "these query-layer functions stayed green under a do-nothing "
            f"(identity) stub: {still_green}"
        )


# ===========================================================================
# 9. `_SKIP_DIR_NAMES` 検査 — 出力から判定する（定数の静的検査ではない）
# ===========================================================================
class TestSkipDirNamesViaOutput:
    def test_directories_not_in_the_skip_list_are_walked(self, tmp_path):
        """除外名以外のディレクトリ配下のファイルが source として辺に現れること（正の対照）."""
        _mkfile(tmp_path, ".claude/hooks/alive.py", "# alive\n")
        _mkfile(
            tmp_path,
            "build/notes.md",
            "参照: `.claude/hooks/alive.py`\n",
        )
        _mkfile(
            tmp_path,
            "coverage/report.md",
            "参照: `.claude/hooks/alive.py`\n",
        )

        graph = refgraph.build_graph(tmp_path)
        sources = {link.source for link in _links(graph, target=".claude/hooks/alive.py")}

        missing = sorted({"build/notes.md", "coverage/report.md"} - sources)
        assert missing == [], (
            f"directories outside the skip list must be walked; missing sources: {missing}"
        )

    def test_known_skip_directory_is_not_walked(self, tmp_path):
        """既知の除外ディレクトリ（`__pycache__`）配下は走査されないこと（負の対照）.

        「在ること」の対照（上のテスト）と同じ考え方で、除外側の実効を出力から見る。
        """
        _mkfile(tmp_path, ".claude/hooks/alive.py", "# alive\n")
        _mkfile(
            tmp_path,
            "__pycache__/notes.md",
            "参照: `.claude/hooks/alive.py`\n",
        )
        _mkfile(
            tmp_path,
            "kept/notes.md",
            "参照: `.claude/hooks/alive.py`\n",
        )

        graph = refgraph.build_graph(tmp_path)
        sources = {link.source for link in _links(graph, target=".claude/hooks/alive.py")}

        assert "kept/notes.md" in sources, "positive control: 除外対象でないディレクトリが消えた"
        assert "__pycache__/notes.md" not in sources, (
            f"__pycache__ directory must not be walked; sources={sorted(sources)}"
        )


# ===========================================================================
# 10. settings 系回帰（source 名固定・現行値の本数に依存しない）
# ===========================================================================
class TestSettingsResolutionRegression:
    """`settings.json` 由来の既存解決を壊していないこと（source 名固定の代表 2 件）."""

    def test_settings_hook_registration_resolves_exact(self, repo_graph):
        """`${CLAUDE_PROJECT_DIR}/.claude/hooks/session_stop.py` → `exact` 解決."""
        hits = _links(
            repo_graph,
            relation="settings_hook",
            source=".claude/settings.json",
            target=".claude/hooks/session_stop.py",
        )
        assert len(hits) >= 1, "settings_hook edge (settings.json -> session_stop.py) is missing"
        bad = [link for link in hits if link.resolution != "exact"]
        assert bad == [], f"settings_hook resolution regressed from exact: {bad}"

    def test_settings_permission_allow_resolves_exact(self, repo_graph):
        """`Bash(c3 run .claude/hooks/stop.py*)` → `exact` 解決（`settings_hook` ではない）."""
        hits = _links(
            repo_graph,
            relation="settings_permission",
            source=".claude/settings.json",
            target=".claude/hooks/stop.py",
        )
        assert len(hits) >= 1, "settings_permission edge (settings.json -> stop.py) is missing"
        bad = [link for link in hits if link.resolution != "exact"]
        assert bad == [], f"settings_permission resolution regressed from exact: {bad}"


# ===========================================================================
# 11. AC-18: resolution / kind の双方向一致（test-ac-gaps タスク）
#
# `TestFormatCoverageBothSidesNonempty` は relation のみを双方向で見ている
# （§1 の項目のまま）。AC-18 はこれに加えて `resolution`（4 値）と `kind`
# （3 値）の双方向一致も要求する。実測: `kind` は現行実装では常に "file" か
# "table" にしかならず、"dir" を一切割り当てない（契約 C-3 / §3 ノード ID の
# 未実装）。この節の kind テストは genuine な Red になる。
# ===========================================================================
CONTRACT_RESOLUTIONS = ("exact", "basename", "ambiguous", "missing")
CONTRACT_KINDS = ("file", "dir", "table")


class TestResolutionAndKindBidirectional:
    """AC-18: `resolution` の 4 値・`kind` の 3 値が契約と実装で双方向一致すること."""

    def test_resolution_values_match_both_directions(self, repo_graph):
        expected = set(CONTRACT_RESOLUTIONS)
        actual = {link.resolution for link in repo_graph.links}
        assert len(expected) > 0 and len(actual) > 0

        missing_in_impl = sorted(expected - actual)
        assert missing_in_impl == [], f"契約にあるが実装が 0 本の resolution: {missing_in_impl}"

        unknown_in_impl = sorted(actual - expected)
        assert unknown_in_impl == [], f"実装にあるが契約に無い resolution: {unknown_in_impl}"

    def test_kind_values_match_both_directions(self, repo_graph):
        expected = set(CONTRACT_KINDS)
        actual = {node.kind for node in repo_graph.nodes}
        assert len(expected) > 0 and len(actual) > 0

        missing_in_impl = sorted(expected - actual)
        assert missing_in_impl == [], (
            "契約にあるが実装が 0 個の kind（現行実装は kind='dir' を一切割り当てない・"
            f"契約 C-3 の未実装）: {missing_in_impl}"
        )

        unknown_in_impl = sorted(actual - expected)
        assert unknown_in_impl == [], f"実装にあるが契約に無い kind: {unknown_in_impl}"


# ===========================================================================
# 12. AC-56: 写像表（`docs/refgraph-acceptance.md` §3）の網羅静的検査
#
# 分類 D（文書・ソース照合型）。`build_graph` もクエリ層関数も呼ばない・fixture を
# 持たない。「引き受け先なし」の行が 0 行であることと、正の対照として「引き受け先
# あり」の行が 1 行以上あることを同じテストで縛る（契約 §7 規律 4）。
# ===========================================================================
class TestMappingTableHasNoUnclaimedRows:
    """AC-56: `docs/refgraph-acceptance.md` §3 写像表に「引き受け先なし」の行が 0 行なこと."""

    @staticmethod
    def _mapping_table_rows():
        text = (REPO_ROOT / "docs" / "refgraph-acceptance.md").read_text(encoding="utf-8")
        start = text.index("## 3. 写像表")
        end = text.index("## 4. 受け入れ条件の条文", start)
        section = text[start:end]

        rows = []
        for line in section.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if not cells or set("".join(cells)) <= set("-: "):
                continue
            rows.append(cells)
        # 先頭はヘッダ行
        return rows[1:]

    def test_no_row_has_an_unclaimed_assignment(self):
        rows = self._mapping_table_rows()
        assert len(rows) > 0, "positive control: 写像表の行が 1 行も取れていない（パーサが壊れている）"

        unclaimed = [row for row in rows if not row[1] or "引き受け先なし" in row[1]]
        assert unclaimed == [], f"写像表に「引き受け先なし」の行がある: {unclaimed}"

    def test_at_least_one_row_is_claimed(self):
        rows = self._mapping_table_rows()
        claimed = [row for row in rows if row[1] and "引き受け先なし" not in row[1]]
        assert len(claimed) >= 1, "positive control: 「引き受け先あり」の行が 1 行も無い"
