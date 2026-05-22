"""src/c3/cli_update.py の _warn_deprecated_paths と DEPRECATED_PATHS のテスト。"""
from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path

import pytest

from c3.cli_update import DEPRECATED_PATHS, _warn_deprecated_paths


def test_deprecated_paths_constant_is_tuple_of_pairs():
    """DEPRECATED_PATHS は tuple であり、各要素は (rel_path, reason) の 2 要素タプル。"""
    assert isinstance(DEPRECATED_PATHS, tuple)
    for entry in DEPRECATED_PATHS:
        assert isinstance(entry, tuple)
        assert len(entry) == 2
        rel_path, reason = entry
        assert isinstance(rel_path, str)
        assert isinstance(reason, str)


def test_no_warning_when_deprecated_path_absent(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """dest_root 配下に廃止パスが存在しない場合、stderr に何も出力しない。"""
    _warn_deprecated_paths(tmp_path)
    captured = capsys.readouterr()
    assert captured.err == ""


def test_warning_when_deprecated_path_present(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """dest_root 配下に廃止パスが残存していたら stderr に warning を出力する。"""
    if not DEPRECATED_PATHS:
        pytest.skip("DEPRECATED_PATHS が空")
    rel_path, _ = DEPRECATED_PATHS[0]
    target = tmp_path / rel_path
    target.mkdir(parents=True, exist_ok=True)
    _warn_deprecated_paths(tmp_path)
    captured = capsys.readouterr()
    assert "deprecated" in captured.err.lower()
    assert rel_path in captured.err
    assert "manual" in captured.err.lower() or "remove" in captured.err.lower()
