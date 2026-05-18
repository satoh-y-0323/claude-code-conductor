"""Tests for .claude/hooks/session_stop.py

Stop hook orchestrator: stdin 読み出し 1 回で stop + consolidate_memory を
順次実行することを検証する。

テストケース:
 1. stdin 読み出しは 1 回のみ（両モジュールが stdin を奪い合わない）
 2. stop.run / consolidate_memory.run_sync が両方呼ばれる
 3. 第 1 フェーズ失敗時でも第 2 フェーズが実行される
 4. 第 2 フェーズ失敗時でも main() は exit 0
 5. 不正な JSON でも crash せず両モジュール呼ばれる
 6. 両モジュールに同じ today が渡されない（stop.py は payload のみ受ける）
 7. 単独 subprocess 実行で動作する（E2E）
 8. _needs_summary() の TOCTOU 耐性（listdir と getmtime の間にファイルが消えても例外を伝播しない）
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


# ---------------------------------------------------------------------------
# Unit: _needs_summary() の TOCTOU 耐性
# [CR-CC-002] listdir と getmtime の間にファイルが削除されると FileNotFoundError が
# 伝播してしまう問題を防ぐ。
# ---------------------------------------------------------------------------

# worktree ルート起点の session_stop.py を参照（_needs_summary が定義されている版）
_ROOT_HOOKS_DIR = WORKTREE_ROOT / ".claude" / "hooks"
_ROOT_HOOK_PATH = _ROOT_HOOKS_DIR / "session_stop.py"


def _load_root_module() -> types.ModuleType:
    """worktree の session_stop.py をロードする（_needs_summary 実装を含む版）."""
    spec = importlib.util.spec_from_file_location("session_stop_root", _ROOT_HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


# session_stop._FLAG_DONE_CONTENT から動的取得（リテラル直書きを避け、定数変更に自動追従）。
# 手動同期は不要。
_DONE = _load_root_module()._FLAG_DONE_CONTENT


class TestNeedsSummaryTOCTOU:
    """_needs_summary() の TOCTOU 耐性テスト.

    os.listdir() でファイル一覧を取得した直後に os.path.getmtime() を呼ぶため、
    その間にファイルが削除されると FileNotFoundError が発生する可能性がある。
    この問題を防ぐため、FileNotFoundError を捕捉して安全な値を返すことを検証する。
    """

    def test_all_tmp_files_deleted_between_listdir_and_getmtime_returns_false(
        self, tmp_path: Path
    ):
        """全 tmp ファイルが listdir 後・getmtime 前に消失しても False を返すこと.

        期待する振る舞い: 全ファイルが消えた場合はセッションなしと同等なので False。
        """
        module = _load_root_module()

        # sessions ディレクトリと tmp ファイルを作成
        sessions_dir = tmp_path / "memory" / "sessions"
        sessions_dir.mkdir(parents=True)
        tmp_file = sessions_dir / "session_20260516.tmp"
        tmp_file.write_text("dummy", encoding="utf-8")

        # llm_summary.md を sessions より古い mtime で作成（本来は True になるケース）
        summary_path = tmp_path / "memory" / "llm_summary.md"
        summary_path.write_text("old summary", encoding="utf-8")
        # summary を tmp より古い時刻に設定
        old_time = tmp_file.stat().st_mtime - 100
        os.utime(str(summary_path), (old_time, old_time))

        # os.path.getmtime が FileNotFoundError を投げるようにモック
        # （listdir 後にファイルが全消失したシミュレーション）
        with patch("os.path.getmtime", side_effect=FileNotFoundError("file gone")):
            result = module._needs_summary(str(tmp_path))

        # 全ファイルが消えた場合は False を返すべき
        assert result is False

    def test_partial_tmp_files_deleted_uses_remaining_files(
        self, tmp_path: Path
    ):
        """一部の tmp ファイルが消失しても残ったファイルから正常に判定できること.

        期待する振る舞い: 残ったファイルの mtime で判定する。
        """
        module = _load_root_module()

        sessions_dir = tmp_path / "memory" / "sessions"
        sessions_dir.mkdir(parents=True)

        # 2 つの tmp ファイルを作成
        old_tmp = sessions_dir / "session_old.tmp"
        new_tmp = sessions_dir / "session_new.tmp"
        old_tmp.write_text("old", encoding="utf-8")
        new_tmp.write_text("new", encoding="utf-8")

        # summary を new_tmp より古くする（残ったファイルで判定すれば True になるはず）
        summary_path = tmp_path / "memory" / "llm_summary.md"
        summary_path.write_text("old summary", encoding="utf-8")
        new_mtime = new_tmp.stat().st_mtime
        old_time = new_mtime - 100
        os.utime(str(summary_path), (old_time, old_time))

        # old_tmp のみ FileNotFoundError、new_tmp は正常な mtime を返す
        old_tmp_path = str(old_tmp)
        new_tmp_path = str(new_tmp)
        real_getmtime = os.path.getmtime

        def _selective_error(path: str) -> float:
            if path == old_tmp_path:
                raise FileNotFoundError(f"file gone: {path}")
            return real_getmtime(path)

        with patch("os.path.getmtime", side_effect=_selective_error):
            result = module._needs_summary(str(tmp_path))

        # new_tmp が残っていて summary より新しいので True を返すべき
        assert result is True

    def test_getmtime_error_does_not_propagate_as_exception(
        self, tmp_path: Path
    ):
        """FileNotFoundError が _needs_summary() の外に伝播しないこと."""
        module = _load_root_module()

        sessions_dir = tmp_path / "memory" / "sessions"
        sessions_dir.mkdir(parents=True)
        tmp_file = sessions_dir / "session_20260516.tmp"
        tmp_file.write_text("dummy", encoding="utf-8")

        with patch("os.path.getmtime", side_effect=FileNotFoundError("race condition")):
            # 例外を伝播せず、True か False のいずれかの bool を返すこと
            try:
                result = module._needs_summary(str(tmp_path))
                assert isinstance(result, bool), f"bool を返すべきだが {type(result)} が返った"
            except FileNotFoundError as exc:
                pytest.fail(
                    f"_needs_summary() が FileNotFoundError を伝播した: {exc}\n"
                    "TOCTOU 耐性が未実装。os.path.getmtime() を try/except で囲む必要がある。"
                )


# ---------------------------------------------------------------------------
# Unit: Phase 3 フラグ制御ロジックのテスト
# [CR-T-001] DONE/"" 区別ロジックと TOCTOU 対策の挙動を検証する。
# ---------------------------------------------------------------------------


class TestFlagControlLogic:
    """session_stop.py Phase 3: フラグファイルの DONE/空 区別と TOCTOU 対策の検証。

    フラグ状態機械:
      - ファイルなし → _needs_summary() が True なら フラグ作成 + exit 2
      - 内容 "" (空) = エージェント実行中 → exit 0（重複防止）
      - 内容 "DONE" = エージェント完了済み → unlink + 判定
        - _needs_summary True → フラグ再作成 + exit 2
        - _needs_summary False → exit 0
      - unlink OSError = 別プロセスが先に削除 → exit 0（TOCTOU 対策）
    """

    def _make_module_with_mocked_phases(
        self,
        monkeypatch: pytest.MonkeyPatch,
        flag_path: Path,
        needs_summary: bool,
    ) -> types.ModuleType:
        """Phase 1/2 をモックし、_FLAG_PATH と _needs_summary を差し替えた module を返す。"""
        module = _load_root_module()
        stop_mock = MagicMock(run=MagicMock(return_value=0))
        consolidate_mock = MagicMock(run_sync=MagicMock(return_value=0))

        def _fake_load(name: str) -> MagicMock:
            return {"stop": stop_mock, "consolidate_memory": consolidate_mock}[name]

        monkeypatch.setattr(module, "_load_module", _fake_load)
        monkeypatch.setattr(module, "_FLAG_PATH", str(flag_path))
        monkeypatch.setattr(module, "_needs_summary", lambda _: needs_summary)
        monkeypatch.setattr(
            "sys.stdin", type("S", (), {"read": staticmethod(lambda: "{}")})()
        )
        return module

    def test_done_flag_and_needs_summary_true_returns_2(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """DONE フラグ + needs_summary=True → フラグ再作成して exit 2。"""
        flag_path = tmp_path / "test.flag"
        flag_path.write_text(_DONE, encoding="utf-8")
        module = self._make_module_with_mocked_phases(monkeypatch, flag_path, needs_summary=True)

        result = module.main()

        assert result == 2
        # フラグが空（実行中）として再作成されている
        assert flag_path.exists()
        assert flag_path.read_text(encoding="utf-8") == ""

    def test_done_flag_and_needs_summary_false_returns_0(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """DONE フラグ + needs_summary=False → フラグ削除して exit 0。"""
        flag_path = tmp_path / "test.flag"
        flag_path.write_text(_DONE, encoding="utf-8")
        module = self._make_module_with_mocked_phases(monkeypatch, flag_path, needs_summary=False)

        result = module.main()

        assert result == 0
        # フラグが削除されている（要約不要のため）
        assert not flag_path.exists()

    def test_empty_flag_running_returns_0(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """空フラグ（実行中）→ 重複防止で exit 0。フラグは変更されない。"""
        flag_path = tmp_path / "test.flag"
        flag_path.write_text("", encoding="utf-8")
        module = self._make_module_with_mocked_phases(monkeypatch, flag_path, needs_summary=True)

        result = module.main()

        assert result == 0
        # フラグはそのまま残る（実行中エージェントを邪魔しない）
        assert flag_path.exists()
        assert flag_path.read_text(encoding="utf-8") == ""

    def test_done_flag_oserror_on_unlink_returns_0(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """DONE フラグだが unlink が OSError → TOCTOU 対策で exit 0（重複起動防止）。
        フラグは変更されずに残る（unlink が失敗したため）。
        """
        flag_path = tmp_path / "test.flag"
        flag_path.write_text(_DONE, encoding="utf-8")
        module = self._make_module_with_mocked_phases(monkeypatch, flag_path, needs_summary=True)
        monkeypatch.setattr(module.os, "unlink", MagicMock(side_effect=OSError("already gone")))

        result = module.main()

        assert result == 0
        # unlink が失敗したためフラグは DONE のまま残る（次回 Stop hook が処理する）
        assert flag_path.exists()
        assert flag_path.read_text(encoding="utf-8") == _DONE

    def test_no_flag_and_needs_summary_true_returns_2(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """フラグなし + needs_summary=True → フラグ作成して exit 2。"""
        flag_path = tmp_path / "test.flag"
        # フラグを作らない
        module = self._make_module_with_mocked_phases(monkeypatch, flag_path, needs_summary=True)

        result = module.main()

        assert result == 2
        assert flag_path.exists()
        assert flag_path.read_text(encoding="utf-8") == ""

    def test_no_flag_and_needs_summary_false_returns_0(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """フラグなし + needs_summary=False → exit 0（要約済み）。"""
        flag_path = tmp_path / "test.flag"
        module = self._make_module_with_mocked_phases(monkeypatch, flag_path, needs_summary=False)

        result = module.main()

        assert result == 0
        assert not flag_path.exists()
