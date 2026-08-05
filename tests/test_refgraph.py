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
from collections import Counter
from pathlib import Path, PurePosixPath

import pytest

# モジュール属性経由で参照する。`from c3.refgraph import write_graph` と書くと
# 未実装時に **collection error** になり全ケースが実行されず、
# 「スタブで緑になるのは API 型検査だけ」（契約 §6 条件 7）の確認ができない。
import c3.refgraph as refgraph  # noqa: E402


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
