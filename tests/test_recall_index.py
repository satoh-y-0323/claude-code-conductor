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
    content_hash,
    default_index_paths,
    is_stale,
    snippet_of,
    warn_if_stale,
    _hnsw_save,
    _hnsw_load,
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
    original unit vector passed in.  We compare with pytest.approx because
    hnswlib may apply minimal float normalisation internally.
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

    hnswlib raises RuntimeError for out-of-range ids; the exact message is
    implementation-defined. We assert that *some* exception is raised so
    an invalid id does NOT silently return a zero vector or None.
    """
    index.build([(_record(chunk_id="c0"), _unit([1.0, 0.0, 0.0]))])
    # After build(), chunk id 0 is valid but 999 is not present in the index.
    # hnswlib's exception type for an out-of-range id is implementation-defined,
    # so we assert that *some* exception is raised (the docstring intent) rather
    # than a self-contradictory tuple that ends in the Exception base class.
    with pytest.raises(Exception):
        index.get_vector(999)


def test_get_vector_not_built_raises_runtime_error(index: RecallIndex) -> None:
    """get_vector before build()/load() must raise RuntimeError with a clear message.

    This test specifically covers the "index not built" path (``_index is None``),
    which is distinct from the "built but unknown id" case tested above.
    """
    # No build() or load() called — _index is None.
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


# ----- _hnsw_save / _hnsw_load Windows non-ASCII path workaround (T1-B1) -----
#
# These tests verify the platform-aware detour logic introduced to work around
# hnswlib's C-level fopen() silently failing on Windows non-ASCII paths.
#
# Cases 1-3 verify call routing (direct vs. tempfile detour).
# Cases 4-5 verify error-handling behaviour (regression guard for G2-B1).


class TestHnswSaveNonWindowsUsesDirectCall:
    """Case 1: Non-Windows always calls save_index directly, even for non-ASCII paths."""

    def test_hnsw_save_on_non_windows_uses_direct_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On Linux/macOS, _hnsw_save must call index.save_index(str(path)) directly.

        The tempfile detour is Windows-only; on other platforms hnswlib handles
        non-ASCII paths natively via the OS.
        """
        hnswlib = pytest.importorskip("hnswlib")

        import c3.recall_index as recall_mod
        from unittest.mock import MagicMock

        monkeypatch.setattr(recall_mod.sys, "platform", "linux")

        non_ascii_path = tmp_path / "テスト索引.hnsw"
        mock_index = MagicMock()

        _hnsw_save(mock_index, non_ascii_path)

        mock_index.save_index.assert_called_once_with(str(non_ascii_path))


class TestHnswSaveWindowsAsciiPathUsesDirectCall:
    """Case 2: Windows + ASCII path calls save_index directly (no tempfile detour)."""

    def test_hnsw_save_on_windows_with_ascii_path_uses_direct_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On Windows with a pure-ASCII path, _hnsw_save must NOT use tempfile.

        The tempfile detour adds overhead and is only justified for non-ASCII
        paths where hnswlib's C fopen() would silently fail.
        """
        hnswlib = pytest.importorskip("hnswlib")

        import c3.recall_index as recall_mod
        from unittest.mock import MagicMock

        monkeypatch.setattr(recall_mod.sys, "platform", "win32")

        ascii_path = tmp_path / "recall.hnsw"
        mock_index = MagicMock()

        _hnsw_save(mock_index, ascii_path)

        mock_index.save_index.assert_called_once_with(str(ascii_path))


class TestHnswSaveWindowsNonAsciiPathUsesTempfile:
    """Case 3: Windows + non-ASCII path must route through an ASCII tempfile."""

    def test_hnsw_save_on_windows_with_non_ascii_path_uses_tempfile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On Windows with a non-ASCII path, _hnsw_save must call save_index with an
        ASCII-only tempfile path, then copy the result to the real destination.

        The argument passed to save_index must be ASCII-only (hnswlib C fopen safe).
        """
        hnswlib = pytest.importorskip("hnswlib")

        import c3.recall_index as recall_mod
        import shutil
        from unittest.mock import MagicMock, patch

        monkeypatch.setattr(recall_mod.sys, "platform", "win32")

        non_ascii_path = tmp_path / "日本語パス" / "recall.hnsw"
        non_ascii_path.parent.mkdir(parents=True, exist_ok=True)

        mock_index = MagicMock()

        # Intercept shutil.copy2 so the test does not require the tempfile to
        # actually contain valid HNSW data.
        with patch.object(recall_mod.shutil, "copy2"):
            _hnsw_save(mock_index, non_ascii_path)

        # save_index must have been called with a path that is ASCII-only.
        assert mock_index.save_index.call_count == 1
        actual_path_arg = mock_index.save_index.call_args[0][0]
        assert actual_path_arg.isascii(), (
            f"save_index was called with non-ASCII path {actual_path_arg!r}; "
            "expected an ASCII-only tempfile path"
        )
        # The ASCII tmp path must differ from the real (non-ASCII) destination.
        assert actual_path_arg != str(non_ascii_path)


class TestHnswSaveTempfileCleanupFailureLogsWarning:
    """regression guard for G2-B1: unlink failure during tempfile cleanup emits a stderr warning.

    Verifies that the finally block logs a warning to stderr instead of
    silently swallowing the OSError when tmp.unlink() fails.
    """

    def test_hnsw_save_tempfile_cleanup_failure_logs_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When tmp.unlink() raises OSError, _hnsw_save must log a warning to stderr.

        The warning must mention that the temporary file could not be removed
        (e.g. contain the word 'warn' or 'cleanup' or 'temp' case-insensitively).
        """
        hnswlib = pytest.importorskip("hnswlib")

        import c3.recall_index as recall_mod
        import shutil
        from unittest.mock import MagicMock, patch

        monkeypatch.setattr(recall_mod.sys, "platform", "win32")

        non_ascii_path = tmp_path / "テスト" / "recall.hnsw"
        non_ascii_path.parent.mkdir(parents=True, exist_ok=True)

        mock_index = MagicMock()

        # Simulate unlink() failure by patching Path.unlink on the tmp path.
        # We patch shutil.copy2 to avoid needing valid HNSW data, and patch
        # Path.unlink to raise OSError unconditionally.
        original_unlink = recall_mod.Path.unlink

        def failing_unlink(self, missing_ok=False):
            raise OSError("simulated cleanup failure")

        with (
            patch.object(recall_mod.shutil, "copy2"),
            patch.object(recall_mod.Path, "unlink", failing_unlink),
        ):
            _hnsw_save(mock_index, non_ascii_path)

        captured = capsys.readouterr()
        assert captured.err, (
            "_hnsw_save must emit a warning to stderr when tmp cleanup fails, "
            "but stderr was empty"
        )
        lower_err = captured.err.lower()
        assert any(kw in lower_err for kw in ("warn", "cleanup", "temp", "unlink", "tmp")), (
            f"stderr warning {captured.err!r} does not mention cleanup failure"
        )


class TestHnswSaveHidesTempPathOnHnswlibError:
    """regression guard for G2-B1: hnswlib exceptions must not expose the internal tempfile path.

    Verifies that when save_index raises, the re-raised exception message contains
    the real destination path (or a generic message) but never the internal ASCII
    tempfile path used as a workaround.
    """

    def test_hnsw_save_hides_temp_path_on_hnswlib_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When save_index raises, the re-raised exception must NOT contain the
        internal ASCII tempfile path in its message.

        The caller should see the real destination path (or a generic message)
        but never the implementation-detail tmp path that was used internally.
        """
        hnswlib = pytest.importorskip("hnswlib")

        import c3.recall_index as recall_mod
        import shutil
        from unittest.mock import MagicMock, patch

        monkeypatch.setattr(recall_mod.sys, "platform", "win32")

        non_ascii_path = tmp_path / "日本語" / "recall.hnsw"
        non_ascii_path.parent.mkdir(parents=True, exist_ok=True)

        captured_tmp_path: list[str] = []

        def spy_save_index(path_str: str) -> None:
            captured_tmp_path.append(path_str)
            raise RuntimeError(f"hnswlib internal error writing to {path_str}")

        mock_index = MagicMock()
        mock_index.save_index.side_effect = spy_save_index

        with pytest.raises(Exception) as exc_info:
            _hnsw_save(mock_index, non_ascii_path)

        assert captured_tmp_path, "save_index was not called; test setup is wrong"
        tmp_path_str = captured_tmp_path[0]

        error_message = str(exc_info.value)
        assert tmp_path_str not in error_message, (
            f"Exception message {error_message!r} leaks the internal tempfile path "
            f"{tmp_path_str!r}. _hnsw_save must wrap the exception and hide the tmp path."
        )


# ----- _hnsw_load Windows non-ASCII path workaround (T1-B1 load-side) -----
#
# These tests are regression guards for the platform-aware load detour mirroring
# the _hnsw_save logic.  The implementation in _hnsw_load already satisfies all
# five cases, so these tests should PASS (verify guard).


class TestHnswLoadNonWindowsUsesDirectCall:
    """regression guard for _hnsw_load: non-Windows calls load_index directly."""

    def test_hnsw_load_non_windows_uses_direct_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """regression guard for _hnsw_load non-ASCII path on Linux/macOS.

        On non-Windows, _hnsw_load must call index.load_index(str(path)) directly
        without tempfile indirection, even for non-ASCII paths.
        """
        pytest.importorskip("hnswlib")

        import c3.recall_index as recall_mod
        from unittest.mock import MagicMock

        monkeypatch.setattr(recall_mod.sys, "platform", "linux")

        non_ascii_path = tmp_path / "テスト索引.hnsw"
        # The file does not need to exist because load_index is mocked.
        mock_index = MagicMock()

        _hnsw_load(mock_index, non_ascii_path)

        mock_index.load_index.assert_called_once_with(str(non_ascii_path))


class TestHnswLoadWindowsAsciiPathUsesDirectCall:
    """regression guard for _hnsw_load: Windows + ASCII path calls load_index directly."""

    def test_hnsw_load_windows_with_ascii_path_uses_direct_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """regression guard for _hnsw_load ASCII path on Windows.

        On Windows with a pure-ASCII path, _hnsw_load must NOT use the tempfile
        detour; load_index must be called with the original path string.
        """
        pytest.importorskip("hnswlib")

        import c3.recall_index as recall_mod
        from unittest.mock import MagicMock

        monkeypatch.setattr(recall_mod.sys, "platform", "win32")

        ascii_path = tmp_path / "recall.hnsw"
        mock_index = MagicMock()

        _hnsw_load(mock_index, ascii_path)

        mock_index.load_index.assert_called_once_with(str(ascii_path))


class TestHnswLoadWindowsNonAsciiPathUsesTempfile:
    """regression guard for _hnsw_load: Windows + non-ASCII path routes through ASCII tempfile."""

    def test_hnsw_load_windows_with_non_ascii_path_uses_tempfile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """regression guard for _hnsw_load non-ASCII path tempfile detour on Windows.

        On Windows with a non-ASCII source path, _hnsw_load must call load_index
        with an ASCII-only tempfile path, not the original non-ASCII path.
        """
        pytest.importorskip("hnswlib")

        import c3.recall_index as recall_mod
        from unittest.mock import MagicMock, patch

        monkeypatch.setattr(recall_mod.sys, "platform", "win32")

        non_ascii_path = tmp_path / "日本語パス" / "recall.hnsw"
        non_ascii_path.parent.mkdir(parents=True, exist_ok=True)
        # Create a dummy file so shutil.copy2 (if not patched) doesn't fail.
        non_ascii_path.write_bytes(b"\x00")

        mock_index = MagicMock()

        with patch.object(recall_mod.shutil, "copy2"):
            _hnsw_load(mock_index, non_ascii_path)

        assert mock_index.load_index.call_count == 1
        actual_path_arg = mock_index.load_index.call_args[0][0]
        assert actual_path_arg.isascii(), (
            f"load_index was called with non-ASCII path {actual_path_arg!r}; "
            "expected an ASCII-only tempfile path"
        )
        assert actual_path_arg != str(non_ascii_path)


class TestHnswLoadTempfileCleanupFailureLogsWarning:
    """regression guard for _hnsw_load: unlink failure during cleanup emits stderr warning."""

    def test_hnsw_load_tempfile_cleanup_failure_logs_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """regression guard for _hnsw_load cleanup-failure warning.

        When tmp.unlink() raises OSError, _hnsw_load must emit a warning to stderr
        instead of silently swallowing the error, so operators can detect temp-
        directory leaks.
        """
        pytest.importorskip("hnswlib")

        import c3.recall_index as recall_mod
        from unittest.mock import MagicMock, patch

        monkeypatch.setattr(recall_mod.sys, "platform", "win32")

        non_ascii_path = tmp_path / "テスト" / "recall.hnsw"
        non_ascii_path.parent.mkdir(parents=True, exist_ok=True)
        non_ascii_path.write_bytes(b"\x00")

        mock_index = MagicMock()

        def failing_unlink(self, missing_ok=False):
            raise OSError("simulated cleanup failure")

        with (
            patch.object(recall_mod.shutil, "copy2"),
            patch.object(recall_mod.Path, "unlink", failing_unlink),
        ):
            _hnsw_load(mock_index, non_ascii_path)

        captured = capsys.readouterr()
        assert captured.err, (
            "_hnsw_load must emit a warning to stderr when tmp cleanup fails, "
            "but stderr was empty"
        )
        lower_err = captured.err.lower()
        assert any(kw in lower_err for kw in ("warn", "cleanup", "temp", "unlink", "tmp")), (
            f"stderr warning {captured.err!r} does not mention cleanup failure"
        )


class TestHnswLoadHidesTempPathOnHnswlibError:
    """regression guard for _hnsw_load: hnswlib exceptions must not expose the internal temp path."""

    def test_hnsw_load_hides_temp_path_on_hnswlib_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """regression guard for _hnsw_load exception message does not leak tmp path.

        When load_index raises, the re-raised exception must NOT contain the
        internal ASCII tempfile path in its message.  The caller should see the
        real destination filename (or a generic message) but never the
        implementation-detail tmp path used internally.
        """
        pytest.importorskip("hnswlib")

        import c3.recall_index as recall_mod
        from unittest.mock import MagicMock, patch

        monkeypatch.setattr(recall_mod.sys, "platform", "win32")

        non_ascii_path = tmp_path / "日本語" / "recall.hnsw"
        non_ascii_path.parent.mkdir(parents=True, exist_ok=True)
        non_ascii_path.write_bytes(b"\x00")

        captured_tmp_path: list[str] = []

        def spy_load_index(path_str: str, **kwargs: object) -> None:
            captured_tmp_path.append(path_str)
            raise RuntimeError(f"hnswlib internal error reading from {path_str}")

        mock_index = MagicMock()
        mock_index.load_index.side_effect = spy_load_index

        with patch.object(recall_mod.shutil, "copy2"):
            with pytest.raises(Exception) as exc_info:
                _hnsw_load(mock_index, non_ascii_path)

        assert captured_tmp_path, "load_index was not called; test setup is wrong"
        tmp_path_str = captured_tmp_path[0]

        error_message = str(exc_info.value)
        assert tmp_path_str not in error_message, (
            f"Exception message {error_message!r} leaks the internal tempfile path "
            f"{tmp_path_str!r}. _hnsw_load must wrap the exception and hide the tmp path."
        )
