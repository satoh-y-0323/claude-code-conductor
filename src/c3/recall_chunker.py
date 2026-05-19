"""Markdown chunker for the ``c3 recall`` feature.

Splits source text into search-friendly chunks while preserving heading
context. The pipeline is:

1. Split on level-2 ``## `` Markdown headings. The leading section before
   the first ``## `` is treated as the implicit "preamble" chunk.
2. Each section that exceeds ``max_chars`` is further split into overlapping
   windows of ``max_chars`` characters with ``overlap_chars`` characters of
   overlap between adjacent windows. The heading line is repeated at the
   top of every sub-chunk so each chunk carries its own context when
   surfaced as a search result.

The chunker is deterministic, dependency-free, and used both for raw
Markdown / session ``.tmp`` files and for the ``patterns.json`` source
(which the caller converts to a Markdown-like body first).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Level-2 heading at the start of a line. We restrict to level 2 because
# the C3 session/report/agent-memory templates standardize on ``## `` for
# the meaningful sections (``## 残タスク`` etc.). Going deeper would
# fragment chunks unnecessarily.
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

DEFAULT_MAX_CHARS = 1000
DEFAULT_OVERLAP_CHARS = 100


@dataclass(frozen=True)
class Chunk:
    """A single chunk ready for embedding.

    Attributes:
        heading: The ``## ...`` heading that contains this chunk, or
            ``""`` for the document preamble (text before the first
            heading).
        content: The chunk text, including the heading line if one was
            present. Sub-chunks of a long section repeat the heading.
        window_index: 0 for the first sub-chunk of a section, 1, 2, ...
            for subsequent overlapping windows.
    """

    heading: str
    content: str
    window_index: int


def chunk_markdown(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[Chunk]:
    """Split ``text`` into chunks suitable for embedding.

    See module docstring for the algorithm. Empty input returns an empty
    list. Whitespace-only sections are dropped.
    """
    if not text or not text.strip():
        return []
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must satisfy 0 <= overlap_chars < max_chars")

    sections = _split_on_headings(text)
    chunks: list[Chunk] = []
    for heading, body in sections:
        if _is_section_empty(heading, body):
            continue
        for window_index, window_text in enumerate(
            _window_text(body, max_chars=max_chars, overlap_chars=overlap_chars)
        ):
            content = _prepend_heading(heading, window_text, window_index)
            chunks.append(Chunk(heading=heading, content=content, window_index=window_index))
    return chunks


def _is_section_empty(heading: str, body: str) -> bool:
    """Return True if ``body`` has no content beyond ``heading`` itself.

    For heading-bearing sections the body starts with the heading line.
    Stripping that line and checking whether what remains is purely
    whitespace lets us drop sections that have a heading but no body
    (e.g. ``## 残タスク`` followed by an empty section).
    """
    if not heading:
        return not body.strip()
    rest = body[len(heading):]
    return not rest.strip()


def _split_on_headings(text: str) -> list[tuple[str, str]]:
    """Return ``[(heading, section_body), ...]`` preserving order.

    The first element's ``heading`` is ``""`` when there is content before
    the first ``## ...`` line. ``section_body`` includes the heading line
    itself for non-empty headings so that the embedded text carries its
    own context.
    """
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [("", text)]

    sections: list[tuple[str, str]] = []
    first_start = matches[0].start()
    if first_start > 0:
        preamble = text[:first_start]
        if preamble.strip():
            sections.append(("", preamble))

    for i, m in enumerate(matches):
        section_start = m.start()
        section_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        heading_text = m.group(0).strip()
        body = text[section_start:section_end]
        sections.append((heading_text, body))
    return sections


def _window_text(
    body: str,
    *,
    max_chars: int,
    overlap_chars: int,
) -> list[str]:
    """Slide a ``max_chars``-wide window over ``body`` with overlap.

    Returns ``[body]`` when ``body`` fits in one window. Otherwise yields
    overlapping windows: window 0 is ``body[:max_chars]``, window 1 is
    ``body[max_chars - overlap_chars : max_chars - overlap_chars + max_chars]``,
    and so on.
    """
    if len(body) <= max_chars:
        return [body]
    step = max_chars - overlap_chars
    windows: list[str] = []
    start = 0
    while start < len(body):
        end = start + max_chars
        windows.append(body[start:end])
        if end >= len(body):
            break
        start += step
    return windows


def _prepend_heading(heading: str, window_text: str, window_index: int) -> str:
    """Ensure non-first sub-chunks repeat the heading for context."""
    if window_index == 0 or not heading:
        return window_text
    # Only prepend if the heading isn't already at the top of the window
    # (it shouldn't be for window_index >= 1 because slicing starts mid-body).
    if window_text.lstrip().startswith(heading):
        return window_text
    return f"{heading}\n\n{window_text}"
