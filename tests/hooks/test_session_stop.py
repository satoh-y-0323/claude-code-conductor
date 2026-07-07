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
        """is_worktree=True の場合、sync_tier_bandit_cost は呼ばれない
        （C-3 DC-GP-003: シム削除⑤後は関数自体が存在しないため monkeypatch.setattr は
        AttributeError になる。「関数が存在しないこと」の確認へ書き換える）。"""
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

        # sync_tier_bandit_cost はシム削除（⑤・ADR-25-4）により c3.db から消えている。
        import c3.db  # noqa: PLC0415 — テスト時点で c3.db をロードしておく (sys.modules["c3.db"] 確定)
        assert not hasattr(c3.db, "sync_tier_bandit_cost"), (
            "sync_tier_bandit_cost はシム削除（⑤・ADR-25-4）により c3.db から消えているはず"
        )

        result = module.main()

        assert result == 0


class TestSyncTierBanditCostCallRemoved:
    """P 群（db-shims-and-cost・Red 先行）: architecture-report ADR-4/§3-6 対応。

    Phase 3 から `sync_tier_bandit_cost()` 呼び出しを完全に削除する
    （ADR-4: cost 列キャッシュ廃止・`c3 tier stats` は `read_tier_cost_rate_summary` 直読みに変更）。
    `ingest_session` は不変のため、worktree でない通常経路でも
    引き続き呼ばれることを併せて確認する。
    """

    def test_sync_not_called_in_non_worktree_path(self, monkeypatch: pytest.MonkeyPatch):
        """sync_tier_bandit_cost は worktree 判定に関わらず Phase 3 から呼ばれない。

        C-3 DC-GP-003: シム削除（⑤・ADR-25-4）により関数自体が c3.db に存在しなく
        なるため、monkeypatch.setattr による呼び出し検知は AttributeError になる。
        「関数が存在しないこと（not hasattr）」の確認へ書き換える（raising=False で
        誤魔化さず削除確認へ一意化）。ingest_session は不変のため、同じ経路で
        引き続き呼ばれることも確認する。
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

        import c3.db  # noqa: PLC0415 — sys.modules["c3.db"] を確定させる
        assert not hasattr(c3.db, "sync_tier_bandit_cost"), (
            "sync_tier_bandit_cost はシム削除（⑤・ADR-25-4）により c3.db から消えているはず"
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


class TestPhase4GapCheckFailureIsolation:
    """Phase 4（tier_gap_check）が例外を投げても Phase 1-3 が完走し exit 0 を
    維持することを固定する回帰テストだった（architecture-report-20260707-065043.md
    §5-1・plan-report-20260707-065732.md test-gap-check）。

    tier_gap_check.py の Phase 4 統合は本 Red フェーズ時点で未実装であり、
    session_stop.py の main() は "tier_gap_check" 名で `_load_module` を
    呼ばない。したがって本テストは gap_mock.run が一度も呼ばれないこと
    （Phase 4 統合が存在しないこと）を理由に失敗した。Phase 1-3 完走・
    exit 0 自体は Phase 4 の有無と無関係に元々満たされていたため、判別点は
    `gap_mock.run.assert_called_once()` に絞った。
    """

    def test_phase4_exception_does_not_block_phase1_to_3(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Phase 4 が例外を投げても Phase 1-3 が完走し main() が exit 0 を返したことを確認した。"""
        module = _load_module()

        stop_mock = MagicMock(run=MagicMock(return_value=0))
        consolidate_mock = MagicMock(run_sync=MagicMock(return_value=0))
        gap_mock = MagicMock()
        gap_mock.run = MagicMock(side_effect=RuntimeError("gap check boom"))

        def _fake_load(name: str):
            if name == "stop":
                return stop_mock
            if name == "consolidate_memory":
                return consolidate_mock
            if name == "tier_gap_check":
                return gap_mock
            raise ValueError(f"unexpected module: {name}")

        monkeypatch.setattr(module, "_load_module", _fake_load)
        monkeypatch.setattr(
            "sys.stdin",
            type("S", (), {"read": staticmethod(lambda: '{"session_id": "sess-x"}')})(),
        )

        result = module.main()

        assert result == 0
        stop_mock.run.assert_called_once()
        consolidate_mock.run_sync.assert_called_once()
        gap_mock.run.assert_called_once()


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
