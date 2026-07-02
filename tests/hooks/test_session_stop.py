"""Tests for .claude/hooks/session_stop.py

Stop hook orchestrator: stdin 読み出し 1 回で stop + consolidate_memory を
順次実行することを検証する。

テストケース:
 1. stdin 読み出しは 1 回のみ（両モジュールが stdin を奪い合わない）
 2. stop.run / consolidate_memory.run_sync が両方呼ばれる
 3. 第 1 フェーズ失敗時でも第 2 フェーズが実行される
 4. 第 2 フェーズ失敗時でも main() は exit 0
 5. 不正な JSON でも crash せず両モジュール呼ばれる
 6. consolidate_memory.run_sync に today が渡される（stop.py は payload のみ受ける）
 7. 単独 subprocess 実行で動作する（E2E）
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "session_stop.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("session_stop", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def _run_subprocess(stdin_text: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Unit: モジュールロード + 呼び出し検証
# ---------------------------------------------------------------------------


class TestOrchestratorCallsBothPhases:
    """stop.run / consolidate_memory.run_sync が両方呼ばれることを検証."""

    def test_both_phases_called_with_payload(self, monkeypatch: pytest.MonkeyPatch):
        module = _load_module()

        stop_mock = MagicMock()
        stop_mock.run = MagicMock(return_value=0)

        consolidate_mock = MagicMock()
        consolidate_mock.run_sync = MagicMock(return_value=0)

        def _fake_load(name: str):
            if name == "stop":
                return stop_mock
            if name == "consolidate_memory":
                return consolidate_mock
            raise ValueError(f"unexpected module: {name}")

        monkeypatch.setattr(module, "_load_module", _fake_load)
        monkeypatch.setattr(
            "sys.stdin",
            type("S", (), {"read": staticmethod(lambda: '{"last_assistant_message": "hello"}')})(),
        )

        result = module.main()

        assert result == 0
        stop_mock.run.assert_called_once()
        # payload が dict として渡される
        call_args = stop_mock.run.call_args
        assert call_args.args[0] == {"last_assistant_message": "hello"}
        # consolidate_memory.run_sync は today を kwarg で受ける
        consolidate_mock.run_sync.assert_called_once()
        call_kwargs = consolidate_mock.run_sync.call_args.kwargs
        assert "today" in call_kwargs

    def test_invalid_json_passes_empty_dict(self, monkeypatch: pytest.MonkeyPatch):
        """不正な JSON でも crash せず、空 dict を払い出す."""
        module = _load_module()

        stop_mock = MagicMock(run=MagicMock(return_value=0))
        consolidate_mock = MagicMock(run_sync=MagicMock(return_value=0))

        def _fake_load(name: str):
            return {"stop": stop_mock, "consolidate_memory": consolidate_mock}[name]

        monkeypatch.setattr(module, "_load_module", _fake_load)
        monkeypatch.setattr(
            "sys.stdin",
            type("S", (), {"read": staticmethod(lambda: "not json !!!")})(),
        )

        result = module.main()

        assert result == 0
        stop_mock.run.assert_called_once()
        assert stop_mock.run.call_args.args[0] == {}
        consolidate_mock.run_sync.assert_called_once()


class TestFailureIsolation:
    """1 フェーズが例外を投げても他フェーズが実行される + exit 0 を保つ."""

    def test_stop_failure_does_not_block_consolidate(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        module = _load_module()

        stop_mock = MagicMock(run=MagicMock(side_effect=RuntimeError("boom")))
        consolidate_mock = MagicMock(run_sync=MagicMock(return_value=0))

        def _fake_load(name: str):
            return {"stop": stop_mock, "consolidate_memory": consolidate_mock}[name]

        monkeypatch.setattr(module, "_load_module", _fake_load)
        monkeypatch.setattr(
            "sys.stdin", type("S", (), {"read": staticmethod(lambda: "{}")})()
        )

        result = module.main()

        assert result == 0
        consolidate_mock.run_sync.assert_called_once()

    def test_consolidate_failure_returns_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        module = _load_module()

        stop_mock = MagicMock(run=MagicMock(return_value=0))
        consolidate_mock = MagicMock(run_sync=MagicMock(side_effect=RuntimeError("boom")))

        def _fake_load(name: str):
            return {"stop": stop_mock, "consolidate_memory": consolidate_mock}[name]

        monkeypatch.setattr(module, "_load_module", _fake_load)
        monkeypatch.setattr(
            "sys.stdin", type("S", (), {"read": staticmethod(lambda: "{}")})()
        )

        result = module.main()

        assert result == 0
        stop_mock.run.assert_called_once()

    def test_both_failures_returns_zero(self, monkeypatch: pytest.MonkeyPatch):
        module = _load_module()

        stop_mock = MagicMock(run=MagicMock(side_effect=RuntimeError("a")))
        consolidate_mock = MagicMock(run_sync=MagicMock(side_effect=RuntimeError("b")))

        def _fake_load(name: str):
            return {"stop": stop_mock, "consolidate_memory": consolidate_mock}[name]

        monkeypatch.setattr(module, "_load_module", _fake_load)
        monkeypatch.setattr(
            "sys.stdin", type("S", (), {"read": staticmethod(lambda: "{}")})()
        )

        result = module.main()

        assert result == 0


class TestStdinMaxBytes:
    """stdin の入力サイズ上限（1 MB）を検証する.

    テストケース:
     - 1 MB 超過の stdin でも main() は exit 0 を返す（[CR-M-003] docstring/実装一貫性）
     - 1 MB 以内の stdin では正常動作する（exit 0）

    [SR-V-001] / [CR-M-003] 対応: session_stop.py docstring「常に exit 0 を返す」との
    一貫性のため、stdin 超過時も return 0 で抜けて stderr に警告を出す仕様。
    """

    def test_main_rejects_stdin_over_max_bytes(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """1 MB を超える stdin を流した場合、main() は exit 0 を返す（[CR-M-003]）.

        session_stop.py docstring「常に exit 0 を返す」との一貫性のため。
        stderr には警告ログが出力される（regression guard）。
        """
        module = _load_module()

        stop_mock = MagicMock(run=MagicMock(return_value=0))
        consolidate_mock = MagicMock(run_sync=MagicMock(return_value=0))

        def _fake_load(name: str):
            return {"stop": stop_mock, "consolidate_memory": consolidate_mock}[name]

        monkeypatch.setattr(module, "_load_module", _fake_load)

        # 1 MB + 1 byte のペイロード（JSON ではなく生のバイト列で上限を超える）
        over_limit_data = "x" * (1024 * 1024 + 1)
        monkeypatch.setattr(
            "sys.stdin",
            type("S", (), {"read": staticmethod(lambda: over_limit_data)})(),
        )

        result = module.main()
        assert result == 0, (
            "stdin 超過時は exit 0 を返すべき（[CR-M-003] docstring/実装一貫性）"
        )
        # stderr に警告が出ていることも確認
        captured = capsys.readouterr()
        assert "exceeds" in captured.err.lower() or "max" in captured.err.lower()

    def test_main_accepts_stdin_within_limit(self, monkeypatch: pytest.MonkeyPatch):
        """1 MB 以内の stdin で正常動作することを検証.

        regression guard for [SR-V-001]: 上限チェックが実装済みの環境で、
        1 MB 以内の通常入力が正常動作することを確認する。
        """
        module = _load_module()

        stop_mock = MagicMock(run=MagicMock(return_value=0))
        consolidate_mock = MagicMock(run_sync=MagicMock(return_value=0))

        def _fake_load(name: str):
            return {"stop": stop_mock, "consolidate_memory": consolidate_mock}[name]

        monkeypatch.setattr(module, "_load_module", _fake_load)

        # 1 MB 以内の正常なペイロード（小さい JSON）
        within_limit_data = json.dumps({"last_assistant_message": "hello"})
        monkeypatch.setattr(
            "sys.stdin",
            type("S", (), {"read": staticmethod(lambda: within_limit_data)})(),
        )

        result = module.main()

        assert result == 0
        stop_mock.run.assert_called_once()
        consolidate_mock.run_sync.assert_called_once()


class TestWorktreeSkipSyncNotCalled:
    """worktree 判定が True の場合に sync_tier_bandit_cost が呼ばれないことを検証。

    A1 AC-(7): session_stop が worktree skip 時に sync を呼ばない。
    """

    def test_sync_not_called_when_worktree(self, monkeypatch: pytest.MonkeyPatch):
        """is_worktree=True の場合、sync_tier_bandit_cost は呼ばれない。"""
        module = _load_module()

        stop_mock = MagicMock(run=MagicMock(return_value=0))
        consolidate_mock = MagicMock(run_sync=MagicMock(return_value=0))

        # is_worktree が True を返す session_utils mock
        session_utils_mock = MagicMock()
        session_utils_mock.is_worktree = MagicMock(return_value=True)

        def _fake_load(name: str):
            if name == "stop":
                return stop_mock
            if name == "consolidate_memory":
                return consolidate_mock
            if name == "session_utils":
                return session_utils_mock
            raise ValueError(f"unexpected module: {name}")

        monkeypatch.setattr(module, "_load_module", _fake_load)
        monkeypatch.setattr(
            "sys.stdin",
            type("S", (), {"read": staticmethod(
                lambda: '{"session_id": "test-sess", "transcript_path": "/tmp/t.jsonl"}'
            )})(),
        )

        sync_called = []

        # c3.db.sync_tier_bandit_cost が呼び出されないことを確認する。
        # session_stop.py は関数内 "from c3.db import sync_tier_bandit_cost" で
        # 取得するため、sys.modules["c3.db"] の属性を差し替えることで確実に捕捉できる。
        import c3.db  # noqa: PLC0415 — テスト時点で c3.db をロードしておく (sys.modules["c3.db"] 確定)

        def tracking_sync(**kw):
            sync_called.append(True)
            return 0

        monkeypatch.setattr(sys.modules["c3.db"], "sync_tier_bandit_cost", tracking_sync)

        result = module.main()

        assert result == 0
        assert not sync_called, (
            "worktree=True の場合、sync_tier_bandit_cost が呼ばれてはいけない"
        )


class TestSyncTierBanditCostCallRemoved:
    """P 群（db-shims-and-cost・Red 先行）: architecture-report ADR-4/§3-6 対応。

    Phase 3 から `sync_tier_bandit_cost()` 呼び出しを完全に削除する
    （ADR-4: cost 列キャッシュ廃止・`c3 tier stats` は `read_tier_cost_rate_summary` 直読みに変更）。
    `ingest_session` は不変のため、worktree でない通常経路でも
    引き続き呼ばれることを併せて確認する。
    """

    def test_sync_not_called_in_non_worktree_path(self, monkeypatch: pytest.MonkeyPatch):
        """sync_tier_bandit_cost は worktree 判定に関わらず Phase 3 から呼ばれなくなる
        （現行実装は is_worktree=False の通常経路で必ず呼ぶため Red）。
        ingest_session は不変のため、同じ経路で引き続き呼ばれることも確認する。
        """
        module = _load_module()

        stop_mock = MagicMock(run=MagicMock(return_value=0))
        consolidate_mock = MagicMock(run_sync=MagicMock(return_value=0))

        # is_worktree=False（通常のメインリポジトリ経路）
        session_utils_mock = MagicMock()
        session_utils_mock.is_worktree = MagicMock(return_value=False)

        def _fake_load(name: str):
            if name == "stop":
                return stop_mock
            if name == "consolidate_memory":
                return consolidate_mock
            if name == "session_utils":
                return session_utils_mock
            raise ValueError(f"unexpected module: {name}")

        monkeypatch.setattr(module, "_load_module", _fake_load)
        monkeypatch.setattr(
            "sys.stdin",
            type("S", (), {"read": staticmethod(
                lambda: '{"session_id": "test-sess", "transcript_path": "/tmp/t.jsonl"}'
            )})(),
        )

        sync_called = []
        import c3.db  # noqa: PLC0415 — sys.modules["c3.db"] を確定させる
        monkeypatch.setattr(
            sys.modules["c3.db"], "sync_tier_bandit_cost",
            lambda **kw: (sync_called.append(True), 0)[1],
        )

        ingest_called = []
        import c3.usage_ingester  # noqa: PLC0415 — sys.modules["c3.usage_ingester"] を確定させる
        monkeypatch.setattr(
            sys.modules["c3.usage_ingester"], "ingest_session",
            lambda **kw: ingest_called.append(kw),
        )

        result = module.main()

        assert result == 0
        assert ingest_called, (
            "ingest_session は不変のため is_worktree=False 経路で引き続き呼ばれるはず"
        )
        assert not sync_called, (
            "sync_tier_bandit_cost 呼び出しは Phase 3 から完全に削除されるべき"
            "（ADR-4: cost 列キャッシュ廃止）"
        )


class TestSubprocessE2E:
    """subprocess で session_stop.py を起動して全体の挙動を確認する.

    実際のセッションファイル / consolidated_summary.md / patterns.json などの
    副作用が出ないよう、worktree っぽい位置で実行することは避ける必要がある。
    本リポジトリは worktree ではないため、副作用が出る。よって最低限の確認のみ行う。
    """

    def test_subprocess_returns_zero_with_empty_payload(self):
        """空 payload でも crash せず exit 0 を返す."""
        result = _run_subprocess('{"stop_hook_active": true}')
        # stop_hook_active=true は早期 return するので副作用が小さい
        assert result.returncode == 0
