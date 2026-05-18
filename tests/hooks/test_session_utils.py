"""Tests for .claude/hooks/session_utils.py

テストケース:
1. is_worktree()
   - .git がファイルのとき True を返す
   - .git がディレクトリのとき False を返す
   - .git が存在しないとき False を返す

2. create_session_template(date_str)
   - 返り値に SESSION: {date_str} が含まれる
   - 返り値に ## うまくいったアプローチ が含まれる
   - 返り値に ## 残タスク が含まれる
   - 返り値に <!-- C3:SESSION:JSON が含まれる（マーカーを使っているか）

3. append_checkpoint(session_file, label, summary)
   - ファイルが存在しない場合、テンプレートを書いてからチェックポイントを追記する
   - ファイルが存在する場合、追記のみ行う（既存内容を消さない）
   - 追記ブロックに ## [Checkpoint: {label} が含まれる
   - TOCTOU テスト: open('x') + FileExistsError パターンを使っているか AST で検証する
   - summary の --> サニタイズテスト: summary に --> が含まれる場合 -- > に置換されること（Low 指摘対応）
"""

from __future__ import annotations

import ast
import importlib.util
import os
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "session_utils.py"


def _load_module() -> types.ModuleType:
    """session_utils.py をモジュールとしてロードする（__main__ 実行なし）。"""
    spec = importlib.util.spec_from_file_location("session_utils", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def _parse_source() -> ast.Module:
    return ast.parse(HOOK_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. is_worktree()
# ---------------------------------------------------------------------------


class TestIsWorktree:
    """is_worktree() が .git の種類に応じて正しい値を返すこと。"""

    def test_returns_true_when_git_is_file(self, tmp_path: Path):
        """.git がファイルのとき（git worktree）True を返す。"""
        git_file = tmp_path / ".git"
        git_file.write_text("gitdir: ../real/.git", encoding="utf-8")

        module = _load_module()
        assert module.is_worktree(str(tmp_path)) is True

    def test_returns_false_when_git_is_directory(self, tmp_path: Path):
        """.git がディレクトリのとき False を返す。"""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        module = _load_module()
        assert module.is_worktree(str(tmp_path)) is False

    def test_returns_false_when_git_does_not_exist(self, tmp_path: Path):
        """.git が存在しないとき False を返す。"""
        module = _load_module()
        assert module.is_worktree(str(tmp_path)) is False


# ---------------------------------------------------------------------------
# 2. create_session_template(date_str)
# ---------------------------------------------------------------------------


class TestCreateSessionTemplate:
    """create_session_template() の返り値が期待するコンテンツを含むこと。"""

    def test_contains_session_date_str(self):
        """返り値に SESSION: {date_str} が含まれる。"""
        module = _load_module()
        date_str = "20260505"
        result = module.create_session_template(date_str)
        assert f"SESSION: {date_str}" in result

    def test_contains_success_section(self):
        """返り値に ## うまくいったアプローチ が含まれる。"""
        module = _load_module()
        result = module.create_session_template("20260505")
        assert "## うまくいったアプローチ" in result

    def test_contains_todos_section(self):
        """返り値に ## 残タスク が含まれる。"""
        module = _load_module()
        result = module.create_session_template("20260505")
        assert "## 残タスク" in result

    def test_contains_json_marker(self):
        """返り値に <!-- C3:SESSION:JSON マーカーが含まれる。"""
        module = _load_module()
        result = module.create_session_template("20260505")
        assert "<!-- C3:SESSION:JSON" in result

    def test_does_not_contain_task_type_line(self):
        """テンプレートから TASK_TYPE: 行が撤去されていること（task_type 概念の廃止）。"""
        module = _load_module()
        result = module.create_session_template("20260505")
        assert "TASK_TYPE" not in result

    def test_header_line_order(self):
        """ヘッダ行は SESSION: -> AGENT: -> DURATION: の順に配置される。"""
        module = _load_module()
        result = module.create_session_template("20260505")
        lines = result.split("\n")
        assert lines[0].startswith("SESSION: ")
        assert lines[1].startswith("AGENT:")
        assert lines[2].startswith("DURATION:")


# ---------------------------------------------------------------------------
# 3. append_checkpoint(session_file, label, summary)
# ---------------------------------------------------------------------------


class TestAppendCheckpoint:
    """append_checkpoint() のファイル書き込みと追記の動作を検証する。"""

    def test_creates_file_with_template_when_not_exists(self, tmp_path: Path):
        """ファイルが存在しない場合、テンプレートを書いてからチェックポイントを追記する。"""
        module = _load_module()
        session_file = str(tmp_path / "20260505.tmp")

        module.append_checkpoint(session_file, "TestLabel", "summary body")

        content = Path(session_file).read_text(encoding="utf-8")
        # テンプレートが書かれていることを確認
        assert "SESSION: 20260505" in content
        assert "## うまくいったアプローチ" in content

    def test_preserves_existing_content(self, tmp_path: Path):
        """ファイルが存在する場合、追記のみ行い既存内容を消さない。"""
        module = _load_module()
        session_file = tmp_path / "20260505.tmp"
        existing_content = "既存のコンテンツ\n"
        session_file.write_text(existing_content, encoding="utf-8")

        module.append_checkpoint(str(session_file), "TestLabel", "summary body")

        content = session_file.read_text(encoding="utf-8")
        assert existing_content.strip() in content, (
            "既存のコンテンツが消えている。追記のみ行うべき。"
        )

    def test_checkpoint_block_contains_label(self, tmp_path: Path):
        """追記ブロックに ## [Checkpoint: {label} が含まれる。"""
        module = _load_module()
        session_file = str(tmp_path / "20260505.tmp")
        label = "Wave 1 success"

        module.append_checkpoint(session_file, label, "summary body")

        content = Path(session_file).read_text(encoding="utf-8")
        assert f"## [Checkpoint: {label}" in content

    def test_checkpoint_contains_summary(self, tmp_path: Path):
        """追記ブロックにサマリーのテキストが含まれる。"""
        module = _load_module()
        session_file = str(tmp_path / "20260505.tmp")
        summary = "これはサマリーテキストです"

        module.append_checkpoint(session_file, "label", summary)

        content = Path(session_file).read_text(encoding="utf-8")
        assert summary in content

    def test_toctou_uses_open_x_mode(self):
        """`append_checkpoint` が `open('x')` + `FileExistsError` パターンを使用していることを AST 検証する。"""
        tree = _parse_source()

        # append_checkpoint 関数ノードを探す
        fn_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "append_checkpoint":
                fn_node = node
                break

        assert fn_node is not None, "append_checkpoint 関数が session_utils.py に見つからない"

        # 関数内の open() 呼び出しを収集し、'x' モードが使われているか確認する
        has_open_x_mode = False
        for node in ast.walk(fn_node):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # open() または builtins.open() の呼び出しを確認
            is_open_call = (
                (isinstance(func, ast.Name) and func.id == "open")
                or (isinstance(func, ast.Attribute) and func.attr == "open")
            )
            if not is_open_call:
                continue
            # 引数を確認: open(path, 'x') または open(path, mode='x')
            for arg in node.args:
                if isinstance(arg, ast.Constant) and arg.value == "x":
                    has_open_x_mode = True
                    break
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant) and kw.value.value == "x":
                    has_open_x_mode = True
                    break

        assert has_open_x_mode, (
            "append_checkpoint は open('x') + FileExistsError パターンを使っていない。\n"
            "現在は open('w') を使っているため、TOCTOU 競合状態が発生しうる。\n"
            "修正: open(session_file, 'x') を使い FileExistsError をキャッチして追記に切り替える。"
        )

    def test_append_checkpoint_sanitizes_comment_closer_in_summary(self, tmp_path: Path):
        """`summary` に `-->` が含まれる場合、`-- >` に置換されてファイルに書き込まれること。

        `<!-- C3:SESSION:JSON ... -->` ブロックはセッションファイルのメタデータを保持する。
        `summary` に `-->` がそのまま含まれると、このブロックが途中で閉じられてしまい
        JSON パースが壊れる可能性がある。

        実装側で `body = summary.strip().replace('-->', '-- >')` 採用済み。
        本テストは将来素通しに退行しないかを守る Green 回帰防止テスト。
        """
        module = _load_module()
        session_file = str(tmp_path / "20260505.tmp")
        # summary に --> を含める（HTMLコメント終端を模したインジェクション）
        summary = "作業完了 --> 次のフェーズへ"

        module.append_checkpoint(session_file, "TestLabel", summary)

        content = Path(session_file).read_text(encoding="utf-8")
        # --> がそのまま書き込まれていないこと（-- > に置換されていること）
        assert "-->" not in content, (
            "summary に含まれる `-->` がサニタイズされずにファイルへ書き込まれている。\n"
            "`<!-- C3:SESSION:JSON ... -->` ブロックが破壊される可能性がある。\n"
            "修正: `body = summary.strip().replace('-->', '-- >')` を追加すること。"
        )
        # サニタイズ後の値 '-- >' が書き込まれていること
        assert "-- >" in content, (
            "`-->` が `-- >` に置換されていない。サニタイズ処理が正しく動作していない。"
        )
