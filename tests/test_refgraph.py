"""
参照抽出器（reference graph extractor）のベンチマークテスト。

`.dev/refgraph-benchmark-20260805.md` §1-6 に基づく。
到達可能性の機械判定で、削除フェーズの安全網として機能する。

完了条件（§6）:
1. §1 の 7 行（採用条件）すべて期待どおり
2. §5 の N-1〜N-3（負の対照）すべて期待どおり
3. wt_systematic-debugger.md への経路に agent_variant_map の辺が含まれること
4. stop.py への経路に settings_hook の辺が含まれていないこと
5. フルスイート緑（基準: 2774 passed / 14 skipped）
6. do-nothing スタブで API 型検査 3 件のみが緑（他は全て赤）
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path as StdPath

import pytest

# Edge / Path の定義元は実装側（`src/c3/refgraph.py`）。
# テストがローカルに dataclass を定義して isinstance で突き合わせると、
# 実装側が「テストを import する」という依存の逆転を招く（配布物が tests/ に
# 依存することになり、wheel には tests/ が入らない）。定義元は 1 つに保つ。
from c3.refgraph import Edge, Path  # noqa: E402


@pytest.fixture
def repo_root():
    """C3 リポジトリルート."""
    return StdPath(__file__).parent.parent


@pytest.fixture
def graph(repo_root):
    """build_graph を呼んだ結果の Graph オブジェクト."""
    from c3.refgraph import build_graph
    return build_graph(repo_root)


class TestAdoptionConditions:
    """採用条件（§1・9ケース）"""

    def test_24_hook_files_reachable(self, graph):
        """`.claude/hooks/*.py` 24 本が到達可能."""
        hooks_dir = StdPath(__file__).parent.parent / ".claude" / "hooks"
        py_files = sorted(hooks_dir.glob("*.py"))
        assert len(py_files) == 24

        for hook_file in py_files:
            node_id = str(hook_file.relative_to(StdPath(__file__).parent.parent)).replace("\\", "/")
            assert graph.is_reachable(node_id), f"{node_id} should be reachable"

    def test_stop_py_reachable_via_importlib(self, graph):
        """stop.py が importlib 経由で到達可能."""
        assert graph.is_reachable(".claude/hooks/stop.py")

    def test_consolidate_memory_py_reachable_via_importlib(self, graph):
        """consolidate_memory.py が importlib 経由で到達可能."""
        assert graph.is_reachable(".claude/hooks/consolidate_memory.py")

    def test_tier_gap_check_py_reachable_via_importlib(self, graph):
        """tier_gap_check.py が importlib 経由で到達可能."""
        assert graph.is_reachable(".claude/hooks/tier_gap_check.py")

    def test_permission_handler_toast_py_reachable_via_subprocess(self, graph):
        """permission_handler_toast.py が subprocess 経由で到達可能."""
        assert graph.is_reachable(".claude/hooks/permission_handler_toast.py")

    def test_wt_systematic_debugger_md_reachable_via_agent_variant_map(self, graph):
        """wt_systematic-debugger.md が写像表経由で到達可能."""
        assert graph.is_reachable(".claude/agents/wt_systematic-debugger.md")

    def test_7_skill_scripts_reachable(self, graph):
        """`.claude/skills/*/scripts/*.py` 7 本が到達可能."""
        skills_dir = StdPath(__file__).parent.parent / ".claude" / "skills"
        script_files = sorted(skills_dir.glob("*/scripts/*.py"))
        assert len(script_files) == 7

        for script_file in script_files:
            node_id = str(script_file.relative_to(StdPath(__file__).parent.parent)).replace("\\", "/")
            assert graph.is_reachable(node_id)

    def test_stop_exit2_test_flag_unreachable_with_positive_case(self, graph):
        """`.claude/state/stop_exit2_test.flag` は到達不能。到達可能例と対にする.

        仕様§5-1 注記 3: ノード ID はルート相対。存在しないノード ID を渡すと
        実装に関係なく False を返すため、正確な ID が必須。
        仕様§5-1 注記 4: 到達不能だけを見ると、何も到達可能と判定しない実装でも
        緑になる。同じ graph 上で到達可能な既知ノードを対置して、判定自体が
        働いていることを担保する。
        """
        # Positive case: settings.json の hooks に登録されている（必ず到達可能）
        assert graph.is_reachable(".claude/hooks/session_start.py"), (
            "session_start.py is registered in settings.json hooks and must be reachable"
        )

        # Negative case: 書き手も読み手も存在しないフラグ
        assert not graph.is_reachable(".claude/state/stop_exit2_test.flag")

    def test_agent_runs_table_unreachable_with_positive_case(self, graph):
        """agent_runs テーブルは到達不能。同時に c3.db の到達可能を確認.

        仕様§5-1 注記 4: 到達不能テストは同じ辺種の到達可能例と対にして、
        辺の生成自体が働いていることを担保する。
        """
        # Positive case: production code が読み書きする db は到達可能
        assert graph.is_reachable("src/c3/db.py"), (
            "src/c3/db.py should be reachable (sql_table edges should exist)"
        )

        # Negative case: 参照のない agent_runs は到達不能
        assert not graph.is_reachable("sqltable:agent_runs")


class TestNegativeControls:
    """負の対照（§5・N-1）"""

    def test_n1_stop_py_has_no_terminal_settings_hook_edge(self, graph):
        """N-1: stop.py を終点とする settings_hook 辺が 1 本も無いこと.

        `settings.json` の `permissions.allow` にある
        `"Bash(c3 run .claude/hooks/stop.py*)"` はポリシー行であって hook 登録ではない。
        そこから辺を張ると「正しい答えを間違った経路で」出すことになる。

        仕様§5 N-1 注記: **経路全体の辺種で判定してはならない**。正しい経路は
        `settings.json --settings_hook--> session_stop.py --py_importlib--> stop.py`
        であり、途中に settings_hook を含むのが正常。判定するのは終点に入る辺。

        仕様§5-1 注記 1: paths が空ならループが 1 度も回らず緑になるため、
        assert len(paths) > 0 で経路の存在を先に検査する。
        """
        node_id = ".claude/hooks/stop.py"
        paths = graph.paths_to(node_id)
        assert len(paths) > 0, "stop.py should be reachable"

        for path in paths:
            terminal = path.edges[-1]
            assert terminal.target_node_id == node_id, (
                "the last edge of a path must terminate at the queried node"
            )
            assert terminal.kind != "settings_hook", (
                "stop.py is not registered in the hooks section; a settings_hook "
                f"edge into it means permissions.allow was treated as an invocation "
                f"(source: {terminal.source_file}:{terminal.source_line})"
            )

        # 正の対照: 正解の機構（session_stop.py の importlib）で到達していること
        assert any(path.edges[-1].kind == "py_importlib" for path in paths), (
            "stop.py must be reachable via the importlib load in session_stop.py"
        )


class TestSyntheticInputs:
    """合成入力での検査（§5・N-1/N-2）.

    仕様§5-1 注記 2: 参照先ファイルを実際に作る。
    作らないと「ノードが存在しない」ので必ず経路ゼロになり、空回りする。
    """

    def test_n1_synthetic_permissions_only_no_settings_hook(self):
        """N-1 合成版: permissions.allow だけの .py には settings_hook 辺を張らない.

        同じ合成 settings.json に **hooks 登録された .py** を対置する（仕様§5-1 注記 4）。
        正の側が赤になるので、辺を 1 本も作らない実装ではこのテストを通過できない。
        争点を「同じファイル形式・同じディレクトリで、hooks 節にあるか
        permissions 節にあるか」だけに絞っている。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = StdPath(tmpdir)

            # 参照先の .py を 2 本とも実際に作成（仕様§5-1 注記 2）
            settings_dir = tmpdir_path / ".claude"
            settings_dir.mkdir()
            hooks_dir = settings_dir / "hooks"
            hooks_dir.mkdir()
            (hooks_dir / "perm_only_hook.py").write_text("# synthetic\n", encoding="utf-8")
            (hooks_dir / "registered_hook.py").write_text("# synthetic\n", encoding="utf-8")

            # settings.json: 一方は permissions.allow のみ、他方は hooks に実登録
            # （hooks 節の形は実 settings.json と同形式にする）
            settings_file = settings_dir / "settings.json"
            settings_data = {
                "permissions": {
                    "allow": ["Bash(c3 run .claude/hooks/perm_only_hook.py)"]
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
            settings_file.write_text(json.dumps(settings_data, indent=2), encoding="utf-8")

            from c3.refgraph import build_graph
            graph_synth = build_graph(tmpdir_path)

            # Positive: hooks 節の登録は settings_hook 辺になる
            registered_paths = graph_synth.paths_to(".claude/hooks/registered_hook.py")
            assert len(registered_paths) > 0, (
                "a hook registered in the hooks section must be reachable"
            )
            assert any(
                "settings_hook" in [e.kind for e in p.edges] for p in registered_paths
            ), "the path to a registered hook must contain a 'settings_hook' edge"

            # Negative: permissions.allow だけの参照からは辺を張らない
            perm_only_paths = graph_synth.paths_to(".claude/hooks/perm_only_hook.py")
            assert perm_only_paths == [], (
                "permissions.allow is a policy entry, not an invocation; "
                f"expected no path, got {perm_only_paths}"
            )

    def test_n2_synthetic_prose_mention_no_agent_variant_map(self):
        """N-2 合成版: 散文の言及には agent_variant_map 辺を張らない.

        同じ SKILL.md に **写像表の行に出る agent** を対置する（仕様§5-1 注記 4）。
        正の側が赤になるので、辺を 1 本も作らない実装ではこのテストを通過できない。
        争点を「同じファイル・同じ agent 名の形で、表の行にあるか本文にあるか」
        だけに絞っている。これは pivot §7 の誤判定 1（wt_systematic-debugger を
        散文参照しか無いと読んで削除可と誤判定した）を機械化したもの。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = StdPath(tmpdir)

            # 参照先の agent .md を 2 本とも実際に作成（仕様§5-1 注記 2）
            agents_dir = tmpdir_path / ".claude" / "agents"
            agents_dir.mkdir(parents=True)
            (agents_dir / "wt_prose_only.md").write_text(
                "# Prose Only Agent\n", encoding="utf-8"
            )
            (agents_dir / "wt_table_row.md").write_text(
                "# Table Row Agent\n", encoding="utf-8"
            )

            # SKILL.md: 一方は本文だけ、他方は写像表の行に出す
            skill_dir = tmpdir_path / ".claude" / "skills" / "test-skill"
            skill_dir.mkdir(parents=True)
            skill_md = skill_dir / "SKILL.md"
            skill_md.write_text(
                "# Test Skill\n"
                "\n"
                "The `wt_prose_only` agent is useful for debugging.\n"  # ← 本文だけ
                "\n"
                "| 元 | 並列バリアント | 備考 |\n"
                "| --- | --- | --- |\n"
                "| `table_row` | `wt_table_row` | worktree 専用 |\n",
                # Windows の既定は cp932。明示しないと日本語が cp932 で書かれ、
                # UTF-8 で読む抽出器が UnicodeDecodeError で沈黙し、辺ゼロになる
                # （実際にこれで 1 件落ちた。C3 の実 SKILL.md は日本語なので
                # 非 ASCII を含む表を検査対象に残す意味がある）
                encoding="utf-8",
            )

            from c3.refgraph import build_graph
            graph_synth = build_graph(tmpdir_path)

            # Positive: 写像表の行は agent_variant_map 辺になる
            table_paths = graph_synth.paths_to(".claude/agents/wt_table_row.md")
            assert len(table_paths) > 0, (
                "an agent named in a variant mapping table row must be reachable"
            )
            assert any(
                "agent_variant_map" in [e.kind for e in p.edges] for p in table_paths
            ), "the path to a table-row agent must contain an 'agent_variant_map' edge"

            # Negative: 散文の言及からは agent_variant_map 辺を張らない
            prose_paths = graph_synth.paths_to(".claude/agents/wt_prose_only.md")
            assert not any(
                "agent_variant_map" in [e.kind for e in p.edges] for p in prose_paths
            ), (
                "a prose mention is documentation, not an invocation; "
                f"expected no 'agent_variant_map' edge, got {prose_paths}"
            )


class TestUnreadableFiles:
    """読めないファイルの扱い（削除安全網としての fail-closed）.

    この道具は「削除してよいか」を判定する安全網なので、読めない・デコードできない
    ファイルの発信辺が黙って消えると、そのファイルが参照している対象が
    「到達不能」に見える＝**削除候補に化ける**。沈黙は許されない。

    2026-08-05: fail-open を潰す是正が、記録後の `continue` を落として
    未代入変数に到達する `UnboundLocalError` を 8 箇所に作った。
    実リポジトリが全ファイル UTF-8 で読めるため 2800 件のテストは緑のままだった。
    ここを機械で押さえる。
    """

    def _build_tree_with_one_undecodable_file(self, tmpdir_path):
        """デコード不能な SKILL.md と、正常に辺を張る SKILL.md を 1 つずつ置く."""
        agents_dir = tmpdir_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "wt_good.md").write_text("# ok\n", encoding="utf-8")

        # 正常側: UTF-8 で書かれ、写像表の行から辺を張る
        good_dir = tmpdir_path / ".claude" / "skills" / "good-skill"
        good_dir.mkdir(parents=True)
        (good_dir / "SKILL.md").write_text(
            "# Good Skill\n"
            "\n"
            "| base | variant |\n"
            "| --- | --- |\n"
            "| `good` | `wt_good` |\n",
            encoding="utf-8",
        )

        # 異常側: cp932 で日本語を書く → UTF-8 としては不正なバイト列
        bad_dir = tmpdir_path / ".claude" / "skills" / "bad-skill"
        bad_dir.mkdir(parents=True)
        (bad_dir / "SKILL.md").write_bytes("# 壊れた見出し\n".encode("cp932"))
        return ".claude/skills/bad-skill/SKILL.md"

    def test_undecodable_file_is_surfaced_to_the_caller(self):
        """読めなかったファイルが呼び出し側から見えること（fail-closed）.

        沈黙して continue すると、その発信辺が消えたことに誰も気づけない。
        build_graph が例外を漏らす（`UnboundLocalError` 等）場合もここで落ちるので、
        クラッシュ回帰は本ケースが兼ねる（「落ちないこと」だけを見る単独テストは
        do-nothing スタブでも緑になるため置かない・仕様§6 完了条件 6）。
        """
        from c3.refgraph import build_graph

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = StdPath(tmpdir)
            bad_rel = self._build_tree_with_one_undecodable_file(tmpdir_path)

            graph = build_graph(tmpdir_path)

            reported = [
                str(entry[0]) if isinstance(entry, (tuple, list)) else str(entry)
                for entry in graph.unreadable
            ]

            # 区切りはノード ID と同じ規約（ルート相対 POSIX・仕様§2）であること。
            # バックスラッシュが混ざると Windows で node_id と突き合わせできず、
            # 削除判定でこの報告を使えない。
            assert all("\\" not in p for p in reported), (
                "unreadable paths must use POSIX separators like node ids; "
                f"got {reported!r}"
            )

            assert bad_rel in reported, (
                "the undecodable file must be reported via Graph.unreadable; "
                f"got {graph.unreadable!r}"
            )

            # 抽出器の数だけ同じファイルを重複報告しない（読み手のノイズになる）
            assert reported.count(bad_rel) == 1, (
                f"each unreadable file should be reported once; got {reported!r}"
            )

    def test_one_undecodable_file_does_not_suppress_other_edges(self):
        """1 本読めなくても、他のファイルの辺は失われないこと（正の双子）.

        仕様§5-1 注記 4: 到達不能側だけを見ると、辺を 1 本も作らない実装でも
        緑になる。同じツリーに到達可能であるべき対照を置いて担保する。
        """
        from c3.refgraph import build_graph

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = StdPath(tmpdir)
            self._build_tree_with_one_undecodable_file(tmpdir_path)

            graph = build_graph(tmpdir_path)

            paths = graph.paths_to(".claude/agents/wt_good.md")
            assert len(paths) > 0, (
                "an unrelated readable SKILL.md must still produce its edges"
            )
            assert any(
                "agent_variant_map" in [e.kind for e in p.edges] for p in paths
            )


class TestPathVerification:
    """経路検査（§6 完了条件 3, 4）"""

    def test_wt_systematic_debugger_has_agent_variant_map_edge(self, graph):
        """wt_systematic-debugger.md への経路に agent_variant_map が含まれる."""
        node_id = ".claude/agents/wt_systematic-debugger.md"
        paths = graph.paths_to(node_id)
        assert len(paths) > 0

        has_edge = any(
            "agent_variant_map" in [e.kind for e in path.edges]
            for path in paths
        )
        assert has_edge

    def test_stop_py_has_no_terminal_settings_hook_edge(self, graph):
        """stop.py を終点とする settings_hook 辺が無い（仕様§6 完了条件 4）."""
        node_id = ".claude/hooks/stop.py"
        paths = graph.paths_to(node_id)
        assert len(paths) > 0

        for path in paths:
            assert path.edges[-1].kind != "settings_hook"


class TestEdgeTypes:
    """辺のデータ構造（developer の契約）"""

    def test_edge_is_dataclass_instance(self, graph):
        """Edge が Edge dataclass のインスタンスであること."""
        node_id = ".claude/hooks/stop.py"
        paths = graph.paths_to(node_id)
        assert len(paths) > 0

        for path in paths:
            for edge in path.edges:
                assert isinstance(edge, Edge), (
                    f"Edge must be Edge dataclass instance, got {type(edge)}"
                )

    def test_edge_kind_valid(self, graph):
        """Edge.kind が 9 種のいずれかであること."""
        valid_kinds = {
            "settings_hook", "c3_run", "code_span_path", "agent_variant_map",
            "py_import", "py_importlib", "py_subprocess_path", "subagent_type", "sql_table"
        }

        node_id = ".claude/hooks/stop.py"
        paths = graph.paths_to(node_id)
        assert len(paths) > 0

        for path in paths:
            for edge in path.edges:
                assert edge.kind in valid_kinds


class TestGraphInterface:
    """API 型検査（§2・仕様§6 完了条件 6）.

    注: この 3 ケースのみ do-nothing スタブで緑になってよい。
    他が緑になったら空回りしている。
    """

    def test_build_graph_returns_object(self, repo_root):
        """build_graph(root) が Graph を返す."""
        from c3.refgraph import build_graph
        g = build_graph(repo_root)
        assert g is not None

    def test_is_reachable_returns_bool(self, graph):
        """Graph.is_reachable(node_id) が bool を返す."""
        result = graph.is_reachable(".claude/hooks/stop.py")
        assert isinstance(result, bool)

    def test_paths_to_returns_list_of_path(self, graph):
        """Graph.paths_to(node_id) が list[Path] を返す."""
        result = graph.paths_to(".claude/hooks/stop.py")
        assert isinstance(result, list)
        if result:
            assert all(isinstance(p, Path) for p in result)


class TestUserReflections:
    """ユーザー反例（自作）"""

    def test_counterexample_1_db_reachable_via_import(self, graph):
        """反例1: c3.db は import 経由で到達可能（settings 非登録）."""
        node_id = "src/c3/db.py"
        repo_root = StdPath(__file__).parent.parent
        assert (repo_root / node_id).exists()

        assert graph.is_reachable(node_id)

        # settings_hook ではなく import 経由
        paths = graph.paths_to(node_id)
        assert len(paths) > 0
        for path in paths:
            if "settings_hook" in [e.kind for e in path.edges]:
                raise AssertionError(
                    "db.py should not be reachable via settings_hook (imported, not registered)"
                )

    def test_counterexample_2_cli_entry_point(self, graph):
        """反例2: cli.py はエントリポイント経由で到達可能."""
        node_id = "src/c3/cli.py"
        repo_root = StdPath(__file__).parent.parent
        assert (repo_root / node_id).exists()

        assert graph.is_reachable(node_id)

    def test_counterexample_3_hook_utils_shared_import(self, graph):
        """反例3: _hook_utils.py は複数フックから import される."""
        node_id = ".claude/hooks/_hook_utils.py"
        repo_root = StdPath(__file__).parent.parent

        if not (repo_root / node_id).exists():
            pytest.skip(f"{node_id} does not exist")

        assert graph.is_reachable(node_id), (
            "_hook_utils.py should be reachable (shared utility)"
        )


class TestCompletionCriteria:
    """§6 完了条件の統合検査"""

    def test_all_adoption_conditions_met(self, graph, repo_root):
        """完了条件 1: 採用条件 7 行すべて期待どおり."""
        # 24 フック
        hooks = sorted((repo_root / ".claude" / "hooks").glob("*.py"))
        assert len(hooks) == 24
        for hook in hooks:
            node_id = str(hook.relative_to(repo_root)).replace("\\", "/")
            assert graph.is_reachable(node_id)

        # 3 つの importlib
        for name in ["stop.py", "consolidate_memory.py", "tier_gap_check.py"]:
            assert graph.is_reachable(f".claude/hooks/{name}")

        # subprocess
        assert graph.is_reachable(".claude/hooks/permission_handler_toast.py")

        # 写像表
        assert graph.is_reachable(".claude/agents/wt_systematic-debugger.md")

        # 7 スキル
        skills = sorted((repo_root / ".claude" / "skills").glob("*/scripts/*.py"))
        assert len(skills) == 7
        for skill in skills:
            node_id = str(skill.relative_to(repo_root)).replace("\\", "/")
            assert graph.is_reachable(node_id)

        # 到達不能 2 件
        assert not graph.is_reachable(".claude/state/stop_exit2_test.flag")
        assert not graph.is_reachable("sqltable:agent_runs")

    def test_negative_controls_met(self, graph):
        """完了条件 2: 負の対照 N-1/N-2/N-3 すべて期待どおり."""
        # N-1: stop.py を終点とする settings_hook 辺が無い
        paths = graph.paths_to(".claude/hooks/stop.py")
        assert len(paths) > 0
        for path in paths:
            assert path.edges[-1].kind != "settings_hook"

        # N-3
        assert not graph.is_reachable(".claude/state/stop_exit2_test.flag")

    def test_wt_systematic_debugger_agent_variant_map(self, graph):
        """完了条件 3: wt_systematic-debugger への agent_variant_map 辺."""
        paths = graph.paths_to(".claude/agents/wt_systematic-debugger.md")
        assert any("agent_variant_map" in [e.kind for e in p.edges] for p in paths)

    def test_stop_py_no_terminal_settings_hook(self, graph):
        """完了条件 4: stop.py を終点とする settings_hook 辺なし."""
        paths = graph.paths_to(".claude/hooks/stop.py")
        assert len(paths) > 0, "stop.py should be reachable"
        for path in paths:
            assert path.edges[-1].kind != "settings_hook"
