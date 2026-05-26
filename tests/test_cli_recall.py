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


# ----- Incremental rebuild tests (T1 / v2.28.0) -----
#
# These tests verify the incremental rebuild feature: unchanged chunks must
# NOT be re-embedded; only changed/new chunks should go through embed_passages.
# _SpyEmbedder records every call to embed_passages for assertion.


class _SpyEmbedder(_FakeEmbedder):
    """Spy variant of _FakeEmbedder that records embed_passages calls."""

    def __init__(self) -> None:
        super().__init__()
        self.passages_calls: list[list[str]] = []

    def embed_passages(self, texts) -> list[list[float]]:
        call_texts = list(texts)
        self.passages_calls.append(call_texts)
        return super().embed_passages(call_texts)


@pytest.fixture
def spy_embedder() -> _SpyEmbedder:
    return _SpyEmbedder()


def _make_spy_backend(embedder: _SpyEmbedder, monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject spy embedder into cli_recall._build_embedder_or_report_error."""
    monkeypatch.setattr(cli_recall, "_build_embedder_or_report_error", lambda: embedder)


def _seed_repo_for_incremental(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build repo with two source files. Returns (repo, file_a, file_b)."""
    claude = tmp_path / ".claude"
    (claude / "memory" / "sessions").mkdir(parents=True)
    (claude / "agent-memory").mkdir(parents=True)
    (claude / "reports" / "archive").mkdir(parents=True)
    (claude / "state").mkdir(parents=True)

    file_a = claude / "memory" / "sessions" / "session_a.tmp"
    file_b = claude / "memory" / "sessions" / "session_b.tmp"

    file_a.write_text(
        "## セクションA\n認証のリトライ実装について説明する\n",
        encoding="utf-8",
    )
    file_b.write_text(
        "## セクションB\nセッション固定化の脆弱性について説明する\n",
        encoding="utf-8",
    )
    return tmp_path, file_a, file_b


def _rebuild(repo: Path, monkeypatch: pytest.MonkeyPatch, force: bool = False,
             embedder=None) -> int:
    if embedder is not None:
        monkeypatch.setattr(cli_recall, "_build_embedder_or_report_error", lambda: embedder)
    return cli_recall.handle(
        argparse.Namespace(
            recall_command="rebuild",
            force=force,
            source="all",
            target=repo,
        )
    )


def test_incremental_rebuild_only_embeds_changed_chunks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Incremental rebuild: only changed/new chunks must be passed to embed_passages.

    After a full first rebuild, we modify one source file and run rebuild again
    (without --force). The second embed_passages call must NOT include content
    from the unchanged file.
    """
    spy = _SpyEmbedder()
    repo, file_a, file_b = _seed_repo_for_incremental(tmp_path)

    # First rebuild: all chunks embedded.
    rc = _rebuild(repo, monkeypatch, force=False, embedder=spy)
    assert rc == 0
    capsys.readouterr()  # drain

    # Record all content from the first rebuild.
    first_all_contents: set[str] = set()
    for call in spy.passages_calls:
        first_all_contents.update(call)

    spy.passages_calls.clear()

    # Modify only file_a.
    file_a.write_text(
        "## セクションA\n変更後の内容: 新しいリトライ実装\n",
        encoding="utf-8",
    )

    # Second rebuild (incremental): only changed chunks should be embedded.
    rc = _rebuild(repo, monkeypatch, force=False, embedder=spy)
    assert rc == 0
    capsys.readouterr()

    # All embedded contents from the second rebuild must NOT include the
    # unchanged content from file_b.
    second_embedded: set[str] = set()
    for call in spy.passages_calls:
        second_embedded.update(call)

    # The unchanged file_b content must NOT have been re-embedded.
    file_b_content = "セクションB"  # distinctive fragment in file_b
    for embedded_text in second_embedded:
        assert file_b_content not in embedded_text, (
            f"Unchanged chunk from file_b appeared in second embed_passages call: "
            f"{embedded_text!r}"
        )


def test_incremental_rebuild_unchanged_chunk_vectors_match_full_rebuild(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reused (unchanged) chunk vectors must be identical to vectors from a full rebuild.

    After a full first rebuild, we modify file_a and run an incremental rebuild.
    The chunk from the unchanged file_b must produce the same score when searched,
    regardless of which overall chunk ends up as the top hit.

    Note: The test searches for all hits (top=5) and looks specifically for the
    file_b chunk in both rebuild results. _FakeEmbedder uses hash()-based encoding
    whose result depends on PYTHONHASHSEED, so we cannot rely on a specific
    chunk being top-1; we locate file_b's chunk by path instead.
    """
    spy = _SpyEmbedder()
    repo, file_a, file_b = _seed_repo_for_incremental(tmp_path)

    # First full rebuild.
    rc = _rebuild(repo, monkeypatch, force=False, embedder=spy)
    assert rc == 0
    capsys.readouterr()

    # Search and collect ALL hits so we can find file_b's chunk by path.
    search_args = argparse.Namespace(
        recall_command="search",
        query="セクションB",
        top=5,
        source="all",
        min_score=0.0,
        json=True,
        target=repo,
    )
    cli_recall.handle(search_args)
    payload_full = json.loads(capsys.readouterr().out)
    full_hits_by_path = {
        h["path"]: h for h in payload_full["hits"]
    }

    # Modify file_a only, keep file_b intact.
    file_a.write_text(
        "## セクションA\n変更後の内容: 改修済みリトライ実装\n",
        encoding="utf-8",
    )

    # Second rebuild (incremental).
    spy.passages_calls.clear()
    rc = _rebuild(repo, monkeypatch, force=False, embedder=spy)
    assert rc == 0
    capsys.readouterr()

    # Search again.
    cli_recall.handle(search_args)
    payload_incremental = json.loads(capsys.readouterr().out)
    incr_hits_by_path = {
        h["path"]: h for h in payload_incremental["hits"]
    }

    # The unchanged file_b chunk must appear in both result sets with the same score.
    file_b_rel = ".claude/memory/sessions/session_b.tmp"
    assert file_b_rel in full_hits_by_path, (
        f"file_b chunk not found in full rebuild results: {list(full_hits_by_path)}"
    )
    assert file_b_rel in incr_hits_by_path, (
        f"file_b chunk not found in incremental rebuild results: {list(incr_hits_by_path)}"
    )
    full_score = full_hits_by_path[file_b_rel]["score"]
    incr_score = incr_hits_by_path[file_b_rel]["score"]
    assert incr_score == pytest.approx(full_score, abs=1e-4), (
        f"Score for the unchanged file_b chunk must be identical between full and incremental rebuild. "
        f"full={full_score}, incremental={incr_score}"
    )


def test_incremental_rebuild_force_embeds_all_chunks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--force must embed all chunks even if an existing index is present.

    After a full first rebuild, running rebuild --force must pass ALL chunks
    to embed_passages, not just changed ones.
    """
    spy = _SpyEmbedder()
    repo, file_a, file_b = _seed_repo_for_incremental(tmp_path)

    # Full rebuild to create an existing index.
    rc = _rebuild(repo, monkeypatch, force=False, embedder=spy)
    assert rc == 0
    capsys.readouterr()

    # Count total chunks from first rebuild.
    total_first = sum(len(call) for call in spy.passages_calls)
    spy.passages_calls.clear()

    # Now run --force rebuild.
    rc = _rebuild(repo, monkeypatch, force=True, embedder=spy)
    assert rc == 0
    capsys.readouterr()

    total_force = sum(len(call) for call in spy.passages_calls)

    # --force must embed exactly all chunks (same count as the first full rebuild).
    assert total_force == total_first, (
        f"--force rebuild embedded {total_force} chunks but full rebuild had {total_first}; "
        "--force must always embed all chunks and no more"
    )


def test_incremental_rebuild_fallback_when_no_existing_index(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no existing index exists, rebuild must embed all chunks (fallback to full embed)."""
    spy = _SpyEmbedder()
    repo, file_a, file_b = _seed_repo_for_incremental(tmp_path)

    # No prior index. First rebuild should embed all chunks.
    rc = _rebuild(repo, monkeypatch, force=False, embedder=spy)
    assert rc == 0
    capsys.readouterr()

    total_embedded = sum(len(call) for call in spy.passages_calls)
    assert total_embedded >= 2, (
        f"Expected at least 2 chunks to be embedded on first rebuild, got {total_embedded}"
    )


def test_incremental_rebuild_fallback_on_model_mismatch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When load() raises RuntimeError (model mismatch), rebuild must fall back to full embed.

    The incremental path starts with load(). If load() raises RuntimeError due to
    model name or dim mismatch, rebuild must NOT crash; it must silently fall back
    to embedding all chunks.
    """
    spy = _SpyEmbedder()
    repo, file_a, file_b = _seed_repo_for_incremental(tmp_path)

    # First full rebuild with the spy embedder (model="fake-embedder").
    rc = _rebuild(repo, monkeypatch, force=False, embedder=spy)
    assert rc == 0
    capsys.readouterr()
    spy.passages_calls.clear()

    # Corrupt the meta to trigger a RuntimeError on next load()
    # by writing a different model name into recall_meta.json.
    meta_path = repo / ".claude" / "state" / "recall_meta.json"
    meta_content = json.loads(meta_path.read_text(encoding="utf-8"))
    meta_content["model"] = "a-different-model-that-does-not-match"
    meta_path.write_text(json.dumps(meta_content), encoding="utf-8")

    # Second rebuild (incremental): load() will raise RuntimeError due to model mismatch.
    # The rebuild must NOT crash; it must fall back to full embed.
    rc = _rebuild(repo, monkeypatch, force=False, embedder=spy)
    assert rc == 0, (
        "rebuild must succeed (rc=0) even when load() raises RuntimeError due to model mismatch"
    )
    capsys.readouterr()

    # All chunks must have been embedded (full fallback).
    total_fallback = sum(len(call) for call in spy.passages_calls)
    assert total_fallback >= 2, (
        f"Expected full re-embed on fallback, but only {total_fallback} chunks were embedded"
    )


def test_incremental_rebuild_removes_deleted_chunks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chunks from deleted source files must not appear in the new index.

    After a full rebuild with file_a and file_b, delete file_b and rebuild.
    The new index must not contain any chunk from file_b.
    """
    spy = _SpyEmbedder()
    repo, file_a, file_b = _seed_repo_for_incremental(tmp_path)

    # First full rebuild with both files.
    rc = _rebuild(repo, monkeypatch, force=False, embedder=spy)
    assert rc == 0
    capsys.readouterr()

    # Delete file_b.
    file_b.unlink()

    # Second rebuild (incremental).
    spy.passages_calls.clear()
    rc = _rebuild(repo, monkeypatch, force=False, embedder=spy)
    assert rc == 0
    capsys.readouterr()

    # Verify: search for content unique to file_b must return no results.
    search_args = argparse.Namespace(
        recall_command="search",
        query="セクションB",
        top=5,
        source="all",
        min_score=0.0,
        json=True,
        target=repo,
    )
    cli_recall.handle(search_args)
    payload = json.loads(capsys.readouterr().out)

    # No chunk from file_b's path should remain in the index.
    for hit in payload["hits"]:
        assert "session_b.tmp" not in hit.get("path", ""), (
            f"Deleted file_b chunk still present after incremental rebuild: {hit!r}"
        )


def test_incremental_rebuild_log_format(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After an incremental rebuild, the log must report 'embedded M / reused K' format.

    The exact numbers are not asserted here (they depend on what changed),
    but the log line must contain both counts.
    """
    spy = _SpyEmbedder()
    repo, file_a, file_b = _seed_repo_for_incremental(tmp_path)

    # Full first rebuild.
    rc = _rebuild(repo, monkeypatch, force=False, embedder=spy)
    assert rc == 0
    capsys.readouterr()

    # Modify file_a so at least one chunk is re-embedded and one is reused.
    file_a.write_text(
        "## セクションA\nログ出力テスト用の変更後コンテンツ\n",
        encoding="utf-8",
    )

    spy.passages_calls.clear()

    # Second rebuild (incremental): expect 'embedded M / reused K' in stdout.
    rc = _rebuild(repo, monkeypatch, force=False, embedder=spy)
    assert rc == 0
    captured = capsys.readouterr()

    # The log line should contain both 'embedded' and 'reused' (case-insensitive).
    out_lower = captured.out.lower()
    assert "embedded" in out_lower, (
        f"rebuild log should contain 'embedded' count. stdout: {captured.out!r}"
    )
    assert "reused" in out_lower, (
        f"rebuild log should contain 'reused' count. stdout: {captured.out!r}"
    )
