"""Tests for .claude/hooks/enable_sandbox.py

subprocess 経由で実行し、終了コード・出力メッセージ・ファイル変更の有無を検証する。

テストケース:
1. settings.json が存在しない: exit 0、「settings.json が見つかりません」を含む出力
2. sandbox がすでに有効: exit 0、「すでに有効」を含む出力、ファイル内容が変わっていない
3. sandbox が未設定: exit 0、「sandbox を有効化」を含む出力、enabled: true が書き込まれる
4. worktree 内（.git がファイル）: exit 0、「スキップ」を含む出力
5. JSON が壊れている: exit 0、「JSON 解析に失敗」を含む出力
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# テスト対象スクリプトへの絶対パス
_HOOK = Path(__file__).parent.parent / ".claude" / "hooks" / "enable_sandbox.py"


def _run_hook(cwd: Path) -> subprocess.CompletedProcess:
    """enable_sandbox.py を subprocess で実行し、結果を返す。"""
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd),
    )


def _make_settings(cwd: Path, content: dict | str) -> Path:
    """cwd/.claude/settings.json を作成して返す。"""
    settings_dir = cwd / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"
    if isinstance(content, dict):
        settings_path.write_text(
            json.dumps(content, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    else:
        settings_path.write_text(content, encoding="utf-8")
    return settings_path


# ---------------------------------------------------------------------------
# 1. settings.json が存在しない
# ---------------------------------------------------------------------------


def test_no_settings_json_exits_zero_with_message(tmp_path: Path):
    """settings.json が存在しないとき exit 0 で「settings.json が見つかりません」を出力する。"""
    result = _run_hook(tmp_path)

    assert result.returncode == 0, (
        f"settings.json なしでも exit 0 であるべき。got: {result.returncode}"
    )
    combined = result.stdout + result.stderr
    assert "settings.json が見つかりません" in combined, (
        f"「settings.json が見つかりません」を含む出力が期待される。\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# 2. sandbox がすでに有効
# ---------------------------------------------------------------------------


def test_sandbox_already_enabled_exits_zero_with_message_and_no_change(tmp_path: Path):
    """sandbox.enabled == True のとき exit 0、「すでに有効」を出力し、ファイルを変更しない。"""
    initial = {
        "someKey": "someValue",
        "sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True},
    }
    settings_path = _make_settings(tmp_path, initial)
    content_before = settings_path.read_text(encoding="utf-8")

    result = _run_hook(tmp_path)

    assert result.returncode == 0, (
        f"sandbox 有効時でも exit 0 であるべき。got: {result.returncode}"
    )
    combined = result.stdout + result.stderr
    assert "すでに有効" in combined, (
        f"「すでに有効」を含む出力が期待される。\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    content_after = settings_path.read_text(encoding="utf-8")
    assert content_after == content_before, (
        "sandbox がすでに有効な場合、settings.json を変更してはいけない。"
    )


# ---------------------------------------------------------------------------
# 3. sandbox が未設定
# ---------------------------------------------------------------------------


def test_sandbox_not_set_exits_zero_with_message_and_writes_enabled(tmp_path: Path):
    """sandbox が未設定のとき exit 0、「sandbox を有効化」を出力し、enabled: true を書き込む。"""
    initial = {"someKey": "someValue"}
    settings_path = _make_settings(tmp_path, initial)

    result = _run_hook(tmp_path)

    assert result.returncode == 0, (
        f"sandbox 未設定時でも exit 0 であるべき。got: {result.returncode}"
    )
    combined = result.stdout + result.stderr
    assert "sandbox を有効化" in combined, (
        f"「sandbox を有効化」を含む出力が期待される。\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    updated = json.loads(settings_path.read_text(encoding="utf-8"))
    assert updated.get("sandbox", {}).get("enabled") is True, (
        f"settings.json の sandbox.enabled が True であるべき。got: {updated.get('sandbox')!r}"
    )
    # 既存キーが保持されていることを確認
    assert updated.get("someKey") == "someValue", (
        "既存の設定キーが保持されるべき。"
    )


# ---------------------------------------------------------------------------
# 4. worktree 内（.git がファイル）
# ---------------------------------------------------------------------------


def test_worktree_git_file_exits_zero_with_skip_message(tmp_path: Path):
    """.git がファイル（git worktree）のとき exit 0 で「スキップ」を含む出力をする。"""
    # .git をファイルとして作成して worktree を模倣
    (tmp_path / ".git").write_text("gitdir: ../real/.git", encoding="utf-8")

    result = _run_hook(tmp_path)

    assert result.returncode == 0, (
        f"worktree 内でも exit 0 であるべき。got: {result.returncode}"
    )
    combined = result.stdout + result.stderr
    assert "スキップ" in combined, (
        f"「スキップ」を含む出力が期待される。\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# 5. JSON が壊れている
# ---------------------------------------------------------------------------


def test_broken_json_exits_zero_with_parse_error_message(tmp_path: Path):
    """settings.json の JSON が壊れているとき exit 0 で「JSON 解析に失敗」を出力する。"""
    settings_path = _make_settings(tmp_path, "{ this is not valid json !!!")
    content_before = settings_path.read_text(encoding="utf-8")

    result = _run_hook(tmp_path)

    assert result.returncode == 0, (
        f"壊れた JSON でも exit 0 であるべき。got: {result.returncode}"
    )
    combined = result.stdout + result.stderr
    assert "JSON 解析に失敗" in combined, (
        f"「JSON 解析に失敗」を含む出力が期待される。\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    # ファイルが変更されていないことも確認
    content_after = settings_path.read_text(encoding="utf-8")
    assert content_after == content_before, (
        "壊れた JSON の場合、ファイルを変更してはいけない。"
    )
