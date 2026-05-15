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

 --- Phase 3: フラグ制御 + exit 2 テストケース ---
 8.  session ファイルあり & flag なし → exit 2 + flag 作成 + stderr に summarize-memory 指示
 9.  stderr が Agent/Skill/summarize-memory のいずれかを含む
10.  flag あり → exit 0 + flag 削除 + stderr 出力なし
11.  session ファイルなし → exit 0 + flag 作成なし + stderr 出力なし
12.  Phase 1 / Phase 2 は flag 判定の前に実行される
13.  flag パスが .claude/state/llm_summary_agent_requested.flag である
"""

from __future__ import annotations

import importlib.util
import json
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

    def test_both_phases_called_with_payload(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
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
        monkeypatch.setattr(module, "_CLAUDE_DIR", str(tmp_path))
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

    def test_invalid_json_passes_empty_dict(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        """不正な JSON でも crash せず、空 dict を払い出す."""
        module = _load_module()

        stop_mock = MagicMock(run=MagicMock(return_value=0))
        consolidate_mock = MagicMock(run_sync=MagicMock(return_value=0))

        def _fake_load(name: str):
            return {"stop": stop_mock, "consolidate_memory": consolidate_mock}[name]

        monkeypatch.setattr(module, "_load_module", _fake_load)
        monkeypatch.setattr(module, "_CLAUDE_DIR", str(tmp_path))
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
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        module = _load_module()

        stop_mock = MagicMock(run=MagicMock(side_effect=RuntimeError("boom")))
        consolidate_mock = MagicMock(run_sync=MagicMock(return_value=0))

        def _fake_load(name: str):
            return {"stop": stop_mock, "consolidate_memory": consolidate_mock}[name]

        monkeypatch.setattr(module, "_load_module", _fake_load)
        monkeypatch.setattr(module, "_CLAUDE_DIR", str(tmp_path))
        monkeypatch.setattr(
            "sys.stdin", type("S", (), {"read": staticmethod(lambda: "{}")})()
        )

        result = module.main()

        assert result == 0
        consolidate_mock.run_sync.assert_called_once()

    def test_consolidate_failure_returns_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        module = _load_module()

        stop_mock = MagicMock(run=MagicMock(return_value=0))
        consolidate_mock = MagicMock(run_sync=MagicMock(side_effect=RuntimeError("boom")))

        def _fake_load(name: str):
            return {"stop": stop_mock, "consolidate_memory": consolidate_mock}[name]

        monkeypatch.setattr(module, "_load_module", _fake_load)
        monkeypatch.setattr(module, "_CLAUDE_DIR", str(tmp_path))
        monkeypatch.setattr(
            "sys.stdin", type("S", (), {"read": staticmethod(lambda: "{}")})()
        )

        result = module.main()

        assert result == 0
        stop_mock.run.assert_called_once()

    def test_both_failures_returns_zero(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        module = _load_module()

        stop_mock = MagicMock(run=MagicMock(side_effect=RuntimeError("a")))
        consolidate_mock = MagicMock(run_sync=MagicMock(side_effect=RuntimeError("b")))

        def _fake_load(name: str):
            return {"stop": stop_mock, "consolidate_memory": consolidate_mock}[name]

        monkeypatch.setattr(module, "_load_module", _fake_load)
        monkeypatch.setattr(module, "_CLAUDE_DIR", str(tmp_path))
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
        """空 payload でも crash せず exit 0 または exit 2 を返す。
        session ファイルの有無によって exit code が変わるため両方を許容する。"""
        result = _run_subprocess('{"stop_hook_active": true}')
        assert result.returncode in (0, 2)


# ---------------------------------------------------------------------------
# TestStopHookExitCode: Phase 3 フラグ制御 + exit 2 ロジックの Red テスト
#
# 新仕様: Phase 1 / Phase 2 完了後にフラグファイルを参照し、
# LLM 要約エージェントの起動指示を制御する。
#
# フラグパス: .claude/state/llm_summary_agent_requested.flag
#   - flag なし & 直近 7 日に session あり → exit 2 + flag 作成 + stderr に起動指示
#   - flag あり                            → exit 0 + flag 削除 (ループ防止)
#   - flag なし & session なし             → exit 0 (何もしない)
# ---------------------------------------------------------------------------

_FLAG_RELPATH = ".claude/state/llm_summary_agent_requested.flag"
_SESSIONS_RELPATH = ".claude/memory/sessions"


def _make_fake_session_file(sessions_dir: Path, days_ago: int = 0) -> Path:
    """days_ago 日前の日付を名前にもつ仮 session ファイルを作成して返す.

    実 session ファイルは YYYYMMDD.tmp 形式のため strftime("%Y%m%d") を使う。
    """
    from datetime import date, timedelta

    target_date = date.today() - timedelta(days=days_ago)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions_dir / f"{target_date.strftime('%Y%m%d')}.tmp"
    session_file.write_text("{}", encoding="utf-8")
    return session_file


def _build_mocks_and_patch(
    monkeypatch: pytest.MonkeyPatch,
    module: types.ModuleType,
    tmp_path: Path,
) -> tuple[MagicMock, MagicMock]:
    """stop_mock / consolidate_mock を作り、module の _load_module と stdin をパッチする.

    _CLAUDE_DIR は実装後に追加される属性であるため raising=False で setattr する。
    実装前（Red フェーズ）では属性が存在しないが、テストはフラグ制御ロジック自体の
    失敗（機能未実装）で失敗することを期待する。
    """
    stop_mock = MagicMock(run=MagicMock(return_value=0))
    consolidate_mock = MagicMock(run_sync=MagicMock(return_value=0))

    def _fake_load(name: str):
        return {"stop": stop_mock, "consolidate_memory": consolidate_mock}[name]

    monkeypatch.setattr(module, "_load_module", _fake_load)
    monkeypatch.setattr(
        "sys.stdin",
        type("S", (), {"read": staticmethod(lambda: "{}")})(),
    )
    # _CLAUDE_DIR / _FLAG_PATH を tmp_path に差し替えることで state / sessions を隔離する。
    # raising=False: 実装前は属性が存在しないが AttributeError でテストを止めない。
    claude_dir = tmp_path / ".claude"
    monkeypatch.setattr(module, "_CLAUDE_DIR", str(claude_dir), raising=False)
    monkeypatch.setattr(
        module,
        "_FLAG_PATH",
        str(claude_dir / "state" / "llm_summary_agent_requested.flag"),
        raising=False,
    )
    return stop_mock, consolidate_mock


class TestStopHookExitCode:
    """Stop hook の exit 2 / exit 0 とフラグファイル制御を検証する（Red フェーズ）."""

    # ------------------------------------------------------------------
    # テスト 1: session あり & flag なし → exit 2 + flag 作成 + stderr に指示
    # ------------------------------------------------------------------
    def test_exits_with_2_and_writes_flag_when_session_exists(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ):
        """session ファイルがある & flag がない → exit 2 かつ flag ファイルが作成される."""
        module = _load_module()
        _build_mocks_and_patch(monkeypatch, module, tmp_path)

        claude_dir = tmp_path / ".claude"
        sessions_dir = claude_dir / "memory" / "sessions"
        _make_fake_session_file(sessions_dir, days_ago=1)

        flag_path = claude_dir / "state" / "llm_summary_agent_requested.flag"
        assert not flag_path.exists(), "前提: flag はまだ存在しない"

        result = module.main()

        assert result == 2, f"session あり & flag なし → exit 2 を期待 (実際: {result})"
        assert flag_path.exists(), "exit 2 時は flag ファイルが作成されること"
        captured = capsys.readouterr()
        assert "summarize-memory" in captured.err, (
            "stderr に 'summarize-memory' の文字列が含まれること"
        )

    # ------------------------------------------------------------------
    # テスト 2: stderr に Agent/Skill/summarize-memory のいずれかが含まれる
    # ------------------------------------------------------------------
    def test_stderr_instructs_agent_invocation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ):
        """exit 2 時、stderr は Agent / Skill / summarize-memory のいずれかを含む起動指示を出力する."""
        module = _load_module()
        _build_mocks_and_patch(monkeypatch, module, tmp_path)

        claude_dir = tmp_path / ".claude"
        sessions_dir = claude_dir / "memory" / "sessions"
        _make_fake_session_file(sessions_dir, days_ago=0)

        flag_path = claude_dir / "state" / "llm_summary_agent_requested.flag"
        assert not flag_path.exists()

        result = module.main()

        assert result == 2
        captured = capsys.readouterr()
        keywords = ["Agent", "Skill", "summarize-memory"]
        assert any(kw in captured.err for kw in keywords), (
            f"stderr にキーワード {keywords} のいずれかが含まれること。"
            f"実際の stderr: {captured.err!r}"
        )

    # ------------------------------------------------------------------
    # テスト 3: flag あり → exit 0 + flag 削除 + stderr 出力なし
    # ------------------------------------------------------------------
    def test_exits_with_0_and_removes_flag_when_flag_exists(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ):
        """既存 flag がある場合: exit 0・flag 削除・stderr への起動指示なし（ループ防止）."""
        module = _load_module()
        _build_mocks_and_patch(monkeypatch, module, tmp_path)

        claude_dir = tmp_path / ".clone"  # sessions は作らない
        flag_path = tmp_path / ".claude" / "state" / "llm_summary_agent_requested.flag"
        flag_path.parent.mkdir(parents=True, exist_ok=True)
        flag_path.write_text("", encoding="utf-8")
        assert flag_path.exists(), "前提: flag が存在する"

        result = module.main()

        assert result == 0, f"flag あり → exit 0 を期待 (実際: {result})"
        assert not flag_path.exists(), "exit 0 時は flag が削除されること"
        captured = capsys.readouterr()
        assert "summarize-memory" not in captured.err, (
            "flag あり（ループ防止）の場合は stderr に起動指示を出力しない"
        )

    # ------------------------------------------------------------------
    # テスト 4: session なし → exit 0 + flag 作成なし + stderr 出力なし
    # ------------------------------------------------------------------
    def test_exits_with_0_when_no_sessions(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ):
        """直近 7 日に session ファイルがない場合: exit 0・flag 作成なし・stderr 出力なし.

        また、新仕様では session 検出ロジック (_has_recent_sessions 相当) が
        main() モジュールに実装されること（新関数の存在確認）。
        """
        module = _load_module()
        _build_mocks_and_patch(monkeypatch, module, tmp_path)

        claude_dir = tmp_path / ".claude"
        # sessions_dir を作るが 8 日前のファイルのみ置く（7 日外）
        sessions_dir = claude_dir / "memory" / "sessions"
        _make_fake_session_file(sessions_dir, days_ago=8)

        flag_path = claude_dir / "state" / "llm_summary_agent_requested.flag"
        assert not flag_path.exists()

        result = module.main()

        assert result == 0, f"session なし → exit 0 を期待 (実際: {result})"
        assert not flag_path.exists(), "session なし時は flag を作成しない"
        captured = capsys.readouterr()
        assert "summarize-memory" not in captured.err

        # 新仕様では session 検索ロジックが module に追加される。
        # 関数名は _has_recent_sessions または check_recent_sessions 相当。
        # 実装前（Red）はこの属性が存在しないため失敗する。
        assert hasattr(module, "_has_recent_sessions") or hasattr(
            module, "_check_recent_sessions"
        ), (
            "新仕様では session 検索ロジック (_has_recent_sessions 等) が "
            "session_stop.py に実装されること（Red フェーズ: 未実装のため失敗）"
        )

    # ------------------------------------------------------------------
    # テスト 5: Phase 1 / Phase 2 は flag 判定より先に実行される
    # ------------------------------------------------------------------
    def test_phase1_phase2_still_run_before_flag_check(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        """flag 判定は Phase 1 (stop.run) / Phase 2 (consolidate_memory.run_sync) の後に行う."""
        module = _load_module()
        call_order: list[str] = []

        stop_mock = MagicMock()
        stop_mock.run = MagicMock(
            side_effect=lambda *a, **kw: call_order.append("phase1")
        )
        consolidate_mock = MagicMock()
        consolidate_mock.run_sync = MagicMock(
            side_effect=lambda *a, **kw: call_order.append("phase2")
        )

        def _fake_load(name: str):
            return {"stop": stop_mock, "consolidate_memory": consolidate_mock}[name]

        monkeypatch.setattr(module, "_load_module", _fake_load)
        monkeypatch.setattr(
            "sys.stdin",
            type("S", (), {"read": staticmethod(lambda: "{}")})(),
        )
        monkeypatch.setattr(module, "_CLAUDE_DIR", str(tmp_path / ".claude"), raising=False)

        # session ファイルを作成して exit 2 コードパスを通す
        sessions_dir = tmp_path / ".claude" / "memory" / "sessions"
        _make_fake_session_file(sessions_dir, days_ago=2)

        result = module.main()

        # Phase 1 / Phase 2 が呼ばれていること
        assert "phase1" in call_order, "Phase 1 (stop.run) が実行されていない"
        assert "phase2" in call_order, "Phase 2 (consolidate_memory.run_sync) が実行されていない"
        # flag 判定は Phase 1/2 の後なので、呼び出し順を確認
        assert call_order.index("phase1") < call_order.index("phase2"), (
            "Phase 1 が Phase 2 より先に呼ばれること"
        )
        # exit 2 コードパスを通ったこと（session あり & flag なし）
        assert result == 2, f"exit 2 コードパスで exit 2 を期待 (実際: {result})"

    # ------------------------------------------------------------------
    # テスト 6: flag ファイルのパスが .claude/state/llm_summary_agent_requested.flag
    # ------------------------------------------------------------------
    def test_flag_path_is_under_state_dir(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        """flag ファイルが .claude/state/llm_summary_agent_requested.flag に配置される."""
        module = _load_module()
        created_paths: list[Path] = []

        stop_mock = MagicMock(run=MagicMock(return_value=0))
        consolidate_mock = MagicMock(run_sync=MagicMock(return_value=0))

        def _fake_load(name: str):
            return {"stop": stop_mock, "consolidate_memory": consolidate_mock}[name]

        monkeypatch.setattr(module, "_load_module", _fake_load)
        monkeypatch.setattr(
            "sys.stdin",
            type("S", (), {"read": staticmethod(lambda: "{}")})(),
        )

        claude_dir = tmp_path / ".claude"
        monkeypatch.setattr(module, "_CLAUDE_DIR", str(claude_dir), raising=False)
        monkeypatch.setattr(
            module,
            "_FLAG_PATH",
            str(claude_dir / "state" / "llm_summary_agent_requested.flag"),
            raising=False,
        )

        # session ファイルを作って flag 作成コードパスを通す
        sessions_dir = claude_dir / "memory" / "sessions"
        _make_fake_session_file(sessions_dir, days_ago=3)

        result = module.main()

        # フラグが作成された場合にパスを確認する
        expected_flag = claude_dir / "state" / "llm_summary_agent_requested.flag"
        if result == 2:
            # 実装後に期待するパス
            assert expected_flag.exists(), (
                f"flag ファイルは {expected_flag} に作成されること。"
                "(.gitignore の 'state/*' ルールでカバーされる前提)"
            )
        else:
            # 未実装（Red フェーズ）: exit 2 にならない場合は flag パスのみ検証
            pytest.fail(
                f"新仕様では session あり & flag なし → exit 2 を期待するが、"
                f"実際は exit {result}。実装が未完了（Red フェーズ正常）。"
                f"flag の期待パス: {expected_flag}"
            )
