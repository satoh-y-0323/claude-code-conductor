"""Tests for ``c3 list-agents`` / ``list-skills`` / ``list-commands`` (cli_list)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from c3 import cli_list


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_list(kind: str, target: Path) -> int:
    return cli_list.handle(argparse.Namespace(kind=kind, target=target))


# ---------------------------------------------------------------------------
# _summary – OSError handling (regression guard)
# ---------------------------------------------------------------------------


def test_summary_returns_unreadable_on_oserror(tmp_path: Path):
    """_summary must return '(unreadable)' when read_text raises OSError.

    Regression guard for the OSError handler (originally added before the
    fix). Verifies that an OSError (e.g. permission denied) does not
    propagate and is instead silently reported as '(unreadable)'.
    """
    fake_md = tmp_path / "agent.md"
    fake_md.write_text("# Some Agent\n", encoding="utf-8")

    with patch.object(Path, "read_text", side_effect=OSError("Permission denied")):
        result = cli_list._summary(fake_md)

    assert result == "(unreadable)", (
        f"Expected '(unreadable)' but got {result!r}. "
        "_summary must wrap the OSError silently."
    )


# ---------------------------------------------------------------------------
# _summary – normal happy-path cases (should pass even before the fix)
# ---------------------------------------------------------------------------


def test_summary_returns_frontmatter_description(tmp_path: Path):
    """_summary extracts the description field from YAML frontmatter."""
    md = tmp_path / "my_agent.md"
    md.write_text(
        '---\ndescription: "My custom agent"\n---\n# My Agent\n',
        encoding="utf-8",
    )
    assert cli_list._summary(md) == "My custom agent"


def test_summary_returns_h1_when_no_frontmatter(tmp_path: Path):
    """_summary falls back to the first H1 heading when there is no frontmatter."""
    md = tmp_path / "my_skill.md"
    md.write_text("# My Skill\nSome description here.\n", encoding="utf-8")
    assert cli_list._summary(md) == "My Skill"


def test_summary_returns_empty_string_when_no_frontmatter_and_no_h1(tmp_path: Path):
    """_summary returns an empty string when neither frontmatter nor H1 is present."""
    md = tmp_path / "bare.md"
    md.write_text("Just some plain text.\n", encoding="utf-8")
    assert cli_list._summary(md) == ""


# ---------------------------------------------------------------------------
# handle – integration-level smoke tests
# ---------------------------------------------------------------------------


def test_handle_returns_1_when_no_claude_dir(tmp_path: Path, capsys):
    """handle returns 1 and prints an error when no .claude/ dir is found."""
    rc = _run_list("agents", tmp_path)
    assert rc == 1
    captured = capsys.readouterr()
    assert "no .claude/" in captured.err


def test_handle_returns_1_when_kind_dir_missing(tmp_path: Path, capsys):
    """handle returns 1 when .claude/ exists but the kind sub-directory does not."""
    (tmp_path / ".claude").mkdir()
    rc = _run_list("agents", tmp_path)
    assert rc == 1
    captured = capsys.readouterr()
    assert "no .claude/agents/" in captured.err


def test_handle_returns_0_and_prints_no_agents_when_dir_empty(tmp_path: Path, capsys):
    """handle returns 0 and prints a placeholder when the kind directory is empty."""
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    rc = _run_list("agents", tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    assert "no agents found" in captured.out


def test_handle_lists_agents(tmp_path: Path, capsys):
    """handle prints one line per .md file found in the kind directory."""
    agents_dir = tmp_path / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "alpha.md").write_text("# Alpha Agent\n", encoding="utf-8")
    (agents_dir / "beta.md").write_text("# Beta Agent\n", encoding="utf-8")

    rc = _run_list("agents", tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    assert "alpha" in captured.out
    assert "beta" in captured.out
