"""HNSW index management for the ``c3 recall`` feature.

The module owns three concerns:

1. :class:`RecallIndex` — a thin wrapper around ``hnswlib.Index`` that
   tracks per-chunk metadata in ``recall_meta.json``. All on-disk writes
   are atomic (tempfile + ``os.replace``) so an interrupted save leaves
   the previous good index intact.
2. :func:`collect_sources` — walks the C3 ``.claude/`` directory and
   yields :class:`SourceChunk` objects ready to be embedded.
3. :func:`is_stale` — compares the newest source mtime against the
   index mtime so the CLI can warn the user to rebuild.

The HNSW knobs (``M``, ``ef_construction``, ``ef``) match the values
recommended in the feature design doc (``§3.1`` and ``§5.1``).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from c3.recall_chunker import chunk_markdown

# HNSW build / query tuning. See design doc §3.1 and §5.1.
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 200
HNSW_EF_QUERY = 50
HNSW_SPACE = "cosine"

# Snippet length surfaced in search results / stored in recall_meta.
SNIPPET_CHARS = 300

# Source kinds. ``"all"`` is the CLI convenience alias and never appears
# in stored metadata.
SOURCE_TYPES = ("session", "agent-memory", "report", "pattern")


@dataclass(frozen=True)
class SourceChunk:
    """A chunk + provenance ready to be embedded."""

    source_type: str
    path: str  # POSIX, repo-relative
    chunk_id: str  # heading + window index (or "pattern:<id>")
    content: str  # the text to embed
    mtime: float  # source file mtime (best-effort)


@dataclass
class ChunkRecord:
    """Persisted per-chunk metadata stored in ``recall_meta.json``."""

    source_type: str
    path: str
    chunk_id: str
    snippet: str
    mtime: float
    source_hash: str = ""

    def __post_init__(self) -> None:
        # SR-L-2: cap snippet at 1000 chars to bound memory and storage.
        if len(self.snippet) > 1000:
            self.snippet = self.snippet[:1000]


@dataclass
class IndexMeta:
    """The full on-disk metadata document."""

    model: str
    dim: int
    created_at: str
    rebuilt_at: str
    next_id: int = 0
    chunks: dict[str, ChunkRecord] = field(default_factory=dict)

    @classmethod
    def empty(cls, *, model: str, dim: int) -> "IndexMeta":
        now = _utcnow_iso()
        return cls(model=model, dim=dim, created_at=now, rebuilt_at=now)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "dim": self.dim,
            "created_at": self.created_at,
            "rebuilt_at": self.rebuilt_at,
            "next_id": self.next_id,
            "chunks": {k: asdict(v) for k, v in self.chunks.items()},
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "IndexMeta":
        chunks_raw = payload.get("chunks", {})
        chunks = {k: ChunkRecord(**v) for k, v in chunks_raw.items()}
        return cls(
            model=payload["model"],
            dim=int(payload["dim"]),
            created_at=payload["created_at"],
            rebuilt_at=payload["rebuilt_at"],
            next_id=int(payload.get("next_id", len(chunks))),
            chunks=chunks,
        )


class RecallIndex:
    """HNSW vector index + JSON metadata, both persisted to ``.claude/state/``."""

    def __init__(
        self,
        *,
        index_path: Path,
        meta_path: Path,
        model_name: str,
        dim: int,
    ) -> None:
        self.index_path = Path(index_path)
        self.meta_path = Path(meta_path)
        self.model_name = model_name
        self.dim = dim
        self._index = None  # populated lazily
        self._meta = IndexMeta.empty(model=model_name, dim=dim)

    # ----- lifecycle -----

    def build(self, items: Sequence[tuple[ChunkRecord, list[float]]]) -> None:
        """Discard any existing index and rebuild from ``items`` in one shot.

        ``items`` is a sequence of ``(record, vector)`` pairs. IDs are
        assigned sequentially starting from 0.
        """
        if not items:
            self._reset_meta()
            self._index = self._new_index(max_elements=max(1, len(items)))
            return

        if any(len(v) != self.dim for _, v in items):
            raise ValueError(f"all vectors must have dim={self.dim}")

        self._reset_meta()
        self._index = self._new_index(max_elements=len(items))
        ids: list[int] = []
        vecs: list[list[float]] = []
        for record, vec in items:
            new_id = self._meta.next_id
            self._meta.next_id += 1
            ids.append(new_id)
            vecs.append(vec)
            # CR-M-04 / SR-L-5: ensure source_hash is populated.
            # If the caller already computed a hash (e.g. from full content),
            # keep it; otherwise derive from the stored snippet as a fallback.
            if not record.source_hash:
                record.source_hash = hashlib.sha256(
                    record.snippet.encode("utf-8", errors="replace")
                ).hexdigest()
            self._meta.chunks[str(new_id)] = record
        self._index.add_items(vecs, ids)
        self._meta.rebuilt_at = _utcnow_iso()

    def search(
        self,
        query_vector: list[float],
        *,
        top_k: int = 5,
    ) -> list[tuple[int, float, ChunkRecord]]:
        """Run k-NN search. Returns ``[(chunk_id, distance, record), ...]``.

        ``distance`` is the cosine distance reported by hnswlib (smaller =
        more similar). Callers convert to a ``score`` via ``1 - distance``.
        """
        if self._index is None or not self._meta.chunks:
            return []
        if len(query_vector) != self.dim:
            raise ValueError(f"query_vector must have dim={self.dim}")

        k = min(top_k, self._index.get_current_count())
        if k <= 0:
            return []
        labels, distances = self._index.knn_query([query_vector], k=k)
        results: list[tuple[int, float, ChunkRecord]] = []
        for label, dist in zip(labels[0], distances[0]):
            record = self._meta.chunks.get(str(int(label)))
            if record is None:
                continue
            results.append((int(label), float(dist), record))
        return results

    def stats(self) -> dict:
        """Return summary counts for ``c3 recall stats``."""
        by_source: dict[str, int] = {s: 0 for s in SOURCE_TYPES}
        for rec in self._meta.chunks.values():
            by_source[rec.source_type] = by_source.get(rec.source_type, 0) + 1
        total = sum(by_source.values())
        return {
            "total_chunks": total,
            "by_source": by_source,
            "model": self._meta.model,
            "dim": self._meta.dim,
            "created_at": self._meta.created_at,
            "rebuilt_at": self._meta.rebuilt_at,
            "index_path": str(self.index_path),
            "index_size_bytes": (
                self.index_path.stat().st_size if self.index_path.exists() else 0
            ),
        }

    # ----- persistence -----

    def save(self) -> None:
        """Atomically write ``recall.hnsw`` and ``recall_meta.json``.

        The HNSW file is written via ``hnswlib.save_index`` to a sibling
        ``.tmp`` path and then ``os.replace``'d over the canonical name,
        keeping the previous file as ``.bak``. The metadata JSON follows
        the same pattern.
        """
        if self._index is None:
            raise RuntimeError("nothing to save; call build() first")
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)

        index_tmp = self.index_path.with_suffix(self.index_path.suffix + ".tmp")
        self._index.save_index(str(index_tmp))
        _atomic_replace(index_tmp, self.index_path)

        meta_tmp = self.meta_path.with_suffix(self.meta_path.suffix + ".tmp")
        meta_tmp.write_text(
            json.dumps(self._meta.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _atomic_replace(meta_tmp, self.meta_path)

    def load(self) -> bool:
        """Load index + metadata from disk. Return ``False`` if not present."""
        if not self.meta_path.exists() or not self.index_path.exists():
            return False
        payload = json.loads(self.meta_path.read_text(encoding="utf-8"))
        # SR-M-2: wrap deserialization errors so callers see a clear message
        # instead of an unguarded TypeError / KeyError stack trace.
        try:
            self._meta = IndexMeta.from_dict(payload)
        except (TypeError, KeyError, ValueError) as exc:
            raise RuntimeError(
                f"recall_meta.json is corrupt or incompatible: {exc}; "
                "run `c3 recall rebuild --force`"
            ) from exc
        if self._meta.dim != self.dim:
            raise RuntimeError(
                f"on-disk index dim={self._meta.dim} does not match expected dim={self.dim};"
                " run `c3 recall rebuild --force`"
            )
        if self._meta.model != self.model_name:
            raise RuntimeError(
                f"on-disk index model={self._meta.model!r} does not match expected"
                f" model={self.model_name!r}; run `c3 recall rebuild --force`"
            )
        max_elements = max(1, len(self._meta.chunks))
        # Construct a bare Index *without* init_index so that load_index
        # does not log "Calling load_index for an already inited index"
        # to stderr. hnswlib infers M / ef_construction from the saved
        # file, so the only knob we need afterwards is the query ef.
        import hnswlib  # noqa: PLC0415 — lazy so unit tests can mock

        self._index = hnswlib.Index(space=HNSW_SPACE, dim=self.dim)
        self._index.load_index(str(self.index_path), max_elements=max_elements)
        self._index.set_ef(HNSW_EF_QUERY)
        return True

    # ----- accessors used by tests / CLI -----

    @property
    def meta(self) -> IndexMeta:
        return self._meta

    def chunk_count(self) -> int:
        return len(self._meta.chunks)

    # ----- internals -----

    def _new_index(self, *, max_elements: int):
        import hnswlib  # noqa: PLC0415 — lazy so unit tests can mock

        index = hnswlib.Index(space=HNSW_SPACE, dim=self.dim)
        index.init_index(
            max_elements=max_elements,
            ef_construction=HNSW_EF_CONSTRUCTION,
            M=HNSW_M,
        )
        index.set_ef(HNSW_EF_QUERY)
        return index

    def _reset_meta(self) -> None:
        now = _utcnow_iso()
        self._meta = IndexMeta(
            model=self.model_name,
            dim=self.dim,
            created_at=now,
            rebuilt_at=now,
            next_id=0,
            chunks={},
        )


# ----- source collection -----


def collect_sources(
    repo_root: Path,
    *,
    sources: Sequence[str] | None = None,
) -> Iterator[SourceChunk]:
    """Yield :class:`SourceChunk` instances for every relevant file.

    ``sources`` filters which kinds to include. ``None`` (or ``"all"`` in
    the sequence) means everything.
    """
    selected = _normalize_sources(sources)
    if "session" in selected:
        yield from _collect_markdown_glob(
            repo_root,
            ".claude/memory/sessions",
            "*.tmp",
            source_type="session",
        )
    if "agent-memory" in selected:
        yield from _collect_markdown_glob(
            repo_root,
            ".claude/agent-memory",
            "**/*.md",
            source_type="agent-memory",
        )
    if "report" in selected:
        yield from _collect_markdown_glob(
            repo_root,
            ".claude/reports/archive",
            "*.md",
            source_type="report",
        )
    if "pattern" in selected:
        yield from _collect_patterns_json(
            repo_root / ".claude" / "memory" / "patterns.json",
            repo_root=repo_root,
        )


def _normalize_sources(sources: Sequence[str] | None) -> set[str]:
    if not sources:
        return set(SOURCE_TYPES)
    selected: set[str] = set()
    for s in sources:
        if s == "all":
            return set(SOURCE_TYPES)
        if s == "agent_memory":  # tolerate either spelling
            selected.add("agent-memory")
            continue
        if s == "sessions":
            selected.add("session")
            continue
        if s == "reports":
            selected.add("report")
            continue
        if s == "patterns":
            selected.add("pattern")
            continue
        if s in SOURCE_TYPES:
            selected.add(s)
    return selected


def _collect_markdown_glob(
    repo_root: Path,
    rel_dir: str,
    pattern: str,
    *,
    source_type: str,
) -> Iterator[SourceChunk]:
    base = repo_root / rel_dir
    if not base.is_dir():
        return
    for path in sorted(base.glob(pattern)):
        # SR-L-4: skip symlinks to avoid indexing duplicate content or
        # paths that escape the repo boundary.
        if not path.is_file() or path.name == ".gitkeep" or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        mtime = path.stat().st_mtime
        rel = path.relative_to(repo_root).as_posix()
        for chunk in chunk_markdown(text):
            chunk_id = f"{chunk.heading or 'preamble'}#{chunk.window_index}"
            yield SourceChunk(
                source_type=source_type,
                path=rel,
                chunk_id=chunk_id,
                content=chunk.content,
                mtime=mtime,
            )


def _collect_patterns_json(
    patterns_path: Path,
    *,
    repo_root: Path,
) -> Iterator[SourceChunk]:
    if not patterns_path.is_file():
        return
    try:
        payload = json.loads(patterns_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    patterns = payload.get("patterns", []) if isinstance(payload, dict) else []
    if not isinstance(patterns, list):
        return
    mtime = patterns_path.stat().st_mtime
    rel = patterns_path.relative_to(repo_root).as_posix()
    for entry in patterns:
        if not isinstance(entry, dict):
            continue
        description = entry.get("description")
        pattern_id = entry.get("id") or "<no-id>"
        if not description:
            continue
        yield SourceChunk(
            source_type="pattern",
            path=rel,
            chunk_id=f"pattern:{pattern_id}",
            content=str(description),
            mtime=mtime,
        )


# ----- stale detection -----


def latest_source_mtime(repo_root: Path) -> float:
    """Best-effort newest mtime across every recall source.

    Returns ``0.0`` when no source files exist.
    """
    latest = 0.0
    for chunk in collect_sources(repo_root):
        if chunk.mtime > latest:
            latest = chunk.mtime
    return latest


def is_stale(repo_root: Path, index_path: Path) -> bool:
    """Return True if the newest source file is newer than the index."""
    if not index_path.exists():
        return True
    index_mtime = index_path.stat().st_mtime
    return latest_source_mtime(repo_root) > index_mtime


# ----- helpers -----


def snippet_of(text: str, *, max_chars: int = SNIPPET_CHARS) -> str:
    """Return a stable preview of ``text`` for storage / display."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def _atomic_replace(src: Path, dst: Path) -> None:
    """``os.replace`` with a ``.bak`` rollover of the previous file."""
    if dst.exists():
        bak = dst.with_suffix(dst.suffix + ".bak")
        # Drop any prior .bak so the rename succeeds on Windows.
        if bak.exists():
            try:
                bak.unlink()
            except OSError:
                pass
        try:
            os.replace(dst, bak)
        except OSError:
            pass
    os.replace(src, dst)


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def default_index_paths(repo_root: Path) -> tuple[Path, Path]:
    """Return ``(index_path, meta_path)`` rooted at ``repo_root``."""
    state = repo_root / ".claude" / "state"
    return state / "recall.hnsw", state / "recall_meta.json"


def warn_if_stale(repo_root: Path, index_path: Path) -> None:
    """Emit a stderr warning when the index is older than its sources.

    Used by the CLI search path so users see a hint without blocking the
    query. Silent if everything is up-to-date or there is no index yet.
    """
    if not index_path.exists():
        return
    if is_stale(repo_root, index_path):
        print(
            "[recall] WARN: index is older than at least one source file."
            " Run `c3 recall rebuild` to refresh.",
            file=sys.stderr,
        )
