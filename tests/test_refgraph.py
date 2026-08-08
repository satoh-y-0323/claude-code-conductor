"""参照抽出器（refgraph）のベンチマークテスト。

`docs/refgraph-contract.md`（2026-08-05 確定）に基づく。**旧契約
（`.dev/refgraph-benchmark-20260805.md`）の到達可能性 API は失効した**ので、
本ファイルは `is_reachable` / `paths_to` を一切呼ばない。

この道具は **関係を漏れなく抽出してファイルへ出力する**だけで、判定はしない。
したがってテストが見るのは「到達可能か」ではなく「**関係が出るか**」である。

カバーする完成条件（契約 §6）:

- 条件 1（形式カバレッジ）: `TestFormatCoverage`
- 条件 2（既知の関係 8 行）: `TestKnownRelations`
- 条件 3（取りこぼしの可視化）: `TestSkippedAndMissingTargets`
- 条件 4（ファイル往復）: `TestFileRoundTrip`
- 条件 5（判定を含まない・機械強制）: `TestNoJudgmentInExtractor` /
  `TestSourceIsNotFiltered`
- 条件 6（フルスイート緑）: 本ファイルの対象外（親が確認する）
- 条件 7（do-nothing スタブ検査）: `TestApiShape` **のみ**がスタブで緑になってよい。
  他クラスは全て「辺が 1 本以上出ること」を positive control として持つので、
  何も抽出しない実装では必ず赤になる（契約 §7 規律 4）。

旧テストからの移植（契約の言葉に翻訳して残したもの）:

- 旧 N-1「`permissions.allow` は `settings_hook` 辺を作らない」
  → `TestKnownRelations.test_settings_permission_records_stop_py_and_is_not_a_hook`
    と `TestMigratedNegativeControls.test_permissions_entry_is_permission_not_hook`
    （**捨てるのではなく `settings_permission` として記録する**）
- 旧 N-2「散文の言及は `agent_variant_map` 辺を作らない」
  → `TestMigratedNegativeControls.test_prose_mention_is_bare_agent_name_not_variant_map`
    （**捨てるのではなく `md_bare_agent_name` として記録する**）
- 旧「正の双子を同じ fixture に置く」→ 全ての「無いこと」検査で維持

テスト作法（契約 §7）:

1. 不在は `assert xs == []` で直接 assert する（空ループ内で assert しない）
2. 合成入力では参照先のファイルを実際に作る
3. ノード ID / `skipped` のパスはルート相対 POSIX
4. 「無いこと」の検査は「在ること」の対照と同じ fixture に置く
5. assert のないテストを書かない
6. 機構を足したら同じ周回でその機構を検査するテストを足す

Windows 注意: `write_text()` には必ず `encoding="utf-8"` を付ける
（既定は cp932 で、UTF-8 で読む抽出器が黙って沈黙する事故が実際に起きた）。
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import sys
import tokenize
from collections import Counter
from pathlib import Path, PurePosixPath

import pytest

# モジュール属性経由で参照する。`from c3.refgraph import write_graph` と書くと
# 未実装時に **collection error** になり全ケースが実行されず、
# 「スタブで緑になるのは API 型検査だけ」（契約 §6 条件 7）の確認ができない。
import c3.refgraph as refgraph  # noqa: E402

# クエリ層（`scripts/refgraph_query.py`）を遅延ロードするための sys.path 設定
# （改訂 5: TestMdLinkT5bSharesReferenceWithBody が settled_links を検査するため）。
# モジュールレベルでは import しない（tests/test_refgraph_query.py と同じ流儀）。
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def query_module():
    """`scripts/refgraph_query.py` をモジュールとして返す(実行時に遅延ロード)."""
    import refgraph_query

    return refgraph_query


# ---------------------------------------------------------------------------
# 契約 §4 の relation 一覧。**実装から import しない**（実装が減らしたら赤になる）。
# ---------------------------------------------------------------------------
RELATIONS = (
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

# 契約 §3 の resolution 4 値
RESOLUTIONS = ("exact", "basename", "ambiguous", "missing")

# 契約 §6 条件 5: 抽出器の公開面に現れてはならない「判定」の語彙。
# 完全一致で見る（`root` 単独は抽出ルートを指す正当な名前なので含めない）。
FORBIDDEN_PUBLIC_NAMES = frozenset(
    {
        "is_reachable",
        "paths_to",
        "reachable",
        "unreachable",
        "reachable_from",
        "is_dead",
        "is_alive",
        "dead_nodes",
        "live_nodes",
        "entry_points",
        "entrypoints",
        "ENTRY_POINTS",
        "ENTRYPOINTS",
        "roots",
        "ROOTS",
        "root_set",
        "ROOT_SET",
    }
)

# 同上をソース（AST）側で縛るための名前パターン。
_JUDGMENT_NAME_RE = re.compile(
    r"reachab|entry_?points?|root_set|rootset|dead_code|dead_node"
    r"|is_alive|is_dead|paths_to",
    re.IGNORECASE,
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


def _targets(graph, relation=None, source=None):
    return {link.target for link in _links(graph, relation=relation, source=source)}


def _node_ids(graph):
    return {node.id for node in graph.nodes}


def _skipped_paths(graph):
    return [entry.path for entry in graph.skipped]


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def repo_graph(repo_root):
    """実リポジトリ全体の抽出結果（session スコープ・1 回だけ構築する）."""
    return refgraph.build_graph(repo_root)


# ===========================================================================
# 契約 §5 API の型検査
#
# ★ do-nothing スタブで緑になってよいのは **このクラスだけ**（契約 §6 条件 7）。
#   他クラスが 1 件でも緑になったら、そのテストは空回りしている。
# ===========================================================================
class TestApiShape:
    def test_build_graph_returns_graph_with_tuple_collections(self, tmp_path):
        """`build_graph(root)` が nodes / links / skipped を tuple で返す（§5）."""
        _mkfile(tmp_path, "README.md", "# empty\n")

        graph = refgraph.build_graph(tmp_path)

        assert isinstance(graph.nodes, tuple)
        assert isinstance(graph.links, tuple)
        assert isinstance(graph.skipped, tuple)

    def test_to_dict_has_schema_shape(self, tmp_path):
        """`Graph.to_dict()` が §3 のスキーマ形をしていること."""
        _mkfile(tmp_path, "README.md", "# empty\n")

        data = refgraph.build_graph(tmp_path).to_dict()

        assert isinstance(data, dict)
        missing_keys = [
            key
            for key in (
                "schema_version",
                "root",
                "generated_from",
                "nodes",
                "links",
                "skipped",
            )
            if key not in data
        ]
        assert missing_keys == []
        assert data["schema_version"] == 1
        assert isinstance(data["root"], str)
        assert isinstance(data["generated_from"], dict)
        assert isinstance(data["generated_from"]["file_count"], int)
        assert isinstance(data["nodes"], list)
        assert isinstance(data["links"], list)
        assert isinstance(data["skipped"], list)

    def test_write_graph_then_read_graph_returns_graph(self, tmp_path):
        """`write_graph` がファイルを作り `read_graph` が Graph を返す（型のみ）."""
        _mkfile(tmp_path, "README.md", "# empty\n")
        out = tmp_path / "out" / "graph.json"

        graph = refgraph.build_graph(tmp_path)
        refgraph.write_graph(graph, out)

        assert out.is_file()

        restored = refgraph.read_graph(out)
        assert isinstance(restored.nodes, tuple)
        assert isinstance(restored.links, tuple)
        assert isinstance(restored.skipped, tuple)


# ===========================================================================
# 契約 §6 条件 1: 形式カバレッジ
# ===========================================================================
class TestFormatCoverage:
    def test_every_relation_has_at_least_one_edge_in_the_real_repo(self, repo_graph):
        """§4 の 14 relation それぞれに、実リポジトリで 1 本以上の辺が出ること.

        0 本の relation があれば、その形式を取りこぼしている
        （14 種すべてが tracked ファイルに実在することは確認済み）。
        """
        counts = Counter(link.relation for link in repo_graph.links)
        missing = [name for name in RELATIONS if counts[name] == 0]
        assert missing == [], f"relation with zero edges: {missing}"

    def test_no_unknown_relation_names(self, repo_graph):
        """§4 に無い relation 名が混ざっていないこと（正の対照つき）."""
        assert len(repo_graph.links) > 0, "positive control: 辺が 1 本も無い"

        unknown = sorted({link.relation for link in repo_graph.links} - set(RELATIONS))
        assert unknown == [], f"relation not defined in contract §4: {unknown}"

    def test_resolution_values_are_within_the_contract(self, repo_graph):
        """`resolution` が §3 の 4 値のいずれかであること."""
        assert len(repo_graph.links) > 0, "positive control: 辺が 1 本も無い"

        unknown = sorted({link.resolution for link in repo_graph.links} - set(RESOLUTIONS))
        assert unknown == [], f"resolution not defined in contract §3: {unknown}"


# ===========================================================================
# 契約 §6 条件 2: 既知の関係が出ること（表 8 行）
# ===========================================================================
class TestKnownRelations:
    def test_settings_hooks_section_produces_settings_hook_edges(self, repo_graph):
        """行 1: `settings*.json` の hooks 登録 → hook `.py`（`settings_hook`）."""
        hook_links = _links(repo_graph, relation="settings_hook")
        assert len(hook_links) > 0, "settings_hook edge is missing entirely"

        bad_sources = sorted(
            {
                link.source
                for link in hook_links
                if not link.source.endswith(("settings.json", "settings.local.json"))
            }
        )
        assert bad_sources == [], f"settings_hook must originate from settings*.json: {bad_sources}"

        targets = {link.target for link in hook_links}
        assert ".claude/hooks/permission_handler.py" in targets

    def test_session_stop_importlib_loads_produce_py_importlib_edges(self, repo_graph):
        """行 2: `session_stop.py` の importlib → 4 モジュール（`py_importlib`）.

        `.dev/pivot-20260805.md` §9 の実測（80/87/97/111 行）を「関係が出るか」に
        読み替えたもの。実測値は再測定しない。
        """
        expected = {
            ".claude/hooks/stop.py",
            ".claude/hooks/consolidate_memory.py",
            ".claude/hooks/session_utils.py",
            ".claude/hooks/tier_gap_check.py",
        }
        got = _targets(repo_graph, "py_importlib", ".claude/hooks/session_stop.py")
        missing = sorted(expected - got)
        assert missing == [], f"py_importlib edges missing from session_stop.py: {missing}"

    def test_permission_handler_subprocess_produces_py_subprocess_path_edge(self, repo_graph):
        """行 3: `permission_handler.py` の subprocess → toast（`py_subprocess_path`）."""
        hits = _links(
            repo_graph,
            relation="py_subprocess_path",
            source=".claude/hooks/permission_handler.py",
            target=".claude/hooks/permission_handler_toast.py",
        )
        assert len(hits) >= 1, "py_subprocess_path edge to permission_handler_toast.py is missing"

    def test_parallel_agents_variant_table_produces_md_agent_variant_map_edges(self, repo_graph):
        """行 4: `parallel-agents/SKILL.md` の写像表 → `wt_*.md`（`md_agent_variant_map`）."""
        got = _targets(
            repo_graph, "md_agent_variant_map", ".claude/skills/parallel-agents/SKILL.md"
        )
        expected = {
            ".claude/agents/wt_tester.md",
            ".claude/agents/wt_developer.md",
            ".claude/agents/wt_systematic-debugger.md",
        }
        missing = sorted(expected - got)
        assert missing == [], f"md_agent_variant_map edges missing: {missing}"

    def test_c3_run_reaches_all_seven_skill_scripts(self, repo_graph, repo_root):
        """行 5: SKILL.md の `c3 run` → skill scripts 7 本（`md_c3_run`）.

        `${CLAUDE_SKILL_DIR}` / skill 相対 / ルート相対の 3 形式が混在しており、
        1 形式でも解決できないと 7 本そろわない。
        """
        script_ids = sorted(
            str(path.relative_to(repo_root)).replace("\\", "/")
            for path in repo_root.glob(".claude/skills/*/scripts/*.py")
        )
        assert len(script_ids) == 7, f"repo fact changed: {script_ids}"

        targets = _targets(repo_graph, "md_c3_run")
        missing = [node_id for node_id in script_ids if node_id not in targets]
        assert missing == [], f"md_c3_run edges missing for skill scripts: {missing}"

    def test_cli_multiline_import_resolves_every_submodule(self, repo_graph):
        """行 6: `cli.py` の複数行括弧付き import → `cli_*.py`（`py_import`）.

        行単位の正規表現では 2 行目以降を取りこぼす。AST 解析の要求（§4 設計上の要点）を
        機械で押さえる。
        """
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
        got = _targets(repo_graph, "py_import", "src/c3/cli.py")
        missing = sorted(expected - got)
        assert missing == [], f"py_import edges missing from cli.py (AST 解析の欠落?): {missing}"

    def test_prose_mention_produces_md_bare_agent_name_edge(self, repo_graph):
        """行 7: SKILL.md の散文 → `security-reviewer.md`（`md_bare_agent_name`）.

        C3 の agent は親 Claude が散文の指示を読んで起動するため、パス形式の参照が
        存在しない。旧契約でこの形式を切り捨てた結果、辺が 0 本になった（§9-3）。
        """
        hits = _links(
            repo_graph,
            relation="md_bare_agent_name",
            target=".claude/agents/security-reviewer.md",
        )
        assert len(hits) >= 1, "md_bare_agent_name edge to security-reviewer.md is missing"

        from_skill_md = [link for link in hits if link.source.endswith("SKILL.md")]
        assert len(from_skill_md) >= 1, (
            f"contract row 7 says the mention is in a SKILL.md; sources were "
            f"{sorted({link.source for link in hits})}"
        )

    def test_settings_permission_records_stop_py_and_is_not_a_hook(self, repo_graph):
        """行 8 ＋ 旧 N-1 の移植: `permissions.allow` → `stop.py`.

        `"Bash(c3 run .claude/hooks/stop.py*)"` は**実在する関係**なので
        `settings_permission` として記録する（旧契約は捨てていた）。
        一方 `stop.py` は hooks 節に登録されていない（登録されているのは
        `session_stop.py`）ので `settings_hook` にはならない。
        `stop.py` は `session_stop.py` の**接尾辞**でもあるため、部分一致で
        解決する実装はここで赤になる。
        """
        perm_links = _links(
            repo_graph, relation="settings_permission", target=".claude/hooks/stop.py"
        )
        assert len(perm_links) >= 1, "settings_permission edge to stop.py is missing"

        bad_sources = sorted(
            {
                link.source
                for link in perm_links
                if not link.source.endswith(("settings.json", "settings.local.json"))
            }
        )
        assert bad_sources == []

        hook_links = _links(
            repo_graph, relation="settings_hook", target=".claude/hooks/stop.py"
        )
        assert hook_links == [], (
            "stop.py is not registered in the hooks section; a settings_hook edge into it "
            "means permissions.allow (or a suffix match on session_stop.py) leaked into the "
            f"hook relation: {[(l.source, l.source_line, l.context) for l in hook_links]}"
        )


class TestOtherRelationsInTheRealRepo:
    """§6 条件 2 の表に無い §4 relation の実リポジトリ実測（形式カバレッジの内訳）."""

    def test_statusline_produces_settings_statusline_edge(self, repo_graph):
        """`statusLine` は `command` 文字列にパスが埋まる（hooks の `args` 形式と別）."""
        hits = _links(
            repo_graph,
            relation="settings_statusline",
            target=".claude/hooks/statusline.py",
        )
        assert len(hits) >= 1, "settings_statusline edge to statusline.py is missing"

    def test_md_link_edge_exists(self, repo_graph):
        """マークダウンリンク `[x](path)` が `md_link` になること."""
        hits = _links(
            repo_graph,
            relation="md_link",
            source=".claude/docs/taxonomy.md",
            target=".claude/docs/platform-adapters.md",
        )
        assert len(hits) >= 1, "md_link edge taxonomy.md -> platform-adapters.md is missing"

    def test_md_subagent_type_edge_exists(self, repo_graph):
        """`subagent_type: "design-critic"` / `agent: design-critic` が辺になること."""
        hits = _links(
            repo_graph,
            relation="md_subagent_type",
            target=".claude/agents/design-critic.md",
        )
        assert len(hits) >= 1, "md_subagent_type edge to design-critic.md is missing"

    def test_md_bare_skill_name_edge_exists(self, repo_graph):
        """`/start` のような素の skill 名が `md_bare_skill_name` になること."""
        hits = _links(
            repo_graph,
            relation="md_bare_skill_name",
            target=".claude/skills/start/SKILL.md",
        )
        assert len(hits) >= 1, "md_bare_skill_name edge to skills/start/SKILL.md is missing"

    def test_py_sql_table_edge_and_node_exist(self, repo_graph):
        """SQL 中のテーブル名が `sqltable:<name>` ノードへの辺になること（§3）."""
        hits = _links(repo_graph, relation="py_sql_table", target="sqltable:agent_outcomes")
        assert len(hits) >= 1, "py_sql_table edge to sqltable:agent_outcomes is missing"
        assert "sqltable:agent_outcomes" in _node_ids(repo_graph)


# ===========================================================================
# 契約 §2 原則 1 / §6 条件 5 の実体: 出所でフィルタしない
# ===========================================================================
class TestSourceIsNotFiltered:
    def test_no_directory_is_excluded_from_extraction(self, tmp_path):
        """旧契約が「汚染源」と呼んだ 5 種すべてから辺が出ること（§2 原則 1）.

        `CHANGELOG` / `_template/` / `.dev/` / `.claude/tmp/` / `.claude/reports/`
        の計 1160 本（全体の 45%）を捨てたのが旧契約の失敗（§9-2）。
        `tests/` も旧実装は走査対象外にしていたので併せて置く。
        """
        _mkfile(tmp_path, ".claude/hooks/target.py", "# target\n")
        span = "参照: `.claude/hooks/target.py`\n"

        sources = {
            "CHANGELOG.md",
            "src/c3/_template/.claude/skills/x/SKILL.md",
            ".dev/notes.md",
            ".claude/tmp/po-manifest.md",
            ".claude/reports/plan-report-20260805-000000.md",
            "tests/doc.md",
        }
        for rel in sources:
            _mkfile(tmp_path, rel, "# doc\n\n" + span)

        graph = refgraph.build_graph(tmp_path)

        got = {
            link.source
            for link in _links(graph, target=".claude/hooks/target.py")
        }
        missing = sorted(sources - got)
        assert missing == [], f"these locations were filtered out of extraction: {missing}"


# ===========================================================================
# 契約 §6 条件 3: 取りこぼしの可視化
# ===========================================================================
class TestSkippedAndMissingTargets:
    @staticmethod
    def _tree_with_one_undecodable_file(root: Path) -> str:
        """デコード不能な SKILL.md と、正常に辺を張る SKILL.md を 1 つずつ置く."""
        _mkfile(root, ".claude/agents/wt_good.md", "# ok\n")

        # 正常側（正の双子・§7 規律 4）
        _mkfile(
            root,
            ".claude/skills/good-skill/SKILL.md",
            "# Good Skill\n\n| base | variant |\n| --- | --- |\n| `good` | `wt_good` |\n",
        )

        # 異常側: cp932 の日本語は UTF-8 として不正なバイト列になる
        bad = root / ".claude/skills/bad-skill/SKILL.md"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes("# 壊れた見出し\n".encode("cp932"))
        return ".claude/skills/bad-skill/SKILL.md"

    def test_undecodable_file_is_reported_once_in_skipped(self, tmp_path):
        """読めなかったファイルが `skipped` に 1 度だけ出ること（§2 原則 3）.

        沈黙して continue すると、その発信辺が消えたことに誰も気づけない。
        2026-08-05 には fail-open を潰す是正が `UnboundLocalError` を 8 箇所に
        作ったが、実リポジトリが全ファイル UTF-8 なので 2800 件の緑が見逃した。
        """
        bad_rel = self._tree_with_one_undecodable_file(tmp_path)

        graph = refgraph.build_graph(tmp_path)
        reported = _skipped_paths(graph)

        assert bad_rel in reported, f"undecodable file must be in Graph.skipped; got {reported!r}"
        assert reported.count(bad_rel) == 1, (
            f"the same file must not be reported once per extractor; got {reported!r}"
        )

        bad_separators = [path for path in reported if "\\" in path]
        assert bad_separators == [], (
            f"skipped paths must use POSIX separators like node ids; got {reported!r}"
        )

        reasons = [entry.reason for entry in graph.skipped if entry.path == bad_rel]
        assert reasons != [] and all(isinstance(r, str) and r for r in reasons)

    def test_undecodable_file_does_not_suppress_other_edges(self, tmp_path):
        """1 本読めなくても、他のファイルの辺は失われないこと（正の双子）."""
        self._tree_with_one_undecodable_file(tmp_path)

        graph = refgraph.build_graph(tmp_path)

        hits = _links(
            graph,
            relation="md_agent_variant_map",
            source=".claude/skills/good-skill/SKILL.md",
            target=".claude/agents/wt_good.md",
        )
        assert len(hits) >= 1, "an unrelated readable SKILL.md must still produce its edges"

    def test_missing_target_is_emitted_as_edge_with_target_exists_false(self, tmp_path):
        """参照先が実在しなくても辺は出し、`target_exists: false` を立てること（§2 原則 3）.

        「消したはずのものを参照している残骸がある」は有用な信号なので捨てない。
        実在する参照（正の双子）を同じ fixture に置く。
        """
        _mkfile(tmp_path, ".claude/hooks/alive.py", "# alive\n")
        _mkfile(
            tmp_path,
            ".claude/tmp/po-manifest.md",
            "# manifest\n"
            "\n"
            "生きている参照: `.claude/hooks/alive.py`\n"
            "消えた参照: `.claude/agents/tdd-develop.md`\n",
        )

        graph = refgraph.build_graph(tmp_path)

        dead = _links(graph, target=".claude/agents/tdd-develop.md")
        assert len(dead) >= 1, "an edge to a deleted file must still be emitted"
        assert [link for link in dead if link.target_exists is not False] == []
        assert [link for link in dead if link.resolution != "missing"] == []

        alive = _links(graph, target=".claude/hooks/alive.py")
        assert len(alive) >= 1, "positive control: the existing target must also produce an edge"
        assert [link for link in alive if link.target_exists is not True] == []

    def test_missing_target_appears_as_a_node_marked_not_existing(self, tmp_path):
        """実在しない参照先も `nodes` に出て `exists: false` が立つこと（§3）."""
        _mkfile(tmp_path, ".claude/hooks/alive.py", "# alive\n")
        _mkfile(
            tmp_path,
            "CHANGELOG.md",
            "# changelog\n\n`.claude/hooks/alive.py` と `.claude/agents/tdd-develop.md`\n",
        )

        graph = refgraph.build_graph(tmp_path)
        by_id = {node.id: node for node in graph.nodes}

        assert ".claude/agents/tdd-develop.md" in by_id
        assert by_id[".claude/agents/tdd-develop.md"].exists is False
        assert ".claude/hooks/alive.py" in by_id
        assert by_id[".claude/hooks/alive.py"].exists is True

    def test_real_repo_surfaces_at_least_one_dead_reference(self, repo_graph, repo_root):
        """実リポジトリでも `target_exists: false` の辺が出ること（§6 条件 3）.

        `CHANGELOG.md` は削除済みファイルをコードスパンで多数言及しており、
        `.claude/agents/tdd-develop.md` はその 1 つ（tracked ファイルなので
        CI のクリーンチェックアウトでも成立する）。
        """
        assert not (repo_root / ".claude/agents/tdd-develop.md").exists(), (
            "premise changed: tdd-develop.md was resurrected"
        )

        dead = _links(repo_graph, target=".claude/agents/tdd-develop.md")
        assert len(dead) >= 1, "the dead reference documented in CHANGELOG.md was dropped"
        assert [link for link in dead if link.target_exists is not False] == []

    def test_every_link_carries_its_provenance(self, repo_graph):
        """すべての辺が `source` / `source_line` / `context` を持つこと（§2 原則 2）."""
        assert len(repo_graph.links) > 0, "positive control: 辺が 1 本も無い"

        bad_line = [
            (link.source, link.relation, link.source_line)
            for link in repo_graph.links
            if not isinstance(link.source_line, int) or link.source_line < 1
        ]
        assert bad_line[:10] == []

        bad_context = [
            (link.source, link.source_line)
            for link in repo_graph.links
            if not isinstance(link.context, str) or link.context.strip() == ""
        ]
        assert bad_context[:10] == []

    def test_every_link_endpoint_has_a_node(self, repo_graph):
        """`links` の両端が `nodes` に載っていること（§3 の参照整合性）."""
        assert len(repo_graph.links) > 0, "positive control: 辺が 1 本も無い"

        node_ids = _node_ids(repo_graph)
        dangling = sorted(
            {link.source for link in repo_graph.links}
            | {link.target for link in repo_graph.links}
        )
        dangling = [node_id for node_id in dangling if node_id not in node_ids]
        assert dangling[:10] == []


# ===========================================================================
# 契約 §3「解決の順序」/「トークン境界」（2026-08-05 追記）
#
# 実リポジトリでの実測から出た 2 件の欠陥を機械で押さえる:
#   - 部分パス（`dev-workflow/SKILL.md`）を解決せず missing と報告していた
#     （上位 20 件だけで 331 件・読む側が「残骸だ」と誤読する）
#   - 許容集合外の文字でトークンが途切れた続きを独立した参照として採っていた
#     （`-{timestamp}.md` 等・missing 辺の 6%）
# ===========================================================================
class TestResolutionOrder:
    # 題材について: skill 名 / agent 名と衝突する語を使うと `md_bare_skill_name` /
    # `md_bare_agent_name` が発火し、**パス解決を測っているつもりで名前解決を測る**
    # ことになる（実際に初版でこれを踏んだ）。また参照元を対象の祖先ディレクトリ配下に
    # 置くと source 相対で解決してしまい、末尾一致の穴を突けない。
    # そのため「skill でも agent でもない場所」×「祖先関係にない参照元」で組む。

    def test_unique_suffix_match_resolves_and_is_not_missing(self, tmp_path):
        """部分パスが一意なら解決し `basename` になる（契約 §3 解決の順序 3）.

        正の対照（解決できる部分パス）と負の対照（どこにも当たらない部分パス）を
        **同じ fixture** に置く（契約 §7 規律 4）。
        """
        _mkfile(tmp_path, ".claude/docs/deep/note.md", "# deep note\n")
        _mkfile(
            tmp_path,
            ".claude/agent-memory/observer/memo.md",
            "# memo\n"
            "\n"
            "- resolvable partial path: `deep/note.md`\n"
            "- nowhere partial path: `nosuch/note.md`\n",
        )

        graph = refgraph.build_graph(tmp_path)
        src = ".claude/agent-memory/observer/memo.md"

        # Positive: 一意な末尾一致は実在ファイルへ解決する
        resolved = _links(graph, source=src, target=".claude/docs/deep/note.md")
        assert len(resolved) == 1, (
            f"a unique suffix match must resolve to the real file; got {resolved}"
        )
        assert resolved[0].resolution == "basename"
        assert resolved[0].target_exists is True

        # 未解決の生トークンが別途 missing で残っていないこと
        stray = _links(graph, source=src, target="deep/note.md")
        assert stray == [], f"the raw partial path must not remain as its own node; got {stray}"

        # Negative: どこにも当たらない部分パスは missing のまま
        nowhere = _links(graph, source=src, target="nosuch/note.md")
        assert len(nowhere) == 1, f"an unresolvable partial path must stay missing; got {nowhere}"
        assert nowhere[0].resolution == "missing"
        assert nowhere[0].target_exists is False

    def test_suffix_match_with_two_candidates_is_ambiguous(self, tmp_path):
        """末尾一致の候補が複数なら候補ごとに 1 本ずつ出す（§3・選ばない）.

        C3 では `src/c3/_template/` が `.claude/` の複製なので、この形が実際に多発する。

        **distractor を置いてディレクトリ成分が効いていることを識別する。**
        `dup/` を無視して basename `note.md` だけで解決する実装だと、
        `other/note.md` まで候補に入って赤になる（これが無いと「たまたま 2 件」で
        緑になり、解決の質を測れない）。
        """
        _mkfile(tmp_path, ".claude/docs/dup/note.md", "# a\n")
        _mkfile(tmp_path, "src/c3/_template/.claude/docs/dup/note.md", "# b\n")
        _mkfile(tmp_path, ".claude/docs/other/note.md", "# distractor\n")
        _mkfile(
            tmp_path,
            ".claude/agent-memory/observer/memo.md",
            "# memo\n\n- ambiguous partial path: `dup/note.md`\n",
        )

        graph = refgraph.build_graph(tmp_path)
        src = ".claude/agent-memory/observer/memo.md"

        got = sorted(
            link.target
            for link in graph.links
            if link.source == src and link.resolution == "ambiguous"
        )
        assert got == [
            ".claude/docs/dup/note.md",
            "src/c3/_template/.claude/docs/dup/note.md",
        ], f"both candidates must be emitted; got {got}"

    def test_token_broken_by_placeholder_does_not_emit_a_fragment(self, tmp_path):
        """許容集合外の文字で途切れた続きを独立した参照にしない（契約 §3 トークン境界）.

        実在しない参照先の捏造であり「採りすぎ」ではない。
        正の対照（同じファイル内の正常なパス）を同居させ、
        何も抽出しない実装では赤になるようにする。
        """
        _mkfile(tmp_path, ".claude/hooks/real.py", "# real\n")
        _mkfile(
            tmp_path,
            ".claude/skills/caller/SKILL.md",
            "# caller\n"
            "\n"
            "- normal: `.claude/hooks/real.py`\n"
            "- japanese placeholder: `.claude/reports/doc-{名前}-{timestamp}.md`\n"
            "- shell substitution: `.claude/state/e0-$(date +%s)-$$-$RANDOM.txt`\n"
            "- angle placeholder: `.claude/skills/<skill>/templates/<name>-template.md`\n",
        )

        graph = refgraph.build_graph(tmp_path)
        src = ".claude/skills/caller/SKILL.md"

        # Positive: 同じファイルの正常なパスは辺になる
        normal = _links(graph, source=src, target=".claude/hooks/real.py")
        assert len(normal) == 1, f"the normal path in the same file must be an edge; got {normal}"

        # Negative: 断片が出ていないこと
        fragments = sorted(
            link.target
            for link in _links(graph, source=src)
            if PurePosixPath(link.target).name.startswith("-")
        )
        assert fragments == [], f"token fragments must not be emitted as targets; got {fragments}"


# ===========================================================================
# 契約 §6 条件 4: 出力がファイルであること
# ===========================================================================
class TestFileRoundTrip:
    @staticmethod
    def _small_tree(root: Path) -> None:
        _mkfile(root, ".claude/hooks/target.py", "# target\n")
        _mkfile(root, ".claude/agents/wt_good.md", "# ok\n")
        _mkfile(
            root,
            ".claude/skills/good-skill/SKILL.md",
            "# 日本語の見出し\n"
            "\n"
            "参照: `.claude/hooks/target.py`\n"
            "\n"
            "| base | variant |\n"
            "| --- | --- |\n"
            "| `good` | `wt_good` |\n",
        )

    def test_write_then_read_restores_the_same_content(self, tmp_path):
        """`write_graph` → `read_graph` で同じ内容が復元できること."""
        self._small_tree(tmp_path)
        out = tmp_path / "graph.json"

        graph = refgraph.build_graph(tmp_path)
        assert len(graph.links) > 0, "positive control: 往復させる辺が 1 本も無い"

        refgraph.write_graph(graph, out)
        restored = refgraph.read_graph(out)

        assert restored.to_dict() == graph.to_dict()
        assert len(restored.links) == len(graph.links)
        assert len(restored.nodes) == len(graph.nodes)

    def test_written_file_is_utf8_json_matching_to_dict(self, tmp_path):
        """書かれたファイルが UTF-8 の JSON で、`to_dict()` と一致すること.

        Windows 既定の cp932 で書くと、非 ASCII の `context` が
        UTF-8 で読む側で壊れる（本リポジトリで実際に起きた事故）。
        """
        self._small_tree(tmp_path)
        out = tmp_path / "graph.json"

        graph = refgraph.build_graph(tmp_path)
        refgraph.write_graph(graph, out)

        data = json.loads(out.read_bytes().decode("utf-8"))
        assert data == graph.to_dict()

        contexts = [link["context"] for link in data["links"]]
        quoted = [c for c in contexts if "`.claude/hooks/target.py`" in c]
        assert quoted != [], f"context should quote the referencing line; got {contexts!r}"

        # 非 ASCII を含む行が JSON 経由で壊れずに戻ること（cp932 書き込みの検出）。
        # fixture の参照行は「参照: `...`」なので、日本語が生きていれば context に残る。
        assert [c for c in quoted if "参照" in c] != [], (
            f"non-ASCII text was lost on the way to the file: {quoted!r}"
        )

    def test_written_ids_are_root_relative_posix(self, tmp_path):
        """書き出した JSON のノード ID / source / target が POSIX 相対であること."""
        self._small_tree(tmp_path)
        out = tmp_path / "graph.json"

        graph = refgraph.build_graph(tmp_path)
        refgraph.write_graph(graph, out)
        data = json.loads(out.read_bytes().decode("utf-8"))

        assert data["nodes"] != []
        assert data["links"] != []

        ids = (
            [node["id"] for node in data["nodes"]]
            + [link["source"] for link in data["links"]]
            + [link["target"] for link in data["links"]]
            + [entry["path"] for entry in data["skipped"]]
        )
        bad = sorted({i for i in ids if not _is_root_relative_posix(i)})
        assert bad == [], f"ids must be root-relative POSIX: {bad}"


def _is_root_relative_posix(node_id: str) -> bool:
    """ノード ID がルート相対 POSIX（または `sqltable:<name>`）であること（§3）."""
    if node_id.startswith("sqltable:"):
        return bool(node_id[len("sqltable:"):])
    if "\\" in node_id:
        return False
    if node_id.startswith("/") or node_id.startswith("./") or node_id.startswith("../"):
        return False
    if re.match(r"^[A-Za-z]:", node_id):
        return False
    return bool(node_id)


# ===========================================================================
# 契約 §6 条件 5: 判定を含まないこと（機械強制）
# ===========================================================================
class TestNoJudgmentInExtractor:
    """判定が抽出器へ再混入したら赤になること.

    §7 規律 4 に従い、いずれのケースにも「辺が 1 本以上出ている」positive control を
    置く。これが無いと、何も抽出しない実装でも「判定が無い」ので緑になってしまう。
    """

    def test_public_surface_has_no_reachability_question(self, repo_graph):
        """モジュール / Graph の公開面に到達可能性の問いが無いこと."""
        assert len(repo_graph.links) > 0, "positive control: 辺が 1 本も無い"

        names = set(dir(refgraph)) | set(dir(type(repo_graph))) | set(vars(repo_graph))
        found = sorted(names & FORBIDDEN_PUBLIC_NAMES)
        assert found == [], f"judgment leaked back into the extractor surface: {found}"

    def test_module_source_defines_no_entry_point_or_reachability_symbol(self, repo_graph):
        """ソース中に判定・ルート集合を示す定義名が無いこと（AST・非公開名も含む）.

        文字列 grep は docstring / コメントに誤マッチするので使わない
        （契約自身が §9 で `is_reachable` に言及するため、説明の記述は許す）。
        """
        assert len(repo_graph.links) > 0, "positive control: 辺が 1 本も無い"

        source_path = Path(refgraph.__file__)
        tree = ast.parse(source_path.read_text(encoding="utf-8"))

        defined = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.append(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        defined.append(target.id)
                    elif isinstance(target, ast.Attribute):
                        defined.append(target.attr)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                defined.append(node.target.id)
            elif isinstance(node, ast.arg):
                defined.append(node.arg)

        # 走査そのものが働いていることの担保
        assert "build_graph" in defined, f"AST scan found nothing usable: {defined[:20]}"

        offending = sorted({name for name in defined if _JUDGMENT_NAME_RE.search(name)})
        assert offending == [], f"judgment-shaped definitions in the extractor: {offending}"

    def test_build_graph_takes_only_the_root(self, repo_graph, repo_root):
        """`build_graph(root)` の引数が `root` だけであること（§5）.

        ルート集合・エントリポイント・フィルタを引数で外から差し込む余地を残さない。
        判定はクエリ側の責務であり、抽出器は「どこを起点に見るか」を知らない。
        """
        assert len(repo_graph.links) > 0, "positive control: 辺が 1 本も無い"

        params = list(inspect.signature(refgraph.build_graph).parameters)
        assert params == ["root"], f"build_graph must take only 'root'; got {params}"


# ===========================================================================
# 旧テストから移植した負の対照（契約の言葉に翻訳）
# ===========================================================================
class TestMigratedNegativeControls:
    def test_permissions_entry_is_permission_not_hook(self, tmp_path):
        """旧 N-1 の翻訳: `permissions` は `settings_permission`・`settings_hook` ではない.

        争点を「同じファイル・同じディレクトリで、hooks 節にあるか permissions 節に
        あるか」だけに絞る。正の側（hooks 登録）が同じ fixture にあるので、
        辺を 1 本も作らない実装ではこのケースを通過できない（§7 規律 4）。
        `deny` も `permissions` なので併せて対照に置く。
        """
        _mkfile(tmp_path, ".claude/hooks/perm_only_hook.py", "# synthetic\n")
        _mkfile(tmp_path, ".claude/hooks/denied_hook.py", "# synthetic\n")
        _mkfile(tmp_path, ".claude/hooks/registered_hook.py", "# synthetic\n")

        settings = {
            "permissions": {
                "allow": ["Bash(c3 run .claude/hooks/perm_only_hook.py*)"],
                "deny": ["Bash(c3 run .claude/hooks/denied_hook.py*)"],
            },
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "c3",
                                "args": [
                                    "run",
                                    "${CLAUDE_PROJECT_DIR}/.claude/hooks/registered_hook.py",
                                ],
                            }
                        ],
                    }
                ]
            },
        }
        _mkfile(tmp_path, ".claude/settings.json", json.dumps(settings, indent=2) + "\n")

        graph = refgraph.build_graph(tmp_path)

        # Positive: hooks 節の登録は settings_hook になる
        registered = _links(
            graph, relation="settings_hook", target=".claude/hooks/registered_hook.py"
        )
        assert len(registered) >= 1, "a hook registered in the hooks section must yield settings_hook"

        # Positive: permissions は捨てず settings_permission として記録する
        for target in (".claude/hooks/perm_only_hook.py", ".claude/hooks/denied_hook.py"):
            recorded = _links(graph, relation="settings_permission", target=target)
            assert len(recorded) >= 1, f"permissions entry must be recorded: {target}"

        # Negative: permissions は settings_hook にはならない
        leaked = _links(
            graph, relation="settings_hook", target=".claude/hooks/perm_only_hook.py"
        ) + _links(graph, relation="settings_hook", target=".claude/hooks/denied_hook.py")
        assert leaked == [], (
            "permissions is a policy section, not a hook registration; "
            f"got {[(l.source, l.context) for l in leaked]}"
        )

        # Negative（逆向き）: hooks 節の登録は settings_permission にはならない
        misfiled = _links(
            graph, relation="settings_permission", target=".claude/hooks/registered_hook.py"
        )
        assert misfiled == []

    def test_settings_edges_quote_the_reference_in_context(self, tmp_path):
        """`settings*.json` 由来の辺も出所を持つこと（旧実装は `source_line=1` 固定だった）."""
        _mkfile(tmp_path, ".claude/hooks/registered_hook.py", "# synthetic\n")
        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "c3",
                                "args": [
                                    "run",
                                    "${CLAUDE_PROJECT_DIR}/.claude/hooks/registered_hook.py",
                                ],
                            }
                        ],
                    }
                ]
            }
        }
        _mkfile(tmp_path, ".claude/settings.json", json.dumps(settings, indent=2) + "\n")

        graph = refgraph.build_graph(tmp_path)
        hits = _links(
            graph, relation="settings_hook", target=".claude/hooks/registered_hook.py"
        )
        assert len(hits) >= 1

        without_reference = [
            (link.source_line, link.context)
            for link in hits
            if "registered_hook.py" not in link.context
        ]
        assert without_reference == [], (
            "context must quote the referencing line, otherwise the reader cannot narrow "
            f"down the relation (§2 原則 2); got {without_reference}"
        )

    def test_prose_mention_is_bare_agent_name_not_variant_map(self, tmp_path):
        """旧 N-2 の翻訳: 散文の言及は `md_bare_agent_name`・`md_agent_variant_map` ではない.

        写像表の行だけが `md_agent_variant_map`。散文は**捨てず**に
        `md_bare_agent_name` として記録する（旧契約はここで
        `security-reviewer.md` への辺を 0 本にした・§9-3）。
        """
        _mkfile(tmp_path, ".claude/agents/prose_only.md", "# Prose Only Agent\n")
        _mkfile(tmp_path, ".claude/agents/wt_table_row.md", "# Table Row Agent\n")
        _mkfile(
            tmp_path,
            ".claude/skills/test-skill/SKILL.md",
            "# Test Skill\n"
            "\n"
            "困ったときは prose_only を起動してデバッグする。\n"
            "\n"
            "| 元 | 並列バリアント | 備考 |\n"
            "| --- | --- | --- |\n"
            "| `table_row` | `wt_table_row` | worktree 専用 |\n",
        )

        graph = refgraph.build_graph(tmp_path)

        # Positive: 写像表の行は md_agent_variant_map になる
        table = _links(
            graph,
            relation="md_agent_variant_map",
            source=".claude/skills/test-skill/SKILL.md",
            target=".claude/agents/wt_table_row.md",
        )
        assert len(table) >= 1, "a table row must yield md_agent_variant_map"

        # Positive: 散文は捨てずに md_bare_agent_name として記録される
        prose = _links(
            graph,
            relation="md_bare_agent_name",
            source=".claude/skills/test-skill/SKILL.md",
            target=".claude/agents/prose_only.md",
        )
        assert len(prose) >= 1, "a prose mention must be recorded as md_bare_agent_name"

        # Negative: 散文は md_agent_variant_map にはならない
        misfiled = _links(
            graph,
            relation="md_agent_variant_map",
            target=".claude/agents/prose_only.md",
        )
        assert misfiled == [], (
            "a prose mention is not a variant mapping row; "
            f"got {[(l.source, l.source_line, l.context) for l in misfiled]}"
        )


# ===========================================================================
# tester 自作の反例
# ===========================================================================
class TestCounterexamples:
    def test_counterexample_1_all_four_resolution_values(self, tmp_path):
        """反例 1: 曖昧な参照でどれかを選ばず候補ごとに 1 本ずつ出すこと（§3）.

        `exact` / `basename` / `ambiguous` / `missing` を 1 つの fixture に同居させる。
        素朴な実装は「最初に見つかった候補」を 1 本だけ出して曖昧さを潰す。
        """
        _mkfile(tmp_path, ".claude/hooks/exact_target.py", "# exact\n")
        _mkfile(tmp_path, ".claude/hooks/unique_name.py", "# unique\n")
        _mkfile(tmp_path, ".claude/hooks/dup.py", "# dup A\n")
        _mkfile(tmp_path, ".claude/skills/s/scripts/dup.py", "# dup B\n")
        _mkfile(
            tmp_path,
            ".claude/skills/s/SKILL.md",
            "# S\n"
            "\n"
            "- exact: `.claude/hooks/exact_target.py`\n"
            "- basename: `unique_name.py`\n"
            "- ambiguous: `dup.py`\n"
            "- missing: `.claude/hooks/gone.py`\n",
        )

        graph = refgraph.build_graph(tmp_path)
        src = ".claude/skills/s/SKILL.md"

        exact = _links(graph, source=src, target=".claude/hooks/exact_target.py")
        assert len(exact) == 1, f"expected exactly one exact edge; got {exact}"
        assert exact[0].resolution == "exact"
        assert exact[0].target_exists is True

        basename = _links(graph, source=src, target=".claude/hooks/unique_name.py")
        assert len(basename) == 1, f"expected exactly one basename edge; got {basename}"
        assert basename[0].resolution == "basename"

        ambiguous = sorted(
            link.target
            for link in graph.links
            if link.source == src and link.resolution == "ambiguous"
        )
        assert ambiguous == [
            ".claude/hooks/dup.py",
            ".claude/skills/s/scripts/dup.py",
        ], f"an ambiguous reference must emit one edge per candidate; got {ambiguous}"

        missing = _links(graph, source=src, target=".claude/hooks/gone.py")
        assert len(missing) == 1, f"expected one missing edge; got {missing}"
        assert missing[0].resolution == "missing"
        assert missing[0].target_exists is False

    def test_counterexample_2_context_is_sanitised_and_bounded(self, tmp_path):
        """反例 2: `context` に制御文字を残さず、長さ上限で切ること（§3）.

        外部由来文字列をそのまま埋め込むと、読む側の端末を壊すか JSON を肥大させる。
        20 万文字の 1 行を用意し、上限で切られることを機械で押さえる。
        """
        _mkfile(tmp_path, ".claude/hooks/target.py", "# target\n")
        padding = "x" * 200_000
        _mkfile(
            tmp_path,
            ".dev/notes.md",
            "# notes\n"
            "\n"
            "参照 `.claude/hooks/target.py` \x07 \x1b " + padding + "\n",
        )

        graph = refgraph.build_graph(tmp_path)
        hits = _links(graph, source=".dev/notes.md", target=".claude/hooks/target.py")
        assert len(hits) >= 1, "positive control: 辺が出ていない"

        for link in hits:
            control = sorted({repr(c) for c in link.context if ord(c) < 32})
            assert control == [], f"context must strip control characters; got {control}"
            # 上限値そのものは契約に数値がないため、tester が「どんな妥当な上限でも
            # 通る」値として 10000 を置く。無切り詰め実装だけが赤になる。
            assert len(link.context) <= 10_000, (
                f"context must be truncated; got {len(link.context)} chars"
            )

    def test_counterexample_3_ids_are_root_relative_posix_in_the_real_repo(self, repo_graph):
        """反例 3: Windows で ID にバックスラッシュや絶対パスが混ざらないこと（§3）.

        `str(path.relative_to(root))` は Windows で `\\` を返す。混ざると読む側で
        突合できず、出力ファイルの価値が消える。実リポジトリの深い階層で確かめる。
        """
        assert len(repo_graph.nodes) > 0 and len(repo_graph.links) > 0

        ids = (
            _node_ids(repo_graph)
            | {link.source for link in repo_graph.links}
            | {link.target for link in repo_graph.links}
            | set(_skipped_paths(repo_graph))
        )
        bad = sorted({i for i in ids if not _is_root_relative_posix(i)})
        assert bad[:10] == [], f"ids must be root-relative POSIX: {bad[:10]}"

        # 平坦な名前だけを出す実装（階層を潰した実装）を弾く
        assert any("/" in i for i in ids), "no nested id at all — separators were flattened?"

    def test_counterexample_4_ast_import_ignores_comments_and_strings(self, tmp_path):
        """反例 4: import は AST で取り、コメント・文字列中の import を拾わないこと（§4）.

        複数行括弧付き import（拾うべき）と、コメント / 文字列リテラルの中の
        import（拾ってはいけない）を同じファイルに置いて両側を測る。
        """
        for name in ("alpha", "beta", "gamma", "delta"):
            _mkfile(tmp_path, f"pkg/{name}.py", f"# {name}\n")
        _mkfile(tmp_path, "pkg/__init__.py", "")
        _mkfile(
            tmp_path,
            "pkg/main.py",
            "from . import (\n"
            "    alpha,\n"
            "    beta,\n"
            ")\n"
            "# import gamma\n"
            'DOC = "from . import delta"\n',
        )

        graph = refgraph.build_graph(tmp_path)
        got = _targets(graph, "py_import", "pkg/main.py")

        missing = sorted({"pkg/alpha.py", "pkg/beta.py"} - got)
        assert missing == [], (
            f"a line-based regex drops everything after the opening paren; missing {missing}"
        )

        leaked = _links(graph, relation="py_import", source="pkg/main.py", target="pkg/gamma.py")
        leaked += _links(graph, relation="py_import", source="pkg/main.py", target="pkg/delta.py")
        assert leaked == [], (
            "an import inside a comment or a string literal is not an import; "
            f"got {[(l.source_line, l.context) for l in leaked]}"
        )

    def test_counterexample_5_bare_names_resolve_against_the_actual_inventory(self, tmp_path):
        """反例 5: 素の名前は実在する agent / skill にだけ解決すること.

        `md_bare_agent_name` / `md_bare_skill_name` は「散文中の語」を拾うため、
        agent 名を**ハードコードした一覧**で解決する実装だと、その一覧に載っている
        名前が本ツリーに存在しなくても辺を作ってしまう。実リポジトリに実在する
        `tester` / `dev-workflow` を囮に置き、本ツリーには置かないことで検出する。
        """
        _mkfile(tmp_path, ".claude/agents/real_agent.md", "# real\n")
        _mkfile(tmp_path, ".claude/skills/known-skill/SKILL.md", "# known\n")
        _mkfile(
            tmp_path,
            ".dev/notes.md",
            "# notes\n"
            "\n"
            "real_agent を起動し、その後 /known-skill を使う。\n"
            "なお tester や nonexistent_agent、/dev-workflow はこのツリーに存在しない。\n",
        )

        graph = refgraph.build_graph(tmp_path)

        agent_hit = _links(
            graph, relation="md_bare_agent_name", target=".claude/agents/real_agent.md"
        )
        assert len(agent_hit) >= 1, "positive control: 実在 agent への辺が無い"

        skill_hit = _links(
            graph,
            relation="md_bare_skill_name",
            target=".claude/skills/known-skill/SKILL.md",
        )
        assert len(skill_hit) >= 1, "positive control: 実在 skill への辺が無い"

        phantom = [
            link
            for link in graph.links
            if link.target
            in {
                ".claude/agents/tester.md",
                ".claude/agents/nonexistent_agent.md",
                ".claude/skills/dev-workflow/SKILL.md",
            }
        ]
        assert phantom == [], (
            "bare names must resolve against the agents/skills that actually exist in the "
            f"scanned tree, not a hardcoded list; got {[(l.relation, l.target) for l in phantom]}"
        )

    def test_counterexample_6_source_line_points_at_the_referencing_line(self, tmp_path):
        """反例 6: `source_line` が参照している行を指すこと（§2 原則 2）.

        定数（1 等）を入れる実装でも、辺の本数を見るテストは全部緑になる。
        出所が嘘なら読む側は絞れないので、行番号そのものを固定する。
        """
        _mkfile(tmp_path, ".claude/hooks/a.py", "# a\n")
        _mkfile(tmp_path, ".claude/hooks/b.py", "# b\n")
        _mkfile(
            tmp_path,
            ".dev/notes.md",
            "# notes\n"  # 1
            "\n"  # 2
            "最初の参照 `.claude/hooks/a.py`\n"  # 3
            "\n"  # 4
            "間に挟まる本文\n"  # 5
            "二番目の参照 `.claude/hooks/b.py`\n",  # 6
        )

        graph = refgraph.build_graph(tmp_path)

        lines = {
            link.target: link.source_line
            for link in _links(graph, source=".dev/notes.md")
            if link.target in {".claude/hooks/a.py", ".claude/hooks/b.py"}
        }
        assert lines == {".claude/hooks/a.py": 3, ".claude/hooks/b.py": 6}, (
            f"source_line must point at the referencing line; got {lines}"
        )


# ===========================================================================
# トークナイザ 2 読み化 ＋ `_dedupe` 4 フィールド化（test-tokenizer タスク）
#
# 規則の正:
#   - `.claude/reports/architecture-report-20260806-173941.md` §4-6（改訂 14・逐語）
#   - `docs/refgraph-contract.md` §3（トークン境界・解決の順序）
#   - `.dev/handoff-20260806-refgraph-design.md` §1-6（トークナイザ細部の実測）／§3（2 読み構成）
#   - `.dev/refgraph-scratch/README.md` 末尾「既知の欠陥」
#     （テストは設計側に合わせる: R3 =「正規化後も `*` を含み、かつ既知拡張子か `/` を持つ」／
#      S3 = 固定点まで繰り返す）
#
# fixture 規律: 同じ target に解決する形を同じ source ファイルへ同居させると
# `_dedupe`（改訂後は (relation, source, target, reference) の 4 フィールド）で
# 潰れて恒久赤になる実測がある（handoff §2・監査 8 周目）。**同じ target を持つ形は
# 必ず別ファイルへ分ける**（異なる target を持つ形は同居させてよい）。
# ===========================================================================
class TestTwoReadingTokenizer:
    """V-37 (a)〜(e)。`*` の役割を判定せず、読み A（`*` を本体に含める）と
    読み B（含めない・現行のトークナイズそのもの）を両方通し、和を最終出力とする。
    """

    # -- (a)(b) 単独 `*` 5 形 ＋ `**` 3 形（読み B ＝現行が既に採れる・正の対照） -------
    #
    # 8 形すべてが同じ target（`stop.py`）に解決するため、1 形 1 ファイルへ分ける
    # （架構レポート §2 監査 8 周目・AC-62 (iii) が同じ罠を踏んだ）。
    _SINGLE_AND_DOUBLE_STAR_FORMS = {
        # (a) 単独 `*` 5 形
        "a1_bare": "*stop.py*",
        "a2_prefix_suffix_jp": "実装*stop.py*を読む",
        "a3_trailing_word": "*stop.py*foo",
        "a4_trailing_hyphen_word": "*stop.py*-bar",
        "a5_trailing_space": "*stop.py* 次",
        # (b) `**` 3 形
        "b1_prefix_suffix_jp": "実装**stop.py**を読む",
        "b2_bare": "**stop.py**",
        "b3_trailing_word": "**stop.py**foo",
    }

    def test_single_and_double_star_forms_all_resolve_to_stop_py(self, tmp_path):
        """V-37 (a)(b): 単独 `*` 5 形・`** ` 3 形のすべてが `stop.py` を含む.

        読み B は現行のトークナイズそのもの（架構レポート §4-2 事実 1/2）なので、
        この 8 形は**現行実装で既に緑**（回帰・正の対照）。読み A の追加で壊れないことを縛る。
        """
        _mkfile(tmp_path, "stop.py", "# stop\n")
        for name, form in self._SINGLE_AND_DOUBLE_STAR_FORMS.items():
            _mkfile(tmp_path, f"{name}.md", f"# {name}\n\n見本: `{form}`\n")

        graph = refgraph.build_graph(tmp_path)

        missing_forms = []
        for name in self._SINGLE_AND_DOUBLE_STAR_FORMS:
            src = f"{name}.md"
            hits = _links(graph, source=src, target="stop.py")
            if len(hits) == 0:
                missing_forms.append(name)
        assert missing_forms == [], (
            f"these forms must still resolve to stop.py after the 2-reading change: {missing_forms}"
        )

    # -- (c) グロブ 4 形（読み A でしか採れない・現行は Red） -----------------------
    def test_glob_forms_keep_the_raw_pattern_as_a_single_token(self, tmp_path):
        """V-37 (c): グロブの原文が 1 トークンとして `missing` の辺で出ること.

        現行実装は `*` を本体クラスに含めないため、これら 4 形は今 0 本（Red）。
        `.claude/skills/*/scripts/**/*.py` は架構レポート §4-5 の検算表にある形で、
        読み B の `.claude/skills`（R2・ディレクトリ形）と読み A の原文が両方出る
        （改訂 12 の成果「185 → 1」が壊れていないことも合わせて縛る）。
        """
        glob_forms = [
            "reports/*-{ts}.md",
            "src/*/x.py",
            "src/**/*.py",
            ".claude/skills/*/scripts/**/*.py",
        ]
        _mkfile(
            tmp_path,
            "globs.md",
            "# globs\n\n" + "\n".join(f"- `{form}`" for form in glob_forms) + "\n",
        )

        graph = refgraph.build_graph(tmp_path)
        src = "globs.md"

        missing_forms = []
        for form in glob_forms:
            hits = _links(graph, source=src, target=form)
            if len(hits) == 0:
                missing_forms.append(form)
        assert missing_forms == [], (
            f"the raw glob token must survive as a single missing-resolution edge: {missing_forms}"
        )
        for form in glob_forms:
            hits = _links(graph, source=src, target=form)
            assert hits, f"unreachable unless previous assert failed for {form!r}"
            assert hits[0].resolution == "missing"
            assert hits[0].target_exists is False

        # 改訂 12 の成果（架構レポート §4-5 の注）: 深い形でも `.claude/skills` は R2 で残る。
        dir_hits = _links(graph, source=src, target=".claude/skills")
        assert len(dir_hits) >= 1, (
            "reading B must still emit the directory-form edge for the nested glob "
            "(this is regression coverage for the 185->1 fix, not new behaviour)"
        )

    def test_glob_forms_do_not_leak_root_external_fragments(self, tmp_path):
        """V-37 (d): 断片非生成 3 形（`/*.py` / `/SKILL.md` / `/*.md`）が target に出ないこと.

        契約 §7 規律 4 に従い、同じ fixture に正の対照（グロブ原文が 1 トークンで出る）を
        置く。正が Red のうちは負の対照は無意味（採らずに 0 本というだけ）だが、
        グロブ原文の捕捉が実装された後もこの負が成立し続けることを機械で縛るために
        同居させる。
        """
        _mkfile(
            tmp_path,
            "globs.md",
            "# globs\n\n"
            "- `.claude/skills/*/scripts/**/*.py`\n"
            "- `/*.md`\n",
        )

        graph = refgraph.build_graph(tmp_path)
        src = "globs.md"

        # Positive（対照）: グロブ原文はそのまま 1 トークンとして出る。
        positive = _links(
            graph, source=src, target=".claude/skills/*/scripts/**/*.py"
        )
        assert len(positive) >= 1, "positive control: the raw glob token must be captured"

        # Negative: ルート外絶対パス風の断片は 1 本も出ない。
        fragments = _links(graph, source=src, target="/*.py")
        fragments += _links(graph, source=src, target="/SKILL.md")
        fragments += _links(graph, source=src, target="/*.md")
        assert fragments == [], f"fragment targets must never be emitted: {fragments}"

    # -- (e) S3 固定点 3 形（handoff §1-6 の実測どおり） ---------------------------
    #
    # `stop.py*.` / `stop.py*}` / `stop.py**` は**読み B だけでも** `stop.py` に
    # 解決してしまう（架構レポート §5-1 の注: 「固定点は喪失防止としては冗長」）ため、
    # target だけを見る検査は現行実装でも空の緑になりうる（過去に踏んだ失敗と同型）。
    # 読み A から生まれた辺だけを `reference`（S3 正規化前の原文断片）で名指しして縛る。
    _FIXED_POINT_FORMS = {
        "e1_dot": "stop.py*.",
        "e2_brace": "stop.py*}",
        "e3_double_star": "stop.py**",
    }

    def test_s3_fixed_point_forms_normalize_to_stop_py_via_reading_a(self, tmp_path):
        """V-37 (e): 読み A の辺が `stop.py*.` 等の原文を `reference` に持ち、
        `target` は固定点まで正規化された `stop.py` になること.

        `.dev/refgraph-scratch/README.md` の既知の欠陥（S3 が 1 回適用）だと
        `target == "stop.py*"` の余分な辺が残る。本テストは `Link.reference`
        （現行に存在しない・改訂 14 §4-6 で新設）を直接見るため、現行実装では
        `AttributeError` で Red になる（フィールド未実装が理由）。
        """
        for name, form in self._FIXED_POINT_FORMS.items():
            _mkfile(tmp_path, f"{name}.md", f"# {name}\n\nsee `{form}` here.\n")
        _mkfile(tmp_path, "stop.py", "# stop\n")

        graph = refgraph.build_graph(tmp_path)

        for name, form in self._FIXED_POINT_FORMS.items():
            src = f"{name}.md"
            hits = _links(graph, source=src, target="stop.py")
            assert len(hits) == 2, (
                f"{name}: expected 2 edges (reading A + reading B) targeting stop.py; "
                f"got {len(hits)}: {hits}"
            )
            references = {link.reference for link in hits}
            assert references == {"stop.py", form}, (
                f"{name}: reading A must carry the pre-normalization run {form!r} as "
                f"reference, reading B must carry stop.py; got {references}"
            )

            # 読み A 側で `target == "stop.py*"`（固定点に達しない中間形）が
            # 別の辺として漏れていないこと（1 回適用バグの検出）。
            leaked = _links(graph, source=src, target="stop.py*")
            assert leaked == [], (
                f"{name}: a non-fixed-point intermediate target leaked through: {leaked}"
            )


# ===========================================================================
# R3 受理条件単体（V-41）
#
# `stop.py*`（正規化後も `*` を含み、拡張子はあるが `/` は無い形）が受理されること。
# `.dev/refgraph-scratch/README.md` の既知の欠陥は
# `accept()` が `"*" in t and "/" in t` で実装されている（`/` を要求する）ことで、
# これだと拡張子はあっても `/` の無い形が全部拒否される。
#
# R3 受理条件は実装の関数として公開されていないため（契約 §5 の公開 API に含まれない）、
# トークン → 辺の end-to-end で同値の検査を書く（plan-report test-tokenizer の指示）。
#
# 題材は S3 が最終的に手を出せない（末尾に `*` 等の除去対象文字を置かない）形にして、
# 「読み A が受理するか」だけを測る。4 形とも target 文字列が異なるので 1 ファイルに同居できる。
# ===========================================================================
class TestR3AcceptancePredicateWithoutSlash:
    _NO_SLASH_WITH_EXTENSION_FORMS = {
        "middle": "sto*p.py",
        "prefix": "s*top.py",
        "double_middle": "st*o*p.py",
        "before_extension": "stop*.py",
    }

    def test_star_containing_tokens_without_slash_but_with_extension_are_accepted(
        self, tmp_path
    ):
        """拡張子あり・`/` なしの 4 形が `missing` 辺として出ること（R3 述語の直接検査）.

        現行実装は読み A 自体を持たないため、この 4 形は今 0 本（Red）。
        バグ版 R3（`/` を要求する）でも同様に 0 本になるため、この Red は
        「読み A 未実装」と「R3 が `/` を要求する既知の欠陥」の両方を同時に検出する。
        """
        _mkfile(
            tmp_path,
            "notes.md",
            "# notes\n\n"
            + "\n".join(
                f"- {name}: `{form}`"
                for name, form in self._NO_SLASH_WITH_EXTENSION_FORMS.items()
            )
            + "\n",
        )

        graph = refgraph.build_graph(tmp_path)
        src = "notes.md"

        missing_forms = []
        for name, form in self._NO_SLASH_WITH_EXTENSION_FORMS.items():
            hits = _links(graph, source=src, target=form)
            if len(hits) == 0:
                missing_forms.append((name, form))
        assert missing_forms == [], (
            f"R3 must accept extension-bearing, slash-less star tokens: {missing_forms}"
        )
        for name, form in self._NO_SLASH_WITH_EXTENSION_FORMS.items():
            hits = _links(graph, source=src, target=form)
            assert hits, f"unreachable unless previous assert failed for {name}"
            assert hits[0].resolution == "missing"


# ===========================================================================
# 断片非生成（ルート外絶対パス）— 合成ツリー全体で検査（plan-report item 3）
#
# `TestTwoReadingTokenizer.test_glob_forms_do_not_leak_root_external_fragments` が
# 単一ファイル・3 リテラルに絞った検査であるのに対し、本クラスは**独立した合成ツリー**で
# edges と nodes の両方を見る（V-34: 185 件の断片は S1 の run ＋ S2 の分割が由来。
# 改訂 14 で S2 を廃止した結果、構造的に発生しなくなったことを確認する）。
# ===========================================================================
class TestFragmentNonGenerationSyntheticTree:
    def test_no_edge_or_node_id_is_a_root_external_absolute_path(self, tmp_path):
        """ネストしたグロブ 2 形を置いても、`/` から始まる辺・ノードが 1 本も出ないこと."""
        _mkfile(
            tmp_path,
            "nested_a.md",
            "# nested a\n\n参照: `.claude/skills/*/scripts/**/*.py`\n",
        )
        _mkfile(
            tmp_path,
            "nested_b.md",
            "# nested b\n\n参照: `src/c3/_template/**/*.py`\n",
        )

        graph = refgraph.build_graph(tmp_path)

        # Positive（対照）: グロブ原文自体は捕捉される。
        positive_a = _links(
            graph, source="nested_a.md", target=".claude/skills/*/scripts/**/*.py"
        )
        positive_b = _links(
            graph, source="nested_b.md", target="src/c3/_template/**/*.py"
        )
        assert len(positive_a) >= 1, "positive control: nested_a's raw glob must be captured"
        assert len(positive_b) >= 1, "positive control: nested_b's raw glob must be captured"

        # Negative: ルート外絶対パス形の辺・ノードが 1 本も無いこと。
        root_external_link_targets = sorted(
            {link.target for link in graph.links if link.target.startswith("/")}
        )
        assert root_external_link_targets == [], (
            f"no link target may be a root-external absolute path fragment: "
            f"{root_external_link_targets}"
        )

        root_external_node_ids = sorted(
            node.id for node in graph.nodes if node.id.startswith("/")
        )
        assert root_external_node_ids == [], (
            f"no node id may be a root-external absolute path fragment: {root_external_node_ids}"
        )


# ===========================================================================
# `_dedupe` の 4 フィールド化（(relation, source, target, reference)）
#
# 現行の `_dedupe` は (relation, source, target) の 3 フィールド（`refgraph.py:937`）。
# 改訂 14 §4-6 は `reference`（辺を生んだ読みの原文断片）を key に足す。
# fixture は 1 形 1 ファイル（plan-report の fixture 規律）。
# ===========================================================================
class TestDedupeFourFields:
    def test_same_relation_source_target_with_different_reference_both_survive(
        self, tmp_path
    ):
        """同一 (relation, source, target) で `reference` が違う 2 本が両方残ること.

        `.claude/hooks/stop.py*` は読み B で `.claude/hooks/stop.py`（`*` の手前で
        止まる・現行の振る舞い）、読み A では `*` を含む run が S3 で `stop.py*` が
        削られて同じ `.claude/hooks/stop.py` に解決する（架構レポート §9 項目 8 の
        実測例そのもの）。3 フィールド dedupe だと 1 本に潰れる（現行は実際に 1 本）。
        """
        _mkfile(tmp_path, ".claude/hooks/stop.py", "# stop\n")
        _mkfile(
            tmp_path,
            "notes.md",
            "# notes\n\nsee `.claude/hooks/stop.py*` for details.\n",
        )

        graph = refgraph.build_graph(tmp_path)

        hits = _links(graph, source="notes.md", target=".claude/hooks/stop.py")
        assert len(hits) == 2, (
            f"expected reading A and reading B to both survive dedupe; got {len(hits)}: {hits}"
        )

        relations = {link.relation for link in hits}
        sources = {link.source for link in hits}
        assert relations == {"md_code_span_path"}, f"relation must be identical: {relations}"
        assert sources == {"notes.md"}, f"source must be identical: {sources}"

        references = {link.reference for link in hits}
        assert references == {".claude/hooks/stop.py*", ".claude/hooks/stop.py"}, (
            f"the two surviving edges must be distinguished by reference; got {references}"
        )

    def test_identical_reference_repeated_in_the_same_file_still_collapses_to_one(
        self, tmp_path
    ):
        """同じ (relation, source, target, reference) の繰り返しは今までどおり 1 本に潰れること.

        4 フィールド化が dedupe そのものを無効化していない（何でも残す実装への転落）
        ことの負の対照。正の対照（前段のテスト）と対になる。
        """
        _mkfile(tmp_path, ".claude/hooks/real.py", "# real\n")
        _mkfile(
            tmp_path,
            "repeats.md",
            "# repeats\n\n"
            "1 回目: `.claude/hooks/real.py`\n"
            "2 回目: `.claude/hooks/real.py`\n"
            "3 回目: `.claude/hooks/real.py`\n",
        )

        graph = refgraph.build_graph(tmp_path)

        hits = _links(graph, source="repeats.md", target=".claude/hooks/real.py")
        assert len(hits) == 1, (
            f"identical (relation, source, target, reference) must still collapse to one; "
            f"got {len(hits)}: {hits}"
        )


# ===========================================================================
# 非 ASCII 境界（契約 §3「境界として許す側の列挙が本体」の回帰）
# ===========================================================================
class TestNonAsciiTokenBoundaryRegression:
    def test_skill_md_is_extracted_after_japanese_punctuation(self, tmp_path):
        """`…という表示になる。SKILL.md の引用では…` から `SKILL.md` が採れること.

        契約 §3 の実測: ASCII だけの境界実装は正当な参照 39 件を落とした
        （`。` の直後から新しいトークンを開始できなかった）。
        現行実装は `unicodedata.category` の先頭が `P`/`Z` を区切りとして扱う
        修正が既に入っているため、この形は**現行実装で既に緑**（回帰・正の対照）。
        """
        _mkfile(tmp_path, ".claude/skills/x/SKILL.md", "# x\n")
        _mkfile(
            tmp_path,
            "prose.md",
            "# prose\n\n`…という表示になる。SKILL.md の引用では…`\n",
        )

        graph = refgraph.build_graph(tmp_path)

        hits = _links(
            graph,
            source="prose.md",
            target=".claude/skills/x/SKILL.md",
        )
        assert len(hits) >= 1, (
            "SKILL.md must be extracted as its own token right after the Japanese "
            "punctuation '。', not silently merged into the preceding run"
        )


# ===========================================================================
# test-ac-gaps タスク: `.claude/reports/test-report-ac-reconcile.md` の
# 「要追加」27 件のテスト化（Red フェーズ）。
#
# 契約は `docs/refgraph-contract.md`（C-1〜C-24 反映済み）。AC 条文は
# `docs/refgraph-acceptance.md`（凍結済み素材集）。実装（`src/c3/refgraph.py`）は
# 編集していない。以下は実測の結果、複数の genuine な実装欠陥を発見している
# （kind が "dir" を一切返さない・ディレクトリが実在索引に入らない・T-5/T-5b が
# 未実装・sqltable の CREATE TABLE 索引が無い・未知の変数プレフィクスが沈黙する等）。
# これらはテストを弱めず、契約の言葉のまま assert している（Red の帰属は
# test-report を参照）。
# ===========================================================================
class TestNodeKindDirectoryTrailingSlashForm(object):
    """AC-2: `.claude/docs/`（R2・末尾 `/` 付き原文）が `exact`・`kind: "dir"` へ解決すること."""

    def test_trailing_slash_directory_reference_resolves_exact_with_dir_kind(self, tmp_path):
        _mkfile(tmp_path, ".claude/docs/note.md", "# note\n")
        _mkfile(
            tmp_path,
            "caller.md",
            "# caller\n\n参照: `.claude/docs/` を見よ。\n",
        )

        graph = refgraph.build_graph(tmp_path)
        hits = _links(graph, source="caller.md", target=".claude/docs")
        assert len(hits) >= 1, (
            "a trailing-slash directory reference must resolve to '.claude/docs'; this is "
            "missing under the current implementation, which indexes only files "
            "(directories never appear in self.present)"
        )
        assert [link for link in hits if link.resolution != "exact"] == [], (
            f"an existing directory reference must resolve exact; got {hits}"
        )

        by_id = {node.id: node for node in graph.nodes}
        assert ".claude/docs" in by_id, "the directory must appear as a node"
        assert by_id[".claude/docs"].kind == "dir", (
            "an existing directory node must have kind == 'dir' (contract C-3); the current "
            f"implementation never assigns kind='dir' anywhere; got {by_id['.claude/docs'].kind!r}"
        )


class TestNoTrailingSlashDirectoryReference:
    """AC-31: 末尾 `/` 無しの多成分ディレクトリ参照が `exact`・`kind: "dir"` へ解決すること."""

    def test_directory_reference_without_trailing_slash_resolves_exact_with_dir_kind(
        self, tmp_path
    ):
        _mkfile(tmp_path, ".claude/hooks/stop.py", "# stop\n")
        _mkfile(
            tmp_path,
            "caller.md",
            "# caller\n\n[hooks ディレクトリ](.claude/hooks) を見よ。\n",
        )

        graph = refgraph.build_graph(tmp_path)
        hits = _links(graph, relation="md_link", source="caller.md", target=".claude/hooks")
        assert len(hits) >= 1, (
            "a directory reference without a trailing slash must still resolve to the "
            "existing directory (contract §3 ノード ID・C-11); this is missing entirely under "
            "the current implementation (no directory index is ever built, and _md_links "
            "suppresses extensionless 'missing' targets so nothing survives)"
        )
        assert all(link.resolution == "exact" for link in hits)

        by_id = {node.id: node for node in graph.nodes}
        assert ".claude/hooks" in by_id
        assert by_id[".claude/hooks"].kind == "dir"
        assert not any(node.id == ".claude/hooks/" for node in graph.nodes), (
            "the node id must not carry a trailing slash"
        )


class TestGlobResidueDirectoryReference:
    """AC-3: `skills/worktree-tdd-workflow/*` の残骸が `missing`・`kind: "dir"` で辺になること."""

    def test_excludes_py_glob_residue_is_a_missing_dir_edge_with_original_reference(
        self, repo_root, repo_graph
    ):
        text = (repo_root / "src" / "c3" / "_excludes.py").read_text(encoding="utf-8")
        assert '"skills/worktree-tdd-workflow/*"' in text, (
            "premise gone: _excludes.py から skills/worktree-tdd-workflow/* の記載が消えた"
        )
        assert not (repo_root / ".claude" / "skills" / "worktree-tdd-workflow").exists(), (
            "premise changed: worktree-tdd-workflow が復活した（v2.1.0 で廃止済みのはず）"
        )

        hits = _links(
            repo_graph,
            source="src/c3/_excludes.py",
            target="skills/worktree-tdd-workflow",
        )
        assert len(hits) >= 1, (
            "the glob residue must survive as a missing edge to the directory prefix"
        )
        bad_resolution = [link for link in hits if link.resolution != "missing"]
        assert bad_resolution == [], f"expected missing; got {bad_resolution}"

        references = {link.reference for link in hits}
        assert "skills/worktree-tdd-workflow/*" in references, (
            f"the original glob token must be preserved verbatim as reference; got {references}"
        )

        by_id = {node.id: node for node in repo_graph.nodes}
        assert "skills/worktree-tdd-workflow" in by_id
        assert by_id["skills/worktree-tdd-workflow"].kind == "dir", (
            "a residue directory reference must be classified as kind == 'dir' "
            f"(contract C-3); got {by_id['skills/worktree-tdd-workflow'].kind!r}"
        )
        assert not any(
            node.id == "skills/worktree-tdd-workflow/" for node in repo_graph.nodes
        ), "the node id must not carry a trailing slash"


class TestGlobExtensionOnlyNegativeControl:
    """AC-8(3) / AC-33: `*.md` から `target == ".md"` の辺が出ないこと（負の対照＋正の双子）.

    `docs/refgraph-acceptance.md` の AC-33 は「AC-8(3) と同一の検査であり、二重に書かない」
    と明記しているため、AC-33 専用のテストは別途起こさない（本テストで両方を担保する）。
    AC-8 の走査ツリー条件（`stop.py` をルート直下に実在させる）も満たす。
    """

    def test_bare_extension_glob_produces_no_edge_while_the_real_twin_does(self, tmp_path):
        _mkfile(tmp_path, "stop.py", "# stop\n")
        _mkfile(tmp_path, "docs/real.md", "# real\n")
        _mkfile(
            tmp_path,
            "a3.md",
            "# a3\n\n`*.md`\n\n`docs/real.md`\n",
        )

        graph = refgraph.build_graph(tmp_path)
        src = "a3.md"

        bare = _links(graph, source=src, target=".md")
        assert bare == [], f"a bare extension-only glob must not produce a '.md' edge; got {bare}"

        real = _links(graph, source=src, target="docs/real.md")
        assert len(real) >= 1, "positive control: the real file reference must produce an edge"


class TestPrefixTruncationComponentBoundary:
    """AC-10: T-5 が成分境界で刻み、末尾 `/` を含まない前置詞を辺にすること."""

    def test_component_followed_by_word_truncates_to_the_accepted_prefix(self, tmp_path):
        _mkfile(tmp_path, "other/hook/SKILL.md", "# hook skill doc\n")
        _mkfile(
            tmp_path,
            "notes.md",
            "# notes\n\n参照: `hook/SKILL.md/release 運用` を見よ。\n",
        )
        graph = refgraph.build_graph(tmp_path)
        hits = _links(
            graph,
            relation="md_code_span_path",
            source="notes.md",
            target="other/hook/SKILL.md",
        )
        assert len(hits) >= 1, (
            "T-5 must truncate the unacceptable run at a component boundary and emit an edge "
            "for 'hook/SKILL.md' (without a trailing slash); this is missing because the "
            "current tokenizer only accepts a run in full (R1/R2/R3 on the whole run) and has "
            "no fallback that retries shorter component-boundary prefixes"
        )
        assert all(link.resolution == "basename" for link in hits)
        assert all(not link.target.endswith("/") for link in hits)


class TestBraceGroupToken:
    """AC-11: `{SKILL.md,scripts/mode_line.py}` から `scripts/mode_line.py` が解決されること."""

    def test_brace_group_second_member_resolves(self, tmp_path):
        _mkfile(tmp_path, ".claude/skills/x/SKILL.md", "# x skill\n")
        _mkfile(tmp_path, ".claude/skills/x/scripts/mode_line.py", "# mode line\n")
        _mkfile(
            tmp_path,
            "notes.md",
            "# notes\n\n変更対象: `{SKILL.md,scripts/mode_line.py}`\n",
        )

        graph = refgraph.build_graph(tmp_path)
        hits = _links(
            graph, source="notes.md", target=".claude/skills/x/scripts/mode_line.py"
        )
        assert len(hits) >= 1, (
            "the second brace-group member 'scripts/mode_line.py' must resolve to an "
            "existing script; this is missing because '{' is not a valid start-of-run "
            "boundary character (_ASCII_BOUNDARY), so nothing inside '{...}' is reachable "
            "as a token at all"
        )


class TestFStringSourceSegmentPreservation:
    """AC-12: f-string 断片が `ast.get_source_segment` の原文通りに切れること."""

    def test_original_fstring_form_survives_verbatim_while_variants_and_fragments_do_not(
        self, tmp_path
    ):
        _mkfile(
            tmp_path,
            "pkg/strings.py",
            "name = 'x'\n"
            "x = 'y'\n"
            'A = f"docs/{name}.md"\n'  # line 3: 正確な形（原文どおり）
            'B = f"docs/{ name }.md"\n'  # line 4: 空白ありの変種
            'C = f"docs/{name!r}.md"\n'  # line 5: 変換指定ありの変種
            'D = f"a/{x}-v2.md"\n',  # line 6: 断片捏造の負の対照
        )

        graph = refgraph.build_graph(tmp_path)
        source = "pkg/strings.py"

        exact = _links(graph, source=source, target="docs/{name}.md")
        assert len(exact) >= 1, (
            "the exact f-string form 'docs/{name}.md' must survive verbatim as a py_string edge"
        )
        lines = {link.source_line for link in exact}
        assert lines == {3}, (
            f"only the exact (unformatted) f-string line must produce this target; got {lines}"
        )

        fragment = _links(graph, source=source, target="-v2.md")
        assert fragment == [], (
            f"a literal chunk inside an f-string must not leak as its own target: {fragment}"
        )


class TestOldSchemaBackwardCompat:
    """AC-15: `reference` を持たない旧スキーマの JSON を `read_graph` が読め、既定値 "" になること."""

    def test_read_graph_defaults_missing_reference_field_to_empty_string(self, tmp_path):
        old_schema = {
            "schema_version": 1,
            "root": str(tmp_path),
            "generated_from": {"file_count": 1},
            "nodes": [{"id": "a.md", "kind": "file", "exists": True}],
            "links": [
                {
                    "relation": "md_link",
                    "source": "a.md",
                    "source_line": 1,
                    "context": "ctx",
                    "target": "b.md",
                    "target_exists": False,
                    "resolution": "missing",
                    # NOTE: 旧スキーマは `reference` フィールドを持たない
                }
            ],
            "skipped": [],
        }
        out = tmp_path / "old.json"
        out.write_text(json.dumps(old_schema), encoding="utf-8")

        graph = refgraph.read_graph(out)

        assert len(graph.links) == 1
        assert graph.links[0].reference == "", (
            f"a link read from a reference-less schema must default reference to ''; "
            f"got {graph.links[0].reference!r}"
        )


class TestNoExceptionOnPathologicalInputs:
    """AC-17: 200 段ネスト f-string・500 成分パスで `build_graph` が例外を投げないこと."""

    def test_two_hundred_chained_fstrings_do_not_raise(self, tmp_path):
        """AC-17 の「200 段ネスト f-string」の代替.

        Python 3.11（本実行環境）では f-string の同一引用符を再利用する真のネストは
        構文的に作れない（PEP 701 以前の制約。4 種の引用符をローテーションしても
        depth 5 で `SyntaxError: unterminated ... string` になることを実測済み）。
        同等の負荷として、200 個の f-string を連鎖させたファイル（各行が直前の変数を
        式に含む）で代替する。
        """
        lines = ["v0 = 'leaf.md'\n"]
        for i in range(1, 201):
            lines.append(f'v{i} = f"a/{{v{i - 1}}}/{i}.md"\n')
        _mkfile(tmp_path, "deep.py", "".join(lines))

        graph = refgraph.build_graph(tmp_path)
        assert isinstance(graph.links, tuple)

    def test_500_component_path_does_not_raise(self, tmp_path):
        deep_rel = "/".join(f"d{i}" for i in range(500)) + "/leaf.md"
        _mkfile(
            tmp_path,
            "caller.md",
            "# caller\n\n参照: `" + deep_rel + "`\n",
        )

        graph = refgraph.build_graph(tmp_path)
        assert isinstance(graph.links, tuple)


class TestR1RejectsExtensionlessNames:
    """AC-20: `copy` / `bash` / `push` のような拡張子を持たない語が R1 で受理されないこと（負の対照）."""

    def test_extensionless_words_are_not_captured_as_targets(self, tmp_path):
        _mkfile(tmp_path, ".claude/hooks/real.py", "# real\n")
        _mkfile(
            tmp_path,
            "notes.md",
            "# notes\n\n"
            "`copy` と `bash` と `push` の話。参照は `.claude/hooks/real.py`\n",
        )

        graph = refgraph.build_graph(tmp_path)
        src = "notes.md"

        real = _links(graph, source=src, target=".claude/hooks/real.py")
        assert len(real) >= 1, "positive control: 同じ行の実在パスが辺にならない"

        leaked = []
        for word in ("copy", "bash", "push"):
            leaked += _links(graph, source=src, target=word)
        assert leaked == [], f"extensionless words must not be captured: {leaked}"


class TestMdLinkExtensionlessNegativeControl:
    """AC-21: 拡張子を持たない md リンクが `md_link` にならないこと（負の対照＋正の双子）."""

    def test_extensionless_link_target_produces_no_md_link_edge(self, tmp_path):
        _mkfile(
            tmp_path,
            "notes.md",
            "# notes\n\n"
            "[extensionless](foo/bar)\n"
            "[japanese missing](docs/日本語.md)\n",
        )

        graph = refgraph.build_graph(tmp_path)
        src = "notes.md"

        no_ext = _links(graph, relation="md_link", source=src, target="foo/bar")
        assert no_ext == [], f"an extensionless md link target must not become md_link: {no_ext}"

        jp = _links(graph, relation="md_link", source=src, target="docs/日本語.md")
        assert len(jp) >= 1, "positive control: 非 ASCII の実在しないリンク先が md_link にならない"
        assert jp[0].resolution == "missing"


class TestSqlTableExistenceSyntheticContrast:
    """AC-28: 合成ツリーで `sqltable:` の存在判定が両側とも正しく出ること."""

    def test_present_and_absent_tables_resolve_correctly(self, tmp_path):
        _mkfile(
            tmp_path,
            "schema.sql",
            "CREATE TABLE t_present (id INTEGER PRIMARY KEY);\n",
        )
        _mkfile(
            tmp_path,
            "query.py",
            "import sqlite3\n"
            'SQL = "SELECT * FROM t_absent"\n',
        )

        graph = refgraph.build_graph(tmp_path)

        present = _links(graph, relation="py_sql_table", target="sqltable:t_present")
        assert len(present) >= 1, "positive control: t_present への辺が無い"
        assert all(link.target_exists is True for link in present)
        assert all(link.resolution == "exact" for link in present)

        absent = _links(graph, relation="py_sql_table", target="sqltable:t_absent")
        assert len(absent) >= 1, "t_absent への辺が無い"
        assert all(link.target_exists is False for link in absent), (
            "a table absent from the CREATE TABLE index must have target_exists=False "
            "(contract C-12); the current implementation has no CREATE TABLE index at all — "
            f"every py_sql_table edge is hardcoded exact/exists=True: {absent}"
        )
        assert all(link.resolution == "missing" for link in absent), (
            f"a table absent from the CREATE TABLE index must resolve missing: {absent}"
        )

        by_id = {node.id: node for node in graph.nodes}
        assert by_id["sqltable:t_present"].exists is True
        assert by_id["sqltable:t_absent"].exists is False, (
            "a table node absent from the CREATE TABLE index must have exists == False "
            "(contract C-3/C-4/C-12); the current implementation hardcodes "
            "exists=True for every sqltable node (comment: テーブルの実在はツリーから "
            "確かめられないため常に true とする — this predates C-12)"
        )


class TestMdFenceBothDelimiterStyles:
    """AC-30: ``` と ~~~ の両方のフェンス本体が `md_fence_path` になり、`md_c3_run` と共存すること."""

    def test_both_fence_styles_produce_md_fence_path_and_c3_run_survives(self, tmp_path):
        _mkfile(tmp_path, ".claude/hooks/target_a.py", "# a\n")
        _mkfile(tmp_path, ".claude/hooks/target_b.py", "# b\n")
        _mkfile(tmp_path, ".claude/hooks/run_me.py", "# run\n")
        _mkfile(
            tmp_path,
            "doc.md",
            "# doc\n\n"
            "```text\n"
            "参照: .claude/hooks/target_a.py\n"
            "c3 run .claude/hooks/run_me.py\n"
            "```\n\n"
            "~~~text\n"
            "参照: .claude/hooks/target_b.py\n"
            "~~~\n",
        )

        graph = refgraph.build_graph(tmp_path)
        src = "doc.md"

        backtick_hits = _links(
            graph, relation="md_fence_path", source=src, target=".claude/hooks/target_a.py"
        )
        assert len(backtick_hits) >= 1, "backtick fence body path must be md_fence_path"

        tilde_hits = _links(
            graph, relation="md_fence_path", source=src, target=".claude/hooks/target_b.py"
        )
        assert len(tilde_hits) >= 1, "tilde fence body path must be md_fence_path"

        run_hits = _links(
            graph, relation="md_c3_run", source=src, target=".claude/hooks/run_me.py"
        )
        assert len(run_hits) >= 1, "c3 run inside a fence must still produce md_c3_run"


class TestMaskingDoesNotDoubleAttribute:
    """AC-43: 同一行のコードスパン内パスと散文裸パスが互いの relation に漏れないこと."""

    def test_code_span_and_prose_path_on_the_same_line_stay_in_their_own_relation(
        self, tmp_path
    ):
        _mkfile(tmp_path, ".claude/hooks/span_target.py", "# span\n")
        _mkfile(tmp_path, ".claude/hooks/prose_target.py", "# prose\n")
        _mkfile(
            tmp_path,
            "notes.md",
            "# notes\n\n"
            "コードスパン `.claude/hooks/span_target.py` と "
            "散文 .claude/hooks/prose_target.py が同居する。\n",
        )

        graph = refgraph.build_graph(tmp_path)
        src = "notes.md"

        span = _links(graph, source=src, target=".claude/hooks/span_target.py")
        assert len(span) >= 1
        assert {link.relation for link in span} == {"md_code_span_path"}, (
            f"the code-span path must only be md_code_span_path; got {[l.relation for l in span]}"
        )

        prose = _links(graph, source=src, target=".claude/hooks/prose_target.py")
        assert len(prose) >= 1
        assert {link.relation for link in prose} == {"md_prose_path"}, (
            f"the prose path must only be md_prose_path; got {[l.relation for l in prose]}"
        )


class TestVariablePrefixResolutionBases:
    """AC-51: `${CLAUDE_SKILL_DIR}/` と `${CLAUDE_PROJECT_DIR}/` が異なる基準で解決されること."""

    def test_skill_dir_is_source_relative_and_project_dir_is_root_relative(self, tmp_path):
        _mkfile(tmp_path, ".claude/skills/x/scripts/y.py", "# y\n")
        _mkfile(tmp_path, ".claude/hooks/z.py", "# z\n")
        _mkfile(
            tmp_path,
            ".claude/skills/x/SKILL.md",
            "# x\n\n"
            "`c3 run ${CLAUDE_SKILL_DIR}/scripts/y.py`\n"
            "`c3 run ${CLAUDE_PROJECT_DIR}/.claude/hooks/z.py`\n",
        )

        graph = refgraph.build_graph(tmp_path)
        src = ".claude/skills/x/SKILL.md"

        skill_relative = _links(
            graph, relation="md_c3_run", source=src, target=".claude/skills/x/scripts/y.py"
        )
        assert len(skill_relative) >= 1, (
            "${CLAUDE_SKILL_DIR}/ must resolve relative to the source's directory"
        )
        assert all(link.resolution == "exact" for link in skill_relative)

        root_relative = _links(
            graph, relation="md_c3_run", source=src, target=".claude/hooks/z.py"
        )
        assert len(root_relative) >= 1, (
            "${CLAUDE_PROJECT_DIR}/ must resolve relative to the repo root"
        )
        assert all(link.resolution == "exact" for link in root_relative)


class TestUnknownVariablePrefixDoesNotVanishSilently:
    """AC-52: 未知の変数プレフィクスが沈黙せず `missing` 辺として残ること."""

    def test_unknown_variable_prefix_emits_missing_edge(self, tmp_path):
        _mkfile(tmp_path, "x.py", "# x\n")
        _mkfile(
            tmp_path,
            "notes.md",
            "# notes\n\n"
            "`${UNKNOWN_VAR}/x.py` と `${CLAUDE_PROJECT_DIR}/x.py`\n",
        )

        graph = refgraph.build_graph(tmp_path)
        src = "notes.md"

        known = _links(graph, source=src, target="x.py")
        exact_hits = [link for link in known if link.resolution == "exact"]
        assert len(exact_hits) >= 1, "positive control: ${CLAUDE_PROJECT_DIR}/x.py must resolve exact"

        unknown = _links(graph, source=src, target="${UNKNOWN_VAR}/x.py")
        assert len(unknown) >= 1, (
            "an unknown variable prefix must not silently vanish; it must be emitted as a "
            "missing edge carrying the original token as target (contract §3 変数プレフィクスの "
            "解決 表 3 行目); the current implementation returns [] for any unrecognized "
            "'${...}' prefix (src/c3/refgraph.py _resolve), which silently drops the reference"
        )
        assert all(link.resolution == "missing" for link in unknown)
        assert all(link.target_exists is False for link in unknown)


class TestEmptyAfterStrippingVariablePrefixProducesNoEdge:
    """AC-53: `${CLAUDE_PROJECT_DIR}/` 単独が空になり辺にならないこと（負の対照＋正の双子）."""

    def test_bare_project_dir_variable_produces_no_edge_while_directory_form_does(
        self, tmp_path
    ):
        _mkfile(tmp_path, ".claude/marker.md", "# marker\n")
        _mkfile(
            tmp_path,
            "notes.md",
            "# notes\n\n"
            "`${CLAUDE_PROJECT_DIR}/` だけ\n"
            "`${CLAUDE_PROJECT_DIR}/.claude/` はディレクトリ\n",
        )

        graph = refgraph.build_graph(tmp_path)
        src = "notes.md"

        bare_prefix_hits = [
            link
            for link in graph.links
            if link.source == src and link.reference == "${CLAUDE_PROJECT_DIR}/"
        ]
        assert bare_prefix_hits == [], (
            f"a bare ${{CLAUDE_PROJECT_DIR}}/ must not produce any edge; got {bare_prefix_hits}"
        )

        dir_hits = _links(graph, source=src, target=".claude")
        assert len(dir_hits) >= 1, (
            "positive control: ${CLAUDE_PROJECT_DIR}/.claude/ must resolve to the existing "
            "directory; missing under the current implementation (directories are never "
            "indexed as present, see AC-2/AC-31)"
        )
        assert all(link.resolution == "exact" for link in dir_hits)

        by_id = {node.id: node for node in graph.nodes}
        assert ".claude" in by_id
        assert by_id[".claude"].kind == "dir"


class TestMemoryErrorDuringReadIsRecordedAndNonFatal:
    """AC-54: `Path.read_text` が `MemoryError` を送出しても `build_graph` がクラッシュしないこと."""

    def test_memory_error_on_one_file_is_skipped_while_others_survive(
        self, tmp_path, monkeypatch
    ):
        _mkfile(tmp_path, ".claude/hooks/alive.py", "# alive\n")
        _mkfile(tmp_path, "bad.md", "# bad\n\n参照: `.claude/hooks/alive.py`\n")
        _mkfile(tmp_path, "good.md", "# good\n\n参照: `.claude/hooks/alive.py`\n")

        real_read_text = Path.read_text

        def fake_read_text(self, *args, **kwargs):
            if self.name == "bad.md":
                raise MemoryError("synthetic out-of-memory during read")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fake_read_text)

        # NOTE (Red の帰属): `_process` の except 節は
        # `(OSError, UnicodeDecodeError, ValueError)` のみを捕捉し、`MemoryError`
        # （`Exception` を直接継承・`OSError` の派生ではない）は捕捉しない。
        # したがって現行実装ではこの呼び出し自体が `MemoryError` を送出して
        # 未処理のまま伝播し、build_graph がクラッシュする（契約 §6 条件 3 違反）。
        graph = refgraph.build_graph(tmp_path)

        skipped_paths = [entry.path for entry in graph.skipped]
        assert "bad.md" in skipped_paths, (
            f"a file whose read raises MemoryError must be recorded in skipped; got {skipped_paths!r}"
        )
        assert skipped_paths.count("bad.md") == 1

        reasons = [entry.reason for entry in graph.skipped if entry.path == "bad.md"]
        assert reasons == ["MemoryError"], f"expected reason MemoryError; got {reasons}"

        alive = _links(graph, source="good.md", target=".claude/hooks/alive.py")
        assert len(alive) >= 1, "an unrelated readable file must still produce its edges"


class TestTokenizeFailureDegradesGracefully:
    """AC-55: `tokenize` が例外を送出しても `py_comment` だけが失われ、`py_import` は生き残ること."""

    def test_tokenize_failure_drops_only_py_comment_for_that_file(self, tmp_path, monkeypatch):
        _mkfile(tmp_path, "pkg/target.py", "# target\n")
        _mkfile(tmp_path, "pkg/__init__.py", "")
        _mkfile(
            tmp_path,
            "pkg/broken.py",
            "import pkg.target  # BREAK_TOKENIZE marker: pkg/target.py\n",
        )
        _mkfile(
            tmp_path,
            "pkg/ok.py",
            "# 参照: pkg/target.py\n",
        )

        real_generate_tokens = tokenize.generate_tokens

        def fake_generate_tokens(readline):
            stream = getattr(readline, "__self__", None)
            content = stream.getvalue() if stream is not None else ""
            if "BREAK_TOKENIZE" in content:
                raise tokenize.TokenError("synthetic tokenize failure")
            return real_generate_tokens(readline)

        monkeypatch.setattr(tokenize, "generate_tokens", fake_generate_tokens)

        graph = refgraph.build_graph(tmp_path)

        broken_comments = _links(graph, relation="py_comment", source="pkg/broken.py")
        assert broken_comments == [], (
            f"py_comment must be empty for the file whose tokenize call failed; got {broken_comments}"
        )

        ok_comments = _links(
            graph, relation="py_comment", source="pkg/ok.py", target="pkg/target.py"
        )
        assert len(ok_comments) >= 1, (
            "positive control: tokenize が成功するファイルからは py_comment が出るはず"
        )

        imports = _links(
            graph, relation="py_import", source="pkg/broken.py", target="pkg/target.py"
        )
        assert len(imports) >= 1, "py_import must survive a tokenize failure in the same file"

        skipped_paths = [entry.path for entry in graph.skipped]
        assert "pkg/broken.py" not in skipped_paths, (
            "a tokenize-only failure is not a file-level read failure; skipped is for that"
        )
        assert "pkg/ok.py" not in skipped_paths


class TestT5bPrefixTruncationAddsResolvedEdges:
    """AC-57: T-5b が正規化後 missing 辺に加え、解決できる前置詞への辺を追加すること."""

    def test_normalization_residue_and_resolved_prefix_both_appear(self, tmp_path):
        _mkfile(tmp_path, ".claude/hooks/stop.py", "# stop\n")
        _mkfile(
            tmp_path,
            "notes.md",
            "# notes\n\n参照: `.claude/hooks/stop.py/../../../malicious.py`\n",
        )

        graph = refgraph.build_graph(tmp_path)
        src = "notes.md"

        normalized_missing = _links(graph, source=src, target="malicious.py")
        assert len(normalized_missing) >= 1, (
            "(i) the normalized-but-unresolvable target must still be emitted as missing"
        )
        assert all(link.resolution == "missing" for link in normalized_missing)

        prefix_resolved = _links(graph, source=src, target=".claude/hooks/stop.py")
        assert len(prefix_resolved) >= 1, (
            "(ii) T-5b must add a resolved edge for the accepted prefix '.claude/hooks/stop.py' "
            "(contract §3 前置詞の追加辺・T-5b); this edge is missing under the current "
            "implementation, which only collapses '..' within a single token via `_normalize` "
            "(posixpath.normpath) and has no mechanism that retries truncated prefixes at all"
        )
        assert all(link.resolution == "exact" for link in prefix_resolved)

    def test_negative_control_1_no_resolvable_prefix_adds_nothing(self, tmp_path):
        """負の対照(1): 受理できる前置詞が無い run は追加されない（`nope` を作らない）."""
        _mkfile(tmp_path, "notes.md", "# notes\n\n参照: `nope/absent.md`\n")

        graph = refgraph.build_graph(tmp_path)
        hits = _links(graph, source="notes.md", target="nope/absent.md")
        assert len(hits) == 1, f"exactly one missing edge, no additions; got {hits}"
        assert hits[0].resolution == "missing"

        stray = [
            link
            for link in graph.links
            if link.source == "notes.md" and link.target == "nope"
        ]
        assert stray == [], f"a nonexistent prefix must not produce an edge: {stray}"

    def test_negative_control_2_prefix_that_only_resolves_to_missing_adds_nothing(
        self, tmp_path
    ):
        """負の対照(2): 前置詞 `foo.md` は R1 で受理されるが missing にしか解決しないので追加しない."""
        _mkfile(tmp_path, "notes.md", "# notes\n\n参照: `foo.md/bar.md`\n")

        graph = refgraph.build_graph(tmp_path)
        hits = _links(graph, source="notes.md", target="foo.md/bar.md")
        assert len(hits) == 1, f"exactly one missing edge for the raw token; got {hits}"
        assert hits[0].resolution == "missing"

        prefix_hits = [
            link
            for link in graph.links
            if link.source == "notes.md" and link.target == "foo.md"
        ]
        assert prefix_hits == [], (
            f"a prefix that itself resolves to missing must not be added: {prefix_hits}"
        )

    def test_positive_control_ambiguous_prefix_adds_one_edge_per_candidate(self, tmp_path):
        """正の対照: 前置詞が 2 候補に当たる場合、候補ごとに 1 本ずつ追加すること.

        是正の経緯（元テストの不発火の理由）: 元 fixture は参照 `skills/absent/z.md` の
        末尾成分を落として作る前置詞が `skills/absent` → `skills` の 2 通りしかなく、
        どちらも契約の受理条件（`docs/refgraph-contract.md` の T-5b 節、行 155-157・
        「受理でき `missing` 以外に解決できるものを追加の辺として出す」）が指す
        R1（既知拡張子で終わる）/ R2（`/` で終わる）/ R3（`*` を含む）のいずれも
        満たさない（`_accepts()`、`src/c3/refgraph.py:477-494`）。したがって
        `_add_prefix_edges`（同 732-763）はどの前置詞でも `continue` し、追加辺は
        恒久的に 0 本になる。これは T-5b 未実装（旧 D4）とは別の、テスト設計側の
        不備だった。

        是正: 参照そのものは複合成分にし、**その中間に位置する前置詞が単独で R1 を
        満たす**ように組む: `skills/x.md/absent.txt`。
        - 参照全体は `.txt`（既知拡張子）で終わるため R1 で直接受理され、実在しないので
          いったん `missing` の辺が出る（T-5b の発火条件）
        - 前置詞候補は `skills/x.md`（R1: `.md` で終わる → 受理）→ `skills`
          （R1/R2/R3 いずれも満たさず不受理）の順。`skills/x.md` はそれ自体が
          リポジトリに存在しないが、`p1/skills/x.md` / `p2/skills/x.md` という
          末尾一致する実体を 2 つ置くことで、解決の第 3 段
          （契約 `docs/refgraph-contract.md:128-129`・パス末尾が `/<参照>` に一致する
          もの）が 2 候補を返し `ambiguous` になる（`test_suffix_match_with_two_candidates_is_ambiguous`
          と同型の distractor パターン。前置詞そのものを実体パスとして直接置くと
          解決の第 1 段（ルート相対で実在）が先に確定してしまい `ambiguous` を作れない
          ため、深い場所に実体を 2 つ置く設計にした）
        """
        _mkfile(tmp_path, "p1/skills/x.md", "# x1\n")
        _mkfile(tmp_path, "p2/skills/x.md", "# x2\n")
        _mkfile(tmp_path, "notes.md", "# notes\n\n参照: `skills/x.md/absent.txt`\n")

        graph = refgraph.build_graph(tmp_path)
        src = "notes.md"

        # (c) 元の missing 辺は必ず残る（契約 T-5b 節・行 163）。
        original_missing = _links(graph, source=src, target="skills/x.md/absent.txt")
        assert len(original_missing) >= 1, (
            "(c) the original unresolvable reference must still be emitted as a missing edge"
        )
        assert all(link.resolution == "missing" for link in original_missing)

        # (a)(b) 前置詞 `skills/x.md` は R1（既知拡張子 `.md`）で受理され、
        # ambiguous（p1/skills/x.md・p2/skills/x.md の 2 候補）に解決する。
        added = sorted(
            link.target
            for link in graph.links
            if link.source == src and link.target != "skills/x.md/absent.txt"
        )
        assert added == ["p1/skills/x.md", "p2/skills/x.md"], (
            "an ambiguous resolvable prefix ('skills/x.md', accepted by R1) must add one edge "
            f"per candidate (T-5b, contract docs/refgraph-contract.md:155-162); got {added}"
        )
        assert all(
            link.resolution == "ambiguous"
            for link in graph.links
            if link.source == src and link.target in added
        )


class TestT5bInTheRealRepo:
    """AC-59: T-5b が実リポジトリで効くこと（`.claude/docs/config-policy.md` の実例）.

    `md_link` 経路での確認は AC-60（`TestT5bViaMdLink`）が合成ツリーで独立に担う
    （AC-59 本文の「あわせて md_link 経路でも…1 件測る」は AC-60 と同一検査のため
    重複させない）。
    """

    def test_config_policy_reference_resolves_to_stop_py_via_t5b(self, repo_root, repo_graph):
        text = (repo_root / ".claude" / "docs" / "config-policy.md").read_text(encoding="utf-8")
        assert "stop.py/../../../malicious.py" in text, (
            "premise gone: config-policy.md から T-5b 題材の記述が消えた"
        )

        source = ".claude/docs/config-policy.md"
        reference = ".claude/hooks/stop.py/../../../malicious.py"

        resolved = [
            link
            for link in repo_graph.links
            if link.source == source
            and link.reference == reference
            and link.target == ".claude/hooks/stop.py"
        ]
        assert len(resolved) >= 1, (
            "T-5b must resolve this reference to .claude/hooks/stop.py with resolution == "
            "exact (contract AC-59); this edge is missing under the current implementation, "
            "which has no T-5b mechanism (only single-token '..' normalization via _normalize)"
        )
        assert all(link.resolution == "exact" and link.target_exists for link in resolved)

        normalized_missing = [
            link
            for link in repo_graph.links
            if link.source == source
            and link.reference == reference
            and link.target == "malicious.py"
        ]
        assert len(normalized_missing) >= 1, (
            "the normalized-but-unresolved target must also survive as a missing edge"
        )
        assert all(link.resolution == "missing" for link in normalized_missing)


class TestT5bViaMdLink:
    """AC-60: T-5b が `md_link` 経路でも効くこと（合成ツリー）.

    リンク先が既知拡張子（`.md`）を持つので `_md_links` の抑止ガードを通る
    （持たない題材では辺自体が出ず T-5b も回らないため成立しない）。
    """

    def test_link_target_prefix_resolves_via_t5b_while_original_stays_missing(self, tmp_path):
        _mkfile(tmp_path, "sub/foo.md", "# foo\n")
        _mkfile(
            tmp_path,
            "notes.md",
            "# notes\n\n[link](foo.md/bar.md)\n",
        )

        graph = refgraph.build_graph(tmp_path)
        src = "notes.md"

        original_missing = _links(graph, relation="md_link", source=src, target="foo.md/bar.md")
        assert len(original_missing) >= 1, (
            "(i) the original unresolvable link target must still appear as a missing edge"
        )
        assert all(link.resolution == "missing" for link in original_missing)

        prefix_resolved = _links(graph, relation="md_link", source=src, target="sub/foo.md")
        assert len(prefix_resolved) >= 1, (
            "(ii) T-5b must add a basename-resolved edge for the truncated prefix 'foo.md' "
            "(contract AC-60); missing because T-5b is not implemented for any "
            "reference-emitting path, md_link included"
        )
        assert all(link.resolution == "basename" for link in prefix_resolved)


class TestGlobFragmentBehaviorNoxWhyMd:
    """AC-62 (ii): `nox/**/why.md` の断片挙動（合成・読み A/B の前置トークン）."""

    def test_nox_why_md_produces_full_token_and_prefix_but_no_bare_fragment(self, tmp_path):
        _mkfile(
            tmp_path,
            "ii.md",
            "# ii\n\n参照: `nox/**/why.md`\n",
        )

        graph = refgraph.build_graph(tmp_path)
        src = "ii.md"

        full = _links(graph, source=src, target="nox/**/why.md")
        assert len(full) == 1, f"(a) reading A の原文まるごとの missing 辺が 1 本のはず; got {full}"
        assert full[0].resolution == "missing"

        prefix = _links(graph, source=src, target="nox")
        assert len(prefix) == 1, (
            f"(b) reading B の前置トークン 'nox' の missing 辺が 1 本のはず; got {prefix}"
        )
        assert prefix[0].resolution == "missing"

        why_md = _links(graph, source=src, target="why.md")
        slash_why_md = _links(graph, source=src, target="/why.md")
        assert why_md == [] and slash_why_md == [], (
            f"(c) 'why.md' / '/why.md' は断片として出てはいけない; got {why_md + slash_why_md}"
        )


class TestGlobRawTokenInTheRealRepo:
    """AC-62 (iv): 実リポジトリで `.claude/skills/*/scripts/**/*.py` のグロブ原文が確認できること.

    題材が CHANGELOG.md の特定行を指すため、CHANGELOG.md が先頭へ追記される慣習で
    行番号がずれるとこのテストの前提が崩れる。前提 assert が失敗したら、AC-62 (iv) が
    明記する代替題材（`.claude/docs/taxonomy.md:147-148`・フェンス内なので relation は
    `md_fence_path`）へ差し替えてよい。
    """

    def test_changelog_line_28_reference_produces_the_raw_glob_and_not_a_shortened_dir(
        self, repo_root, repo_graph
    ):
        text = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")
        lines = text.split("\n")
        assert len(lines) >= 28 and "`.claude/skills/*/scripts/**/*.py`" in lines[27], (
            "premise gone: CHANGELOG.md:28 の記載が変わった "
            "(代替題材: .claude/docs/taxonomy.md:147-148・relation は md_fence_path)"
        )

        hits = [
            link
            for link in repo_graph.links
            if link.relation == "md_code_span_path"
            and link.source == "CHANGELOG.md"
            and link.source_line == 28
            and link.reference == ".claude/skills/*/scripts/**/*.py"
        ]

        full_glob = [
            link for link in hits if link.target == ".claude/skills/*/scripts/**/*.py"
        ]
        assert len(full_glob) >= 1, (
            "(a) the raw glob token must survive as its own missing-resolution target"
        )

        shortened_dir = [link for link in hits if link.target == ".claude/skills/*/scripts"]
        assert shortened_dir == [], (
            f"(b) the glob must not be shortened to a directory-only target: {shortened_dir}"
        )


# ===========================================================================
# E 周回 1 findings（改訂 5・最終実行版）
# ===========================================================================
class TestMdLinkT5bSharesReferenceWithBody:
    """DC-AM-001/DC-AM-002: `md_link` の T-5b 追加辺が本体 missing 辺と同一の
    非空 `reference` を持ち、その組が `settled_links` から排除されること.

    fixture は AC-57 の正の対照（`tests/test_refgraph.py:2417-2473`
    `test_positive_control_ambiguous_prefix_adds_one_edge_per_candidate`）と同型
    （`skills/x.md/absent.txt` ＋ `p1/skills/x.md` / `p2/skills/x.md` の 2 実体）だが、
    経路を `md_code_span_path` ではなく `md_link`（`[t](...)`）にする。
    """

    def test_missing_and_t5b_edges_share_a_nonempty_reference_and_are_excluded_from_settled(
        self, tmp_path
    ):
        _mkfile(tmp_path, "p1/skills/x.md", "# x1\n")
        _mkfile(tmp_path, "p2/skills/x.md", "# x2\n")
        _mkfile(tmp_path, "notes.md", "# notes\n\n[t](skills/x.md/absent.txt)\n")

        graph = refgraph.build_graph(tmp_path)
        src = "notes.md"

        missing = _links(graph, relation="md_link", source=src, target="skills/x.md/absent.txt")
        assert len(missing) >= 1, "positive control: 本体 missing 辺が出ていない"
        assert all(link.resolution == "missing" for link in missing)

        added = _links(graph, relation="md_link", source=src, target="p1/skills/x.md") + _links(
            graph, relation="md_link", source=src, target="p2/skills/x.md"
        )
        assert len(added) == 2, (
            f"positive control: T-5b が候補ごとに 1 本ずつ追加するはず(AC-57); got {added}"
        )
        assert all(link.resolution == "ambiguous" for link in added)

        assert missing[0].reference != "", (
            "md_link の reference は「解決に使った文字列」であるべき(C-25); "
            "現行は _add() の既定値 '' のまま"
        )
        references = {link.reference for link in missing + added}
        assert references == {"skills/x.md/absent.txt"}, (
            f"本体辺と T-5b 辺は同一のリンク先文字列を reference に持つはず; got {references}"
        )

        folded = query_module().fold_links(graph.links)
        settled = query_module().settled_links(folded)
        settled_group = [
            link
            for link in settled
            if link.source == src and link.reference == "skills/x.md/absent.txt"
        ]
        assert settled_group == [], (
            "3 候補(本体 + T-5b 2 本)を持つグループは settled_links から排除されるべき; "
            f"got {settled_group}"
        )

    def test_hash_fragment_does_not_split_the_shared_reference(self, tmp_path):
        """`#` 断片つきでも本体辺と T-5b 辺の reference が同一(非空)であること.

        断片の前後で reference が割れると、settled_links のグループキー
        (relation, source, reference) が分裂し missing 本体辺が単独グループになって
        settled をすり抜ける(CR High の再開・DC-AM-002)。
        """
        _mkfile(tmp_path, "p1/skills/x.md", "# x1\n")
        _mkfile(tmp_path, "p2/skills/x.md", "# x2\n")
        _mkfile(tmp_path, "notes.md", "# notes\n\n[t](skills/x.md/absent.txt#sec)\n")

        graph = refgraph.build_graph(tmp_path)
        src = "notes.md"

        missing = _links(graph, relation="md_link", source=src, target="skills/x.md/absent.txt")
        added = _links(graph, relation="md_link", source=src, target="p1/skills/x.md") + _links(
            graph, relation="md_link", source=src, target="p2/skills/x.md"
        )
        assert len(missing) >= 1 and len(added) == 2, (
            f"positive control: T-5b の発火自体は # 断片の有無で変わらない; "
            f"missing={missing} added={added}"
        )

        references = {link.reference for link in missing + added}
        assert len(references) == 1, (
            f"本体辺と T-5b 辺の reference が # 断片の前後で割れてはいけない; got {references}"
        )
        (only_reference,) = references
        assert only_reference != "", "reference は非空であること"


class TestPyJoinedStrMultibyteRegressionGuard:
    """DC-AS-001/DC-AS-002: `_py_joined_str` の UTF-8 バイトスライス化後も、日本語を
    含む f-string の `reference`（原文断片）が正しく切れること（現行緑を維持する正の対照）.

    `col_offset` / `end_col_offset` は UTF-8 **バイト**単位（CPython `Lib/ast.py`）。
    行頭に日本語（マルチバイト）の識別子を置くと、文字インデックスとバイトインデックスの
    値が乖離する（`名前` は 2 文字＝6 バイト）。単一行・複数行の 2 形を検査する。
    """

    def test_single_line_fstring_with_japanese_prefix_and_body(self, tmp_path):
        _mkfile(
            tmp_path,
            "pkg/strings2.py",
            'x = "z"\n'
            '名前 = f"docs/日本語-{x}.md"\n',
        )

        graph = refgraph.build_graph(tmp_path)
        source = "pkg/strings2.py"

        hits = [
            link
            for link in graph.links
            if link.relation == "py_string"
            and link.source == source
            and link.reference == "docs/日本語-{x}.md"
        ]
        assert hits != [], (
            "f-string 原文断片 'docs/日本語-{x}.md' が reference と逐語一致しない "
            f"(見つかった py_string の reference: "
            f"{[l.reference for l in graph.links if l.source == source]})"
        )
        assert {link.source_line for link in hits} == {2}

    def test_multiline_fstring_with_japanese_prefix_and_body(self, tmp_path):
        _mkfile(
            tmp_path,
            "pkg/strings3.py",
            'x = "z"\n'
            '名前 = f"""docs/report.md\n'
            '日本語-{x}のパス.txt"""\n',
        )

        graph = refgraph.build_graph(tmp_path)
        source = "pkg/strings3.py"

        first_line_hit = [
            link
            for link in graph.links
            if link.relation == "py_string"
            and link.source == source
            and link.reference == "docs/report.md"
        ]
        assert first_line_hit != [], (
            "先頭行の断片 'docs/report.md' が reference と逐語一致しない(col_offset は "
            f"バイト単位であること・名前 = の日本語プレフィクスでバイト/文字インデックスが乖離する); "
            f"got {[l.reference for l in graph.links if l.source == source]}"
        )
        assert {link.source_line for link in first_line_hit} == {2}

        last_line_hit = [
            link
            for link in graph.links
            if link.relation == "py_string"
            and link.source == source
            and link.reference == "日本語-{x}のパス.txt"
        ]
        assert last_line_hit != [], (
            "末尾行の断片 '日本語-{x}のパス.txt' が reference と逐語一致しない(end_col_offset は "
            f"バイト単位であること); got {[l.reference for l in graph.links if l.source == source]}"
        )
        assert {link.source_line for link in last_line_hit} == {3}


class TestSymlinkFilesAreIndexedButNotRead:
    """SR-V-002 L-1: symlink は索引(ノード)に残るが内容は読まず skipped(reason=Symlink)."""

    def test_symlink_file_is_present_as_node_but_content_is_not_read(self, tmp_path):
        real = tmp_path / "secret.md"
        real.write_text("# secret\n\n参照: `should-not-be-read.py`\n", encoding="utf-8")
        link_path = tmp_path / "link.md"
        try:
            link_path.symlink_to(real)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlink creation unsupported on this platform: {exc}")

        # 正の双子: symlink でない通常ファイルも同じツリーに置く（§7 規律 4）
        _mkfile(tmp_path, "normal.md", "# normal\n\n参照: `alive.py`\n")
        _mkfile(tmp_path, "alive.py", "# alive\n")

        graph = refgraph.build_graph(tmp_path)

        assert "link.md" in _node_ids(graph), (
            "symlink 自身はノードとして索引に残るはず(SR-V-002 是正後の仕様)"
        )

        leaked = _links(graph, source="link.md", target="should-not-be-read.py")
        assert leaked == [], (
            f"symlink 先の内容が読まれ辺が出ている(内容を読んではいけない): {leaked}"
        )

        reported = _skipped_paths(graph)
        assert "link.md" in reported, (
            "symlink は内容不読として skipped に記録されるはず(現行は symlink をそのまま "
            f"path.read_text() で読む実装のため未記録); got {reported!r}"
        )
        reasons = [entry.reason for entry in graph.skipped if entry.path == "link.md"]
        assert reasons == ["Symlink"], f"skipped reason は 'Symlink' のはず; got {reasons}"

        alive_hits = _links(graph, source="normal.md", target="alive.py")
        assert len(alive_hits) >= 1, "symlink 対応が他ファイルの辺を壊してはいけない(正の双子)"


class TestOversizedFilesAreSkippedNotRead:
    """SR-NEW L-2: `_MAX_TEXT_BYTES` を超えるファイルは読まず skipped(reason=TooLarge)."""

    def test_file_over_the_size_limit_is_skipped_and_its_content_not_parsed(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(refgraph, "_MAX_TEXT_BYTES", 100, raising=False)

        big = tmp_path / "big.md"
        big.write_text("参照: `should-not-be-read.py`\n" + ("x" * 200), encoding="utf-8")
        _mkfile(tmp_path, "normal.md", "# normal\n\n参照: `alive.py`\n")
        _mkfile(tmp_path, "alive.py", "# alive\n")

        graph = refgraph.build_graph(tmp_path)

        leaked = _links(graph, source="big.md", target="should-not-be-read.py")
        assert leaked == [], f"上限超過ファイルの内容が読まれ辺が出ている: {leaked}"

        reported = _skipped_paths(graph)
        assert "big.md" in reported, (
            "サイズ上限を超えるファイルは読み込み前チェックで skipped に記録されるはず "
            f"(現行は '_MAX_TEXT_BYTES' を参照する事前チェックが無い); got {reported!r}"
        )
        reasons = [entry.reason for entry in graph.skipped if entry.path == "big.md"]
        assert reasons == ["TooLarge"], f"skipped reason は 'TooLarge' のはず; got {reasons}"

        alive_hits = _links(graph, source="normal.md", target="alive.py")
        assert len(alive_hits) >= 1, "サイズ上限対応が他ファイルの辺を壊してはいけない(正の双子)"


class TestDirectoryJunctionDoesNotDoubleIndexAndHandlesSelfReferences:
    """CR-NEW High: Windows のディレクトリジャンクション（NTFS reparse point）が
    symlink 防御をすり抜け、同一ファイルが二重インデックスされる・自己参照で無限走査
    する問題。mklink /J（管理者権限不要）で実機ジャンクションを作成し、3 点を検証:
    (i) ジャンクション配下のファイルが二重インデックスされない
    (ii) 自己参照ジャンクションでも build_graph が有限時間で完走する
    (iii) ジャンクションが skipped に reason つきで記録される
    """

    def test_junction_does_not_double_index_files(self, tmp_path):
        """実ファイル realdir/inner.py をジャンクション linkdir で指す場合、
        同一ファイルが 2 つのノード ID（linkdir/inner.py と realdir/inner.py）
        で二重にインデックスされていないこと。"""
        import subprocess
        import platform

        if platform.system() != "Windows":
            pytest.skip("Directory junction test is Windows-only")

        realdir = tmp_path / "realdir"
        realdir.mkdir()
        _mkfile(tmp_path, "realdir/inner.py", "# inner\n\n参照: `target.py`\n")
        _mkfile(tmp_path, "target.py", "# target\n")

        # ジャンクション作成（mklink /J 管理者権限不要）
        linkdir = tmp_path / "linkdir"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(linkdir), str(realdir)],
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )

        if result.returncode != 0:
            pytest.skip(f"mklink /J failed: {result.stderr}")

        try:
            graph = refgraph.build_graph(tmp_path)

            # ノード ID の個数を確認（realdir/inner.py のみ、linkdir/inner.py は不可）
            nodes = _node_ids(graph)
            realdir_node = "realdir/inner.py"
            linkdir_node = "linkdir/inner.py"

            # 実ファイルへの参照は出ること
            assert realdir_node in nodes, (
                f"実ファイル {realdir_node} がノードとして出ているはず; got {nodes}"
            )

            # ジャンクション経由での二重インデックスは出ていないこと
            assert linkdir_node not in nodes, (
                f"ジャンクション経由の二重ノード {linkdir_node} が出ている(バグ); got {nodes}"
            )

            # 正の双子: ジャンクション自体が skipped に出ていること
            reported = _skipped_paths(graph)
            assert "linkdir" in reported or linkdir_node.split("/")[0] in reported, (
                f"ジャンクションディレクトリが skipped に記録されるはず; got {reported}"
            )

        finally:
            # teardown: ジャンクション自体のみ削除（実体 realdir は残す）
            if linkdir.exists():
                try:
                    linkdir.rmdir()
                except Exception:
                    pass

    def test_self_referencing_junction_completes_in_finite_time(self, tmp_path):
        """自己参照ジャンクション（親ディレクトリを指すジャンクション）でも
        build_graph が有限時間で完走すること（タイムアウト付きサブプロセス）。"""
        import subprocess
        import platform

        if platform.system() != "Windows":
            pytest.skip("Directory junction test is Windows-only")

        testdir = tmp_path / "testdir"
        testdir.mkdir()
        _mkfile(tmp_path, "testdir/file.py", "# file\n")

        # 自己参照ジャンクション: testdir/loopback -> testdir
        loopback = testdir / "loopback"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(loopback), str(testdir)],
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )

        if result.returncode != 0:
            pytest.skip(f"mklink /J failed: {result.stderr}")

        try:
            # タイムアウト付きで実行（デッドロックしないことを確認）
            import time
            start = time.perf_counter()
            graph = refgraph.build_graph(tmp_path)
            elapsed = time.perf_counter() - start

            # 有限時間で完走したこと
            assert elapsed < 10, (
                f"自己参照ジャンクションで build_graph が長すぎる: {elapsed:.2f} 秒"
            )

            # ノードが作られていること（少なくとも file.py）
            nodes = _node_ids(graph)
            assert "testdir/file.py" in nodes, (
                f"通常ファイル testdir/file.py がノードとして出ているはず; got {nodes}"
            )

        finally:
            # teardown: ジャンクションのみ削除
            if loopback.exists():
                try:
                    loopback.rmdir()
                except Exception:
                    pass

    def test_junction_is_recorded_in_skipped_with_reason(self, tmp_path):
        """ジャンクションディレクトリが skipped に reason つきで記録されること。"""
        import subprocess
        import platform

        if platform.system() != "Windows":
            pytest.skip("Directory junction test is Windows-only")

        realdir = tmp_path / "realdir"
        realdir.mkdir()
        _mkfile(tmp_path, "realdir/inner.py", "# inner\n")

        linkdir = tmp_path / "linkdir"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(linkdir), str(realdir)],
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )

        if result.returncode != 0:
            pytest.skip(f"mklink /J failed: {result.stderr}")

        try:
            graph = refgraph.build_graph(tmp_path)

            # skipped に reason つきで出ていること
            reported = _skipped_paths(graph)
            reasons = [entry.reason for entry in graph.skipped if "linkdir" in entry.path]

            assert len(reasons) > 0, (
                f"ジャンクションが skipped に reason つきで出ているはず; "
                f"got skipped paths {reported}"
            )
            assert "Symlink" in reasons, (
                f"skipped reason に 'Symlink' が含まれているはず; got {reasons}"
            )

        finally:
            if linkdir.exists():
                try:
                    linkdir.rmdir()
                except Exception:
                    pass


class TestT5AndT5bPickFirstVsAllSymmetry:
    """impl 10.（DRY / `_resolve` 分割）の回帰ガード: T-5 は最初の 1 つだけ、T-5b は
    全件を試すという非対称性が、共通ジェネレータ化後も保たれること（現行緑の正の対照）.

    `a.md/b.md/c.md/d.unknown` は直接受理されない。T-5（`_chop_to_accepted`）は
    末尾から刻んで**最初に受理できた** `a.md/b.md/c.md`（3 成分）を採る——2 成分の
    `a.md/b.md` や 1 成分の `a.md` も単独では受理できるが、T-5 はそこまで見ない
    （最初の 1 つで停止する）。`a.md/b.md/c.md` 自体は実在せず missing になるため
    T-5b が発火し、そこから**全ての**受理できる前置詞（`a.md/b.md` と `a.md`）を
    候補ごとに追加する（`a.md` はディレクトリとして実在）。
    """

    def test_t5_picks_the_longest_prefix_while_t5b_adds_every_shorter_one(self, tmp_path):
        (tmp_path / "a.md").mkdir()
        _mkfile(tmp_path, "a.md/b.md", "# b\n")
        _mkfile(tmp_path, "notes.md", "# notes\n\n参照: `a.md/b.md/c.md/d.unknown`\n")

        graph = refgraph.build_graph(tmp_path)
        src = "notes.md"

        t5_pick = _links(graph, source=src, target="a.md/b.md/c.md")
        assert len(t5_pick) >= 1, (
            "T-5 は末尾から刻んで最初に受理できた前置詞 'a.md/b.md/c.md'(3 成分) を "
            f"採るはず; got {[l.target for l in graph.links if l.source == src]}"
        )
        assert all(link.resolution == "missing" for link in t5_pick)

        t5b_two_component = _links(graph, source=src, target="a.md/b.md")
        assert len(t5b_two_component) >= 1, (
            "T-5b は resolved_input 'a.md/b.md/c.md' から更に刻み、2 成分 'a.md/b.md' "
            f"も追加辺にするはず; got {[l.target for l in graph.links if l.source == src]}"
        )
        assert all(link.resolution == "exact" for link in t5b_two_component)

        t5b_one_component = _links(graph, source=src, target="a.md")
        assert len(t5b_one_component) >= 1, (
            "T-5b は 1 成分 'a.md'(ディレクトリとして実在) も追加辺にするはず"
        )
        assert all(link.resolution == "exact" for link in t5b_one_component)
