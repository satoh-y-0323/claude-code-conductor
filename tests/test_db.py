"""Tests for src/c3/db.py:locate_c3_db()

`locate_c3_db()` の 3 経路を契約として固定する回帰テスト:
  1. C3_DB_PATH env (worktree 内子プロセス用、v2.0.0+)
  2. C3_PO_DB_PATH env (legacy, v1.x 互換)
  3. CWD からの親遡り fallback

worktree 環境変化（Claude Code v2.1.149 sandbox 厳格化など）に対する
C3 側の env-aware path 解決の壊れにくさを担保する。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from c3.db import locate_c3_db


def _make_fake_db(base: Path) -> Path:
    """`base/.claude/state/c3.db` を作って返す。

    locate_c3_db は is_file() 判定のみ行い中身を読まないため、
    空ファイルで十分。
    """
    db_path = base / ".claude" / "state" / "c3.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.touch()
    return db_path


def _assert_fallback_warning(caplog: pytest.LogCaptureFixture) -> None:
    """env が無効値・ディレクトリ等で fallback した時の警告内容を契約として固定する。

    NOTE: src/c3/db.py の現在の警告フォーマット
      "%s set but file not found: %s (falling back to traversal)"
    を現状スナップショットとして検証する。将来ログメッセージを精緻化する場合は
    本ヘルパーを意図的に更新すること（caplog の片側 OR では検出できないため）。
    """
    assert "C3_DB_PATH" in caplog.text, "Warning must mention env var name C3_DB_PATH."
    assert "file not found" in caplog.text, (
        "Warning must mention 'file not found' (current db.py message). "
        "If db.py message is intentionally refined, update this assertion."
    )
    assert "falling back to traversal" in caplog.text, (
        "Warning must mention 'falling back to traversal' (current db.py message). "
        "If db.py message is intentionally refined, update this assertion."
    )


def test_locate_c3_db_env_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """C3_DB_PATH が設定されていれば、CWD 親遡りより優先される."""
    env_db = _make_fake_db(tmp_path / "env_root")
    cwd_db = _make_fake_db(tmp_path / "cwd_root")
    cwd_subdir = tmp_path / "cwd_root" / "sub" / "deeper"
    cwd_subdir.mkdir(parents=True)

    monkeypatch.setenv("C3_DB_PATH", str(env_db))
    monkeypatch.delenv("C3_PO_DB_PATH", raising=False)

    result = locate_c3_db(start=cwd_subdir)

    assert result == env_db.resolve(), (
        f"C3_DB_PATH should win over parent traversal. "
        f"Expected {env_db.resolve()}, got {result}"
    )
    assert result != cwd_db.resolve(), (
        "Parent traversal should NOT have been triggered while C3_DB_PATH is valid."
    )


def test_locate_c3_db_legacy_env_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """C3_DB_PATH 未設定で C3_PO_DB_PATH が設定されていれば legacy 経路で解決。
    またこのとき deprecation 警告が WARNING で発火する。"""
    legacy_db = _make_fake_db(tmp_path / "legacy_root")

    monkeypatch.delenv("C3_DB_PATH", raising=False)
    monkeypatch.setenv("C3_PO_DB_PATH", str(legacy_db))

    with caplog.at_level(logging.WARNING, logger="c3.db"):
        result = locate_c3_db(start=tmp_path)

    assert result == legacy_db.resolve(), (
        f"C3_PO_DB_PATH should resolve when C3_DB_PATH is unset. "
        f"Expected {legacy_db.resolve()}, got {result}"
    )
    assert "C3_PO_DB_PATH is deprecated" in caplog.text, (
        "Legacy env path should emit a deprecation warning."
    )


def test_locate_c3_db_parent_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """env 未設定なら start から親遡りで .claude/state/c3.db を見つける."""
    root_db = _make_fake_db(tmp_path)
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)

    monkeypatch.delenv("C3_DB_PATH", raising=False)
    monkeypatch.delenv("C3_PO_DB_PATH", raising=False)

    result = locate_c3_db(start=deep)

    assert result == root_db.resolve(), (
        f"Parent traversal should find .claude/state/c3.db. "
        f"Expected {root_db.resolve()}, got {result}"
    )


def test_locate_c3_db_invalid_env_falls_back_to_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """env が設定されていても指すパスが無効ならば親遡り fallback に進む."""
    valid_db = _make_fake_db(tmp_path)

    monkeypatch.setenv("C3_DB_PATH", str(tmp_path / "nonexistent.db"))
    monkeypatch.delenv("C3_PO_DB_PATH", raising=False)

    with caplog.at_level(logging.WARNING, logger="c3.db"):
        result = locate_c3_db(start=tmp_path)

    assert result == valid_db.resolve(), (
        "Invalid env path should fall through to parent traversal."
    )
    _assert_fallback_warning(caplog)


def test_locate_c3_db_env_pointing_to_directory_falls_back_to_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """ディレクトリ指定のテスト（SR L-3 追加）。

    `test_locate_c3_db_invalid_env_falls_back_to_traversal` は env が「存在しない
    ファイルパス」を指すケースをカバーするが、本テストは env が「実在するディレクトリ」
    を指すケースをカバーする（is_file() False の別経路）。setup は env に
    `tmp_path/some_dir` (mkdir 済) を設定し、親遡り fallback 用に
    `_make_fake_db(tmp_path)` で `tmp_path/.claude/state/c3.db` を別途用意する。

    SR L-3 指摘: ディレクトリ指定でも "file not found" のログが出る（診断メッセージの
    誤解可能性）。この挙動をテストとして固定し、将来の修正時に意図的な変更として検出する。
    """
    some_dir = tmp_path / "some_dir"
    some_dir.mkdir()
    valid_db = _make_fake_db(tmp_path)

    monkeypatch.setenv("C3_DB_PATH", str(some_dir))
    monkeypatch.delenv("C3_PO_DB_PATH", raising=False)

    with caplog.at_level(logging.WARNING, logger="c3.db"):
        result = locate_c3_db(start=tmp_path)

    assert result == valid_db.resolve(), (
        "Directory env path should fall through to parent traversal."
    )
    _assert_fallback_warning(caplog)
