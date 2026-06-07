"""Tests for .claude/hooks/recall_autorebuild.py

Stop hook: recall 索引が stale なら c3 recall rebuild を detached background で起動する。

テスト対象の純粋関数:
  find_repo_root()         -- CLAUDE_PROJECT_DIR or cwd から .claude を辿る
  index_exists(repo_root)  -- recall.hnsw の存在確認
  index_is_stale_fast(repo_root) -- stat のみ・ファイル内容を読まない軽量版
  is_worktree(repo_root)   -- .git がファイルなら True（git worktree）
  acquire_lock(lock_path, ttl, now=...) -- atomic O_CREAT|O_EXCL + mtime TTL 回収
  release_lock(lock_path)  -- ロックファイル削除
  build_worker_argv(self_path, repo_root) -- --rebuild-worker 起動コマンドリスト
  run_rebuild_worker(repo_root) -- subprocess.run で c3 recall rebuild を実行
  main()                   -- Stop hook 本体エントリ

カバーするシナリオ（10 項目）:
  1. C3_RECALL_AUTOREBUILD_DISABLE=1 で即 exit 0 / worker 起動しない
  2. 索引（recall.hnsw）不在で skip
  3. stale でない（軽量 stat 判定 False）で skip
  4. worktree（.git がファイル）で skip
  5. stale かつ索引あり → ロック取得 → detached spawn 呼ばれる
  6. ロック保持中（fresh）→ skip（多重起動防止）
  7. stale ロック（mtime TTL 超）→ 回収して取得できる
  8. worker モード: recall rebuild subprocess を呼び finally でロック解放
  9. 異常系（stdin 破損 / repo root 不明 / c3 import 不可）すべて exit 0 / stderr を汚さない
 10. stop を decision:block しない（stdout が空 or 無害な JSON のみ）
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
import time
import types
from pathlib import Path
import pytest

# ---------------------------------------------------------------------------
# Loader helpers
# ---------------------------------------------------------------------------

# 配布 hook（.claude/hooks/）。tests/hooks/ から見て repo root は parents[2]。
# pytest の通常ディスカバリ対象（testpaths = ["tests"]）。
REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_PATH = REPO_ROOT / ".claude" / "hooks" / "recall_autorebuild.py"

def _load_hook_module() -> types.ModuleType:
    """Load recall_autorebuild.py without executing __main__ block.

    Raises FileNotFoundError when the hook file does not exist.
    """
    if not HOOK_PATH.is_file():
        raise FileNotFoundError(
            f"Hook not found: {HOOK_PATH}"
        )
    spec = importlib.util.spec_from_file_location("recall_autorebuild", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


@pytest.fixture
def hook() -> types.ModuleType:
    return _load_hook_module()


# ---------------------------------------------------------------------------
# Helper: build a minimal Stop payload
# ---------------------------------------------------------------------------

def _stop_payload(session_id: str = "test-session") -> str:
    return json.dumps(
        {
            "hook_event_name": "Stop",
            "session_id": session_id,
            "stop_hook_active": False,
        }
    )


def _patch_stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


# ---------------------------------------------------------------------------
# 1. C3_RECALL_AUTOREBUILD_DISABLE=1 で即 exit 0 / worker 起動しない
# ---------------------------------------------------------------------------


class TestDisableEnvVar:
    """C3_RECALL_AUTOREBUILD_DISABLE=1 が設定されているとき main() は即 exit 0 を返す。"""

    def test_main_returns_zero_when_disabled(
        self, hook: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("C3_RECALL_AUTOREBUILD_DISABLE", "1")
        _patch_stdin(monkeypatch, _stop_payload())

        spawn_called = []

        def _fake_spawn(repo_root: Path) -> None:
            spawn_called.append(repo_root)

        monkeypatch.setattr(hook, "spawn_detached", _fake_spawn)

        rc = hook.main()
        assert rc == 0, "main() must return 0 when disabled"
        assert not spawn_called, "spawn_detached must NOT be called when disabled"

    def test_main_produces_no_stdout_when_disabled(
        self, hook: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("C3_RECALL_AUTOREBUILD_DISABLE", "1")
        _patch_stdin(monkeypatch, _stop_payload())
        hook.main()
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


# ---------------------------------------------------------------------------
# 2. 索引（recall.hnsw）不在で skip
# ---------------------------------------------------------------------------


class TestIndexNotFound:
    """recall.hnsw が存在しないとき worker を起動しない。"""

    def test_main_skips_when_index_absent(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("C3_RECALL_AUTOREBUILD_DISABLE", raising=False)
        _patch_stdin(monkeypatch, _stop_payload())

        # repo root あり、索引なし
        monkeypatch.setattr(hook, "find_repo_root", lambda: tmp_path)
        monkeypatch.setattr(hook, "index_exists", lambda root: False)
        monkeypatch.setattr(hook, "is_worktree", lambda root: False)

        spawn_called = []
        monkeypatch.setattr(hook, "spawn_detached", lambda root: spawn_called.append(root))

        rc = hook.main()
        assert rc == 0
        assert not spawn_called, "spawn_detached must NOT be called when index is absent"

    def test_index_exists_false_when_hnsw_missing(
        self, hook: types.ModuleType, tmp_path: Path
    ) -> None:
        """index_exists() は recall.hnsw が存在しないとき False を返す。"""
        (tmp_path / ".claude" / "state").mkdir(parents=True)
        assert hook.index_exists(tmp_path) is False

    def test_index_exists_true_when_hnsw_present(
        self, hook: types.ModuleType, tmp_path: Path
    ) -> None:
        state = tmp_path / ".claude" / "state"
        state.mkdir(parents=True)
        (state / "recall.hnsw").write_bytes(b"\x00")
        assert hook.index_exists(tmp_path) is True


# ---------------------------------------------------------------------------
# 3. stale でない（軽量 stat 判定 False）で skip
# ---------------------------------------------------------------------------


class TestNotStale:
    """索引が最新（stale でない）とき worker を起動しない。"""

    def test_main_skips_when_not_stale(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("C3_RECALL_AUTOREBUILD_DISABLE", raising=False)
        _patch_stdin(monkeypatch, _stop_payload())

        monkeypatch.setattr(hook, "find_repo_root", lambda: tmp_path)
        monkeypatch.setattr(hook, "index_exists", lambda root: True)
        monkeypatch.setattr(hook, "is_worktree", lambda root: False)
        monkeypatch.setattr(hook, "index_is_stale_fast", lambda root: False)

        spawn_called = []
        monkeypatch.setattr(hook, "spawn_detached", lambda root: spawn_called.append(root))

        rc = hook.main()
        assert rc == 0
        assert not spawn_called, "spawn_detached must NOT be called when index is fresh"

    def test_index_is_stale_fast_false_when_index_newer(
        self, hook: types.ModuleType, tmp_path: Path
    ) -> None:
        """index_is_stale_fast() はソースより index が新しければ False を返す。"""
        sessions = tmp_path / ".claude" / "memory" / "sessions"
        sessions.mkdir(parents=True)
        src = sessions / "s.tmp"
        src.write_text("body", encoding="utf-8")

        state = tmp_path / ".claude" / "state"
        state.mkdir(parents=True)
        index = state / "recall.hnsw"
        index.write_bytes(b"\x00")

        # index を src より 60 秒新しくする
        src_mtime = src.stat().st_mtime
        os.utime(index, (src_mtime + 60, src_mtime + 60))

        assert hook.index_is_stale_fast(tmp_path) is False

    def test_index_is_stale_fast_true_when_source_newer(
        self, hook: types.ModuleType, tmp_path: Path
    ) -> None:
        """index_is_stale_fast() はソースの方が新しければ True を返す。"""
        sessions = tmp_path / ".claude" / "memory" / "sessions"
        sessions.mkdir(parents=True)
        src = sessions / "s.tmp"
        src.write_text("body", encoding="utf-8")

        state = tmp_path / ".claude" / "state"
        state.mkdir(parents=True)
        index = state / "recall.hnsw"
        index.write_bytes(b"\x00")

        # index を src より 60 秒古くする
        src_mtime = src.stat().st_mtime
        os.utime(index, (src_mtime - 60, src_mtime - 60))

        assert hook.index_is_stale_fast(tmp_path) is True

    def test_index_is_stale_fast_false_when_no_sources(
        self, hook: types.ModuleType, tmp_path: Path
    ) -> None:
        """index_is_stale_fast() はソースファイルが存在しない場合に False を返す。

        index だけがあり、ソースディレクトリ（sessions / agent-memory / reports/archive）
        にファイルが存在しない状況をテストする。

        NOTE: この関数が stat のみを使いファイル内容を読まないことはテストで
        構造的に証明困難なため、その保証は実装コメントとコードレビューに委ねる。
        """
        state = tmp_path / ".claude" / "state"
        state.mkdir(parents=True)
        index = state / "recall.hnsw"
        index.write_bytes(b"\x00")
        # ソースディレクトリは存在しない（または空）→ stale でない
        assert hook.index_is_stale_fast(tmp_path) is False

    def test_index_is_stale_fast_ignores_gitkeep(
        self, hook: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """.gitkeep ファイルは stale 判定の対象外とする（recall_inject と同様の除外）。

        _STALE_SOURCE_GLOBS のパターンを "*" に差し替えて .gitkeep が列挙される
        状況を再現し、.gitkeep 除外ロジックにより stale 判定が誤発火しないことを確認する。
        """
        from pathlib import Path as _Path

        state = tmp_path / ".claude" / "state"
        state.mkdir(parents=True)
        index = state / "recall.hnsw"
        index.write_bytes(b"\x00")

        # index を現在時刻に設定
        now = time.time()
        os.utime(index, (now, now))

        # sessions ディレクトリに index より新しい .gitkeep を作成
        sessions = tmp_path / ".claude" / "memory" / "sessions"
        sessions.mkdir(parents=True)
        gitkeep = sessions / ".gitkeep"
        gitkeep.write_bytes(b"")
        # .gitkeep を index より 60 秒新しくする
        os.utime(gitkeep, (now + 60, now + 60))

        # glob パターンを "*" に差し替えて .gitkeep が列挙されるようにする
        # （現実の glob "*.tmp" では .gitkeep はマッチしないが、将来のパターン変更や
        #   recall_inject との対称性を保つためのロジック検証として "*" を使用）
        rel_sessions = _Path(".claude") / "memory" / "sessions"
        monkeypatch.setattr(hook, "_STALE_SOURCE_GLOBS", ((rel_sessions, "*"),))

        # .gitkeep は除外対象なので stale にならない（除外ロジックなしなら True になる）
        assert hook.index_is_stale_fast(tmp_path) is False, (
            ".gitkeep files must not trigger stale detection "
            "(recall_inject parity: path.name == '.gitkeep' must be skipped)"
        )


# ---------------------------------------------------------------------------
# 4. worktree（.git がファイル）で skip
# ---------------------------------------------------------------------------


class TestWorktreeGuard:
    """git worktree 内では worker を起動しない（ADR-4）。"""

    def test_is_worktree_true_when_dot_git_is_file(
        self, hook: types.ModuleType, tmp_path: Path
    ) -> None:
        """is_worktree() は .git がファイルのとき True を返す。"""
        (tmp_path / ".git").write_text("gitdir: ../.git/worktrees/foo\n", encoding="utf-8")
        assert hook.is_worktree(tmp_path) is True

    def test_is_worktree_false_when_dot_git_is_dir(
        self, hook: types.ModuleType, tmp_path: Path
    ) -> None:
        """is_worktree() は .git がディレクトリのとき False を返す（通常リポジトリ）。"""
        (tmp_path / ".git").mkdir()
        assert hook.is_worktree(tmp_path) is False

    def test_is_worktree_false_when_no_dot_git(
        self, hook: types.ModuleType, tmp_path: Path
    ) -> None:
        """is_worktree() は .git が存在しないとき False を返す。"""
        assert hook.is_worktree(tmp_path) is False

    def test_main_skips_in_worktree(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("C3_RECALL_AUTOREBUILD_DISABLE", raising=False)
        _patch_stdin(monkeypatch, _stop_payload())

        monkeypatch.setattr(hook, "find_repo_root", lambda: tmp_path)
        monkeypatch.setattr(hook, "is_worktree", lambda root: True)
        monkeypatch.setattr(hook, "index_exists", lambda root: True)
        monkeypatch.setattr(hook, "index_is_stale_fast", lambda root: True)

        spawn_called = []
        monkeypatch.setattr(hook, "spawn_detached", lambda root: spawn_called.append(root))

        rc = hook.main()
        assert rc == 0
        assert not spawn_called, "spawn_detached must NOT be called inside a worktree"


# ---------------------------------------------------------------------------
# 5. stale かつ索引あり → ロック取得 → detached spawn が呼ばれる
# ---------------------------------------------------------------------------


class TestSpawnOnStale:
    """stale 検出時に spawn_detached が呼ばれることを検証する（spawn は monkeypatch）。"""

    def test_main_calls_spawn_when_stale(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("C3_RECALL_AUTOREBUILD_DISABLE", raising=False)
        _patch_stdin(monkeypatch, _stop_payload())

        # ロックパスが tmp_path 配下を向くように find_repo_root を差し替え
        (tmp_path / ".claude" / "state").mkdir(parents=True)

        monkeypatch.setattr(hook, "find_repo_root", lambda: tmp_path)
        monkeypatch.setattr(hook, "is_worktree", lambda root: False)
        monkeypatch.setattr(hook, "index_exists", lambda root: True)
        monkeypatch.setattr(hook, "index_is_stale_fast", lambda root: True)

        spawn_calls = []

        def _fake_spawn(repo_root: Path) -> None:
            spawn_calls.append(repo_root)

        monkeypatch.setattr(hook, "spawn_detached", _fake_spawn)

        rc = hook.main()
        assert rc == 0
        assert len(spawn_calls) == 1, "spawn_detached must be called exactly once when stale"
        assert spawn_calls[0] == tmp_path

    def test_main_returns_zero_even_when_spawn_fails(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """spawn_detached が例外を投げても main() は exit 0 を返す（ADR-6）。"""
        monkeypatch.delenv("C3_RECALL_AUTOREBUILD_DISABLE", raising=False)
        _patch_stdin(monkeypatch, _stop_payload())

        (tmp_path / ".claude" / "state").mkdir(parents=True)

        monkeypatch.setattr(hook, "find_repo_root", lambda: tmp_path)
        monkeypatch.setattr(hook, "is_worktree", lambda root: False)
        monkeypatch.setattr(hook, "index_exists", lambda root: True)
        monkeypatch.setattr(hook, "index_is_stale_fast", lambda root: True)

        def _failing_spawn(repo_root: Path) -> None:
            raise OSError("simulated spawn failure")

        monkeypatch.setattr(hook, "spawn_detached", _failing_spawn)

        rc = hook.main()
        assert rc == 0


# ---------------------------------------------------------------------------
# 6. ロック保持中（fresh）→ skip（多重起動防止）
# ---------------------------------------------------------------------------


class TestLockFreshSkip:
    """fresh なロックが存在するとき worker を起動しない（ADR-3）。"""

    def test_acquire_lock_fails_when_fresh_lock_exists(
        self, hook: types.ModuleType, tmp_path: Path
    ) -> None:
        """acquire_lock() は fresh なロックが存在するとき None または False を返す。"""
        lock_path = tmp_path / "recall_rebuild.lock"
        ttl = 600

        # 現在時刻を固定し、ロックの mtime も現在にする
        now = time.time()
        lock_path.write_text(json.dumps({"pid": 12345, "started": now}), encoding="utf-8")
        os.utime(lock_path, (now, now))

        # fresh なロック（mtime = now, TTL 600s → まだ有効）
        result = hook.acquire_lock(lock_path, ttl, now=now)
        assert result is None or result is False, (
            "acquire_lock() must fail (None/False) when fresh lock exists"
        )

    def test_main_skips_when_lock_is_fresh(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("C3_RECALL_AUTOREBUILD_DISABLE", raising=False)
        _patch_stdin(monkeypatch, _stop_payload())

        (tmp_path / ".claude" / "state").mkdir(parents=True)

        # fresh なロックを事前に作成
        lock_path = tmp_path / ".claude" / "state" / "recall_rebuild.lock"
        now = time.time()
        lock_path.write_text(json.dumps({"pid": 99999, "started": now}), encoding="utf-8")
        os.utime(lock_path, (now, now))

        monkeypatch.setattr(hook, "find_repo_root", lambda: tmp_path)
        monkeypatch.setattr(hook, "is_worktree", lambda root: False)
        monkeypatch.setattr(hook, "index_exists", lambda root: True)
        monkeypatch.setattr(hook, "index_is_stale_fast", lambda root: True)

        spawn_calls = []
        monkeypatch.setattr(hook, "spawn_detached", lambda root: spawn_calls.append(root))

        rc = hook.main()
        assert rc == 0
        assert not spawn_calls, "spawn_detached must NOT be called when lock is fresh (multi-launch prevention)"


# ---------------------------------------------------------------------------
# 7. stale ロック（mtime TTL 超）→ 回収して取得できる
# ---------------------------------------------------------------------------


class TestStaleLockRecovery:
    """TTL 超過のロックは回収して新規取得できる（ADR-3 stale 回収）。"""

    def test_acquire_lock_recovers_stale_lock(
        self, hook: types.ModuleType, tmp_path: Path
    ) -> None:
        """acquire_lock() は TTL 超過ロックを削除して取得に成功する。"""
        lock_path = tmp_path / "recall_rebuild.lock"
        ttl = 600  # 600 秒

        # 現在時刻を "now" とし、ロックの mtime を TTL + 1 秒前に設定
        now = time.time()
        stale_mtime = now - ttl - 1  # 601 秒前 → stale

        lock_path.write_text(json.dumps({"pid": 99999, "started": stale_mtime}), encoding="utf-8")
        os.utime(lock_path, (stale_mtime, stale_mtime))

        result = hook.acquire_lock(lock_path, ttl, now=now)
        assert result is not False and result is not None, (
            "acquire_lock() must succeed (not None/False) when existing lock is stale (TTL exceeded)"
        )
        # 取得後はロックファイルが存在する（新しく作成されているはず）
        assert lock_path.exists(), "lock file must exist after successful acquire"

    def test_acquire_lock_creates_lock_when_absent(
        self, hook: types.ModuleType, tmp_path: Path
    ) -> None:
        """ロックが存在しないとき acquire_lock() はファイルを作成して成功する。"""
        lock_path = tmp_path / "recall_rebuild.lock"
        now = time.time()

        assert not lock_path.exists()
        result = hook.acquire_lock(lock_path, ttl=600, now=now)
        assert result is not False and result is not None
        assert lock_path.exists()

    def test_release_lock_removes_file(
        self, hook: types.ModuleType, tmp_path: Path
    ) -> None:
        """release_lock() はロックファイルを削除する。"""
        lock_path = tmp_path / "recall_rebuild.lock"
        lock_path.write_bytes(b"lock")
        hook.release_lock(lock_path)
        assert not lock_path.exists(), "release_lock() must remove the lock file"

    def test_release_lock_is_safe_when_missing(
        self, hook: types.ModuleType, tmp_path: Path
    ) -> None:
        """release_lock() はファイルが存在しなくても例外を投げない。"""
        lock_path = tmp_path / "nonexistent.lock"
        hook.release_lock(lock_path)  # should not raise


# ---------------------------------------------------------------------------
# 8. worker モード: recall rebuild subprocess を呼び、finally でロック解放
# ---------------------------------------------------------------------------


class TestWorkerMode:
    """--rebuild-worker モード: c3 recall rebuild を subprocess 実行しロックを解放する。"""

    def test_run_rebuild_worker_calls_c3_recall_rebuild(
        self, hook: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """run_rebuild_worker() は c3.cli recall rebuild --target を subprocess で呼ぶ。"""
        import subprocess as _subprocess

        captured_calls: list[dict] = []

        class _FakeResult:
            returncode = 0

        def _fake_run(args, **kwargs):
            captured_calls.append({"args": list(args), "kwargs": kwargs})
            return _FakeResult()

        monkeypatch.setattr(_subprocess, "run", _fake_run)

        # ロックパスが tmp_path 配下を向くように（worker は lock 解放も担う）
        lock_path = tmp_path / ".claude" / "state" / "recall_rebuild.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_bytes(b"lock")
        monkeypatch.setattr(hook, "_lock_path_for", lambda root: lock_path)

        hook.run_rebuild_worker(tmp_path)

        assert captured_calls, "subprocess.run must be called by run_rebuild_worker()"
        cmd_args = captured_calls[0]["args"]
        # c3.cli recall rebuild --target <root> が含まれていること
        assert "-m" in cmd_args or "c3.cli" in " ".join(str(a) for a in cmd_args), (
            "subprocess call must invoke c3.cli"
        )
        assert "recall" in cmd_args, "subprocess args must include 'recall'"
        assert "rebuild" in cmd_args, "subprocess args must include 'rebuild'"
        assert str(tmp_path) in cmd_args, "subprocess args must include the repo root as --target"

    def test_run_rebuild_worker_releases_lock_after_success(
        self, hook: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """run_rebuild_worker() は成功後もロックを解放する（finally ブロック）。"""
        import subprocess as _subprocess

        class _FakeResult:
            returncode = 0

        monkeypatch.setattr(_subprocess, "run", lambda *a, **kw: _FakeResult())

        lock_path = tmp_path / ".claude" / "state" / "recall_rebuild.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_bytes(b"lock")
        monkeypatch.setattr(hook, "_lock_path_for", lambda root: lock_path)

        hook.run_rebuild_worker(tmp_path)
        assert not lock_path.exists(), "lock must be released after successful rebuild"

    def test_run_rebuild_worker_releases_lock_on_failure(
        self, hook: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """run_rebuild_worker() はサブプロセス失敗時もロックを解放する（finally）。"""
        import subprocess as _subprocess

        def _fail(*a, **kw):
            raise RuntimeError("simulated subprocess failure")

        monkeypatch.setattr(_subprocess, "run", _fail)

        lock_path = tmp_path / ".claude" / "state" / "recall_rebuild.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_bytes(b"lock")
        monkeypatch.setattr(hook, "_lock_path_for", lambda root: lock_path)

        # worker は例外を飲み込んで終了する（silent exit）
        hook.run_rebuild_worker(tmp_path)
        assert not lock_path.exists(), "lock must be released even when subprocess fails"

    def test_build_worker_argv_contains_rebuild_worker_flag(
        self, hook: types.ModuleType, tmp_path: Path
    ) -> None:
        """build_worker_argv() は --rebuild-worker --target を含むリストを返す。"""
        self_path = Path("/path/to/recall_autorebuild.py")
        argv = hook.build_worker_argv(self_path, tmp_path)
        assert isinstance(argv, list), "build_worker_argv() must return a list"
        assert "--rebuild-worker" in argv, "argv must contain --rebuild-worker"
        assert "--target" in argv, "argv must contain --target"
        assert str(tmp_path) in argv, "argv must contain the repo_root as target"

    def test_run_rebuild_worker_logs_exception_type_to_stderr_on_failure(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """run_rebuild_worker() は subprocess が例外を投げたとき、stderr に exception type 名のみを出力する。

        出力形式: "[recall_autorebuild] worker error: <ExceptionTypeName>"
        - メッセージ本文は含まない（ADR-6 の診断困難問題へ最低限の対処）
        - finally でロックが解放されることも同時に確認する

        I-01 堅牢化: capsys の代わりに sys.stderr を io.StringIO で明示的に差し替えることで、
        hook モジュールロード時の sys.stderr.reconfigure() による capsys 偽陰性を回避する
        （security-review-report I-01 参照）。
        """
        import subprocess as _subprocess

        class _SimulatedError(RuntimeError):
            pass

        def _fail(*a, **kw):
            raise _SimulatedError("secret message that must NOT appear in stderr")

        monkeypatch.setattr(_subprocess, "run", _fail)

        lock_path = tmp_path / ".claude" / "state" / "recall_rebuild.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_bytes(b"lock")
        monkeypatch.setattr(hook, "_lock_path_for", lambda root: lock_path)

        # I-01: sys.stderr を明示的な StringIO に差し替えて reconfigure の影響を回避する
        fake_stderr = io.StringIO()
        monkeypatch.setattr(sys, "stderr", fake_stderr)

        hook.run_rebuild_worker(tmp_path)

        err_output = fake_stderr.getvalue()

        # stderr に exception type 名が含まれる
        assert "_SimulatedError" in err_output, (
            "stderr must contain the exception type name when subprocess raises; "
            f"got: {err_output!r}"
        )

        # stderr にメッセージ本文が含まれない
        assert "secret message" not in err_output, (
            "stderr must NOT contain the exception message body (type only); "
            f"got: {err_output!r}"
        )

        # finally でロックが解放される
        assert not lock_path.exists(), "lock must be released even when subprocess raises"


# ---------------------------------------------------------------------------
# 9. 異常系: stdin 破損 / repo root 不明 / c3 import 不可 → exit 0 / stderr 無汚染
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """異常系はすべて silent な exit 0（ADR-6）。"""

    def test_main_handles_broken_stdin(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("C3_RECALL_AUTOREBUILD_DISABLE", raising=False)
        monkeypatch.setattr("sys.stdin", io.StringIO("this is not valid JSON {{{"))
        rc = hook.main()
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.err == "", "stderr must be empty on broken stdin"

    def test_main_handles_empty_stdin(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("C3_RECALL_AUTOREBUILD_DISABLE", raising=False)
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        rc = hook.main()
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.err == ""

    def test_main_handles_no_repo_root(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("C3_RECALL_AUTOREBUILD_DISABLE", raising=False)
        _patch_stdin(monkeypatch, _stop_payload())
        monkeypatch.setattr(hook, "find_repo_root", lambda: None)
        rc = hook.main()
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.err == ""

    def test_main_handles_exception_in_stale_check(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """index_is_stale_fast() が例外を投げても main() は exit 0 を返す（ADR-6）。"""
        monkeypatch.delenv("C3_RECALL_AUTOREBUILD_DISABLE", raising=False)
        _patch_stdin(monkeypatch, _stop_payload())

        monkeypatch.setattr(hook, "find_repo_root", lambda: tmp_path)
        monkeypatch.setattr(hook, "is_worktree", lambda root: False)
        monkeypatch.setattr(hook, "index_exists", lambda root: True)

        def _raise_stale(root: Path) -> bool:
            raise OSError("simulated OSError in stale check")

        monkeypatch.setattr(hook, "index_is_stale_fast", _raise_stale)

        rc = hook.main()
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.err == ""


# ---------------------------------------------------------------------------
# 10. stop を decision:block しない（stdout が空 or 無害な JSON のみ）
# ---------------------------------------------------------------------------


class TestNoDecisionBlock:
    """hook は `{"decision": "block"}` を出力しない（ADR-5 / 自己再トリガーループ不発）。"""

    def _run_main_capturing(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        stale: bool = True,
    ) -> tuple[int, str]:
        monkeypatch.delenv("C3_RECALL_AUTOREBUILD_DISABLE", raising=False)
        _patch_stdin(monkeypatch, _stop_payload())

        (tmp_path / ".claude" / "state").mkdir(parents=True)

        monkeypatch.setattr(hook, "find_repo_root", lambda: tmp_path)
        monkeypatch.setattr(hook, "is_worktree", lambda root: False)
        monkeypatch.setattr(hook, "index_exists", lambda root: True)
        monkeypatch.setattr(hook, "index_is_stale_fast", lambda root: stale)
        monkeypatch.setattr(hook, "spawn_detached", lambda root: None)

        rc = hook.main()
        out = capsys.readouterr().out
        return rc, out

    def test_stdout_does_not_contain_decision_block(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """stale 時でも stdout に decision:block が含まれない。"""
        rc, out = self._run_main_capturing(hook, monkeypatch, tmp_path, capsys, stale=True)
        assert rc == 0
        if out.strip():
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                pytest.fail(f"stdout is not valid JSON: {out!r}")
            assert data.get("decision") != "block", (
                "hook must NOT output {\"decision\": \"block\"} to avoid Stop re-trigger loop (ADR-5)"
            )

    def test_stdout_is_empty_or_valid_json_when_not_stale(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc, out = self._run_main_capturing(hook, monkeypatch, tmp_path, capsys, stale=False)
        assert rc == 0
        stripped = out.strip()
        if stripped:
            try:
                json.loads(stripped)
            except json.JSONDecodeError:
                pytest.fail(f"stdout must be empty or valid JSON, got: {out!r}")

    def test_stdout_is_empty_when_disabled(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("C3_RECALL_AUTOREBUILD_DISABLE", "1")
        _patch_stdin(monkeypatch, _stop_payload())
        hook.main()
        out = capsys.readouterr().out
        assert out.strip() == "", "stdout must be empty when disabled"


# ---------------------------------------------------------------------------
# 11. 純粋関数: find_repo_root
# ---------------------------------------------------------------------------


class TestFindRepoRoot:
    """find_repo_root() は CLAUDE_PROJECT_DIR or cwd から .claude ディレクトリを辿る。"""

    def test_finds_dotclaude_from_env(
        self, hook: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / ".claude").mkdir()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        result = hook.find_repo_root()
        assert result == tmp_path

    def test_returns_none_when_no_dotclaude(
        self, hook: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        # .claude ディレクトリを作らない
        result = hook.find_repo_root()
        assert result is None


# ---------------------------------------------------------------------------
# 12. UTF-8 reconfigure が冒頭で行われていることの構造的検証
# ---------------------------------------------------------------------------


class TestUtf8Reconfigure:
    """hook は冒頭で sys.stdout/stderr.reconfigure(encoding='utf-8') を呼ぶ（CLAUDE.md §9-3）。"""

    def test_hook_source_contains_reconfigure(self) -> None:
        """ソースコードに reconfigure('utf-8') または reconfigure(encoding='utf-8') が含まれる。"""
        if not HOOK_PATH.is_file():
            raise FileNotFoundError(
                f"Hook not found: {HOOK_PATH}"
            )
        source = HOOK_PATH.read_text(encoding="utf-8")
        assert "reconfigure" in source, (
            "hook must call sys.stdout.reconfigure(encoding='utf-8') per CLAUDE.md §9-3"
        )
        assert "utf-8" in source or "utf_8" in source.replace("-", "_"), (
            "hook must specify utf-8 encoding in reconfigure()"
        )


# ---------------------------------------------------------------------------
# 13. M-01: worker モードで --target が .claude/ を持たないパスなら subprocess を呼ばない
# ---------------------------------------------------------------------------


class TestWorkerTargetValidation:
    """M-01 [SR-V-002]: --target パスの .claude/ 存在を検証し、なければ rebuild しない。"""

    def test_run_rebuild_worker_skips_subprocess_when_no_dotclaude(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """`run_rebuild_worker(repo_root)` は repo_root/.claude/ が存在しないとき
        subprocess.run を呼ばずに終了する（M-01）。

        worker は --target で受け取った repo_root が正当（.claude/ を持つ）かを検証する。
        """
        import subprocess as _subprocess

        # .claude/ を作らない（不正なターゲット）
        assert not (tmp_path / ".claude").exists()

        subprocess_called = []

        class _FakeResult:
            returncode = 0

        def _fake_run(args, **kwargs):
            subprocess_called.append(args)
            return _FakeResult()

        monkeypatch.setattr(_subprocess, "run", _fake_run)

        # ロックパスのために lock_path_for を差し替え（lock ファイル操作を tmp_path に向ける）
        lock_path = tmp_path / "recall_rebuild.lock"
        monkeypatch.setattr(hook, "_lock_path_for", lambda root: lock_path)

        hook.run_rebuild_worker(tmp_path)

        assert not subprocess_called, (
            "subprocess.run must NOT be called when repo_root/.claude/ does not exist (M-01)"
        )

    def test_run_rebuild_worker_calls_subprocess_when_dotclaude_exists(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """`run_rebuild_worker(repo_root)` は repo_root/.claude/ が存在するとき
        subprocess.run を呼ぶ（正常系：M-01 の逆ケース）。"""
        import subprocess as _subprocess

        # .claude/ を作成（正当なターゲット）
        (tmp_path / ".claude").mkdir()

        subprocess_called = []

        class _FakeResult:
            returncode = 0

        def _fake_run(args, **kwargs):
            subprocess_called.append(args)
            return _FakeResult()

        monkeypatch.setattr(_subprocess, "run", _fake_run)

        lock_path = tmp_path / ".claude" / "recall_rebuild.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_bytes(b"lock")
        monkeypatch.setattr(hook, "_lock_path_for", lambda root: lock_path)

        hook.run_rebuild_worker(tmp_path)

        assert subprocess_called, (
            "subprocess.run must be called when repo_root/.claude/ exists"
        )


# ---------------------------------------------------------------------------
# 14. L-01: acquire_lock が作成するロックファイルの権限が 0o600（POSIX のみ）
# ---------------------------------------------------------------------------


class TestLockFilePermissions:
    """L-01 [SR-NEW]: ロックファイルの権限が所有者のみ（0o600）であることを検証。"""

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only permission check")
    def test_lock_file_created_with_0o600_permissions(
        self, hook: types.ModuleType, tmp_path: Path
    ) -> None:
        """acquire_lock() が作成するロックファイルの権限は 0o600 である（L-01）。

        os.open に mode=0o600 を渡し、所有者のみ読み書き可能にする（umask 非依存）。
        """
        lock_path = tmp_path / "recall_rebuild.lock"
        now = time.time()

        result = hook.acquire_lock(lock_path, ttl=600, now=now)
        assert result is not None and result is not False, "acquire_lock() must succeed"
        assert lock_path.exists(), "lock file must exist"

        mode = stat.S_IMODE(os.stat(lock_path).st_mode)
        assert mode == 0o600, (
            f"lock file must have 0o600 permissions (owner-only), got 0o{mode:03o} (L-01)"
        )


# ---------------------------------------------------------------------------
# 15. L-02: spawn_detached の env から ANTHROPIC_/CLAUDE_/OPENAI_ が除外される
# ---------------------------------------------------------------------------


class TestSpawnDetachedEnvFiltering:
    """L-02 [SR-K-003]: spawn_detached が子プロセスに渡す env から API キーを除外する。"""

    def test_spawn_detached_excludes_anthropic_api_key(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """spawn_detached() が Popen に渡す env に ANTHROPIC_API_KEY が含まれない（L-02）。

        spawn_detached は ANTHROPIC_/CLAUDE_/OPENAI_ プレフィックスを除外した最小 env を渡す。
        """
        import subprocess as _subprocess

        # テスト用のダミー API キーを環境に設定
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret")
        monkeypatch.setenv("CLAUDE_ACCESS_TOKEN", "claude-test-secret")
        monkeypatch.setenv("OPENAI_API_KEY", "openai-test-secret")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")  # 無害な変数は残る

        captured_env: list[dict | None] = []

        class _FakeProcess:
            pid = 12345

        def _fake_popen(args, **kwargs):
            captured_env.append(kwargs.get("env"))
            return _FakeProcess()

        monkeypatch.setattr(_subprocess, "Popen", _fake_popen)

        hook.spawn_detached(tmp_path)

        assert captured_env, "Popen must be called by spawn_detached()"
        env = captured_env[0]

        # env= が指定されていること（None の場合は os.environ 全体が渡される）
        assert env is not None, (
            "spawn_detached() must pass an explicit env= to Popen, not None (L-02)"
        )

        # API キーが除外されていること
        assert "ANTHROPIC_API_KEY" not in env, (
            "ANTHROPIC_API_KEY must be excluded from child process env (L-02)"
        )
        assert "CLAUDE_ACCESS_TOKEN" not in env, (
            "CLAUDE_ACCESS_TOKEN must be excluded from child process env (L-02)"
        )
        assert "OPENAI_API_KEY" not in env, (
            "OPENAI_API_KEY must be excluded from child process env (L-02)"
        )

    def test_spawn_detached_preserves_safe_env_vars(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """spawn_detached() は ANTHROPIC_/CLAUDE_/OPENAI_ 以外の環境変数を除外しない。"""
        import subprocess as _subprocess

        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("HOME", "/home/testuser")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret")  # 除外対象

        captured_env: list[dict | None] = []

        class _FakeProcess:
            pid = 12345

        def _fake_popen(args, **kwargs):
            captured_env.append(kwargs.get("env"))
            return _FakeProcess()

        monkeypatch.setattr(_subprocess, "Popen", _fake_popen)

        hook.spawn_detached(tmp_path)

        assert captured_env, "Popen must be called"
        env = captured_env[0]

        if env is not None:
            # PATH や HOME は残っているべき
            assert "PATH" in env, "PATH must be preserved in child env"


# ---------------------------------------------------------------------------
# 16. L-03: C3_RECALL_AUTOREBUILD_DEBUG=1 のとき run_rebuild_worker の stderr が DEVNULL でない
# ---------------------------------------------------------------------------


class TestWorkerDebugStderr:
    """L-03 [SR-R-004]: デバッグフラグ付きのとき subprocess.run の stderr が DEVNULL でない。"""

    def test_run_rebuild_worker_stderr_is_devnull_when_debug_not_set(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """C3_RECALL_AUTOREBUILD_DEBUG 未設定のとき subprocess.run の stderr は DEVNULL。"""
        import subprocess as _subprocess

        monkeypatch.delenv("C3_RECALL_AUTOREBUILD_DEBUG", raising=False)

        captured_kwargs: list[dict] = []

        class _FakeResult:
            returncode = 0

        def _fake_run(args, **kwargs):
            captured_kwargs.append(kwargs)
            return _FakeResult()

        monkeypatch.setattr(_subprocess, "run", _fake_run)

        lock_path = tmp_path / ".claude" / "state" / "recall_rebuild.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_bytes(b"lock")
        monkeypatch.setattr(hook, "_lock_path_for", lambda root: lock_path)

        # M-01 影響を避けるため .claude/ を用意する
        (tmp_path / ".claude").mkdir(exist_ok=True)

        hook.run_rebuild_worker(tmp_path)

        assert captured_kwargs, "subprocess.run must be called"
        assert captured_kwargs[0].get("stderr") == _subprocess.DEVNULL, (
            "stderr must be DEVNULL when C3_RECALL_AUTOREBUILD_DEBUG is not set (L-03)"
        )

    def test_run_rebuild_worker_stderr_is_not_devnull_when_debug_set(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """C3_RECALL_AUTOREBUILD_DEBUG=1 のとき subprocess.run の stderr が DEVNULL でない（L-03）。

        debug フラグ設定時のみ子 stderr を親に継承（None）、未設定時は DEVNULL。
        """
        import subprocess as _subprocess

        monkeypatch.setenv("C3_RECALL_AUTOREBUILD_DEBUG", "1")

        captured_kwargs: list[dict] = []

        class _FakeResult:
            returncode = 0

        def _fake_run(args, **kwargs):
            captured_kwargs.append(kwargs)
            return _FakeResult()

        monkeypatch.setattr(_subprocess, "run", _fake_run)

        lock_path = tmp_path / ".claude" / "state" / "recall_rebuild.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_bytes(b"lock")
        monkeypatch.setattr(hook, "_lock_path_for", lambda root: lock_path)

        # M-01 影響を避けるため .claude/ を用意する
        (tmp_path / ".claude").mkdir(exist_ok=True)

        hook.run_rebuild_worker(tmp_path)

        assert captured_kwargs, "subprocess.run must be called"
        stderr_kwarg = captured_kwargs[0].get("stderr")
        assert stderr_kwarg != _subprocess.DEVNULL, (
            "stderr must NOT be DEVNULL when C3_RECALL_AUTOREBUILD_DEBUG=1 (L-03); "
            f"got: {stderr_kwarg!r}"
        )

    def test_run_rebuild_worker_uses_create_no_window_on_windows(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Windows では孫プロセス(c3 recall rebuild)に CREATE_NO_WINDOW を付ける。

        worker は DETACHED_PROCESS でコンソールを持たないため、明示しないと
        孫の python.exe が新しいコンソールウィンドウを確保して一瞬表示される。
        """
        import subprocess as _subprocess

        if sys.platform != "win32":
            pytest.skip("CREATE_NO_WINDOW は Windows 専用フラグ")

        captured_kwargs: list[dict] = []

        class _FakeResult:
            returncode = 0

        def _fake_run(args, **kwargs):
            captured_kwargs.append(kwargs)
            return _FakeResult()

        monkeypatch.setattr(_subprocess, "run", _fake_run)

        lock_path = tmp_path / ".claude" / "state" / "recall_rebuild.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_bytes(b"lock")
        monkeypatch.setattr(hook, "_lock_path_for", lambda root: lock_path)
        (tmp_path / ".claude").mkdir(exist_ok=True)

        hook.run_rebuild_worker(tmp_path)

        assert captured_kwargs, "subprocess.run must be called"
        flags = captured_kwargs[0].get("creationflags", 0)
        assert flags & _subprocess.CREATE_NO_WINDOW, (
            "subprocess.run must pass CREATE_NO_WINDOW on Windows to suppress "
            f"console window flash; got creationflags={flags!r}"
        )


# ---------------------------------------------------------------------------
# 17. L-04: CLAUDE_PROJECT_DIR に null バイトで find_repo_root() が None を返す
# ---------------------------------------------------------------------------


class TestFindRepoRootNullByte:
    """L-04 [SR-V-001]: find_repo_root() は Path.resolve() の ValueError を伝播させない。"""

    def test_find_repo_root_returns_none_on_null_byte_in_env(
        self,
        hook: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CLAUDE_PROJECT_DIR に null バイトが含まれるとき find_repo_root() は None を返す（L-04）。

        Path(value_with_null).resolve() は ValueError: embedded null byte を投げるが、
        find_repo_root は try/except (OSError, ValueError) で保護し None を返す。

        注意: Windows の os.environ は null バイト値を拒否するため、
        monkeypatch.setenv の代わりに os.getenv を直接 monkeypatch して
        null バイト値を返すよう差し替える。
        """
        null_path = "some/path\x00evil"

        # os.getenv を差し替えて null バイトを含む値を返させる
        original_getenv = os.getenv

        def _fake_getenv(key, default=None):
            if key == "CLAUDE_PROJECT_DIR":
                return null_path
            return original_getenv(key, default)

        monkeypatch.setattr(os, "getenv", _fake_getenv)

        # ValueError が伝播しないことを確認（None が返るべき）
        try:
            result = hook.find_repo_root()
        except (ValueError, OSError) as e:
            pytest.fail(
                f"find_repo_root() must not propagate {type(e).__name__} on null byte in env (L-04); "
                f"got: {e!r}"
            )

        assert result is None, (
            f"find_repo_root() must return None when CLAUDE_PROJECT_DIR contains null byte (L-04); "
            f"got: {result!r}"
        )

    def test_find_repo_root_path_resolve_raises_valueerror_on_null_byte(self) -> None:
        """Path(value_with_null).resolve() が実際に ValueError を投げることを確認（前提検証）。

        このテスト自体は常に PASS する。L-04 テストの前提（resolve が例外を投げること）を示す。
        """
        import platform

        null_path = "some/path\x00evil"
        try:
            Path(null_path).resolve()
            # resolve() が例外を投げなかった場合（Windows の一部環境で起きうる）
            # このテストはスキップ扱い（前提が成立しない環境）
            pytest.skip(
                f"Path(null_byte_path).resolve() did not raise on this platform "
                f"({platform.system()}); L-04 test_find_repo_root_returns_none_on_null_byte_in_env "
                f"precondition does not hold on this system"
            )
        except (ValueError, OSError):
            pass  # 期待通り例外が発生した
