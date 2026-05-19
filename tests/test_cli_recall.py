"""Tests for ``c3 recall`` (cli_recall).

These tests substitute a fake embedder for ``FastEmbedBackend`` so the
fastembed model is never downloaded. The real model is exercised in the
slow integration test in ``tests/test_embedding.py``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from c3 import cli_recall
from c3.embedding import Embedder


class _FakeEmbedder(Embedder):
    """Deterministic 3-D embedder for CLI tests.

    Returns a vector whose first component is the (truncated) length of
    the input. Cosine distance ordering is dominated by the first axis,
    which is enough to verify the search wiring without using a real
    model.
    """

    def __init__(self) -> None:
        self._dim = 3

    @property
    def model_name(self) -> str:
        return "fake-embedder"

    @property
    def dim(self) -> int:
        return self._dim

    def embed_query(self, text: str) -> list[float]:
        return self._encode(text)

    def embed_passages(self, texts) -> list[list[float]]:
        return [self._encode(t) for t in texts]

    def _encode(self, text: str) -> list[float]:
        import math

        # Spread inputs into a 3D unit sphere using simple hash buckets.
        if not text:
            return [1.0, 0.0, 0.0]
        h = hash(text)
        x = ((h >> 0) & 0xFF) / 255.0
        y = ((h >> 8) & 0xFF) / 255.0
        z = ((h >> 16) & 0xFF) / 255.0
        norm = math.sqrt(x * x + y * y + z * z) or 1.0
        return [x / norm, y / norm, z / norm]


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace FastEmbedBackend with the fake one above."""
    monkeypatch.setattr(cli_recall, "_build_embedder_or_report_error", lambda: _FakeEmbedder())


def _seed_repo(tmp_path: Path) -> Path:
    """Build a minimal repo with sample sources."""
    claude = tmp_path / ".claude"
    (claude / "memory" / "sessions").mkdir(parents=True)
    (claude / "agent-memory" / "code-reviewer").mkdir(parents=True)
    (claude / "reports" / "archive").mkdir(parents=True)
    (claude / "state").mkdir(parents=True)

    (claude / "memory" / "sessions" / "20260510.tmp").write_text(
        "## うまくいったアプローチ\n認証のリトライ実装\n\n## 残タスク\n- フォロー\n",
        encoding="utf-8",
    )
    (claude / "agent-memory" / "code-reviewer" / "lessons.md").write_text(
        "## CR-Q-001\n関数の責務を単一に\n",
        encoding="utf-8",
    )
    (claude / "reports" / "archive" / "report-001.md").write_text(
        "## High\nセッション固定化\n",
        encoding="utf-8",
    )
    (claude / "memory" / "patterns.json").write_text(
        json.dumps({"patterns": [{"id": "p1", "description": "ログ出力前にサニタイズ"}]}),
        encoding="utf-8",
    )
    return tmp_path


# ----- argparse wiring -----


def test_register_adds_recall_command() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    cli_recall.register(sub)
    args = parser.parse_args(["recall", "search", "テスト"])
    assert args.recall_command == "search"
    assert args.query == "テスト"
    assert args.top == 5
    assert args.source == "all"
    assert args.min_score == 0.3
    assert args.json is False


def test_register_rebuild_supports_force() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    cli_recall.register(sub)
    args = parser.parse_args(["recall", "rebuild", "--force"])
    assert args.recall_command == "rebuild"
    assert args.force is True


def test_register_stats_supports_json() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    cli_recall.register(sub)
    args = parser.parse_args(["recall", "stats", "--json"])
    assert args.recall_command == "stats"
    assert args.json is True


# ----- handle dispatch -----


def test_handle_search_without_claude_dir_returns_1(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fake_backend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``claude_root_for`` walks up the filesystem and may locate the user-global
    # ``.claude/`` directory (e.g. ``~/.claude/``). Force a miss for this test.
    monkeypatch.setattr(cli_recall, "_resolve_repo_root", lambda target=None: None)
    args = argparse.Namespace(
        recall_command="search",
        query="x",
        top=5,
        source="all",
        min_score=0.0,
        json=False,
        target=tmp_path,
    )
    rc = cli_recall.handle(args)
    assert rc == 1
    captured = capsys.readouterr()
    assert "no .claude/" in captured.err


def test_handle_search_without_index_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fake_backend
) -> None:
    repo = _seed_repo(tmp_path)
    args = argparse.Namespace(
        recall_command="search",
        query="認証",
        top=5,
        source="all",
        min_score=0.0,
        json=False,
        target=repo,
    )
    rc = cli_recall.handle(args)
    assert rc == 1
    captured = capsys.readouterr()
    assert "rebuild" in captured.err


def test_handle_rebuild_then_search_roundtrip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fake_backend
) -> None:
    repo = _seed_repo(tmp_path)
    rb = argparse.Namespace(
        recall_command="rebuild",
        force=False,
        source="all",
        target=repo,
    )
    rc = cli_recall.handle(rb)
    assert rc == 0
    assert (repo / ".claude" / "state" / "recall.hnsw").exists()
    assert (repo / ".claude" / "state" / "recall_meta.json").exists()
    capsys.readouterr()  # drain rebuild output

    search = argparse.Namespace(
        recall_command="search",
        query="認証",
        top=5,
        source="all",
        min_score=0.0,
        json=True,
        target=repo,
    )
    rc = cli_recall.handle(search)
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["query"] == "認証"
    assert isinstance(payload["hits"], list)


def test_handle_stats_returns_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fake_backend
) -> None:
    repo = _seed_repo(tmp_path)
    cli_recall.handle(
        argparse.Namespace(
            recall_command="rebuild",
            force=False,
            source="all",
            target=repo,
        )
    )
    capsys.readouterr()  # drain
    rc = cli_recall.handle(
        argparse.Namespace(
            recall_command="stats",
            json=True,
            target=repo,
        )
    )
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["total_chunks"] >= 1
    assert payload["model"] == "fake-embedder"
    assert "by_source" in payload


def test_handle_stats_missing_meta_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fake_backend
) -> None:
    repo = _seed_repo(tmp_path)
    rc = cli_recall.handle(
        argparse.Namespace(recall_command="stats", json=False, target=repo)
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "rebuild" in captured.err


def test_search_respects_source_filter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fake_backend
) -> None:
    repo = _seed_repo(tmp_path)
    cli_recall.handle(
        argparse.Namespace(
            recall_command="rebuild",
            force=False,
            source="all",
            target=repo,
        )
    )
    capsys.readouterr()
    cli_recall.handle(
        argparse.Namespace(
            recall_command="search",
            query="x",
            top=10,
            source="patterns",
            min_score=0.0,
            json=True,
            target=repo,
        )
    )
    payload = json.loads(capsys.readouterr().out)
    assert all(h["source_type"] == "pattern" for h in payload["hits"])


def test_search_with_min_score_can_return_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fake_backend
) -> None:
    repo = _seed_repo(tmp_path)
    cli_recall.handle(
        argparse.Namespace(
            recall_command="rebuild",
            force=False,
            source="all",
            target=repo,
        )
    )
    capsys.readouterr()
    cli_recall.handle(
        argparse.Namespace(
            recall_command="search",
            query="無関係",
            top=5,
            source="all",
            min_score=2.0,  # impossible threshold
            json=True,
            target=repo,
        )
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["hits"] == []


def test_rebuild_force_recreates_index(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fake_backend
) -> None:
    repo = _seed_repo(tmp_path)
    cli_recall.handle(
        argparse.Namespace(
            recall_command="rebuild",
            force=False,
            source="all",
            target=repo,
        )
    )
    rc = cli_recall.handle(
        argparse.Namespace(
            recall_command="rebuild",
            force=True,
            source="all",
            target=repo,
        )
    )
    assert rc == 0


def test_search_human_output_includes_score_and_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fake_backend
) -> None:
    repo = _seed_repo(tmp_path)
    cli_recall.handle(
        argparse.Namespace(
            recall_command="rebuild",
            force=False,
            source="all",
            target=repo,
        )
    )
    capsys.readouterr()
    cli_recall.handle(
        argparse.Namespace(
            recall_command="search",
            query="認証",
            top=5,
            source="all",
            min_score=0.0,
            json=False,
            target=repo,
        )
    )
    out = capsys.readouterr().out
    assert "score=" in out or "No matches" in out


def test_unknown_subcommand_returns_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = argparse.Namespace(recall_command="bogus")
    rc = cli_recall.handle(args)
    assert rc == 2
    assert "unknown subcommand" in capsys.readouterr().err


# ----- CR-H-03: rebuild returns 1 when no sources (regression guard) -----


def test_rebuild_returns_1_when_no_sources(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fake_backend,
) -> None:
    """rebuild must return exit code 1 and print to stderr when 0 chunks found.

    Regression guard for CR-H-03: an earlier revision returned 0 and wrote
    an empty index, masking misconfigured source paths.
    """
    # Create an empty .claude/ tree with no indexable files.
    claude = tmp_path / ".claude"
    (claude / "memory" / "sessions").mkdir(parents=True)
    (claude / "agent-memory").mkdir(parents=True)
    (claude / "reports" / "archive").mkdir(parents=True)
    (claude / "state").mkdir(parents=True)

    rb = argparse.Namespace(
        recall_command="rebuild",
        force=False,
        source="all",
        target=tmp_path,
    )
    rc = cli_recall.handle(rb)
    captured = capsys.readouterr()
    assert rc == 1, f"Expected exit code 1 when no sources, got {rc}"
    assert "no source files" in captured.err.lower(), (
        f"Expected 'no source files' in stderr, got: {captured.err!r}"
    )


# ----- CR-M-07: stats separator matches title length (regression guard) -----


def test_stats_separator_matches_title_length(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fake_backend,
) -> None:
    """The '===' separator in human stats output must match the title length.

    Regression guard for CR-M-07: an earlier revision hardcoded the
    separator to 26 '=' chars regardless of the actual title
    ``Recall Index Statistics`` (22 chars). The current implementation
    derives the length dynamically from ``_STATS_TITLE``.
    """
    repo = _seed_repo(tmp_path)
    cli_recall.handle(
        argparse.Namespace(
            recall_command="rebuild",
            force=False,
            source="all",
            target=repo,
        )
    )
    capsys.readouterr()  # drain rebuild output

    rc = cli_recall.handle(
        argparse.Namespace(
            recall_command="stats",
            json=False,
            target=repo,
        )
    )
    assert rc == 0
    out = capsys.readouterr().out
    lines = out.splitlines()

    # Find the title line and the separator line directly after it.
    title_line = None
    sep_line = None
    for i, line in enumerate(lines):
        # Strip ANSI codes for comparison.
        import re
        clean = re.sub(r"\x1b\[[0-9;]*m", "", line)
        if "Recall Index Statistics" in clean:
            title_line = "Recall Index Statistics"
            if i + 1 < len(lines):
                sep_candidate = re.sub(r"\x1b\[[0-9;]*m", "", lines[i + 1])
                if set(sep_candidate.strip()) == {"="}:
                    sep_line = sep_candidate.strip()
            break

    assert title_line is not None, "Title 'Recall Index Statistics' not found in stats output"
    assert sep_line is not None, "Separator '===' line not found after title"
    assert len(sep_line) == len(title_line), (
        f"Separator length {len(sep_line)} != title length {len(title_line)}. "
        f"Separator: {sep_line!r}, Title: {title_line!r}"
    )


# ----- CR-M-06: --force help clarifies full rebuild (regression guard) -----


def test_force_flag_help_clarifies_full_rebuild() -> None:
    """The --force flag help text must mention 'always processes all sources'.

    Regression guard for CR-M-06 (Group A docs): an earlier revision used a
    vague help string. The current help spells out that rebuild always
    processes all sources because incremental rebuild is not yet supported.
    """
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    cli_recall.register(sub)

    # Find the rebuild subparser's --force action.
    rebuild_parser = None
    for action in parser._subparsers._group_actions:
        for name, sp in action.choices.items():
            if name == "recall":
                for sub_action in sp._subparsers._group_actions:
                    for sub_name, sub_sp in sub_action.choices.items():
                        if sub_name == "rebuild":
                            rebuild_parser = sub_sp
                            break

    assert rebuild_parser is not None, "rebuild subparser not found"

    force_action = None
    for action in rebuild_parser._actions:
        if "--force" in (action.option_strings or []):
            force_action = action
            break

    assert force_action is not None, "--force action not found"
    help_text = (force_action.help or "").lower()
    assert "always processes all sources" in help_text or "force overwrite" in help_text, (
        f"--force help should clarify 'always processes all sources' or 'force overwrite'. "
        f"Got: {force_action.help!r}"
    )
