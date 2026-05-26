"""Tests for ``c3 init`` and ``c3 update`` against a temporary project root."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from c3 import cli_init, cli_update


def _run_init(target: Path, *, force: bool = False, platform: str = "claude") -> int:
    return cli_init.handle(argparse.Namespace(target=target, force=force, platform=platform))


def _run_update(target: Path, *, dry_run: bool = False, platform: str = "claude", yes: bool = False) -> int:
    return cli_update.handle(argparse.Namespace(target=target, dry_run=dry_run, platform=platform, yes=yes))


def test_init_scaffolds_claude_dir(tmp_path: Path, capsys):
    rc = _run_init(tmp_path)
    assert rc == 0
    assert (tmp_path / ".claude").is_dir()
    assert (tmp_path / ".claude" / "agents").is_dir()
    assert (tmp_path / ".claude" / "skills").is_dir()
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


def test_init_excludes_personal_files(tmp_path: Path, monkeypatch):
    """Even if templates_dir() returns a 'dirty' tree (live .claude/ during dev),
    `c3 init` must filter out personal/working files via c3._excludes.
    """
    dirty_template = tmp_path / "dirty_template"
    dirty_template.mkdir()
    (dirty_template / "agents").mkdir()
    (dirty_template / "agents" / "architect.md").write_text("framework", encoding="utf-8")
    (dirty_template / "memory").mkdir()
    (dirty_template / "memory" / ".gitkeep").write_text("", encoding="utf-8")
    (dirty_template / "memory" / "patterns.json").write_text("{}", encoding="utf-8")
    (dirty_template / "memory" / "sessions").mkdir()
    (dirty_template / "memory" / "sessions" / "20260427.tmp").write_text(
        "session-data", encoding="utf-8"
    )
    (dirty_template / "reports").mkdir()
    (dirty_template / "reports" / ".gitkeep").write_text("", encoding="utf-8")
    (dirty_template / "reports" / "plan-report-x.md").write_text("plan", encoding="utf-8")
    (dirty_template / "docs").mkdir()
    (dirty_template / "docs" / "decisions.md").write_text("local", encoding="utf-8")
    (dirty_template / "docs" / "settings.json.md").write_text(
        "spec", encoding="utf-8"
    )

    target = tmp_path / "target"
    monkeypatch.setattr("c3.cli_init.templates_dir", lambda: dirty_template)

    rc = _run_init(target)
    assert rc == 0

    dest = target / ".claude"
    # Framework files are copied
    assert (dest / "agents" / "architect.md").is_file()
    assert (dest / "docs" / "settings.json.md").is_file()
    # .gitkeep stubs survive
    assert (dest / "memory" / ".gitkeep").is_file()
    assert (dest / "reports" / ".gitkeep").is_file()
    # Personal/working files are dropped
    assert not (dest / "memory" / "patterns.json").exists()
    assert not (dest / "memory" / "sessions" / "20260427.tmp").exists()
    assert not (dest / "reports" / "plan-report-x.md").exists()
    assert not (dest / "docs" / "decisions.md").exists()
    # The empty sessions/ dir is dropped (no .gitkeep was provided)
    assert not (dest / "memory" / "sessions").exists()


def test_init_codex_scaffolds_adapter_without_moving_claude(tmp_path: Path):
    rc = _run_init(tmp_path, platform="codex")
    assert rc == 0

    assert (tmp_path / ".claude" / "skills" / "start" / "SKILL.md").is_file()
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8").startswith(
        "<!-- BEGIN C3 CODEX ADAPTER -->"
    )
    skill_text = (tmp_path / ".agents" / "skills" / "start" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "c3_ask_user_question" in skill_text
    assert "subagents and the generated custom agents" in skill_text
    assert "Get-Content -Encoding UTF8" in skill_text
    assert (tmp_path / ".codex" / "agents" / "developer.toml").is_file()
    config_text = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert 'args = ["-m", "c3.mcp_server"]' in config_text
    assert "PYTHONPATH" in config_text


def test_init_cursor_scaffolds_rule_and_mcp(tmp_path: Path):
    rc = _run_init(tmp_path, platform="cursor")
    assert rc == 0

    assert (tmp_path / ".claude").is_dir()
    rule = tmp_path / ".cursor" / "rules" / "c3-core.mdc"
    assert "AskUserQuestion" in rule.read_text(encoding="utf-8")
    mcp = tmp_path / ".cursor" / "mcp.json"
    mcp_text = mcp.read_text(encoding="utf-8")
    assert "c3.mcp_server" in mcp_text
    assert "PYTHONPATH" in mcp_text


def test_init_all_uses_existing_claude_when_adding_adapters(tmp_path: Path):
    _run_init(tmp_path)
    marker = tmp_path / ".claude" / "local.txt"
    marker.write_text("local", encoding="utf-8")

    rc = _run_init(tmp_path, platform="all")

    assert rc == 0
    assert marker.read_text(encoding="utf-8") == "local"
    assert (tmp_path / "AGENTS.md").is_file()
    assert (tmp_path / ".cursor" / "mcp.json").is_file()


def test_update_codex_dry_run_reports_adapter_diff(tmp_path: Path, capsys):
    _run_init(tmp_path)

    rc = _run_update(tmp_path, platform="codex", dry_run=True)

    assert rc == 0
    assert "adapter file(s) would change" in capsys.readouterr().out
    assert not (tmp_path / "AGENTS.md").exists()


def test_init_codex_refuses_unmanaged_existing_c3_mcp_table(tmp_path: Path, capsys):
    _run_init(tmp_path)
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text("[mcp_servers.c3]\ncommand = \"custom\"\n", encoding="utf-8")

    rc = _run_init(tmp_path, platform="codex")

    assert rc == 1
    assert "already defines [mcp_servers.c3]" in capsys.readouterr().err


@pytest.mark.skipif(
    os.sep != "\\",
    reason=(
        "config の PYTHONPATH バックスラッシュエスケープは Windows パスでのみ発生する"
        "（escape ロジック自体は tests/test_adapters.py で OS 非依存に検証済み）"
    ),
)
def test_update_codex_preserves_escaped_backslashes_in_managed_config(tmp_path: Path):
    _run_init(tmp_path, platform="codex")
    config = tmp_path / ".codex" / "config.toml"
    original = config.read_text(encoding="utf-8")
    assert "\\\\" in original

    rc = _run_update(tmp_path, platform="codex")

    assert rc == 0
    updated = config.read_text(encoding="utf-8")
    assert "\\\\" in updated
