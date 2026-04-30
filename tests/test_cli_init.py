"""Tests for ``c3 init`` and ``c3 update`` against a temporary project root."""

from __future__ import annotations

import argparse
from pathlib import Path

from c3 import cli_init, cli_update


def _run_init(target: Path, *, force: bool = False) -> int:
    return cli_init.handle(argparse.Namespace(target=target, force=force))


def _run_update(target: Path, *, dry_run: bool = False) -> int:
    return cli_update.handle(argparse.Namespace(target=target, dry_run=dry_run))


def test_init_scaffolds_claude_dir(tmp_path: Path, capsys):
    rc = _run_init(tmp_path)
    assert rc == 0
    assert (tmp_path / ".claude").is_dir()
    assert (tmp_path / ".claude" / "agents").is_dir()
    assert (tmp_path / ".claude" / "skills").is_dir()
    assert (tmp_path / ".claude" / "commands").is_dir()
    captured = capsys.readouterr()
    assert "initialized" in captured.out


def test_init_refuses_existing_without_force(tmp_path: Path, capsys):
    (tmp_path / ".claude").mkdir()
    rc = _run_init(tmp_path)
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to overwrite" in err


def test_init_force_overwrites(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "stale.txt").write_text("old", encoding="utf-8")
    rc = _run_init(tmp_path, force=True)
    assert rc == 0
    assert not (tmp_path / ".claude" / "stale.txt").exists()
    assert (tmp_path / ".claude" / "agents").is_dir()


def test_update_no_target_dir(tmp_path: Path, capsys):
    rc = _run_update(tmp_path)
    assert rc == 1
    assert "no .claude/" in capsys.readouterr().err


def test_update_dry_run_shows_diff(tmp_path: Path, capsys):
    _run_init(tmp_path)
    # Modify a single file to create a diff.
    target_file = tmp_path / ".claude" / "agents" / "developer.md"
    target_file.write_text("MODIFIED\n", encoding="utf-8")
    rc = _run_update(tmp_path, dry_run=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "developer.md" in out


def test_update_idempotent_after_init(tmp_path: Path, capsys):
    _run_init(tmp_path)
    rc = _run_update(tmp_path)
    assert rc == 0
    assert "up to date" in capsys.readouterr().out
