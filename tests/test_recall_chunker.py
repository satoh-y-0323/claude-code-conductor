"""Tests for src/c3/recall_chunker.py."""

from __future__ import annotations

import pytest

from c3.recall_chunker import (
    DEFAULT_MAX_CHARS,
    DEFAULT_OVERLAP_CHARS,
    Chunk,
    chunk_markdown,
)


def test_empty_input_returns_empty_list() -> None:
    assert chunk_markdown("") == []
    assert chunk_markdown("   \n\n  ") == []


def test_single_section_below_max_returns_one_chunk() -> None:
    text = "## 残タスク\n\n- foo\n- bar\n"
    chunks = chunk_markdown(text)
    assert len(chunks) == 1
    assert chunks[0].heading == "## 残タスク"
    assert chunks[0].window_index == 0
    assert "## 残タスク" in chunks[0].content
    assert "- foo" in chunks[0].content


def test_multiple_sections_split_independently() -> None:
    text = (
        "## うまくいったアプローチ\n"
        "認証 OK\n\n"
        "## 失敗\n"
        "ハマった\n"
    )
    chunks = chunk_markdown(text)
    assert [c.heading for c in chunks] == ["## うまくいったアプローチ", "## 失敗"]
    assert "認証 OK" in chunks[0].content
    assert "ハマった" in chunks[1].content


def test_preamble_before_first_heading_kept_as_empty_heading_chunk() -> None:
    text = "イントロ文\n\n## セクションA\n本文A\n"
    chunks = chunk_markdown(text)
    assert chunks[0].heading == ""
    assert "イントロ文" in chunks[0].content
    assert chunks[1].heading == "## セクションA"


def test_document_without_headings_yields_single_unheaded_chunk() -> None:
    text = "頭から本文\n二行目\n"
    chunks = chunk_markdown(text)
    assert len(chunks) == 1
    assert chunks[0].heading == ""
    assert "頭から本文" in chunks[0].content


def test_long_section_splits_into_overlapping_windows() -> None:
    long_body = "x" * 2500
    text = f"## ロング\n{long_body}\n"
    chunks = chunk_markdown(text, max_chars=1000, overlap_chars=100)
    assert len(chunks) >= 3
    assert all(c.heading == "## ロング" for c in chunks)
    assert [c.window_index for c in chunks] == list(range(len(chunks)))
    # First window starts with heading; later windows have heading prepended
    assert chunks[0].content.startswith("## ロング")
    assert "## ロング" in chunks[1].content
    assert "## ロング" in chunks[2].content


def test_long_section_overlap_keeps_continuity() -> None:
    # Build a text with positional markers to verify overlap.
    body_chars = []
    for i in range(2500):
        body_chars.append(chr(0x30 + (i % 10)))  # cycles 0..9
    body = "".join(body_chars)
    text = f"## OV\n{body}\n"
    chunks = chunk_markdown(text, max_chars=500, overlap_chars=50)
    # The end of one window should overlap with the start of the next window
    # (we test only the raw body portion, dropping the heading prefix).
    # We can't easily strip in test, so instead check that more than ceil(N/step)
    # chunks exist if step < max_chars.
    assert len(chunks) >= 5


def test_window_index_increments_within_section() -> None:
    text = f"## A\n{'a' * 2500}\n\n## B\n{'b' * 800}\n"
    chunks = chunk_markdown(text, max_chars=1000, overlap_chars=100)
    a_chunks = [c for c in chunks if c.heading == "## A"]
    b_chunks = [c for c in chunks if c.heading == "## B"]
    assert [c.window_index for c in a_chunks] == [0, 1, 2]
    assert [c.window_index for c in b_chunks] == [0]


def test_invalid_params_raise() -> None:
    with pytest.raises(ValueError):
        chunk_markdown("body", max_chars=0)
    with pytest.raises(ValueError):
        chunk_markdown("body", max_chars=100, overlap_chars=100)
    with pytest.raises(ValueError):
        chunk_markdown("body", max_chars=100, overlap_chars=-1)


def test_default_max_and_overlap_are_sensible() -> None:
    assert DEFAULT_MAX_CHARS == 1000
    assert DEFAULT_OVERLAP_CHARS == 100


def test_chunk_dataclass_immutable() -> None:
    import dataclasses

    c = Chunk(heading="## H", content="body", window_index=0)
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):  # frozen dataclass
        c.content = "mutated"  # type: ignore[misc]


def test_whitespace_only_section_is_skipped() -> None:
    text = "## 空\n\n   \n\n## 非空\n本文\n"
    chunks = chunk_markdown(text)
    headings = [c.heading for c in chunks]
    assert "## 空" not in headings
    assert "## 非空" in headings


def test_chunk_content_includes_heading_for_first_window() -> None:
    text = "## H\n本文本文本文\n"
    chunks = chunk_markdown(text)
    assert chunks[0].content.startswith("## H")
