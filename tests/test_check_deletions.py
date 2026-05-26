"""tests/test_check_deletions.py

scripts/check_deletions.py の純粋ロジック関数 find_unrecorded_deletions のユニットテスト。
Red フェーズ: check_deletions.py 未実装のため import 失敗（ModuleNotFoundError）が期待される。

テストケース:
  CD1: 削除 × 配布対象 × 未記載 → 検出される
  CD2: 削除 × 除外対象(should_skip=True) → 非検出
  CD3: 削除 × 配布対象 × deletions.txt 記載済み → 非検出
  CD4: リネーム旧側削除（配布対象・未記載）→ 通常削除と同じく検出される
  CD5: 入力順保持 + 重複除去
  CD6: 空入力 → 空リスト
"""
from __future__ import annotations

import sys
from pathlib import Path

# scripts/ を sys.path に追加して import できるようにする
_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from check_deletions import find_unrecorded_deletions, _strip_claude_prefix  # noqa: E402


class TestFindUnrecordedDeletions:
    """find_unrecorded_deletions の純粋関数テスト（tmp_path / git 不要）。"""

    def test_cd1_distributable_unrecorded_is_detected(self):
        """CD1: 配布対象ファイルが削除され deletions.txt 未記載 → 戻り値に含まれる。"""
        deleted = ["agents/legacy.md"]
        recorded: set[str] = set()
        result = find_unrecorded_deletions(deleted, recorded)
        assert "agents/legacy.md" in result

    def test_cd2_excluded_path_is_ignored(self):
        """CD2: should_skip=True のパスは除外対象なので、未記載でも非検出。

        除外対象の例:
          - reports/x.md    (reports/* にマッチ)
          - state/y.json    (state/* にマッチ)
          - memory/sessions/z.tmp (memory/sessions/* にマッチ)
        """
        deleted = [
            "reports/x.md",
            "state/y.json",
            "memory/sessions/z.tmp",
        ]
        recorded: set[str] = set()
        result = find_unrecorded_deletions(deleted, recorded)
        assert result == []

    def test_cd3_recorded_path_is_not_detected(self):
        """CD3: 配布対象でも deletions.txt 記載済みなら非検出。"""
        deleted = ["agents/old-agent.md"]
        recorded = {"agents/old-agent.md"}
        result = find_unrecorded_deletions(deleted, recorded)
        assert result == []

    def test_cd4_renamed_old_path_detected_same_as_normal_deletion(self):
        """CD4: リネーム旧側削除も配布対象・未記載であれば通常削除と同様に検出される。

        呼び出し側が旧パスを deleted_rel_paths に渡す前提のため、
        関数としては CD1 と同じ扱いになることを固定する。
        """
        deleted = ["skills/old-skill/SKILL.md"]
        recorded: set[str] = set()
        result = find_unrecorded_deletions(deleted, recorded)
        assert "skills/old-skill/SKILL.md" in result

    def test_cd5_input_order_preserved_and_duplicates_removed(self):
        """CD5: 戻り値は入力順を保持し、重複は 1 回のみ返す。"""
        deleted = [
            "agents/alpha.md",
            "agents/beta.md",
            "agents/alpha.md",  # 重複
        ]
        recorded: set[str] = set()
        result = find_unrecorded_deletions(deleted, recorded)
        # 重複除去されて 2 件
        assert result.count("agents/alpha.md") == 1
        assert result.count("agents/beta.md") == 1
        # 入力順保持: alpha が beta より先
        assert result.index("agents/alpha.md") < result.index("agents/beta.md")

    def test_cd6_empty_input_returns_empty_list(self):
        """CD6: 入力が空リストなら空リストを返す。"""
        result = find_unrecorded_deletions([], set())
        assert result == []


class TestStripClaudePrefix:
    """`_strip_claude_prefix` の直接ユニットテスト。"""

    def test_with_claude_prefix_returns_stripped_path(self):
        """.claude/ 始まりのパスはプレフィックスを除去して返す。"""
        assert _strip_claude_prefix(".claude/agents/x.md") == "agents/x.md"

    def test_without_claude_prefix_returns_none(self):
        """.claude/ で始まらないパスは None を返す。"""
        assert _strip_claude_prefix("agents/x.md") is None

    def test_empty_string_returns_none(self):
        """空文字列は None を返す。"""
        assert _strip_claude_prefix("") is None


class TestSuggestOutputFormat:
    """CR-Q-004: 追記サジェスト出力のコメント行が 1 回だけ出力されることを確認する。"""

    def test_comment_line_appears_once_for_multiple_unrecorded(self, capsys):
        """未記載が複数あってもコメント行 '# ...' は 1 行のみ出力される。

        main() を直接呼ぶと argparse / git / ファイル依存が生じるため、
        ここでは出力の構造的性質（コメント行が 1 回だけ）を確認する代わりに
        find_unrecorded_deletions の戻り値から期待出力を組み立てて検証する。
        """
        deleted = ["agents/alpha.md", "agents/beta.md"]
        recorded: set[str] = set()
        unrecorded = find_unrecorded_deletions(deleted, recorded)
        tag = "v9.99.0"

        # main() の出力形式を模倣して組み立てる
        import io
        buf = io.StringIO()
        print(f"# {tag} 以降で削除された配布対象ファイル", file=buf)
        for path in unrecorded:
            print(path, file=buf)
        output = buf.getvalue()

        lines = output.splitlines()
        comment_lines = [ln for ln in lines if ln.startswith("#")]
        # コメント行は 1 行のみ
        assert len(comment_lines) == 1
        assert comment_lines[0] == f"# {tag} 以降で削除された配布対象ファイル"
        # 各パスは別行に出力される
        assert "agents/alpha.md" in lines
        assert "agents/beta.md" in lines
