"""Tests for .claude/hooks/enable_sandbox.py

subprocess で実行する方式を採用（sys.stdout.reconfigure 問題を避けるため）。

テストケース:
1. .git がファイルのとき（worktree 内）: スキップして何もしない（settings.json を書き換えない）
2. settings.json が存在しないとき: スキップ（settings.json を作らない）
3. settings.json の JSON が壊れているとき: スキップ
4. settings.json に sandbox.enabled: true が設定済みのとき: 変更しない
5. settings.json に sandbox が未設定のとき: FULL_SANDBOX_CONFIG が書き込まれる
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "enable_sandbox.py"


def _run_hook(cwd: Path) -> subprocess.CompletedProcess:
    """enable_sandbox.py を subprocess で実行し、結果を返す。"""
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd),
    )


def _write_settings(cwd: Path, content: dict | str) -> Path:
    """cwd/.claude/settings.json を作成して返す。"""
    settings_dir = cwd / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"
    if isinstance(content, dict):
        settings_path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        # 壊れた JSON を書く場合はそのまま書き込む
        settings_path.write_text(content, encoding="utf-8")
    return settings_path


# ---------------------------------------------------------------------------
# 1. .git がファイルのとき（worktree 内）: settings.json を書き換えない
# ---------------------------------------------------------------------------


def test_worktree_skips_without_modifying_settings(tmp_path: Path):
    """.git がファイル（git worktree）のとき、settings.json を書き換えない。"""
    # .git をファイルとして作成（worktree を模倣）
    (tmp_path / ".git").write_text("gitdir: ../real/.git", encoding="utf-8")

    # settings.json を初期状態で作成
    initial_content = {"someKey": "someValue"}
    settings_path = _write_settings(tmp_path, initial_content)
    initial_mtime = settings_path.stat().st_mtime

    result = _run_hook(tmp_path)

    assert result.returncode == 0, (
        f"worktree 内での実行は exit 0 であるべき。got: {result.returncode}\n"
        f"stderr: {result.stderr}"
    )
    # settings.json が変更されていないことを確認
    updated_content = json.loads(settings_path.read_text(encoding="utf-8"))
    assert updated_content == initial_content, (
        "worktree 内では settings.json を変更してはいけない。\n"
        f"変更前: {initial_content}\n変更後: {updated_content}"
    )


# ---------------------------------------------------------------------------
# 2. settings.json が存在しないとき: settings.json を作らない
# ---------------------------------------------------------------------------


def test_no_settings_json_does_not_create_file(tmp_path: Path):
    """settings.json が存在しないとき、スキップして新たにファイルを作らない。"""
    # .claude ディレクトリ自体も作らない（settings.json が存在しないケース）
    settings_path = tmp_path / ".claude" / "settings.json"

    result = _run_hook(tmp_path)

    assert result.returncode == 0, (
        f"settings.json なしでの実行は exit 0 であるべき。got: {result.returncode}"
    )
    assert not settings_path.exists(), (
        "settings.json が存在しない場合、新規作成してはいけない。"
    )


# ---------------------------------------------------------------------------
# 3. settings.json の JSON が壊れているとき: スキップ
# ---------------------------------------------------------------------------


def test_broken_json_is_skipped(tmp_path: Path):
    """settings.json の JSON が壊れているとき、スキップして変更しない。"""
    broken_json = "{ this is not valid json !!!"
    settings_path = _write_settings(tmp_path, broken_json)
    content_before = settings_path.read_text(encoding="utf-8")

    result = _run_hook(tmp_path)

    assert result.returncode == 0, (
        f"壊れた JSON でも exit 0 であるべき。got: {result.returncode}"
    )
    content_after = settings_path.read_text(encoding="utf-8")
    assert content_after == content_before, (
        "壊れた JSON の場合、ファイルを変更してはいけない。"
    )


# ---------------------------------------------------------------------------
# 4. sandbox.enabled: true が設定済みのとき: 変更しない
# ---------------------------------------------------------------------------


def test_sandbox_already_enabled_does_not_modify(tmp_path: Path):
    """sandbox.enabled が True のとき、settings.json を変更しない。"""
    initial_content = {
        "someKey": "someValue",
        "sandbox": {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
        },
    }
    settings_path = _write_settings(tmp_path, initial_content)
    content_before = settings_path.read_text(encoding="utf-8")

    result = _run_hook(tmp_path)

    assert result.returncode == 0, (
        f"sandbox 有効時の実行は exit 0 であるべき。got: {result.returncode}"
    )
    content_after = settings_path.read_text(encoding="utf-8")
    assert content_after == content_before, (
        "sandbox がすでに有効な場合、settings.json を変更してはいけない。"
    )


# ---------------------------------------------------------------------------
# 5. sandbox が未設定のとき: FULL_SANDBOX_CONFIG が書き込まれる
# ---------------------------------------------------------------------------


def test_sandbox_not_set_writes_full_config(tmp_path: Path):
    """sandbox が未設定のとき、FULL_SANDBOX_CONFIG が settings.json に書き込まれる。"""
    initial_content = {"someKey": "someValue"}
    settings_path = _write_settings(tmp_path, initial_content)

    result = _run_hook(tmp_path)

    assert result.returncode == 0, (
        f"sandbox 未設定時の実行は exit 0 であるべき。got: {result.returncode}"
    )
    updated = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "sandbox" in updated, (
        "sandbox が未設定のとき、settings.json に sandbox キーを追加すべき。"
    )
    sandbox = updated["sandbox"]
    assert sandbox.get("enabled") is True, (
        f"sandbox.enabled が True であるべき。got: {sandbox.get('enabled')!r}"
    )
    assert sandbox.get("autoAllowBashIfSandboxed") is True, (
        f"sandbox.autoAllowBashIfSandboxed が True であるべき。got: {sandbox.get('autoAllowBashIfSandboxed')!r}"
    )
    assert sandbox.get("allowUnsandboxedCommands") is False, (
        f"sandbox.allowUnsandboxedCommands が False であるべき。got: {sandbox.get('allowUnsandboxedCommands')!r}"
    )
    assert "network" in sandbox, (
        "sandbox.network キーが存在すべき。"
    )
    # 既存のキー（someKey）が保持されていることを確認
    assert updated.get("someKey") == "someValue", (
        "既存の設定（someKey）が保持されるべき。"
    )
