"""
tests/conftest.py

pytest 共通セットアップ。
worktree の src/ を sys.path の最初に追加し、テストがworktree のコードを使用するようにする。
.claude/hooks/ も追加し、importlib 経由で stop.py / pre_compact.py を
ロードするテストが session_utils をインポートできるようにする。

リンク（symlink / junction）を実ファイルシステム上に作る共有ヘルパー ``_make_link``
もここに置く。利用側は ``from tests.conftest import _make_link`` の**絶対 import**
で参照する（tests/__init__.py が実在するパッケージ構成のため
``from conftest import ...`` は ModuleNotFoundError になる）。
"""
import os
import sys
from pathlib import Path

import pytest

# Ensure worktree src is imported, not system-installed c3 package
worktree_root = Path(__file__).parent.parent
sys.path.insert(0, str(worktree_root / "src"))
sys.path.insert(1, str(worktree_root / ".claude" / "hooks"))


def _make_link(target: Path, link: Path) -> None:
    """``link`` を ``target`` を指すリンクとして作る（OS 差を吸収する共有ヘルパー）.

    Windows は junction（``_winapi.CreateJunction``・管理者権限不要）、
    POSIX は ``os.symlink`` を使う。作成に失敗した場合は skip せず fail させる
    （skip すると「リンクを辿らない」という契約が全 OS で無検証になりうる）。

    呼び出し側が知っておくべき OS 差（実測・2026-08-15 / Windows 11）:

    - junction は ``Path.is_symlink()`` が **False** を返し、``Path.is_dir()`` は
      **True** を返す（＝素朴な ``is_symlink()`` ガードでは弾けない）
    - junction は**ディレクトリ専用**。ファイルへのリンクは
      ``os.symlink`` が必要で、非特権 Windows では ``WinError 1314``
      (privilege not held) で失敗する
    - 存在しないターゲットへの junction は ``FileNotFoundError`` で作れない
      （dangling リンクのテストは POSIX 限定になる）

    元は tests/test_stop_agent_memory_warn.py のローカルヘルパーだったものを、
    tests/test_cli_init_symlink_guard.py と共有するため conftest へ移設した。
    """
    if sys.platform == "win32":
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        except Exception as exc:  # pragma: no cover - 環境不備時のみ
            pytest.fail(f"junction の作成に失敗した（skip せず fail させる契約）: {exc!r}")
    else:
        try:
            os.symlink(str(target), str(link), target_is_directory=True)
        except OSError as exc:  # pragma: no cover - 環境不備時のみ
            pytest.fail(f"symlink の作成に失敗した（skip せず fail させる契約）: {exc!r}")
