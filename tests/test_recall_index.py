"""Tests for src/c3/recall_index.py.

The RecallIndex backend uses numpy brute-force cosine search (no compiled
extension required). Tests use synthetic float vectors so no model weights
are downloaded and they are fast enough to stay in the default suite.
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
    content_hash,
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


# ----- content_hash (CR-M-001) -----


def test_content_hash_returns_64_hex_chars() -> None:
    """content_hash must return a 64-character lowercase hex string (SHA-256)."""
    import re

    result = content_hash("hello world")
    assert re.fullmatch(r"[0-9a-f]{64}", result), (
        f"Expected 64-char hex, got {result!r}"
    )


def test_content_hash_same_input_same_output() -> None:
    """content_hash must be deterministic: same text always produces same digest."""
    text = "認証のリトライ実装"
    assert content_hash(text) == content_hash(text)


def test_content_hash_different_inputs_differ() -> None:
    """Different texts must produce different digests."""
    assert content_hash("foo") != content_hash("bar")


def test_content_hash_known_digest() -> None:
    """Verify a known SHA-256 digest to pin the algorithm and encoding."""
    # python -c "import hashlib; print(hashlib.sha256('abc'.encode('utf-8')).hexdigest())"
    # -> ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad
    assert content_hash("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_content_hash_errors_replace_on_surrogate() -> None:
    """errors='replace' must not raise on strings containing surrogates."""
    # A lone surrogate is invalid UTF-8 but must not crash content_hash.
    surrogate_text = "ok\udcff"
    result = content_hash(surrogate_text)
    assert isinstance(result, str) and len(result) == 64


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


# ----- get_vector accessor (T1 / v2.28.0 incremental rebuild) -----


def test_get_vector_returns_stored_vector_after_build(index: RecallIndex) -> None:
    """get_vector(chunk_id) must return the exact vector stored at build time.

    After build(), each integer chunk id (0, 1, ...) must yield back the
    original unit vector passed in. The numpy backend stores vectors as
    float32 and returns list[float] via tolist(), so we allow a small
    absolute tolerance.
    """
    a = _unit([1.0, 0.0, 0.0])
    b = _unit([0.0, 1.0, 0.0])
    index.build(
        [
            (_record(chunk_id="c0"), a),
            (_record(chunk_id="c1"), b),
        ]
    )
    assert index.get_vector(0) == pytest.approx(a, abs=1e-5)
    assert index.get_vector(1) == pytest.approx(b, abs=1e-5)


def test_get_vector_returns_list_of_float(index: RecallIndex) -> None:
    """get_vector must return list[float], not numpy array or other type."""
    index.build([(_record(chunk_id="c0"), _unit([1.0, 0.0, 0.0]))])
    result = index.get_vector(0)
    assert isinstance(result, list), f"expected list, got {type(result)}"
    assert all(isinstance(x, float) for x in result), "all elements must be float"
    assert len(result) == 3


def test_get_vector_unknown_id_raises(index: RecallIndex) -> None:
    """get_vector with an unknown id must raise after a successful build().

    Precondition: index has been built (build() called with at least one item).
    This test verifies the "built index, unknown id" case specifically — it is
    NOT testing the "index not yet built" path (that path raises RuntimeError
    with "has not been built" message; see test_get_vector_not_built_raises).

    The numpy backend raises IndexError for out-of-range ids. We assert that
    *some* exception is raised so an invalid id does NOT silently return a
    zero vector or None.
    """
    index.build([(_record(chunk_id="c0"), _unit([1.0, 0.0, 0.0]))])
    # After build(), chunk id 0 is valid but 999 is not present in the index.
    with pytest.raises(Exception):
        index.get_vector(999)


def test_get_vector_not_built_raises_runtime_error(index: RecallIndex) -> None:
    """get_vector before build()/load() must raise RuntimeError with a clear message.

    This test specifically covers the "index not built" path (``_index is None``
    or equivalent uninitialized state), which is distinct from the "built but
    unknown id" case tested above.
    """
    # No build() or load() called — internal state is uninitialized.
    with pytest.raises(RuntimeError, match="has not been built"):
        index.get_vector(0)


def test_get_vector_after_save_and_load(index: RecallIndex, tmp_path: Path) -> None:
    """get_vector must work after a save/load cycle."""
    vec = _unit([0.5, 0.5, 0.0])
    index.build([(_record(chunk_id="X"), vec)])
    index.save()

    fresh = RecallIndex(
        index_path=index.index_path,
        meta_path=index.meta_path,
        model_name="test-model",
        dim=3,
    )
    fresh.load()
    assert fresh.get_vector(0) == pytest.approx(vec, abs=1e-5)


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


# ----- numpy backend: cosine ranking correctness -----


def test_cosine_ranking_returns_most_similar_first(index: RecallIndex) -> None:
    """search() must return results ordered by cosine similarity (most similar first).

    Given a set of known orthogonal unit vectors, the query that matches one
    exactly must appear first with distance ≈ 0.0. The remaining results must
    appear in ascending distance order (distance = 1 - cosine_sim).
    """
    # Three orthogonal unit vectors in 3-D space.
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    c = [0.0, 0.0, 1.0]
    index.build(
        [
            (_record(chunk_id="A"), a),
            (_record(chunk_id="B"), b),
            (_record(chunk_id="C"), c),
        ]
    )

    # Query identical to vector A — must come first with distance ≈ 0.
    results = index.search(a, top_k=3)
    assert len(results) == 3
    chunk_ids = [r[2].chunk_id for r in results]
    assert chunk_ids[0] == "A", f"expected A first, got {chunk_ids}"
    assert results[0][1] == pytest.approx(0.0, abs=1e-5), (
        f"distance for identical vector must be ≈ 0.0, got {results[0][1]}"
    )

    # All distances must be non-negative and in ascending order.
    distances = [r[1] for r in results]
    for i in range(len(distances) - 1):
        assert distances[i] <= distances[i + 1] + 1e-6, (
            f"results not sorted by distance: {distances}"
        )

    # Query that is equidistant from B and C but closest to A — only A is exact.
    diagonal = _unit([1.0, 0.5, 0.5])
    results2 = index.search(diagonal, top_k=1)
    assert results2[0][2].chunk_id == "A", (
        f"diagonal query closest to A, got {results2[0][2].chunk_id}"
    )


def test_search_distance_equals_one_minus_cosine_sim(index: RecallIndex) -> None:
    """distance returned by search() must equal 1 - cosine_similarity.

    This verifies the contract consumed by cli_recall.py: score = 1.0 - distance.
    """
    a = [1.0, 0.0, 0.0]
    b = [0.5, 0.5, 0.0]  # not unit — raw storage, normalized at search time
    index.build(
        [
            (_record(chunk_id="A"), a),
            (_record(chunk_id="B"), b),
        ]
    )

    query = _unit([1.0, 0.0, 0.0])
    results = index.search(query, top_k=2)

    for chunk_id_int, dist, record in results:
        if record.chunk_id == "A":
            # Exact match: cosine_sim = 1.0, distance = 0.0
            assert dist == pytest.approx(0.0, abs=1e-5)
        elif record.chunk_id == "B":
            # cosine_sim(query=[1,0,0], b=[0.5,0.5,0]) = 0.5 / sqrt(0.5)
            import math as _math
            norm_b = _math.sqrt(0.5 * 0.5 + 0.5 * 0.5)
            expected_sim = 0.5 / norm_b  # ≈ 0.7071
            expected_dist = 1.0 - expected_sim
            assert dist == pytest.approx(expected_dist, abs=1e-4), (
                f"distance {dist} does not match 1-cosine_sim {expected_dist}"
            )


# ----- numpy backend: load() returns False for legacy hnswlib binary -----


def test_load_returns_false_for_legacy_hnswlib_binary(tmp_path: Path) -> None:
    """load() must return False (not raise) when the index file is not a valid numpy array.

    After upgrading from hnswlib to numpy backend, existing index files contain
    hnswlib binary data that np.load() cannot parse. The load() method must catch
    the parse error and return False so that cli_recall can fall back to a full
    rebuild rather than crashing.
    """
    index_path = tmp_path / "recall.hnsw"
    meta_path = tmp_path / "recall_meta.json"

    # Write a syntactically valid meta so load() reaches the np.load() step.
    valid_meta = {
        "model": "test-model",
        "dim": 3,
        "created_at": "2026-01-01T00:00:00+00:00",
        "rebuilt_at": "2026-01-01T00:00:00+00:00",
        "next_id": 0,
        "chunks": {},
    }
    meta_path.write_text(json.dumps(valid_meta), encoding="utf-8")

    # Write a non-numpy binary payload (mimics a legacy hnswlib binary index).
    index_path.write_bytes(b"\x00\x01garbage not a numpy array")

    ri = RecallIndex(
        index_path=index_path,
        meta_path=meta_path,
        model_name="test-model",
        dim=3,
    )

    result = ri.load()
    assert result is False, (
        f"load() must return False for non-numpy index file, got {result!r}"
    )


# ----- numpy backend: empty index and top_k > N -----


def test_search_on_empty_build_returns_empty_list(index: RecallIndex) -> None:
    """search() on an index built with zero items must return an empty list."""
    index.build([])
    result = index.search([1.0, 0.0, 0.0])
    assert result == [], f"expected [], got {result!r}"


def test_search_top_k_larger_than_corpus_returns_all(index: RecallIndex) -> None:
    """When top_k exceeds the number of indexed items, return all items (not top_k).

    The result length must equal the actual corpus size, not top_k, and must not
    raise an exception.
    """
    items = [
        (_record(chunk_id="A"), _unit([1.0, 0.0, 0.0])),
        (_record(chunk_id="B"), _unit([0.0, 1.0, 0.0])),
    ]
    index.build(items)

    results = index.search([1.0, 0.0, 0.0], top_k=100)
    assert len(results) == 2, (
        f"expected 2 results (corpus size), got {len(results)}"
    )


# ----- load() rejects malformed npy shapes (H-01 / SR M-1) -----


def _valid_meta(dim: int, model: str = "test-model") -> dict:
    """Return a minimal valid recall_meta.json payload for the given dim."""
    return {
        "model": model,
        "dim": dim,
        "created_at": "2026-01-01T00:00:00+00:00",
        "rebuilt_at": "2026-01-01T00:00:00+00:00",
        "next_id": 0,
        "chunks": {},
    }


def _write_npy(path: Path, arr: "np.ndarray") -> None:
    """Save a numpy array to *path* using file-object form (same as RecallIndex.save)."""
    import numpy as _np

    with open(path, "wb") as f:
        _np.save(f, arr)


def test_load_returns_false_for_1d_npy(tmp_path: Path) -> None:
    """load() must return False when the npy file contains a 1-D array.

    A valid numpy array with ndim=1 is syntactically loadable but not a valid
    index matrix (which must be 2-D with shape (N, dim)). load() must detect
    the shape mismatch and return False instead of leaving a broken internal
    state or raising.
    """
    import numpy as _np

    dim = 3
    index_path = tmp_path / "recall.hnsw"
    meta_path = tmp_path / "recall_meta.json"
    meta_path.write_text(json.dumps(_valid_meta(dim)), encoding="utf-8")
    _write_npy(index_path, _np.ones(dim, dtype=_np.float32))  # 1-D shape (dim,)

    ri = RecallIndex(
        index_path=index_path,
        meta_path=meta_path,
        model_name="test-model",
        dim=dim,
    )
    result = ri.load()
    assert result is False, (
        f"load() must return False for 1-D npy array, got {result!r}"
    )


def test_load_returns_false_for_0d_npy(tmp_path: Path) -> None:
    """load() must return False when the npy file contains a 0-D (scalar) array.

    np.array(1.0) produces a valid npy file but shape=(), which is not a valid
    index matrix. load() must detect this and return False.
    """
    import numpy as _np

    dim = 3
    index_path = tmp_path / "recall.hnsw"
    meta_path = tmp_path / "recall_meta.json"
    meta_path.write_text(json.dumps(_valid_meta(dim)), encoding="utf-8")
    _write_npy(index_path, _np.array(1.0, dtype=_np.float32))  # 0-D scalar

    ri = RecallIndex(
        index_path=index_path,
        meta_path=meta_path,
        model_name="test-model",
        dim=dim,
    )
    result = ri.load()
    assert result is False, (
        f"load() must return False for 0-D npy array, got {result!r}"
    )


def test_load_returns_false_for_3d_npy(tmp_path: Path) -> None:
    """load() must return False when the npy file contains a 3-D array.

    A 3-D array (e.g. shape (2, dim, 4)) is a valid numpy file but is not an
    index matrix. load() must detect ndim != 2 and return False.
    """
    import numpy as _np

    dim = 3
    index_path = tmp_path / "recall.hnsw"
    meta_path = tmp_path / "recall_meta.json"
    meta_path.write_text(json.dumps(_valid_meta(dim)), encoding="utf-8")
    _write_npy(index_path, _np.ones((2, dim, 4), dtype=_np.float32))  # 3-D

    ri = RecallIndex(
        index_path=index_path,
        meta_path=meta_path,
        model_name="test-model",
        dim=dim,
    )
    result = ri.load()
    assert result is False, (
        f"load() must return False for 3-D npy array, got {result!r}"
    )


def test_load_returns_false_for_wrong_column_count(tmp_path: Path) -> None:
    """load() must return False when the npy shape[1] does not match dim.

    A 2-D array with shape (N, dim+1) is structurally a matrix but the column
    count is inconsistent with the RecallIndex.dim setting. load() must detect
    this mismatch and return False rather than leaving a silently broken index
    that would corrupt search results.
    """
    import numpy as _np

    dim = 3
    index_path = tmp_path / "recall.hnsw"
    meta_path = tmp_path / "recall_meta.json"
    meta_path.write_text(json.dumps(_valid_meta(dim)), encoding="utf-8")
    _write_npy(
        index_path,
        _np.ones((2, dim + 1), dtype=_np.float32),  # 2-D but wrong column count
    )

    ri = RecallIndex(
        index_path=index_path,
        meta_path=meta_path,
        model_name="test-model",
        dim=dim,
    )
    result = ri.load()
    assert result is False, (
        f"load() must return False for 2-D npy with wrong column count (dim mismatch), "
        f"got {result!r}"
    )


def test_load_emits_stderr_on_parse_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """load() must write a failure reason to stderr when returning False due to a parse error.

    When load() returns False because np.load() fails (e.g. legacy hnswlib
    binary), it must emit a message to stderr containing '[recall] index load
    failed' so that the operator can diagnose the rebuild trigger without
    reading internal paths.
    """
    index_path = tmp_path / "recall.hnsw"
    meta_path = tmp_path / "recall_meta.json"
    meta_path.write_text(json.dumps(_valid_meta(3)), encoding="utf-8")
    # Non-numpy binary triggers a parse error in np.load().
    index_path.write_bytes(b"\x00\x01garbage not a numpy array")

    ri = RecallIndex(
        index_path=index_path,
        meta_path=meta_path,
        model_name="test-model",
        dim=3,
    )
    result = ri.load()
    assert result is False

    captured = capsys.readouterr()
    assert "[recall] index load failed" in captured.err, (
        f"Expected '[recall] index load failed' in stderr, got: {captured.err!r}"
    )


def test_load_emits_stderr_on_shape_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """load() must write a failure reason to stderr when returning False due to a shape error.

    When load() returns False because the loaded array has a wrong shape (1-D,
    0-D, 3-D, or wrong column count), it must emit a message to stderr
    containing '[recall] index load failed' with a brief description of the
    shape problem. This enables operators to distinguish shape errors from
    binary parse errors in logs.
    """
    import numpy as _np

    dim = 3
    index_path = tmp_path / "recall.hnsw"
    meta_path = tmp_path / "recall_meta.json"
    meta_path.write_text(json.dumps(_valid_meta(dim)), encoding="utf-8")
    _write_npy(index_path, _np.ones(dim, dtype=_np.float32))  # 1-D, triggers shape check

    ri = RecallIndex(
        index_path=index_path,
        meta_path=meta_path,
        model_name="test-model",
        dim=dim,
    )
    result = ri.load()
    assert result is False

    captured = capsys.readouterr()
    assert "[recall] index load failed" in captured.err, (
        f"Expected '[recall] index load failed' in stderr, got: {captured.err!r}"
    )


# ----- numpy backend: non-ASCII path save/load roundtrip -----


def test_save_load_roundtrip_non_ascii_path(tmp_path: Path) -> None:
    """save() and load() must succeed on a path containing non-ASCII characters.

    The numpy backend uses file-object-based np.save/np.load (not passing path
    strings to C extensions), so non-ASCII paths must work transparently on all
    platforms including Windows.
    """
    non_ascii_dir = tmp_path / "日本語"
    non_ascii_dir.mkdir()
    index_path = non_ascii_dir / "recall.hnsw"
    meta_path = non_ascii_dir / "recall_meta.json"

    idx = RecallIndex(
        index_path=index_path,
        meta_path=meta_path,
        model_name="test-model",
        dim=3,
    )
    items = [
        (_record(chunk_id="X"), _unit([1.0, 0.0, 0.0])),
        (_record(chunk_id="Y"), _unit([0.0, 1.0, 0.0])),
    ]
    idx.build(items)
    idx.save()

    fresh = RecallIndex(
        index_path=index_path,
        meta_path=meta_path,
        model_name="test-model",
        dim=3,
    )
    assert fresh.load() is True, "load() must succeed for non-ASCII path"
    assert fresh.chunk_count() == 2

    results = fresh.search(_unit([1.0, 0.0, 0.0]), top_k=1)
    assert results[0][2].chunk_id == "X", (
        f"expected chunk X to be nearest, got {results[0][2].chunk_id}"
    )
