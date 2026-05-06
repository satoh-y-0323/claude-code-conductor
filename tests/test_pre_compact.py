"""
Tests for .claude/hooks/pre_compact.py (Round 5 - Red phase)

  TestContextItemsBeforeNoneShowsNA (Low-2)
    - context_items_before キーが存在しない場合、summary に N/A が含まれること

Current behavior:
    context_items_before = payload.get('context_items_before', 0)
    summary = (
        f"- context_items_before: {context_items_before}\\n"
        ...
    )
    When key is absent: outputs "- context_items_before: 0"
    When key is present with value 0: also outputs "- context_items_before: 0"
    These two cases are indistinguishable.

Expected after fix:
    When key is absent: outputs "- context_items_before: N/A"
    When key is present with value 0: outputs "- context_items_before: 0"

This test FAILS on the unfixed implementation because absent key produces "0" not "N/A".
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parent.parent
PRE_COMPACT_PY = WORKTREE_ROOT / ".claude" / "hooks" / "pre_compact.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_pre_compact(
    cwd: Path, payload: dict
) -> subprocess.CompletedProcess:
    """Run pre_compact.py as a subprocess with the given cwd and JSON payload."""
    return subprocess.run(
        [sys.executable, str(PRE_COMPACT_PY)],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        cwd=str(cwd),
    )


# ---------------------------------------------------------------------------
# TestContextItemsBeforeNoneShowsNA (Low-2)
# ---------------------------------------------------------------------------


class TestContextItemsBeforeNoneShowsNA:
    """[Red Round 5] context_items_before キー不在時のサマリ出力を検証する。

    Current implementation:
        context_items_before = payload.get('context_items_before', 0)

    Problem:
        - Key absent  -> value = 0 -> summary shows "- context_items_before: 0"
        - Key present with value 0 -> value = 0 -> summary shows "- context_items_before: 0"
        These two situations are indistinguishable. You can't tell if it was
        "actually 0" or "key was missing (unknown)".

    Expected after fix:
        Use payload.get('context_items_before') (default None) and then:
        - If None  -> output "- context_items_before: N/A"
        - If 0     -> output "- context_items_before: 0"

    This test FAILS on the unfixed implementation because absent key produces
    "0" instead of "N/A".
    """

    def test_context_items_before_none_shows_na(self, tmp_path: Path) -> None:
        """[Low-2] context_items_before キーが payload に存在しない場合、
        セッションファイルへ書き込まれるサマリに 'N/A' が含まれること。

        検証方法: pre_compact.py をサブプロセスで実行し、セッションファイルの内容を確認する。
        pre_compact.py は append_checkpoint() を通じてセッションファイルにサマリを書き込む。
        キーが存在しない payload を渡したとき、書き込まれたサマリに 'N/A' が含まれるか確認する。

        この テスト は未修正の実装に対して FAIL する。
        現在の実装では 'context_items_before' キーが不在のとき '0' が出力される。

        注意: pre_compact.py はセッションファイルを実際の .claude/memory/sessions/ に書き込む。
        テスト用に cwd を tmp_path（.git ファイルを持つ worktree に見せかけない場所）に設定し、
        セッションファイルの場所は pre_compact.py 内部のパス（__file__ 基準）に依存する。
        セッションファイルの内容を直接読み込むため、実際のセッションディレクトリを参照する。
        """
        # payload に context_items_before キーを含めない
        payload_without_key = {"trigger": "manual"}

        result = _run_pre_compact(tmp_path, payload_without_key)

        assert result.returncode == 0, (
            f"pre_compact.py が異常終了した。exit code: {result.returncode}\n"
            f"stderr: {result.stderr.decode(errors='replace')}"
        )

        # stdout は JSON 形式のフック出力
        stdout_text = result.stdout.strip()
        assert stdout_text, (
            "pre_compact.py の stdout が空。worktree として検出されていないか確認が必要。"
        )

        # stdout の JSON に additionalContext が含まれていることを確認
        try:
            output = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"stdout が JSON でない: {exc}\n"
                f"stdout: {stdout_text!r}"
            )

        assert "hookSpecificOutput" in output, (
            f"stdout JSON に hookSpecificOutput がない。keys: {list(output.keys())}"
        )

        # セッションファイルに書き込まれたサマリを確認するため、
        # 実際のセッションディレクトリを参照する
        from datetime import datetime, timezone
        sessions_dir = WORKTREE_ROOT / ".claude" / "memory" / "sessions"
        today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        session_file = sessions_dir / f"{today_str}.tmp"

        assert session_file.exists(), (
            f"セッションファイルが存在しない: {session_file}\n"
            f"pre_compact.py がセッションファイルを作成したはず。"
        )

        content = session_file.read_text(encoding="utf-8")

        # context_items_before: N/A が含まれるか確認
        assert "context_items_before: N/A" in content, (
            "[Low-2] context_items_before キーが payload に存在しない場合、"
            "サマリに 'N/A' が含まれるべき。\n"
            "現在の実装: `payload.get('context_items_before', 0)` は\n"
            "  - キー不在のとき 0 を返す\n"
            "  - '0' と 'N/A' を区別できない\n"
            "期待する修正: `payload.get('context_items_before')` を使い、\n"
            "  値が None の場合は 'N/A' を出力する。\n"
            f"実際のセッションファイル内容（最後の200文字）:\n"
            f"{content[-200:]!r}"
        )

    def test_context_items_before_zero_shows_zero(self, tmp_path: Path) -> None:
        """[Low-2] context_items_before キーが 0 の場合、サマリに '0' が含まれること。

        キーが存在して値が 0 の場合と、キーが不在の場合（N/A）を区別できるように、
        値が 0 のときは '0' がそのまま出力されること。

        この テスト は現在の実装でも PASS するが、修正後も引き続き PASS することを確認する
        リグレッションテストとして機能する。
        """
        payload_with_zero = {"trigger": "manual", "context_items_before": 0}

        result = _run_pre_compact(tmp_path, payload_with_zero)

        assert result.returncode == 0, (
            f"pre_compact.py が異常終了した。exit code: {result.returncode}\n"
            f"stderr: {result.stderr.decode(errors='replace')}"
        )

        # セッションファイルの内容を確認
        from datetime import datetime, timezone
        sessions_dir = WORKTREE_ROOT / ".claude" / "memory" / "sessions"
        today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        session_file = sessions_dir / f"{today_str}.tmp"

        if not session_file.exists():
            pytest.skip(
                f"セッションファイルが存在しない: {session_file}。"
                f"test_context_items_before_none_shows_na の後に実行されている想定。"
            )

        content = session_file.read_text(encoding="utf-8")

        # context_items_before: 0 が含まれるか確認（N/A ではなく 0）
        assert "context_items_before: 0" in content, (
            "[Low-2] context_items_before=0 のとき、サマリに '0' が含まれるべき。\n"
            "N/A ではなく実際の値 '0' が出力されること。\n"
            f"実際のセッションファイル内容（最後の200文字）:\n"
            f"{content[-200:]!r}"
        )
