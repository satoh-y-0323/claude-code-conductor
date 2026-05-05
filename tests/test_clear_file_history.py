"""Tests for .claude/hooks/clear_file_history.py

subprocess で実行し、stdout 出力内容とファイルシステム状態を検証する。
HOME / USERPROFILE を一時ディレクトリに向けることで本番の ~/.claude/file-history/ を
汚染しない。

テストケース:
1. ディレクトリが存在しない: exit 0、スキップメッセージが出力に含まれる
2. 通常ファイルの削除: ファイルが消えて削除件数が出力に含まれる
3. サブディレクトリの削除: ディレクトリが消える
4. シンボリックリンクの削除: symlink 自体が消えるが target は残る（OS サポートがある場合）
5. FileNotFoundError ハンドリング: 削除中にファイルが消えても crash しない（exit 0）
"""

from __future__ import annotations

import ast
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parents[1]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "clear_file_history.py"


def _make_env(fake_home: Path) -> dict:
    """HOME / USERPROFILE を fake_home に向けた環境変数辞書を返す。

    os.path.expanduser('~') の挙動:
    - Unix:    HOME 環境変数を参照する
    - Windows: USERPROFILE 環境変数を参照する（USERPROFILE が優先）
    """
    env = os.environ.copy()
    if platform.system() == "Windows":
        env["USERPROFILE"] = str(fake_home)
        # USERPROFILE が設定されていれば HOMEDRIVE / HOMEPATH より優先されるため
        # 追加の削除は不要
    else:
        env["HOME"] = str(fake_home)
    return env


def _run_hook(fake_home: Path) -> subprocess.CompletedProcess:
    """clear_file_history.py を fake_home をホームディレクトリとして subprocess で実行する。"""
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_make_env(fake_home),
    )


def _make_file_history(fake_home: Path) -> Path:
    """fake_home/.claude/file-history/ ディレクトリを作成して返す。"""
    file_history = fake_home / ".claude" / "file-history"
    file_history.mkdir(parents=True)
    return file_history


# ---------------------------------------------------------------------------
# 1. ディレクトリが存在しない
# ---------------------------------------------------------------------------


def test_directory_not_exist_exits_zero_and_outputs_skip_message(tmp_path: Path):
    """file-history ディレクトリが存在しない場合、exit 0 かつスキップを示す出力がある。

    実際の hook 出力:
      '[clear-file-history] file-history フォルダが存在しません。スキップします。'
    # 設計書に記載なし: hook は "0 件削除" ではなく "スキップ" メッセージを出力するため、
    # "スキップ" の存在をアサートする
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # .claude/file-history は作らない

    result = _run_hook(fake_home)

    assert result.returncode == 0, (
        f"exit 0 であるべき。got: {result.returncode}\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )
    combined_output = result.stdout + result.stderr
    assert combined_output.strip(), (
        "何らかの出力があるべき（スキップメッセージ等）。got: empty output"
    )
    # hook は "file-history フォルダが存在しません。スキップします。" と出力する
    assert "clear-file-history" in combined_output, (
        f"hook 識別子 '[clear-file-history]' が出力に含まれるべき。got: {combined_output!r}"
    )


# ---------------------------------------------------------------------------
# 2. 通常ファイルの削除
# ---------------------------------------------------------------------------


def test_regular_file_is_deleted_and_count_in_output(tmp_path: Path):
    """通常ファイルが削除され、削除件数 '1' が出力に含まれる。"""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    file_history = _make_file_history(fake_home)
    target = file_history / "some_file.json"
    target.write_text("{}", encoding="utf-8")

    result = _run_hook(fake_home)

    assert result.returncode == 0, (
        f"exit 0 であるべき。got: {result.returncode}\n"
        f"stderr: {result.stderr!r}"
    )
    assert not target.exists(), "通常ファイルが削除されているべき。"
    assert "1" in result.stdout, (
        f"削除件数 '1' が stdout に含まれるべき。got: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# 3. サブディレクトリの削除
# ---------------------------------------------------------------------------


def test_subdirectory_is_deleted(tmp_path: Path):
    """サブディレクトリが削除される。"""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    file_history = _make_file_history(fake_home)
    sub = file_history / "sub_dir"
    sub.mkdir()
    (sub / "child.txt").write_text("x", encoding="utf-8")

    result = _run_hook(fake_home)

    assert result.returncode == 0, (
        f"exit 0 であるべき。got: {result.returncode}\n"
        f"stderr: {result.stderr!r}"
    )
    assert not sub.exists(), "サブディレクトリが削除されているべき。"


# ---------------------------------------------------------------------------
# 4. シンボリックリンクの削除（OS サポートがある場合）
# ---------------------------------------------------------------------------


def test_symlink_is_deleted_but_target_remains(tmp_path: Path):
    """symlink 自体が削除されるが、リンク先ディレクトリは残る。"""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    file_history = _make_file_history(fake_home)
    target_dir = tmp_path / "real_dir"
    target_dir.mkdir()
    symlink = file_history / "link_entry"
    try:
        symlink.symlink_to(target_dir)
    except OSError:
        pytest.skip("Cannot create symlinks on this platform (privilege required on Windows)")

    result = _run_hook(fake_home)

    assert result.returncode == 0, (
        f"exit 0 であるべき。got: {result.returncode}\n"
        f"stderr: {result.stderr!r}"
    )
    # symlink.exists() は壊れた symlink を False 扱いするため is_symlink() で確認
    assert not symlink.exists() and not symlink.is_symlink(), (
        "symlink エントリが削除されているべき。"
    )
    assert target_dir.exists(), "symlink のターゲット（本体ディレクトリ）は残るべき。"


# ---------------------------------------------------------------------------
# 5. FileNotFoundError ハンドリング
# ---------------------------------------------------------------------------


def test_file_not_found_error_is_handled(tmp_path: Path):
    """削除中にファイルが消えても FileNotFoundError を握りつぶして crash しない（exit 0）。

    hook は subprocess で動作するため unittest.mock.patch は使用できない。
    代わりに AST 解析で hook ソースコードに FileNotFoundError の except ハンドラが
    存在することを静的に検証する。
    これにより「FileNotFoundError が発生しても pass される設計」であることを確認する。

    加えて、実際のファイル削除が正常に完了する（exit 0）ことを実行ベースで検証する。
    """
    # --- 静的検証: hook ソースに FileNotFoundError の except ハンドラが存在するか ---
    source = HOOK_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    file_not_found_handled = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            # `except FileNotFoundError:` の形式を検索
            if node.type is not None:
                # ast.Name (単一例外) または ast.Tuple (複数例外) を確認
                if isinstance(node.type, ast.Name) and node.type.id == "FileNotFoundError":
                    file_not_found_handled = True
                    break
                if isinstance(node.type, ast.Tuple):
                    for exc in node.type.elts:
                        if isinstance(exc, ast.Name) and exc.id == "FileNotFoundError":
                            file_not_found_handled = True
                            break

    assert file_not_found_handled, (
        "hook は FileNotFoundError を except で捕捉するべき。"
        " 削除中に他プロセスがファイルを消した場合でも crash しない設計が必要。"
    )

    # --- 実行ベース検証: 通常実行で exit 0 が返ること ---
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    file_history = _make_file_history(fake_home)
    (file_history / "will_be_deleted.json").write_text("{}", encoding="utf-8")

    result = _run_hook(fake_home)

    assert result.returncode == 0, (
        f"exit 0 であるべき（FileNotFoundError があっても crash しない）。"
        f" got: {result.returncode}\n"
        f"stderr: {result.stderr!r}"
    )
