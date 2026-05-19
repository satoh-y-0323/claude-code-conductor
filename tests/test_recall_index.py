"""Tests for src/c3/recall_index.py.

The HNSW-backed tests require the ``hnswlib`` package (shipped via the
``chroma-hnswlib`` dependency). They run with synthetic float vectors so
no model weights are downloaded and they are fast enough to stay in the
default suite.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Sequence

import pytest

from c3.recall_chunker import chunk_markdown
from c3.recall_index import (
    ChunkRecord,
    IndexMeta,
    RecallIndex,
    SourceChunk,
    collect_sources,
    default_index_paths,
    is_stale,
    snippet_of,
    warn_if_stale,
)


def _unit(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _record(source_type: str = "session", chunk_id: str = "## A#0") -> ChunkRecord:
    return ChunkRecord(
        source_type=source_type,
        path=".claude/memory/sessions/20260519.tmp",
        chunk_id=chunk_id,
        snippet="sample snippet",
        mtime=1_700_000_000.0,
        source_hash="",
    )


@pytest.fixture
def index(tmp_path: Path) -> RecallIndex:
    return RecallIndex(
        index_path=tmp_path / "recall.hnsw",
        meta_path=tmp_path / "recall_meta.json",
        model_name="test-model",
        dim=3,
    )


# ----- snippet helper -----


def test_snippet_truncates_long_text() -> None:
    assert snippet_of("a" * 1000, max_chars=10).startswith("a" * 10)
    assert snippet_of("a" * 1000, max_chars=10).endswith("...")


def test_snippet_short_text_unchanged() -> None:
    assert snippet_of("hi") == "hi"


# ----- IndexMeta round-trip -----


def test_index_meta_roundtrip() -> None:
    meta = IndexMeta.empty(model="m", dim=3)
    meta.next_id = 2
    meta.chunks["0"] = _record(chunk_id="c0")
    meta.chunks["1"] = _record(chunk_id="c1")
    payload = meta.to_dict()
    restored = IndexMeta.from_dict(payload)
    assert restored.model == "m"
    assert restored.dim == 3
    assert restored.next_id == 2
    assert set(restored.chunks) == {"0", "1"}
    assert restored.chunks["0"].chunk_id == "c0"


# ----- RecallIndex core -----


def test_build_assigns_sequential_ids(index: RecallIndex) -> None:
    items = [
        (_record(chunk_id="c0"), _unit([1.0, 0.0, 0.0])),
        (_record(chunk_id="c1"), _unit([0.0, 1.0, 0.0])),
    ]
    index.build(items)
    assert index.chunk_count() == 2
    assert set(index.meta.chunks) == {"0", "1"}
    assert index.meta.next_id == 2


def test_search_returns_nearest_first(index: RecallIndex) -> None:
    a = _unit([1.0, 0.0, 0.0])
    b = _unit([0.0, 1.0, 0.0])
    c = _unit([0.0, 0.0, 1.0])
    index.build(
        [
            (_record(chunk_id="A"), a),
            (_record(chunk_id="B"), b),
            (_record(chunk_id="C"), c),
        ]
    )
    results = index.search(a, top_k=2)
    assert results, "expected at least one search hit"
    top_id, top_dist, top_record = results[0]
    assert top_record.chunk_id == "A"
    assert top_dist == pytest.approx(0.0, abs=1e-5)


def test_search_empty_index_returns_empty(index: RecallIndex) -> None:
    index.build([])
    assert index.search([1.0, 0.0, 0.0]) == []


def test_search_validates_query_dim(index: RecallIndex) -> None:
    index.build([(_record(), _unit([1.0, 0.0, 0.0]))])
    with pytest.raises(ValueError):
        index.search([1.0, 0.0])


def test_build_rejects_wrong_dim(index: RecallIndex) -> None:
    with pytest.raises(ValueError):
        index.build([(_record(), [1.0, 2.0])])


def test_save_and_load_roundtrip(index: RecallIndex, tmp_path: Path) -> None:
    items = [
        (_record(chunk_id="X"), _unit([1.0, 0.0, 0.0])),
        (_record(chunk_id="Y"), _unit([0.0, 1.0, 0.0])),
    ]
    index.build(items)
    index.save()

    fresh = RecallIndex(
        index_path=index.index_path,
        meta_path=index.meta_path,
        model_name="test-model",
        dim=3,
    )
    assert fresh.load() is True
    assert fresh.chunk_count() == 2
    results = fresh.search(_unit([1.0, 0.05, 0.0]), top_k=1)
    assert results[0][2].chunk_id == "X"


def test_load_returns_false_when_missing(tmp_path: Path) -> None:
    index = RecallIndex(
        index_path=tmp_path / "noindex.hnsw",
        meta_path=tmp_path / "nometa.json",
        model_name="m",
        dim=3,
    )
    assert index.load() is False


def test_save_keeps_previous_as_bak(tmp_path: Path) -> None:
    index = RecallIndex(
        index_path=tmp_path / "recall.hnsw",
        meta_path=tmp_path / "recall_meta.json",
        model_name="m",
        dim=3,
    )
    index.build([(_record(), _unit([1.0, 0.0, 0.0]))])
    index.save()
    index.build([(_record(), _unit([0.0, 1.0, 0.0]))])
    index.save()
    bak = index.index_path.with_suffix(index.index_path.suffix + ".bak")
    assert bak.exists(), "previous index should be retained as .bak"


def test_load_detects_dim_mismatch(tmp_path: Path) -> None:
    index = RecallIndex(
        index_path=tmp_path / "recall.hnsw",
        meta_path=tmp_path / "recall_meta.json",
        model_name="m",
        dim=3,
    )
    index.build([(_record(), _unit([1.0, 0.0, 0.0]))])
    index.save()

    bad = RecallIndex(
        index_path=index.index_path,
        meta_path=index.meta_path,
        model_name="m",
        dim=4,
    )
    with pytest.raises(RuntimeError, match="dim"):
        bad.load()


def test_load_detects_model_mismatch(tmp_path: Path) -> None:
    index = RecallIndex(
        index_path=tmp_path / "recall.hnsw",
        meta_path=tmp_path / "recall_meta.json",
        model_name="orig",
        dim=3,
    )
    index.build([(_record(), _unit([1.0, 0.0, 0.0]))])
    index.save()

    bad = RecallIndex(
        index_path=index.index_path,
        meta_path=index.meta_path,
        model_name="other",
        dim=3,
    )
    with pytest.raises(RuntimeError, match="model"):
        bad.load()


def test_stats_counts_by_source(index: RecallIndex) -> None:
    items = [
        (_record(source_type="session", chunk_id="s0"), _unit([1.0, 0.0, 0.0])),
        (_record(source_type="session", chunk_id="s1"), _unit([0.0, 1.0, 0.0])),
        (_record(source_type="report", chunk_id="r0"), _unit([0.0, 0.0, 1.0])),
    ]
    index.build(items)
    stats = index.stats()
    assert stats["total_chunks"] == 3
    assert stats["by_source"]["session"] == 2
    assert stats["by_source"]["report"] == 1
    assert stats["by_source"]["pattern"] == 0
    assert stats["model"] == "test-model"
    assert stats["dim"] == 3


def test_save_before_build_raises(index: RecallIndex) -> None:
    with pytest.raises(RuntimeError):
        index.save()


# ----- source collection -----


def _make_repo(tmp_path: Path) -> Path:
    """Build a minimal .claude/ layout for collect_sources()."""
    claude = tmp_path / ".claude"
    (claude / "memory" / "sessions").mkdir(parents=True)
    (claude / "agent-memory" / "code-reviewer").mkdir(parents=True)
    (claude / "reports" / "archive").mkdir(parents=True)
    (claude / "memory").mkdir(exist_ok=True)
    return tmp_path


def test_collect_sources_yields_session_chunks(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    session = repo / ".claude" / "memory" / "sessions" / "20260519.tmp"
    session.write_text(
        "## うまくいったアプローチ\n認証 OK\n\n## 残タスク\n- foo\n",
        encoding="utf-8",
    )
    chunks = list(collect_sources(repo))
    sessions = [c for c in chunks if c.source_type == "session"]
    assert len(sessions) == 2
    assert all(c.path.endswith("20260519.tmp") for c in sessions)


def test_collect_sources_yields_agent_memory_chunks(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    note = repo / ".claude" / "agent-memory" / "code-reviewer" / "lessons.md"
    note.write_text("## CR-Q-001\n説明\n", encoding="utf-8")
    chunks = list(collect_sources(repo))
    agent_chunks = [c for c in chunks if c.source_type == "agent-memory"]
    assert len(agent_chunks) == 1
    assert agent_chunks[0].path.endswith("lessons.md")


def test_collect_sources_yields_report_chunks(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = repo / ".claude" / "reports" / "archive" / "report-001.md"
    report.write_text("## Summary\n本文\n", encoding="utf-8")
    chunks = list(collect_sources(repo))
    reports = [c for c in chunks if c.source_type == "report"]
    assert len(reports) == 1


def test_collect_sources_yields_pattern_descriptions(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    patterns_path = repo / ".claude" / "memory" / "patterns.json"
    patterns_path.write_text(
        json.dumps(
            {
                "patterns": [
                    {"id": "p1", "description": "認証成功後にセッションを再発行"},
                    {"id": "p2", "description": "ログ出力前にサニタイズ"},
                    {"id": "p3"},  # missing description, skipped
                ]
            }
        ),
        encoding="utf-8",
    )
    chunks = list(collect_sources(repo))
    pattern_chunks = [c for c in chunks if c.source_type == "pattern"]
    assert len(pattern_chunks) == 2
    assert {c.chunk_id for c in pattern_chunks} == {"pattern:p1", "pattern:p2"}


def test_collect_sources_filters_by_source(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    session = repo / ".claude" / "memory" / "sessions" / "20260519.tmp"
    session.write_text("## H\n本文\n", encoding="utf-8")
    patterns_path = repo / ".claude" / "memory" / "patterns.json"
    patterns_path.write_text(
        json.dumps({"patterns": [{"id": "p1", "description": "d"}]}),
        encoding="utf-8",
    )
    chunks = list(collect_sources(repo, sources=["sessions"]))
    assert all(c.source_type == "session" for c in chunks)


def test_collect_sources_skips_gitkeep(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / ".claude" / "memory" / "sessions" / ".gitkeep").write_text("", encoding="utf-8")
    chunks = list(collect_sources(repo))
    assert chunks == []


def test_collect_sources_handles_missing_dirs(tmp_path: Path) -> None:
    # No .claude/ at all.
    assert list(collect_sources(tmp_path)) == []


# ----- stale detection -----


def test_is_stale_when_index_missing(tmp_path: Path) -> None:
    assert is_stale(tmp_path, tmp_path / "nope.hnsw") is True


def test_is_stale_compares_mtimes(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    session = repo / ".claude" / "memory" / "sessions" / "20260519.tmp"
    session.write_text("## H\n本文\n", encoding="utf-8")
    index_path = repo / ".claude" / "state" / "recall.hnsw"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_bytes(b"\0")
    # Make the index look older than the session file.
    import os as _os

    src_mtime = session.stat().st_mtime
    _os.utime(index_path, (src_mtime - 60, src_mtime - 60))
    assert is_stale(repo, index_path) is True

    # Then refresh the index time to be newer.
    _os.utime(index_path, (src_mtime + 60, src_mtime + 60))
    assert is_stale(repo, index_path) is False


def test_warn_if_stale_silent_when_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    warn_if_stale(tmp_path, tmp_path / "noindex.hnsw")
    captured = capsys.readouterr()
    assert captured.err == ""


def test_warn_if_stale_emits_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _make_repo(tmp_path)
    session = repo / ".claude" / "memory" / "sessions" / "20260519.tmp"
    session.write_text("## H\n本文\n", encoding="utf-8")
    index_path = repo / ".claude" / "state" / "recall.hnsw"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_bytes(b"\0")
    import os as _os

    src_mtime = session.stat().st_mtime
    _os.utime(index_path, (src_mtime - 60, src_mtime - 60))
    warn_if_stale(repo, index_path)
    captured = capsys.readouterr()
    assert "older than" in captured.err
    assert "rebuild" in captured.err


# ----- helper paths -----


def test_default_index_paths(tmp_path: Path) -> None:
    idx, meta = default_index_paths(tmp_path)
    assert idx == tmp_path / ".claude" / "state" / "recall.hnsw"
    assert meta == tmp_path / ".claude" / "state" / "recall_meta.json"


# ----- integration with chunker (no embedding model needed) -----


def test_collect_sources_chunks_via_recall_chunker(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    session = repo / ".claude" / "memory" / "sessions" / "20260519.tmp"
    body = "## A\n" + ("x" * 2500) + "\n"
    session.write_text(body, encoding="utf-8")
    direct_chunks = chunk_markdown(body)
    collected = [c for c in collect_sources(repo) if c.source_type == "session"]
    assert len(collected) == len(direct_chunks)


# ----- source_hash (SR-L-5 / CR-M-04) -----


def test_rebuild_populates_source_hash(index: RecallIndex) -> None:
    """ChunkRecord.source_hash must be a non-empty hex string after build().

    Same content must produce the same hash; different content must produce
    different hashes.  The expected format is SHA-256 (64 hex chars) or a
    truncated variant (at least 8 hex chars).
    """
    import re

    content_a = "認証のリトライ実装"
    content_b = "セッション固定化脆弱性の修正"

    rec_a = ChunkRecord(
        source_type="session",
        path="a.md",
        chunk_id="## A#0",
        snippet=content_a,
        mtime=1_700_000_000.0,
        source_hash="",  # placeholder — build() should fill this
    )
    rec_a_dup = ChunkRecord(
        source_type="session",
        path="a_dup.md",
        chunk_id="## A_dup#0",
        snippet=content_a,
        mtime=1_700_000_001.0,
        source_hash="",
    )
    rec_b = ChunkRecord(
        source_type="session",
        path="b.md",
        chunk_id="## B#0",
        snippet=content_b,
        mtime=1_700_000_002.0,
        source_hash="",
    )

    vec = _unit([1.0, 0.0, 0.0])
    index.build([(rec_a, vec), (rec_a_dup, vec), (rec_b, _unit([0.0, 1.0, 0.0]))])

    stored = list(index.meta.chunks.values())
    assert len(stored) == 3

    # All hashes must be non-empty hex strings.
    hex_re = re.compile(r"^[0-9a-f]{8,}$")
    for rec in stored:
        assert rec.source_hash, f"source_hash is empty for {rec.chunk_id!r}"
        assert hex_re.match(rec.source_hash), (
            f"source_hash {rec.source_hash!r} is not a hex string"
        )

    # Same content -> same hash.
    hash_a = stored[0].source_hash
    hash_a_dup = stored[1].source_hash
    assert hash_a == hash_a_dup, "identical content should produce identical source_hash"

    # Different content -> different hash.
    hash_b = stored[2].source_hash
    assert hash_a != hash_b, "different content should produce different source_hash"


# ----- load() wraps TypeError in RuntimeError (SR-M-2) -----


def test_load_wraps_typeerror_in_runtimeerror(tmp_path: Path) -> None:
    """A corrupt recall_meta.json with invalid field types must raise RuntimeError.

    The TypeError from ChunkRecord(**invalid_dict) should be caught and
    re-raised as a RuntimeError containing 'rebuild --force' guidance.
    """
    index_path = tmp_path / "recall.hnsw"
    meta_path = tmp_path / "recall_meta.json"

    # Write a well-structured outer envelope but a chunk record with an
    # unexpected field that will cause ChunkRecord(**v) to raise TypeError.
    corrupt_meta = {
        "model": "test-model",
        "dim": 3,
        "created_at": "2026-01-01T00:00:00+00:00",
        "rebuilt_at": "2026-01-01T00:00:00+00:00",
        "next_id": 1,
        "chunks": {
            "0": {"invalid_field": "x"},  # Missing required ChunkRecord fields
        },
    }
    meta_path.write_text(json.dumps(corrupt_meta), encoding="utf-8")
    # We also need a dummy index file so load() doesn't return False early.
    index_path.write_bytes(b"\x00")

    ri = RecallIndex(
        index_path=index_path,
        meta_path=meta_path,
        model_name="test-model",
        dim=3,
    )

    with pytest.raises(RuntimeError) as exc_info:
        ri.load()

    assert "rebuild --force" in str(exc_info.value), (
        "RuntimeError should contain 'rebuild --force' guidance"
    )


# ----- ChunkRecord __post_init__ truncates snippet (SR-L-2) -----


def test_chunk_record_truncates_long_snippet() -> None:
    """ChunkRecord should truncate snippet > 1000 chars in __post_init__."""
    long_snippet = "x" * 5000
    rec = ChunkRecord(
        source_type="session",
        path="a.md",
        chunk_id="## A#0",
        snippet=long_snippet,
        mtime=1_700_000_000.0,
    )
    assert len(rec.snippet) <= 1000, (
        f"snippet length {len(rec.snippet)} exceeds 1000 chars"
    )


def test_chunk_record_short_snippet_unchanged() -> None:
    """Short snippets must not be modified by __post_init__."""
    rec = ChunkRecord(
        source_type="session",
        path="a.md",
        chunk_id="## A#0",
        snippet="hi",
        mtime=1_700_000_000.0,
    )
    assert rec.snippet == "hi"


# ----- collect_sources skips symlinks (SR-L-4) -----


def _can_symlink(tmp_path: Path) -> bool:
    """Return True if we can create a symlink in tmp_path."""
    target = tmp_path / "_link_test_target.txt"
    link = tmp_path / "_link_test_link.txt"
    target.write_text("x", encoding="utf-8")
    try:
        link.symlink_to(target)
        link.unlink()
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        if target.exists():
            target.unlink()


import sys as _sys


@pytest.mark.skipif(
    _sys.platform == "win32",
    reason="Symlink creation requires elevated privileges on Windows",
)
def test_collect_sources_skips_symlinks(tmp_path: Path) -> None:
    """collect_sources must not yield SourceChunks for symlinked files."""
    repo = _make_repo(tmp_path)
    sessions = repo / ".claude" / "memory" / "sessions"

    # Create a real file and a symlink pointing to it.
    real_file = sessions / "real_session.tmp"
    real_file.write_text("## Real\n本文\n", encoding="utf-8")

    link_file = sessions / "linked_session.tmp"
    link_file.symlink_to(real_file)

    chunks = list(collect_sources(repo))
    chunk_paths = {c.path for c in chunks}

    # The real file should be indexed.
    assert any("real_session.tmp" in p for p in chunk_paths), (
        "real file should appear in collect_sources output"
    )
    # The symlink should NOT be indexed.
    assert not any("linked_session.tmp" in p for p in chunk_paths), (
        "symlinked file should be skipped by collect_sources"
    )
