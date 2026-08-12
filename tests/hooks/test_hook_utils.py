"""Tests for .claude/hooks/_hook_utils.py (配布対象 共通ヘルパー)

planner_check.py / check_agent_invocation.py に重複していた _write_debug_log を
_hook_utils.write_debug_log に集約した検証。CR-M-001 (L-01) 対応。

検証観点:
  - _hook_utils.py が存在する
  - write_debug_log 関数を公開している
  - 両 hook が共通モジュールから import している（重複実装が消えている）
  - C3_HOOK_DEBUG=1 のときのみ書き込みする fail-safe 設計が保たれている

## E 周回 1 修正サイクル追加分（T5・Red — plan-report-20260725-180252.md §2-B）

F5（`sanitize_for_terminal` の共有化）の未実装による Red。

  - `_hook_utils.sanitize_for_terminal()` が存在し、既存 `_CONTROL_CHARS_RE`
    （C0 + DEL + C1）と同じ除去範囲を持つこと
  - (f) 重複消滅の機械検査: P1/P2/P3 の 3 hook に `def _sanitize` の独自定義が
    残っていないこと（既存 `test_no_duplicate_write_debug_log_definitions` と同型・
    rework2 DC-GP-004）
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

# T5 追加分: F5 の共有化対象となる 3 hook（P1 / P2 / P3）
PATTERNS_GUARD_PATH = HOOK_DIR / "patterns_guard.py"
REPORT_CONTRACT_CHECK_PATH = HOOK_DIR / "report_contract_check.py"
SESSION_MODE_WATCH_PATH = HOOK_DIR / "session_mode_watch.py"

# task test-reg 追記分: P4（新規 PreToolUse hook・未実装）
REPORT_PATH_GUARD_PATH = HOOK_DIR / "report_path_guard.py"

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


# ===========================================================================
# T5 追加分（E 周回 1 修正サイクル・Red）— F5: sanitize_for_terminal の共有化
# ===========================================================================


def test_hook_utils_exposes_sanitize_for_terminal() -> None:
    """[F5・Red] _hook_utils.py が sanitize_for_terminal 関数を公開している。"""
    source = HOOK_UTILS_PATH.read_text(encoding="utf-8")
    assert "def sanitize_for_terminal" in source, (
        "_hook_utils.py に sanitize_for_terminal 関数が見つからない"
    )


def _run_sanitize_for_terminal(tmp_path: Path, sample: str) -> str:
    """subprocess で `_hook_utils.sanitize_for_terminal(sample)` を評価し結果を返す。

    結果は cp932 の stdout を経由せずファイル（UTF-8）へ書き出して読み戻す
    （CLAUDE.md §9 の文字コード前提を回避するため）。
    """
    out_path = tmp_path / "sanitized.txt"
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(HOOK_DIR)!r})
        from _hook_utils import sanitize_for_terminal
        from pathlib import Path
        Path({str(out_path)!r}).write_text(
            sanitize_for_terminal({sample!r}), encoding="utf-8"
        )
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, (
        f"sanitize_for_terminal の呼び出しに失敗（未実装のため Red）: {result.stderr}"
    )
    return out_path.read_text(encoding="utf-8")


def test_sanitize_for_terminal_removes_c0_and_del(tmp_path: Path) -> None:
    """[F5・Red] C0 制御文字（ANSI ESC を含む）と DEL を除去する。"""
    sample = "before\x1b[31mRED\x1b[0mafter\x00null\x7fdel"
    sanitized = _run_sanitize_for_terminal(tmp_path, sample)
    for ch in ("\x00", "\x1b", "\x7f"):
        assert ch not in sanitized, (
            f"C0/DEL 制御文字 {ch!r} が除去されていない: {sanitized!r}"
        )
    for keyword in ("before", "RED", "after", "null", "del"):
        assert keyword in sanitized, (
            f"可読部分 {keyword!r} が消失している: {sanitized!r}"
        )


def test_sanitize_for_terminal_removes_c1(tmp_path: Path) -> None:
    """[F5・Red] C1 制御文字（U+0080-U+009F）も除去する（既存 _CONTROL_CHARS_RE 準拠）。"""
    sample = "before\x80pad\x9bcsi\x9fapc after"
    sanitized = _run_sanitize_for_terminal(tmp_path, sample)
    for ch in ("\x80", "\x9b", "\x9f"):
        assert ch not in sanitized, (
            f"C1 制御文字 {ch!r} が除去されていない: {sanitized!r}"
        )
    for keyword in ("before", "pad", "csi", "apc", "after"):
        assert keyword in sanitized, (
            f"可読部分 {keyword!r} が消失している: {sanitized!r}"
        )


def test_sanitize_for_terminal_keeps_plain_text_unchanged(tmp_path: Path) -> None:
    """[F5・Red] 制御文字を含まない文字列は変更しない（過剰除去がない）。"""
    sample = ".claude/reports/plan-report-20260725-180252.md"
    sanitized = _run_sanitize_for_terminal(tmp_path, sample)
    assert sanitized == sample, (
        f"制御文字を含まない文字列が変更された: {sanitized!r}"
    )


def test_no_duplicate_sanitize_definitions_in_p1_p2_p3() -> None:
    """[F5(f)・Red] P1/P2/P3 の 3 hook から `_sanitize` の独自定義が消えている。

    共有 `_hook_utils.sanitize_for_terminal()` への置換後は、hook ファイル側に
    `def _sanitize` / `def sanitize_for_terminal` の関数定義が残っていてはならない
    （呼び出しは許可）。既存 `test_no_duplicate_write_debug_log_definitions` と同型。
    """
    for path in (
        PATTERNS_GUARD_PATH,
        REPORT_CONTRACT_CHECK_PATH,
        SESSION_MODE_WATCH_PATH,
        REPORT_PATH_GUARD_PATH,
    ):
        assert path.is_file(), f"{path} が見つからない"
        source = path.read_text(encoding="utf-8")
        assert "def _sanitize" not in source, (
            f"{path.name} に _sanitize の独自定義が残っている"
            "（_hook_utils.sanitize_for_terminal へ置換すること）"
        )
        assert "def sanitize_for_terminal" not in source, (
            f"{path.name} に sanitize_for_terminal の独自定義が残っている"
        )


def test_p1_p2_p3_import_sanitize_from_hook_utils() -> None:
    """[F5・Red] 3 hook が _hook_utils から sanitize_for_terminal を import している。"""
    for path in (
        PATTERNS_GUARD_PATH,
        REPORT_CONTRACT_CHECK_PATH,
        SESSION_MODE_WATCH_PATH,
        REPORT_PATH_GUARD_PATH,
    ):
        source = path.read_text(encoding="utf-8")
        assert "from _hook_utils import" in source, (
            f"{path.name} が _hook_utils から import していない"
        )
        assert "sanitize_for_terminal" in source, (
            f"{path.name} が sanitize_for_terminal を使っていない"
        )


# ===========================================================================
# task test-reg 追記分（Red）— P4: report_path_guard.py の norm_component 共有
#
# 共有ループ検査（test_no_duplicate_sanitize_definitions_in_p1_p2_p3 /
# test_p1_p2_p3_import_sanitize_from_hook_utils）は対象リストへ
# REPORT_PATH_GUARD_PATH を足すだけとし、アサーション内容
# （sanitize_for_terminal のみ）は変更していない。
# 理由（実測）: リスト内の既存 hook `patterns_guard.py:37` は
# `from _hook_utils import sanitize_for_terminal` のみで norm_component を
# import していないため、ループ内の要求を norm_component へ広げると
# 既存 hook が恒久赤になる。
# したがって norm_component の要求は report_path_guard.py 単体を対象にした
# 下記の新規 test 関数で固定する。
# ===========================================================================


def test_report_path_guard_imports_norm_component_from_hook_utils() -> None:
    """[P4・Red] report_path_guard.py が _hook_utils から norm_component を import する。

    パス成分比較（`.claude/reports/` 判定）は共有ヘルパー
    `_hook_utils.norm_component()` に委ねる（report_contract_check.py /
    session_mode_watch.py と同型）。hook 未実装の現状では
    「ファイルが存在しない」ため AssertionError で Red になる。
    """
    assert REPORT_PATH_GUARD_PATH.is_file(), (
        f"{REPORT_PATH_GUARD_PATH} が見つからない（P4 hook 未実装）"
    )
    source = REPORT_PATH_GUARD_PATH.read_text(encoding="utf-8")
    assert "from _hook_utils import" in source, (
        "report_path_guard.py が _hook_utils から import していない"
    )
    assert "norm_component" in source, (
        "report_path_guard.py が norm_component を使っていない"
    )


def test_report_path_guard_has_no_local_norm_component_definition() -> None:
    """[P4・Red] report_path_guard.py に norm_component の同名ローカル定義が無い。

    共有ヘルパーを import せず自前で再定義すると正規化規則が分岐するため禁止
    （既存 `test_no_duplicate_write_debug_log_definitions` と同型）。
    """
    assert REPORT_PATH_GUARD_PATH.is_file(), (
        f"{REPORT_PATH_GUARD_PATH} が見つからない（P4 hook 未実装）"
    )
    source = REPORT_PATH_GUARD_PATH.read_text(encoding="utf-8")
    assert "def norm_component" not in source, (
        "report_path_guard.py に norm_component の独自定義が残っている"
        "（_hook_utils.norm_component を import すること）"
    )
    assert "def _norm_component" not in source, (
        "report_path_guard.py に _norm_component の独自定義が残っている"
    )


def test_no_duplicate_timestamp_pattern_definitions_in_guards() -> None:
    """[CR-M-001] report_path_guard.py と report_contract_check.py が
    timestamp_pattern を import して重複ロジックを排除している。

    共有 `_hook_utils.timestamp_pattern()` から import した後は、
    hook ファイル側で同じ正規表現ロジック（`re.compile(re.escape(prefix) + ...`）を
    ローカルに再実装していてはならない。既存 `test_no_duplicate_write_debug_log_definitions`
    と同型。_is_timestamp_name などの内部関数は import した timestamp_pattern を
    使って実装される限り許可（API の安定性と実装の自由度を両立させるため）。
    """
    for path in (REPORT_PATH_GUARD_PATH, REPORT_CONTRACT_CHECK_PATH):
        assert path.is_file(), f"{path} が見つからない"
        source = path.read_text(encoding="utf-8")
        # hook ファイル側で直接 re.compile(...pattern...) を作成していないことを確認
        # （timestamp_pattern をローカルで再定義していないこと）
        assert "def timestamp_pattern" not in source, (
            f"{path.name} に timestamp_pattern の独自定義が残っている"
            "（_hook_utils.timestamp_pattern を import すること）"
        )


def test_report_guard_and_contract_check_import_timestamp_pattern() -> None:
    """[CR-M-001] report_path_guard.py と report_contract_check.py が
    _hook_utils から timestamp_pattern を import している。
    """
    for path in (REPORT_PATH_GUARD_PATH, REPORT_CONTRACT_CHECK_PATH):
        assert path.is_file(), f"{path} が見つからない"
        source = path.read_text(encoding="utf-8")
        assert "from _hook_utils import" in source, (
            f"{path.name} が _hook_utils から import していない"
        )
        assert "timestamp_pattern" in source, (
            f"{path.name} が timestamp_pattern を import していない"
        )
