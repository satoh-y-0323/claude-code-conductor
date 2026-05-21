"""Tests for .claude/hooks/check_agent_invocation.py

PreToolUse Agent hook (R5) の挙動を検証する。

検査ルール:
  R5: subagent_type が code-reviewer / security-reviewer のとき
      isolation: "worktree" を指定すると exit 2 で BLOCK。
      worktree 自動クリーンアップで .claude/reports/*.md（gitignored）が消失するため。

fail-safe 設計:
  Agent ツール tool_input のキー名（subagent_type / isolation）は公式未公開のため、
  キー不在時は exit 0（許可）にフォールバックする。誤検知で全 Agent 呼び出しを
  ブロックしないことを保証する。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "check_agent_invocation.py"

pytestmark = pytest.mark.skipif(
    not HOOK_PATH.is_file(),
    reason=".claude/hooks/check_agent_invocation.py not found",
)


def _run_hook(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(WORKTREE_ROOT),
    )


def _agent_payload(subagent_type: str, isolation: str | None = None, **extra) -> dict:
    """Agent ツール呼び出しを模擬する payload。"""
    tool_input: dict = {"subagent_type": subagent_type, "prompt": "test"}
    if isolation is not None:
        tool_input["isolation"] = isolation
    tool_input.update(extra)
    return {"tool_name": "Agent", "tool_input": tool_input}


# ---------------------------------------------------------------------------
# Group R5: read_only タスクの worktree 違反を BLOCK
# ---------------------------------------------------------------------------

class TestR5Block:
    """reviewer 系 subagent_type + isolation=worktree の組み合わせを exit 2 で BLOCK する。"""

    def test_code_reviewer_with_worktree_blocks(self) -> None:
        """code-reviewer + isolation=worktree → exit 2 + BLOCK メッセージ。"""
        result = _run_hook(_agent_payload("code-reviewer", isolation="worktree"))
        assert result.returncode == 2
        assert "[CheckAgentInvocation BLOCK]" in result.stderr
        assert "R5" in result.stderr

    def test_security_reviewer_with_worktree_blocks(self) -> None:
        """security-reviewer + isolation=worktree → exit 2 + BLOCK メッセージ。"""
        result = _run_hook(_agent_payload("security-reviewer", isolation="worktree"))
        assert result.returncode == 2
        assert "[CheckAgentInvocation BLOCK]" in result.stderr


# ---------------------------------------------------------------------------
# Group R5 正常: reviewer でも isolation なしなら許可、reviewer 以外は全て許可
# ---------------------------------------------------------------------------

class TestR5Pass:
    """正常な Agent ツール呼び出しは exit 0 で素通り。"""

    def test_code_reviewer_without_isolation_passes(self) -> None:
        """code-reviewer + isolation なし → exit 0（main で直接実行が正常）。"""
        result = _run_hook(_agent_payload("code-reviewer"))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_security_reviewer_without_isolation_passes(self) -> None:
        """security-reviewer + isolation なし → exit 0。"""
        result = _run_hook(_agent_payload("security-reviewer"))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_developer_with_worktree_passes(self) -> None:
        """developer（read_only: false）+ isolation=worktree → exit 0。R5 対象外。"""
        result = _run_hook(_agent_payload("developer", isolation="worktree"))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_tester_with_worktree_passes(self) -> None:
        """tester + isolation=worktree → exit 0。R5 対象外。"""
        result = _run_hook(_agent_payload("tester", isolation="worktree"))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_wt_developer_with_worktree_passes(self) -> None:
        """wt_developer + isolation=worktree → exit 0。並列バリアントは想定動作。"""
        result = _run_hook(_agent_payload("wt_developer", isolation="worktree"))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_reviewer_with_other_isolation_passes(self) -> None:
        """code-reviewer + isolation=他の値（worktree 以外）→ exit 0。"""
        result = _run_hook(_agent_payload("code-reviewer", isolation="sandbox"))
        assert result.returncode == 0
        assert result.stderr == ""


# ---------------------------------------------------------------------------
# Group: 対象外動作（fail-safe）
# ---------------------------------------------------------------------------

class TestOutOfScope:
    """Agent ツール以外、または不正な入力は exit 0 で素通り（fail-safe）。"""

    def test_non_agent_tool_is_ignored(self) -> None:
        """tool_name が Agent 以外 → exit 0。"""
        result = _run_hook(
            {"tool_name": "Write", "tool_input": {"file_path": "/tmp/x.txt"}}
        )
        assert result.returncode == 0
        assert result.stderr == ""

    def test_bash_tool_is_ignored(self) -> None:
        result = _run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        assert result.returncode == 0
        assert result.stderr == ""

    def test_missing_subagent_type_is_ignored(self) -> None:
        """subagent_type 欠落 → exit 0（reviewer 系か判定できないため許可）。"""
        result = _run_hook(
            {"tool_name": "Agent", "tool_input": {"isolation": "worktree"}}
        )
        assert result.returncode == 0
        assert result.stderr == ""

    def test_missing_isolation_field_is_ignored(self) -> None:
        """isolation キー欠落 → reviewer でも exit 0。"""
        result = _run_hook(
            {"tool_name": "Agent", "tool_input": {"subagent_type": "code-reviewer"}}
        )
        assert result.returncode == 0
        assert result.stderr == ""

    def test_non_dict_tool_input_is_ignored(self) -> None:
        """tool_input が dict でない異常入力 → exit 0。"""
        result = _run_hook({"tool_name": "Agent", "tool_input": "invalid"})
        assert result.returncode == 0
        assert result.stderr == ""

    def test_invalid_json_does_not_crash(self) -> None:
        """不正な JSON 入力 → exit 0（crash しない）。"""
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input="this is not valid json {{{",
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(WORKTREE_ROOT),
        )
        assert result.returncode == 0

    def test_empty_payload_does_not_crash(self) -> None:
        """空の JSON オブジェクト → exit 0。"""
        result = _run_hook({})
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Group Security: 守備深化（size limit / debug path / newline escape）
# ---------------------------------------------------------------------------

class TestSecurityHardening:
    """セキュリティ・防御的コーディングの検証（SR-V-001/SR-NEW 対応）。"""

    def test_stdin_read_has_size_limit(self) -> None:
        """L-2 [SR-V-001]: sys.stdin.read() がサイズ制限引数を持つ。

        DoS 対策として stdin から読み取る最大バイト数を制限する。
        """
        import re
        source = HOOK_PATH.read_text(encoding="utf-8")
        assert re.search(
            r"sys\.stdin\.read\(\s*[^\s)][^)]*\)", source
        ), "sys.stdin.read() にサイズ制限引数がない（無制限読み取り）"

    def test_debug_log_path_is_absolute(self) -> None:
        """L-6 [SR-NEW]: DEBUG_LOG_PATH が cwd 依存の相対パスでなく絶対パスである。"""
        source = HOOK_PATH.read_text(encoding="utf-8")
        assert "__file__" in source, (
            "hook ファイルが __file__ を参照していない（絶対パス変換に必要）"
        )
        import re
        # `Path(".claude/tmp/...")` のような相対パス直書きを禁止する
        assert not re.search(
            r'DEBUG_LOG_PATH\s*=\s*Path\(\s*["\']\.claude/tmp/', source
        ), "DEBUG_LOG_PATH が cwd 相対の Path 文字列のまま"

    def test_isolation_with_newline_is_escaped_in_block_message(self) -> None:
        """L-7 [SR-NEW]: isolation 値に改行を含んでも BLOCK メッセージの行構造が破壊されない。

        isolation の改行はデバッグログだけでなく stderr メッセージにも影響しうる。
        `isolation` を `repr` または `\\n` エスケープして出力することで、
        WARN 行構造の整合性を保つ。
        """
        # isolation 値に改行を含む（worktree\n何か）
        # ただし R5 BLOCK は isolation == "worktree" の正確一致を判定するので、
        # 改行付き値は worktree と一致せず exit 0 になる。
        # 観点: 「改行付きの reviewer 呼び出しが BLOCK でも PASS でも、
        # stderr の出力に生の改行を含まないこと」を確認する。
        result = _run_hook(
            _agent_payload("code-reviewer", isolation="worktree\nINJECTED")
        )
        # exit code は worktree と完全一致しないので 0 を期待
        assert result.returncode == 0
        # 出力に "INJECTED" が直接行を成すように残らないこと
        # （改行サニタイズが効いていれば "INJECTED" の単独行は現れない）
        for line in result.stderr.splitlines():
            assert line.strip() != "INJECTED", (
                f"isolation の改行がサニタイズされず INJECTED が独立行になっている: {result.stderr!r}"
            )
