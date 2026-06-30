"""Tests for ``c3 init`` and ``c3 update`` against a temporary project root."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from c3 import cli_init, cli_update


def _run_init(
    target: Path,
    *,
    force: bool = False,
    platform: str = "claude",
    git: bool = False,
    no_git: bool = False,
) -> int:
    return cli_init.handle(
        argparse.Namespace(target=target, force=force, platform=platform, git=git, no_git=no_git)
    )


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


# ---------------------------------------------------------------------------
# git consent flow — new cases for c3 init --git / --no-git
# ---------------------------------------------------------------------------


def test_init_inside_repo_is_silent(tmp_path: Path, monkeypatch, capsys) -> None:
    """INSIDE_REPO: git_init is never called and no git-specific message is emitted."""
    from c3.gitutil import GitStatus

    def never_called(_root):
        raise AssertionError("git_init must not be called when already inside a repo")

    monkeypatch.setattr("c3.gitutil.detect_git_status", lambda _root: GitStatus.INSIDE_REPO)
    monkeypatch.setattr("c3.gitutil.git_init", never_called)

    rc = _run_init(tmp_path)

    assert rc == 0
    out = capsys.readouterr().out
    assert "git init" not in out
    assert "worktree" not in out


def test_init_not_a_repo_with_git_flag(tmp_path: Path, monkeypatch, capsys) -> None:
    """--git flag on a non-git directory: git_init is called and a success message is printed."""
    from c3.gitutil import GitStatus

    git_init_calls: list = []

    def spy_git_init(root):
        git_init_calls.append(root)
        return True

    monkeypatch.setattr("c3.gitutil.detect_git_status", lambda _root: GitStatus.NOT_A_REPO)
    monkeypatch.setattr("c3.gitutil.git_init", spy_git_init)

    rc = _run_init(tmp_path, git=True)

    assert rc == 0
    assert len(git_init_calls) == 1
    out = capsys.readouterr().out
    assert "git init を実行しました" in out


def test_init_not_a_repo_with_no_git_flag(tmp_path: Path, monkeypatch, capsys) -> None:
    """--no-git flag: git_init is not called and a guidance message is printed."""
    from c3.gitutil import GitStatus

    def never_called(_root):
        raise AssertionError("git_init must not be called when --no-git is passed")

    monkeypatch.setattr("c3.gitutil.detect_git_status", lambda _root: GitStatus.NOT_A_REPO)
    monkeypatch.setattr("c3.gitutil.git_init", never_called)

    rc = _run_init(tmp_path, no_git=True)

    assert rc == 0
    out = capsys.readouterr().out
    assert "worktree" in out


def test_init_non_tty_no_flags(tmp_path: Path, monkeypatch, capsys) -> None:
    """Non-TTY, no flags: input() is never called, git_init is skipped, a warning is shown."""
    from c3.gitutil import GitStatus

    def never_called_git_init(_root):
        raise AssertionError("git_init must not be called in non-TTY no-flag mode")

    def never_called_input(_prompt: str) -> str:
        raise AssertionError("input() must not be called in non-TTY mode")

    monkeypatch.setattr("c3.gitutil.detect_git_status", lambda _root: GitStatus.NOT_A_REPO)
    monkeypatch.setattr("c3.gitutil.git_init", never_called_git_init)
    monkeypatch.setattr("sys.stdin", type("_NonTTY", (), {"isatty": staticmethod(lambda: False)})())
    monkeypatch.setattr("builtins.input", never_called_input)

    rc = _run_init(tmp_path)

    assert rc == 0
    out = capsys.readouterr().out
    assert "worktree" in out


def test_init_tty_input_yes_calls_git_init(tmp_path: Path, monkeypatch) -> None:
    """TTY, no flags, input='y': git_init is called and exit code is 0."""
    from c3.gitutil import GitStatus

    git_init_calls: list = []

    def spy_git_init(root):
        git_init_calls.append(root)
        return True

    monkeypatch.setattr("c3.gitutil.detect_git_status", lambda _root: GitStatus.NOT_A_REPO)
    monkeypatch.setattr("c3.gitutil.git_init", spy_git_init)
    monkeypatch.setattr("sys.stdin", type("_TTY", (), {"isatty": staticmethod(lambda: True)})())
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    rc = _run_init(tmp_path)

    assert rc == 0
    assert len(git_init_calls) == 1


def test_init_tty_input_no_skips_git_init(tmp_path: Path, monkeypatch, capsys) -> None:
    """TTY, no flags, input='n': git_init is not called and a guidance message is shown."""
    from c3.gitutil import GitStatus

    def never_called(_root):
        raise AssertionError("git_init must not be called when user answers 'n'")

    monkeypatch.setattr("c3.gitutil.detect_git_status", lambda _root: GitStatus.NOT_A_REPO)
    monkeypatch.setattr("c3.gitutil.git_init", never_called)
    monkeypatch.setattr("sys.stdin", type("_TTY", (), {"isatty": staticmethod(lambda: True)})())
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    rc = _run_init(tmp_path)

    assert rc == 0
    out = capsys.readouterr().out
    assert "worktree" in out


def test_init_git_missing_warns_no_git_init(tmp_path: Path, monkeypatch, capsys) -> None:
    """GIT_MISSING: a warning mentioning git is printed and git_init is not called."""
    from c3.gitutil import GitStatus

    def never_called(_root):
        raise AssertionError("git_init must not be called when git is absent")

    monkeypatch.setattr("c3.gitutil.detect_git_status", lambda _root: GitStatus.GIT_MISSING)
    monkeypatch.setattr("c3.gitutil.git_init", never_called)

    rc = _run_init(tmp_path)

    assert rc == 0
    out = capsys.readouterr().out
    assert "git" in out.lower()


def test_init_mutual_exclusive_git_flags() -> None:
    """--git and --no-git are mutually exclusive; passing both causes argparse exit 2."""
    main_parser = argparse.ArgumentParser()
    subparsers = main_parser.add_subparsers()
    cli_init.register(subparsers)
    with pytest.raises(SystemExit) as exc_info:
        main_parser.parse_args(["init", "--git", "--no-git"])
    assert exc_info.value.code == 2


def test_init_git_init_failure_does_not_affect_exit_code(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """git_init returning False (failure) does not change c3 init's exit code."""
    from c3.gitutil import GitStatus

    monkeypatch.setattr("c3.gitutil.detect_git_status", lambda _root: GitStatus.NOT_A_REPO)
    monkeypatch.setattr("c3.gitutil.git_init", lambda _root: False)

    rc = _run_init(tmp_path, git=True)

    assert rc == 0
    out = capsys.readouterr().out
    assert "git init" in out


# ---------------------------------------------------------------------------
# L-01: sys.stdin is None ガード（CR-E-001）
# ---------------------------------------------------------------------------


def test_init_stdin_none_treated_as_non_tty(tmp_path: Path, monkeypatch, capsys) -> None:
    """sys.stdin=None のとき非 TTY として扱い、AttributeError を起こさずに rc=0 を返す（L-01）。

    git_init と input() は呼ばれず、worktree 誘導メッセージのみ stdout に出力する。
    _maybe_init_git は例外を外に漏らさない設計であるため、None ガードが必須。
    """
    from c3.gitutil import GitStatus

    def never_called_git_init(_root):
        raise AssertionError("git_init must not be called when sys.stdin is None")

    def never_called_input(_prompt: str) -> str:
        raise AssertionError("input() must not be called when sys.stdin is None")

    monkeypatch.setattr("c3.gitutil.detect_git_status", lambda _root: GitStatus.NOT_A_REPO)
    monkeypatch.setattr("c3.gitutil.git_init", never_called_git_init)
    monkeypatch.setattr("sys.stdin", None)
    monkeypatch.setattr("builtins.input", never_called_input)

    rc = _run_init(tmp_path)

    assert rc == 0
    out = capsys.readouterr().out
    assert "worktree" in out


# ---------------------------------------------------------------------------
# M-01: _input_fn 注入ポイント経由のテスト（CR-M-003）
# ---------------------------------------------------------------------------


def test_maybe_init_git_input_fn_yes_calls_git_init(tmp_path: Path, monkeypatch) -> None:
    """_input_fn=lambda _: "y" を注入すると git_init が 1 回呼ばれる（M-01）。

    _maybe_init_git に _input_fn キーワード引数（テスト注入ポイント）が追加されたとき、
    "y" を返す関数を渡すと git_init が実行されることを検証する。
    builtins.input の monkeypatch に依存せず、注入経由で挙動を制御できることを確認する。
    """
    from c3.gitutil import GitStatus
    from c3.cli_init import _maybe_init_git

    git_init_calls: list = []

    def spy_git_init(root):
        git_init_calls.append(root)
        return True

    monkeypatch.setattr("c3.gitutil.detect_git_status", lambda _root: GitStatus.NOT_A_REPO)
    monkeypatch.setattr("c3.gitutil.git_init", spy_git_init)
    monkeypatch.setattr(
        "sys.stdin",
        type("_TTY", (), {"isatty": staticmethod(lambda: True)})(),
    )

    _maybe_init_git(tmp_path, git=False, no_git=False, _input_fn=lambda _: "y")

    assert len(git_init_calls) == 1


def test_maybe_init_git_input_fn_no_skips_git_init(tmp_path: Path, monkeypatch, capsys) -> None:
    """_input_fn=lambda _: "n" を注入すると git_init がスキップされ誘導メッセージが出る（M-01）。

    _maybe_init_git に _input_fn キーワード引数（テスト注入ポイント）が追加されたとき、
    "n" を返す関数を渡すと git_init が呼ばれず worktree 誘導メッセージのみ出力されることを検証する。
    """
    from c3.gitutil import GitStatus
    from c3.cli_init import _maybe_init_git

    def never_called(_root):
        raise AssertionError("git_init must not be called when user answers 'n'")

    monkeypatch.setattr("c3.gitutil.detect_git_status", lambda _root: GitStatus.NOT_A_REPO)
    monkeypatch.setattr("c3.gitutil.git_init", never_called)
    monkeypatch.setattr(
        "sys.stdin",
        type("_TTY", (), {"isatty": staticmethod(lambda: True)})(),
    )

    _maybe_init_git(tmp_path, git=False, no_git=False, _input_fn=lambda _: "n")

    out = capsys.readouterr().out
    assert "worktree" in out
