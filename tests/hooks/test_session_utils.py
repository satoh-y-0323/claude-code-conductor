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
   - label に C1/DEL を含む場合に除去されること（F2 後の期待値 / CR M-02）
   - body（summary）の複数行が保持されること（F2 §3.2 非互換回避の回帰防止）

4. sanitize_value(text)（F2 / SR M-1 / CR M-02 / SR L-1）
   - DEL（\\x7f）が除去される
   - C1 制御文字（\\x80-\\x9f / CSI=\\x9b 等）が除去される
   - U+2028（Line Separator）が除去される
   - U+2029（Paragraph Separator）が除去される
   - \\t（タブ）は保持される（SR L-1）
   - --> が -- > に置換される
   - 改行（\\n/\\r）が除去される
   - 通常の ASCII 文字・日本語は不変
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

    def test_contains_genba_field(self):
        """テンプレートに「現在地: 」行が含まれる（AC-1）。

        「現在地:」は DURATION: の直後・空行の前に配置される行フィールド。
        architecture §2.1 に従い、SESSION: / AGENT: / DURATION: / 現在地: の
        メタ行クラスタに連続して存在し、## うまくいったアプローチ より前に位置する。
        """
        module = _load_module()
        result = module.create_session_template("20260505")
        assert "現在地:" in result, (
            "テンプレートに「現在地:」行が含まれていない。"
            "DURATION: の直後に「現在地: \\n」を追加すること（AC-1）。"
        )

    def test_genba_field_position_after_duration_before_sections(self):
        """「現在地:」行が DURATION: の直後かつ ## うまくいったアプローチ の前に位置する。

        architecture §2.1 のヘッダ順序: SESSION: -> AGENT: -> DURATION: -> 現在地: ->
        空行 -> ## うまくいったアプローチ
        """
        module = _load_module()
        result = module.create_session_template("20260505")
        lines = result.split("\n")

        duration_idx = next(
            (i for i, line in enumerate(lines) if line.startswith("DURATION:")), None
        )
        genba_idx = next(
            (i for i, line in enumerate(lines) if line.startswith("現在地:")), None
        )
        success_idx = next(
            (i for i, line in enumerate(lines) if line.startswith("## うまくいったアプローチ")),
            None,
        )

        assert duration_idx is not None, "DURATION: 行がテンプレートに存在しない"
        assert genba_idx is not None, (
            "「現在地:」行がテンプレートに存在しない。DURATION: の直後に追加すること（AC-1）。"
        )
        assert success_idx is not None, "## うまくいったアプローチ 行がテンプレートに存在しない"

        assert genba_idx == duration_idx + 1, (
            f"「現在地:」行（行 {genba_idx}）は DURATION:（行 {duration_idx}）の"
            f"直後に配置されるべき。現在の位置: {genba_idx}"
        )
        assert genba_idx < success_idx, (
            f"「現在地:」行（行 {genba_idx}）は ## うまくいったアプローチ（行 {success_idx}）"
            f"より前に配置されるべき。"
        )


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

    def test_label_with_c1_del_characters_is_sanitized(self, tmp_path: Path):
        """label に C1 制御文字（\\x9b）・DEL（\\x7f）が含まれる場合、除去されること（F2 / CR M-02）。

        F2 で sanitize_value が共通化され、label の DEL/C1 制御文字を除去する確定仕様。
        """
        module = _load_module()
        session_file = str(tmp_path / "20260505.tmp")
        label = "Wave\x9b 1\x7f success"  # C1 CSI + DEL を含む label

        module.append_checkpoint(session_file, label, "summary body")

        content = Path(session_file).read_text(encoding="utf-8")
        # C1 制御文字・DEL がファイルに書き込まれていないこと
        assert "\x9b" not in content, (
            "label の C1 制御文字 CSI (\\x9b) が除去されずにファイルへ書き込まれている（F2 / CR M-02）。"
            "sanitize_value 共通化後は DEL/C1 も除去されるべき。"
        )
        assert "\x7f" not in content, (
            "label の DEL (\\x7f) が除去されずにファイルへ書き込まれている（F2 / CR M-02）。"
        )

    def test_body_multiline_is_preserved(self, tmp_path: Path):
        """summary（body）の複数行が保持されること（F2 §3.2 非互換回避の回帰防止）。

        sanitize_value は改行を除去するが、body（summary）には適用しない設計（F2 §3.2）。
        body に sanitize_value が誤って適用されると複数行が1行化してしまう。
        本テストはその非互換を回避するための回帰防止テストとして機能する。
        """
        module = _load_module()
        session_file = str(tmp_path / "20260505.tmp")
        summary = "行1: 成功\n行2: 詳細情報\n行3: 追加メモ"

        module.append_checkpoint(session_file, "TestLabel", summary)

        content = Path(session_file).read_text(encoding="utf-8")
        # summary の各行が独立した行として保持されていること
        assert "行1: 成功" in content, "summary の1行目が保持されていない。"
        assert "行2: 詳細情報" in content, "summary の2行目が保持されていない。"
        assert "行3: 追加メモ" in content, "summary の3行目が保持されていない。"
        # 複数行として保持されていること（改行が1行化されていないこと）
        assert "行1: 成功\n行2: 詳細情報" in content, (
            "summary の複数行が保持されていない（改行が除去されて1行化している可能性）。"
            "body には sanitize_value を適用しない設計（F2 §3.2）に違反している。"
        )


# ---------------------------------------------------------------------------
# 4. sanitize_value(text)（F2 / SR M-1 / CR M-02 / SR L-1）
# ---------------------------------------------------------------------------

# テストファイル内で U+2028/U+2029 実体を文字列リテラルとして使わないよう定義する
_LS = chr(0x2028)  # Line Separator（U+2028）
_PS = chr(0x2029)  # Paragraph Separator（U+2029）


class TestSanitizeValue:
    """session_utils.sanitize_value(text) の単体テスト（F2 / SR M-1 / CR M-02 / SR L-1）。

    session_utils.py の共通サニタイズ関数 sanitize_value の仕様を固定する。

    設計（plan-report §3.1）:
    - 改行（\\n / \\r）を除去
    - C0/C1 制御文字・DEL・U+2028/U+2029 を除去（\\t は保持）
    - --> を -- > に置換
    """

    def _load(self) -> object:
        return _load_module()

    def test_removes_del_character(self) -> None:
        """DEL（\\x7f）が除去されること（SR M-1）。"""
        module = self._load()
        result = module.sanitize_value("abc\x7fdef")
        assert "\x7f" not in result, f"DEL (\\x7f) が除去されていない。実際: {result!r}"
        assert "abcdef" in result or result == "abcdef", (
            f"DEL 除去後に 'abcdef' が残るべき。実際: {result!r}"
        )

    def test_removes_c1_csi_character(self) -> None:
        """C1 制御文字 CSI（\\x9b）が除去されること（SR M-1）。"""
        module = self._load()
        result = module.sanitize_value("abc\x9bdef")
        assert "\x9b" not in result, f"C1 CSI (\\x9b) が除去されていない。実際: {result!r}"

    def test_removes_c1_range_characters(self) -> None:
        """C1 制御文字範囲（\\x80-\\x9f）が除去されること（SR M-1）。"""
        module = self._load()
        for codepoint in range(0x80, 0xA0):
            char = chr(codepoint)
            result = module.sanitize_value(f"abc{char}def")
            assert char not in result, (
                f"C1 文字 U+{codepoint:04X} が除去されていない。実際: {result!r}"
            )

    def test_removes_unicode_line_separator(self) -> None:
        """U+2028（Line Separator）が除去されること（SR M-1）。"""
        module = self._load()
        result = module.sanitize_value("abc" + _LS + "def")
        assert _LS not in result, (
            f"U+2028 (Line Separator) が除去されていない。実際: {result!r}"
        )

    def test_removes_unicode_paragraph_separator(self) -> None:
        """U+2029（Paragraph Separator）が除去されること（SR M-1）。"""
        module = self._load()
        result = module.sanitize_value("abc" + _PS + "def")
        assert _PS not in result, (
            f"U+2029 (Paragraph Separator) が除去されていない。実際: {result!r}"
        )

    def test_preserves_tab_character(self) -> None:
        """\\t（タブ文字）は保持されること（SR L-1：タブ保持の設計意図）。

        タブは現在地値の正当なユースケースとして許容される設計。
        既存の append_checkpoint と挙動を一致させるための意図的な設計（SR L-1）。
        """
        module = self._load()
        result = module.sanitize_value("abc\tdef")
        assert "\t" in result, f"タブ (\\t) が除去されている。SR L-1 の設計に反する。実際: {result!r}"

    def test_replaces_comment_closer(self) -> None:
        """'-->' を '-- >' に置換すること。"""
        module = self._load()
        result = module.sanitize_value("abc --> def")
        assert "-->" not in result, f"'-->' が置換されていない。実際: {result!r}"
        assert "-- >" in result, f"'-- >' への置換がされていない。実際: {result!r}"

    def test_removes_newline(self) -> None:
        """改行文字（\\n）が除去されること。"""
        module = self._load()
        result = module.sanitize_value("abc\ndef")
        assert "\n" not in result, f"改行 (\\n) が除去されていない。実際: {result!r}"

    def test_removes_carriage_return(self) -> None:
        """キャリッジリターン（\\r）が除去されること。"""
        module = self._load()
        result = module.sanitize_value("abc\rdef")
        assert "\r" not in result, f"CR (\\r) が除去されていない。実際: {result!r}"

    def test_normal_ascii_and_japanese_preserved(self) -> None:
        """通常の ASCII 文字と日本語テキストは変更されないこと（過剰除去しない）。"""
        module = self._load()
        text = "フェーズD 実装中 / abc123 !@# ok"
        result = module.sanitize_value(text)
        assert result == text, (
            f"通常文字が意図せず変更されている。元: {text!r}、実際: {result!r}"
        )

    def test_removes_c0_control_characters(self) -> None:
        """C0 制御文字（\\x00-\\x08/\\x0b-\\x1f）が除去されること（\\t=\\x09 は除外）。"""
        module = self._load()
        # BEL, BS, ESC などの C0 制御文字
        for codepoint in [0x00, 0x01, 0x07, 0x08, 0x0b, 0x0c, 0x1b, 0x1f]:
            char = chr(codepoint)
            result = module.sanitize_value(f"abc{char}def")
            assert char not in result, (
                f"C0 制御文字 U+{codepoint:04X} が除去されていない。実際: {result!r}"
            )
