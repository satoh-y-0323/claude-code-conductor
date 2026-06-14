"""
Tests for .claude/hooks/pre_compact.py

  TestContextItemsBeforeNoneShowsNA (Low-2)
    - context_items_before キーが存在しない場合、summary に N/A が含まれること

  TestSaveInstruction (AC-7)
    - SAVE_INSTRUCTION が新仕様（「現在地」更新指示と「- [x]」チェックリスト更新を含む）を含む

実装は 'N/A' 出力・新 SAVE_INSTRUCTION 文面に修正済み。本テスト群は将来の退行を防ぐ回帰防止テスト。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
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


def _load_pre_compact_module() -> types.ModuleType:
    """pre_compact.py をモジュールとしてロードする（__main__ 実行なし）。

    pre_compact.py はモジュールレベルで session_utils を import するため、
    sys.path に hooks ディレクトリを追加してからロードする。
    """
    hooks_dir = str(PRE_COMPACT_PY.parent)
    if hooks_dir not in sys.path:
        sys.path.insert(0, hooks_dir)
    spec = importlib.util.spec_from_file_location("pre_compact", PRE_COMPACT_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


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

    実装は 'N/A' 出力で修正済み。本テストは将来 '0' 出力に退行しないかを守る Green 回帰防止テスト。
    """

    def test_context_items_before_none_shows_na(self, tmp_path: Path) -> None:
        """[Low-2] context_items_before キーが payload に存在しない場合、
        セッションファイルへ書き込まれるサマリに 'N/A' が含まれること。

        検証方法: pre_compact.py をサブプロセスで実行し、セッションファイルの内容を確認する。
        pre_compact.py は append_checkpoint() を通じてセッションファイルにサマリを書き込む。
        キーが存在しない payload を渡したとき、書き込まれたサマリに 'N/A' が含まれるか確認する。

        実装側で 'N/A' 出力に修正済み。本テストは将来 '0' 出力に退行しないかを守る Green 回帰防止テスト。

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


# ---------------------------------------------------------------------------
# TestSaveInstructionNewSpec (AC-7)
# ---------------------------------------------------------------------------


class TestSaveInstruction:
    """[AC-7] SAVE_INSTRUCTION が新仕様（「更新」志向）の文面を含むこと。

    architecture §5.2 に従い、SAVE_INSTRUCTION は以下の2点を必ず含む:
    1. 「現在地:」を現フェーズ名に更新する指示（「現在地」というキーワード）
    2. 「## 残タスク」をチェックリストとして更新する指示（「- [x]」によるチェック化指示）

    旧文面（「書き出してください」という無上限追記指示）とは異なり、
    「更新」を促す文面であること。「無上限追記」を促す文面でないこと。
    """

    def test_save_instruction_contains_genba_update_keyword(self) -> None:
        """SAVE_INSTRUCTION に「現在地」の更新指示が含まれる（AC-7）。

        architecture §5.2 で確定した新文面: 「現在地:」行を現フェーズ名に更新することを
        明示的に促す文言が含まれること。
        """
        module = _load_pre_compact_module()
        instruction = module.SAVE_INSTRUCTION
        assert "現在地" in instruction, (
            "[AC-7] SAVE_INSTRUCTION に「現在地」更新指示が含まれていない。\n"
            "architecture §5.2 の新文面: 「現在地:」行を現フェーズ名に更新することを\n"
            "明示的に含む文面に変更すること。\n"
            f"現在の SAVE_INSTRUCTION:\n{instruction!r}"
        )

    def test_save_instruction_contains_checklist_update_keyword(self) -> None:
        """SAVE_INSTRUCTION に「- [x]」チェックリスト更新指示が含まれる（AC-7）。

        architecture §5.2 で確定した新文面: 完了タスクを「- [x]」でチェック化することを
        明示的に促す文言（「- [x]」という文字列）が含まれること。
        """
        module = _load_pre_compact_module()
        instruction = module.SAVE_INSTRUCTION
        assert "- [x]" in instruction, (
            "[AC-7] SAVE_INSTRUCTION に「- [x]」チェックリスト更新指示が含まれていない。\n"
            "architecture §5.2 の新文面: 完了タスクを「- [x]」化することを\n"
            "明示的に含む文面に変更すること。\n"
            f"現在の SAVE_INSTRUCTION:\n{instruction!r}"
        )

    def test_save_instruction_does_not_promote_unlimited_append(self) -> None:
        """SAVE_INSTRUCTION が「無上限追記」を促す旧文面でないこと（AC-7）。

        旧文面は「書き出してください」という追記指示だった。
        新文面は「更新」であり、追記を促さないこと（「書き出してください」が含まれないこと）。
        """
        module = _load_pre_compact_module()
        instruction = module.SAVE_INSTRUCTION
        assert "書き出してください" not in instruction, (
            "[AC-7] SAVE_INSTRUCTION に旧文面「書き出してください」が残っている。\n"
            "無上限追記を促す文面から「更新」を促す文面への転換が未完了。\n"
            "architecture §5.2 の新文面に置き換えること。\n"
            f"現在の SAVE_INSTRUCTION:\n{instruction!r}"
        )
