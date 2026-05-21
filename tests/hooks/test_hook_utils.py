"""Tests for .claude/hooks/_hook_utils.py (配布対象 共通ヘルパー)

planner_check.py / check_agent_invocation.py に重複していた _write_debug_log を
_hook_utils.write_debug_log に集約した検証。CR-M-001 (L-01) 対応。

検証観点:
  - _hook_utils.py が存在する
  - write_debug_log 関数を公開している
  - 両 hook が共通モジュールから import している（重複実装が消えている）
  - C3_HOOK_DEBUG=1 のときのみ書き込みする fail-safe 設計が保たれている
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_DIR = WORKTREE_ROOT / ".claude" / "hooks"
HOOK_UTILS_PATH = HOOK_DIR / "_hook_utils.py"
PLANNER_CHECK_PATH = HOOK_DIR / "planner_check.py"
AGENT_HOOK_PATH = HOOK_DIR / "check_agent_invocation.py"

pytestmark = pytest.mark.skipif(
    not HOOK_UTILS_PATH.is_file(),
    reason=".claude/hooks/_hook_utils.py not found",
)


def test_hook_utils_module_exists() -> None:
    """_hook_utils.py が hooks ディレクトリに存在する。"""
    assert HOOK_UTILS_PATH.is_file(), (
        f"_hook_utils.py が見つからない: {HOOK_UTILS_PATH}"
    )


def test_hook_utils_exposes_write_debug_log() -> None:
    """_hook_utils.py が write_debug_log 関数を公開している。"""
    source = HOOK_UTILS_PATH.read_text(encoding="utf-8")
    assert "def write_debug_log" in source, (
        "_hook_utils.py に write_debug_log 関数が見つからない"
    )


def test_planner_check_imports_from_hook_utils() -> None:
    """planner_check.py が _hook_utils から write_debug_log を import する。"""
    source = PLANNER_CHECK_PATH.read_text(encoding="utf-8")
    assert "from _hook_utils import" in source, (
        "planner_check.py が _hook_utils から import していない"
    )
    assert "write_debug_log" in source, (
        "planner_check.py が write_debug_log を使っていない"
    )


def test_check_agent_invocation_imports_from_hook_utils() -> None:
    """check_agent_invocation.py が _hook_utils から write_debug_log を import する。"""
    source = AGENT_HOOK_PATH.read_text(encoding="utf-8")
    assert "from _hook_utils import" in source, (
        "check_agent_invocation.py が _hook_utils から import していない"
    )
    assert "write_debug_log" in source, (
        "check_agent_invocation.py が write_debug_log を使っていない"
    )


def test_no_duplicate_write_debug_log_definitions() -> None:
    """両 hook から _write_debug_log の独自定義が消えている。

    共通化後は `def _write_debug_log` または `def write_debug_log` の
    関数定義が hook ファイル側に残っていてはならない。
    """
    for path in (PLANNER_CHECK_PATH, AGENT_HOOK_PATH):
        source = path.read_text(encoding="utf-8")
        # 関数定義そのものが残っていないことを確認（呼び出しの "write_debug_log(...)" は許可）
        assert "def _write_debug_log" not in source, (
            f"{path.name} に _write_debug_log の独自定義が残っている"
        )
        assert "def write_debug_log" not in source, (
            f"{path.name} に write_debug_log の独自定義が残っている"
        )


def test_write_debug_log_skips_when_env_unset(tmp_path: Path) -> None:
    """C3_HOOK_DEBUG が未設定なら何も書き込まない（fail-safe）。

    _hook_utils.py を subprocess で import して write_debug_log を呼び出し、
    ログファイルが作成されないことを確認する。
    """
    log_path = tmp_path / "tmp" / "test.log"
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(HOOK_DIR)!r})
        from _hook_utils import write_debug_log
        from pathlib import Path
        write_debug_log(Path({str(log_path)!r}), "test-line")
    """)
    env = os.environ.copy()
    env.pop("C3_HOOK_DEBUG", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert not log_path.exists(), (
        "C3_HOOK_DEBUG が未設定なのにログが書き込まれている"
    )


def test_write_debug_log_writes_when_env_set(tmp_path: Path) -> None:
    """C3_HOOK_DEBUG=1 ならタイムスタンプ + 引数行をログに追記する。"""
    log_path = tmp_path / "tmp" / "test.log"
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(HOOK_DIR)!r})
        from _hook_utils import write_debug_log
        from pathlib import Path
        write_debug_log(Path({str(log_path)!r}), "test-line")
    """)
    env = os.environ.copy()
    env["C3_HOOK_DEBUG"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert log_path.exists(), "C3_HOOK_DEBUG=1 なのにログファイルが作成されていない"
    content = log_path.read_text(encoding="utf-8")
    assert "test-line" in content, (
        f"ログに渡した文字列が含まれていない: {content!r}"
    )
    # ISO8601 タイムスタンプ（YYYY-MM-DDTHH:MM:SS）が先頭にある
    import re
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", content
    ), f"先頭に ISO8601 タイムスタンプがない: {content!r}"


def test_write_debug_log_sanitizes_control_chars(tmp_path: Path) -> None:
    """N-2 [SR-V-001]: line に C0 制御文字・ANSI ESC が含まれていれば除去される。

    呼び出し側の hook 入力（stdin JSON の `file_path` 等）に ANSI エスケープが混入しても、
    debug ログを後段で `cat` 等で表示した際にエスケープが解釈されない。
    """
    log_path = tmp_path / "tmp" / "test.log"
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(HOOK_DIR)!r})
        from _hook_utils import write_debug_log
        from pathlib import Path
        write_debug_log(
            Path({str(log_path)!r}),
            "before\\x1b[31mRED\\x1b[0mafter\\x00null\\x7fdel\\nnewline",
        )
    """)
    env = os.environ.copy()
    env["C3_HOOK_DEBUG"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    content = log_path.read_text(encoding="utf-8")
    # C0 制御文字・DEL・改行が混入していないこと
    for ch in ("\x00", "\x1b", "\x7f", "\n"):
        # ファイル末尾の最後の `\n`（fh.write 自体が付与する行終端）は許可
        body = content.rstrip("\n")
        assert ch not in body, (
            f"制御文字 {ch!r} がログに残っている: {content!r}"
        )
    # 可読部分は残っていること
    for keyword in ("before", "RED", "after", "null", "del", "newline"):
        assert keyword in content, (
            f"可読部分 {keyword!r} がログから消失している: {content!r}"
        )


def test_write_debug_log_sanitizes_c1_control_chars(tmp_path: Path) -> None:
    """L-1 [SR-V-001] (iter3): C1 制御文字 (U+0080-U+009F) も除去される。

    Latin-1 拡張領域の制御文字。一部の端末で CSI (U+009B) などとして解釈される
    可能性があるため、debug ログでも除去する。
    """
    log_path = tmp_path / "tmp" / "test.log"
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(HOOK_DIR)!r})
        from _hook_utils import write_debug_log
        from pathlib import Path
        # \\x80 (PAD), \\x9b (CSI), \\x9f (APC) を含む文字列
        write_debug_log(
            Path({str(log_path)!r}),
            "before\\x80c1lo\\x9bcsi\\x9fapc after",
        )
    """)
    env = os.environ.copy()
    env["C3_HOOK_DEBUG"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    content = log_path.read_text(encoding="utf-8")
    # C1 制御文字が混入していないこと
    for ch in ("\x80", "\x9b", "\x9f"):
        assert ch not in content, (
            f"C1 制御文字 {ch!r} がログに残っている: {content!r}"
        )
    # 可読部分は残っていること
    for keyword in ("before", "c1lo", "csi", "apc", "after"):
        assert keyword in content, (
            f"可読部分 {keyword!r} がログから消失している: {content!r}"
        )
