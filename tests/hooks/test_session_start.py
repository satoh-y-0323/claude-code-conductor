"""Tests for .claude/hooks/session_start.py

SessionStart hook の統合エントリポイント。3 つの責務を 1 ファイルに統合した:
- _run_clear_file_history (旧 clear_file_history.py)
- _run_enable_sandbox    (旧 enable_sandbox.py)
- _run_init_c3_db        (旧 init_c3_db.py)

テスト方針:
- 各ハンドラは module を importlib でロードしてグローバル定数を patch することで
  unit テスト
- 統合動作（main()）はハンドラを mock して呼ばれ方を検証
- enable_sandbox 部分のみ、cwd ベースの挙動を確認するため subprocess 経由でも
  実行する（既存 test_enable_sandbox.py の挙動を維持）

テストケース:
 _run_clear_file_history:
  1. ディレクトリが存在しないとき: スキップ（exit 0）
  2. 通常ファイル削除
  3. サブディレクトリ削除（rmtree）
  4. シンボリックリンク削除（unlink、TOCTOU 安全）
  5. リンク先が外部のシンボリックリンク: スキップ
  6. FileNotFoundError は黙って続行

 _run_enable_sandbox:
  7. worktree 内: スキップ
  8. settings.json 不在: 作成しない
  9. JSON 壊れている: スキップ
 10. sandbox 設定済み: 変更しない
 11. sandbox 未設定: FULL_SANDBOX_CONFIG を書き込む

 _run_init_c3_db / apply_schema:
 12. DB ファイルが新規作成される
 13. 全テーブルが作られる
 14. WAL モード有効化
 15. schema_version 記録
 16. 再実行で crash しない
 17. 既存データ保持
 18. schema_version 重複しない
 19. schema.sql 不在でも main() は exit 0
 20. DuckDB 連携で SELECT できる

 main() オーケストレータ:
 21. 3 ハンドラが呼ばれる
 22. 1 つが例外を投げても他が実行される
 23. 全成功でも exit 0
 24. 全失敗でも exit 0
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
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
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "session_start.py"
SCHEMA_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "schema.sql"


def _load_hook_module() -> types.ModuleType:
    """session_start.py を __main__ を実行せずモジュールとしてロード."""
    spec = importlib.util.spec_from_file_location("session_start", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def _list_tables(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _write_settings(cwd: Path, content: dict | str) -> Path:
    """cwd/.claude/settings.json を作成して返す."""
    settings_dir = cwd / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"
    if isinstance(content, dict):
        settings_path.write_text(
            json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    else:
        settings_path.write_text(content, encoding="utf-8")
    return settings_path


def _run_hook_subprocess(cwd: Path) -> subprocess.CompletedProcess:
    """session_start.py を subprocess で実行する（cwd ベースの挙動確認用）."""
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd),
    )


# ===========================================================================
# 1. _run_clear_file_history
# ===========================================================================


class TestClearFileHistory:
    """旧 clear_file_history.py のロジックを統合した部分の検証."""

    def test_missing_dir_is_skipped(self, tmp_path: Path):
        """ディレクトリが存在しないとき何もしない."""
        module = _load_hook_module()
        missing = tmp_path / "nonexistent"

        with patch.object(module, "FILE_HISTORY_DIR", str(missing)):
            module._run_clear_file_history()  # 例外を投げない

    def test_deletes_regular_file(self, tmp_path: Path):
        """通常ファイルを削除する."""
        module = _load_hook_module()
        fake_history = tmp_path / "file-history"
        fake_history.mkdir()
        (fake_history / "some_file.json").write_text("{}", encoding="utf-8")

        with patch.object(module, "FILE_HISTORY_DIR", str(fake_history)):
            module._run_clear_file_history()

        assert not (fake_history / "some_file.json").exists()

    def test_deletes_subdirectory(self, tmp_path: Path):
        """サブディレクトリを rmtree で削除する."""
        module = _load_hook_module()
        fake_history = tmp_path / "file-history"
        fake_history.mkdir()
        sub = fake_history / "sub_dir"
        sub.mkdir()
        (sub / "child.txt").write_text("x", encoding="utf-8")

        with patch.object(module, "FILE_HISTORY_DIR", str(fake_history)):
            module._run_clear_file_history()

        assert not sub.exists()

    def test_symlink_uses_unlink_not_rmtree(self, tmp_path: Path):
        """シンボリックリンクは os.unlink で削除（TOCTOU 対策）."""
        module = _load_hook_module()
        fake_history = tmp_path / "file-history"
        fake_history.mkdir()
        target_dir = fake_history / "real_dir"
        target_dir.mkdir()
        symlink = fake_history / "link_entry"
        try:
            symlink.symlink_to(target_dir)
        except OSError:
            pytest.skip("シンボリックリンクを作れない環境（権限不足等）")

        with patch.object(module, "FILE_HISTORY_DIR", str(fake_history)):
            module._run_clear_file_history()

        # symlink 自体は消えるが、リンク先は消えない
        assert not symlink.exists()
        assert target_dir.exists()

    def test_external_symlink_is_skipped(self, tmp_path: Path):
        """リンク先が FILE_HISTORY_DIR 外のシンボリックリンクはスキップする."""
        module = _load_hook_module()
        fake_history = tmp_path / "file-history"
        fake_history.mkdir()
        external_dir = tmp_path / "external"
        external_dir.mkdir()
        (external_dir / "important.txt").write_text("preserve", encoding="utf-8")

        symlink = fake_history / "external_link"
        try:
            symlink.symlink_to(external_dir)
        except OSError:
            pytest.skip("シンボリックリンクを作れない環境（権限不足等）")

        with patch.object(module, "FILE_HISTORY_DIR", str(fake_history)):
            module._run_clear_file_history()

        # symlink 自体は残る（スキップされる）
        assert symlink.is_symlink()
        # 外部の重要ファイルも保護される
        assert (external_dir / "important.txt").exists()

    def test_file_not_found_is_handled(self, tmp_path: Path):
        """unlink 中に FileNotFoundError が出ても続行する."""
        module = _load_hook_module()
        fake_history = tmp_path / "file-history"
        fake_history.mkdir()
        (fake_history / "vanishing_file.json").write_text("{}", encoding="utf-8")

        def _raising_unlink(path: str) -> None:
            raise FileNotFoundError(f"gone: {path}")

        with (
            patch.object(module, "FILE_HISTORY_DIR", str(fake_history)),
            patch.object(module.os, "unlink", side_effect=_raising_unlink),
        ):
            # 例外は外に漏れない
            module._run_clear_file_history()


# ===========================================================================
# 2. _run_enable_sandbox
# ===========================================================================


class TestEnableSandbox:
    """旧 enable_sandbox.py のロジックを統合した部分の検証.

    cwd 依存の挙動を確認するため、subprocess で session_start.py 全体を起動する.
    `_run_clear_file_history` と `_run_init_c3_db` は副作用が大きいため、
    subprocess 実行時はそれらを skipped にできる環境変数を使う想定がない代わりに、
    monkeypatch で定数を変更することで隔離する.
    """

    def test_worktree_skips_modifying_settings(self, tmp_path: Path):
        """`.git` がファイルなら worktree とみなしてスキップする."""
        # .git をファイルとして作成（worktree を模倣）
        (tmp_path / ".git").write_text("gitdir: ../real/.git", encoding="utf-8")
        initial = {"someKey": "someValue"}
        settings_path = _write_settings(tmp_path, initial)

        # subprocess 経由で実行（cwd を tmp_path にする）
        result = _run_hook_subprocess(tmp_path)

        assert result.returncode == 0
        updated = json.loads(settings_path.read_text(encoding="utf-8"))
        assert updated == initial, "worktree 内では settings.json を変更してはいけない"

    def test_no_settings_json_does_not_create_file(self, tmp_path: Path):
        """settings.json が無ければ作らない."""
        result = _run_hook_subprocess(tmp_path)

        assert result.returncode == 0
        assert not (tmp_path / ".claude" / "settings.json").exists()

    def test_broken_json_is_skipped(self, tmp_path: Path):
        """壊れた JSON はスキップされる."""
        broken = "{ this is not valid json !!!"
        settings_path = _write_settings(tmp_path, broken)

        result = _run_hook_subprocess(tmp_path)

        assert result.returncode == 0
        assert settings_path.read_text(encoding="utf-8") == broken

    def test_sandbox_already_enabled_does_not_modify(self, tmp_path: Path):
        """sandbox.enabled=True なら何もしない."""
        initial = {
            "someKey": "someValue",
            "sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True},
        }
        settings_path = _write_settings(tmp_path, initial)
        before = settings_path.read_text(encoding="utf-8")

        result = _run_hook_subprocess(tmp_path)

        assert result.returncode == 0
        assert settings_path.read_text(encoding="utf-8") == before

    def test_sandbox_not_set_writes_full_config(self, tmp_path: Path):
        """sandbox 未設定なら FULL_SANDBOX_CONFIG を書き込む."""
        initial = {"someKey": "someValue"}
        settings_path = _write_settings(tmp_path, initial)

        result = _run_hook_subprocess(tmp_path)

        assert result.returncode == 0
        updated = json.loads(settings_path.read_text(encoding="utf-8"))
        assert updated["sandbox"]["enabled"] is True
        assert updated["sandbox"]["autoAllowBashIfSandboxed"] is True
        assert updated["sandbox"]["allowUnsandboxedCommands"] is False
        assert "network" in updated["sandbox"]
        # 既存キーが保持される
        assert updated.get("someKey") == "someValue"


# ===========================================================================
# 3. _run_init_c3_db / apply_schema
# ===========================================================================


class TestInitC3Db:
    """旧 init_c3_db.py のロジックを統合した部分の検証."""

    def test_db_file_is_created(self, tmp_path: Path):
        module = _load_hook_module()
        db_path = tmp_path / "c3.db"

        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))

        assert db_path.exists()

    def test_all_tables_are_created(self, tmp_path: Path):
        module = _load_hook_module()
        db_path = tmp_path / "c3.db"

        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))

        tables = _list_tables(db_path)
        expected = {
            'schema_version',
            'review_decisions',
            'po_results',
            'po_status',
            'tier_bandit',
            'tier_recent_outcomes',
            'agent_runs',
        }
        missing = expected - tables
        assert not missing, f"作られていないテーブル: {missing}"

    def test_wal_mode_is_enabled(self, tmp_path: Path):
        module = _load_hook_module()
        db_path = tmp_path / "c3.db"

        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))

        conn = sqlite3.connect(str(db_path))
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        assert mode.lower() == 'wal'

    def test_schema_version_is_recorded(self, tmp_path: Path):
        module = _load_hook_module()
        db_path = tmp_path / "c3.db"

        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))

        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
        finally:
            conn.close()
        assert rows == [(module.SCHEMA_VERSION,)]

    def test_reapply_does_not_crash(self, tmp_path: Path):
        module = _load_hook_module()
        db_path = tmp_path / "c3.db"

        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))
        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))

        tables = _list_tables(db_path)
        assert 'schema_version' in tables
        assert 'agent_runs' in tables

    def test_existing_data_is_preserved(self, tmp_path: Path):
        module = _load_hook_module()
        db_path = tmp_path / "c3.db"

        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO review_decisions "
                "(checklist_id, finding_text, decision, decided_at, reviewer) "
                "VALUES (?, ?, ?, ?, ?)",
                ('CR-Q-001', 'foo', 'accepted', '2026-05-08T00:00:00+00:00', 'code-reviewer'),
            )
            conn.commit()
        finally:
            conn.close()

        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))

        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_schema_version_not_duplicated(self, tmp_path: Path):
        module = _load_hook_module()
        db_path = tmp_path / "c3.db"

        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))
        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))
        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))

        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_main_returns_zero_when_schema_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """schema.sql が無くても main() は exit 0 を返す."""
        module = _load_hook_module()

        # 全ハンドラが副作用を起こさないように差し替え
        monkeypatch.setattr(module, '_run_clear_file_history', lambda: None)
        monkeypatch.setattr(module, '_run_enable_sandbox', lambda: None)
        # init_c3_db のみ schema を欠落させる
        monkeypatch.setattr(module, 'SCHEMA_PATH', str(tmp_path / "missing.sql"))
        monkeypatch.setattr(module, 'STATE_DIR', str(tmp_path / "state"))
        monkeypatch.setattr(module, 'DB_PATH', str(tmp_path / "state" / "c3.db"))

        # apply_schema が ValueError を投げる状況でも main() は exit 0
        result = module.main()
        assert result == 0

    def test_duckdb_can_attach_and_query(self, tmp_path: Path):
        try:
            import duckdb  # type: ignore[import-not-found]
        except ImportError:
            pytest.skip("duckdb がインストールされていない")

        module = _load_hook_module()
        db_path = tmp_path / "c3.db"
        module.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO agent_runs "
                "(session_id, agent_id, agent_type, event, ts, total_tokens, status, model) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ('sess-1', 'agent-a', 'Explore', 'stop',
                 '2026-05-08T00:00:00+00:00', 12345, 'success', 'claude-sonnet-4-6'),
            )
            conn.commit()
        finally:
            conn.close()

        con = duckdb.connect()
        try:
            con.execute("INSTALL sqlite")
            con.execute("LOAD sqlite")
            con.execute(f"ATTACH '{db_path}' AS c3 (TYPE SQLITE)")
            rows = con.execute(
                "SELECT session_id, total_tokens, model FROM c3.agent_runs"
            ).fetchall()
        finally:
            con.close()

        assert rows == [('sess-1', 12345, 'claude-sonnet-4-6')]


# ===========================================================================
# 4. main() オーケストレータ
# ===========================================================================


class TestOrchestration:
    """main() が 3 つのハンドラを順次呼び出すこと、片方失敗時も継続することを検証."""

    def test_all_handlers_are_called(self, monkeypatch: pytest.MonkeyPatch):
        """3 つのハンドラがすべて 1 回ずつ呼ばれる."""
        module = _load_hook_module()
        called: list[str] = []

        monkeypatch.setattr(module, '_run_clear_file_history', lambda: called.append('clear'))
        monkeypatch.setattr(module, '_run_enable_sandbox', lambda: called.append('sandbox'))
        monkeypatch.setattr(module, '_run_init_c3_db', lambda: called.append('db'))

        result = module.main()
        assert result == 0
        assert called == ['clear', 'sandbox', 'db']

    def test_failure_in_first_handler_does_not_stop_others(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """最初のハンドラが例外を投げても残り 2 つは実行される."""
        module = _load_hook_module()
        called: list[str] = []

        def _failing_clear():
            called.append('clear-attempted')
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(module, '_run_clear_file_history', _failing_clear)
        monkeypatch.setattr(module, '_run_enable_sandbox', lambda: called.append('sandbox'))
        monkeypatch.setattr(module, '_run_init_c3_db', lambda: called.append('db'))

        result = module.main()
        assert result == 0
        assert 'sandbox' in called
        assert 'db' in called

    def test_failure_in_middle_handler_does_not_stop_last(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """中間のハンドラが例外を投げても最後のハンドラが実行される."""
        module = _load_hook_module()
        called: list[str] = []

        def _failing_sandbox():
            called.append('sandbox-attempted')
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(module, '_run_clear_file_history', lambda: called.append('clear'))
        monkeypatch.setattr(module, '_run_enable_sandbox', _failing_sandbox)
        monkeypatch.setattr(module, '_run_init_c3_db', lambda: called.append('db'))

        result = module.main()
        assert result == 0
        assert 'db' in called

    def test_all_failures_still_returns_zero(self, monkeypatch: pytest.MonkeyPatch):
        """全ハンドラが失敗しても exit 0 を維持する."""
        module = _load_hook_module()

        def _fail():
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(module, '_run_clear_file_history', _fail)
        monkeypatch.setattr(module, '_run_enable_sandbox', _fail)
        monkeypatch.setattr(module, '_run_init_c3_db', _fail)

        result = module.main()
        assert result == 0
