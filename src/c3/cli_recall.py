"""``c3 recall`` - semantic memory search across sessions, reports, and patterns.

Subcommands:

* ``c3 recall search <query>`` — query the HNSW index for relevant chunks.
* ``c3 recall rebuild`` — re-embed every source file and rewrite the index.
* ``c3 recall stats`` — report what's in the index.

The heavy dependencies (``fastembed``, ``hnswlib``) are imported lazily
inside the handlers so that ``c3 --help`` and unrelated subcommands stay
fast even when the embedding model has not yet been downloaded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from c3.embedding import Embedder

from c3._terminal import sanitize_terminal_text, supports_color
from c3.paths import claude_root_for
from c3.recall_index import (
    ChunkRecord,
    RecallIndex,
    SourceChunk,
    collect_sources,
    default_index_paths,
    snippet_of,
    warn_if_stale,
)

_SOURCE_CHOICES = ("all", "sessions", "agent-memory", "reports", "patterns")
_DEFAULT_TOP = 5
# Practical threshold determined by E2E testing on the C3 distribution
# repository: 0.3 surfaces useful matches (e.g. similar incident reports
# with score ~0.43-0.63) without producing noise. Users can raise it
# with ``--min-score 0.5`` when they only want strong matches.
_DEFAULT_MIN_SCORE = 0.3

# ANSI SGR — guarded with supports_color()
_ANSI_BOLD = "\033[1m"
_ANSI_DIM = "\033[2m"
_ANSI_RESET = "\033[0m"


def _add_target_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--target",
        type=Path,
        default=None,
        help="Project root containing .claude/ (default: walk up from cwd)",
    )


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "recall",
        help="Search semantic memory across sessions, reports, and patterns",
        description=(
            "Semantic search over .claude/memory/sessions, .claude/agent-memory, "
            ".claude/reports/archive, and .claude/memory/patterns.json. "
            "Run `c3 recall rebuild` once before the first search."
        ),
    )
    recall_sub = parser.add_subparsers(dest="recall_command", metavar="<subcommand>")
    recall_sub.required = True

    p_search = recall_sub.add_parser("search", help="Run a semantic search")
    p_search.add_argument("query", help="The natural-language query (Japanese OK)")
    p_search.add_argument(
        "--top",
        type=int,
        default=_DEFAULT_TOP,
        help=f"Maximum number of hits to return (default: {_DEFAULT_TOP})",
    )
    p_search.add_argument(
        "--source",
        choices=_SOURCE_CHOICES,
        default="all",
        help="Restrict to a single source kind (default: all)",
    )
    p_search.add_argument(
        "--min-score",
        type=float,
        default=_DEFAULT_MIN_SCORE,
        help=(
            "Drop hits with similarity below this threshold "
            f"(default: {_DEFAULT_MIN_SCORE}). Raise to 0.5 to only see "
            "strong matches; pass 0 to disable."
        ),
    )
    p_search.add_argument(
        "--json",
        action="store_true",
        help="Print results as machine-readable JSON",
    )
    _add_target_arg(p_search)

    p_rebuild = recall_sub.add_parser("rebuild", help="Rebuild the index from disk")
    p_rebuild.add_argument(
        "--force",
        action="store_true",
        # CR-M-06: clarify that rebuild always processes all sources in the
        # current implementation (no incremental / partial update support).
        help=(
            "Force overwrite of the existing index files "
            "(rebuild always processes all sources in the current implementation)"
        ),
    )
    p_rebuild.add_argument(
        "--source",
        choices=_SOURCE_CHOICES,
        default="all",
        help="Restrict rebuild to a single source kind (default: all)",
    )
    _add_target_arg(p_rebuild)

    p_stats = recall_sub.add_parser("stats", help="Show index statistics")
    p_stats.add_argument(
        "--json",
        action="store_true",
        help="Print stats as machine-readable JSON",
    )
    _add_target_arg(p_stats)

    parser.set_defaults(handler=handle, kind="recall")


def handle(args: argparse.Namespace) -> int:
    if args.recall_command == "search":
        return _handle_search(args)
    if args.recall_command == "rebuild":
        return _handle_rebuild(args)
    if args.recall_command == "stats":
        return _handle_stats(args)
    print(f"c3 recall: unknown subcommand {args.recall_command!r}", file=sys.stderr)
    return 2


# ----- handlers -----


def _handle_search(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(getattr(args, "target", None))
    if repo_root is None:
        print(
            "c3 recall: no .claude/ found in this directory or its ancestors",
            file=sys.stderr,
        )
        return 1

    index_path, meta_path = default_index_paths(repo_root)
    if not meta_path.exists() or not index_path.exists():
        print(
            "c3 recall: index not found. Run `c3 recall rebuild` first.",
            file=sys.stderr,
        )
        return 1

    embedder = _build_embedder_or_report_error()
    if embedder is None:
        return 1

    index = RecallIndex(
        index_path=index_path,
        meta_path=meta_path,
        model_name=embedder.model_name,
        dim=embedder.dim,
    )
    try:
        if not index.load():
            print("c3 recall: failed to load index", file=sys.stderr)
            return 1
    except RuntimeError as exc:
        print(f"c3 recall: {exc}", file=sys.stderr)
        return 1

    warn_if_stale(repo_root, index_path)

    query_vec = embedder.embed_query(args.query)
    raw_hits = index.search(query_vec, top_k=max(args.top, 1) * 3)

    selected = _filter_hits(
        raw_hits,
        source_filter=args.source,
        min_score=args.min_score,
        top=args.top,
    )

    if args.json:
        _print_search_json(args.query, selected)
    else:
        _print_search_human(args.query, selected)
    return 0


def _handle_rebuild(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(getattr(args, "target", None))
    if repo_root is None:
        print(
            "c3 recall: no .claude/ found in this directory or its ancestors",
            file=sys.stderr,
        )
        return 1

    embedder = _build_embedder_or_report_error()
    if embedder is None:
        return 1

    sources = None if args.source == "all" else [args.source]
    chunks = list(collect_sources(repo_root, sources=sources))

    if not chunks:
        # CR-H-03: abort early — writing an empty index is not useful and
        # can mask misconfigured source paths.
        print(
            "c3 recall: no source files found to index. Aborting.",
            file=sys.stderr,
        )
        return 1

    print(f"[recall] embedding {len(chunks)} chunks...")
    vectors = embedder.embed_passages([c.content for c in chunks]) if chunks else []
    items: list[tuple[ChunkRecord, list[float]]] = []
    for src, vec in zip(chunks, vectors):
        # CR-M-04 / SR-L-5: compute source_hash from the full content so that
        # identical chunks in different files produce the same hash and changed
        # content is reliably detected across rebuilds.
        content_hash = hashlib.sha256(
            src.content.encode("utf-8", errors="replace")
        ).hexdigest()
        record = ChunkRecord(
            source_type=src.source_type,
            path=src.path,
            chunk_id=src.chunk_id,
            snippet=snippet_of(src.content),
            mtime=src.mtime,
            source_hash=content_hash,
        )
        items.append((record, vec))

    index_path, meta_path = default_index_paths(repo_root)

    if args.force:
        for p in (index_path, meta_path):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

    index = RecallIndex(
        index_path=index_path,
        meta_path=meta_path,
        model_name=embedder.model_name,
        dim=embedder.dim,
    )
    index.build(items)
    index.save()

    print(
        f"[recall] wrote {len(items)} chunks to {index_path.relative_to(repo_root).as_posix()}"
    )
    return 0


def _handle_stats(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(getattr(args, "target", None))
    if repo_root is None:
        print(
            "c3 recall: no .claude/ found in this directory or its ancestors",
            file=sys.stderr,
        )
        return 1

    index_path, meta_path = default_index_paths(repo_root)
    if not meta_path.exists():
        print("c3 recall: index not built yet. Run `c3 recall rebuild`.", file=sys.stderr)
        return 1

    # Read meta directly so `stats` does not require fastembed to be importable.
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"c3 recall: cannot read {meta_path}: {exc}", file=sys.stderr)
        return 1

    chunks = payload.get("chunks", {})
    by_source: dict[str, int] = {}
    for rec in chunks.values():
        st = rec.get("source_type", "unknown")
        by_source[st] = by_source.get(st, 0) + 1

    stats = {
        "total_chunks": len(chunks),
        "by_source": by_source,
        "model": payload.get("model"),
        "dim": payload.get("dim"),
        "created_at": payload.get("created_at"),
        "rebuilt_at": payload.get("rebuilt_at"),
        "index_path": str(index_path),
        "index_size_bytes": (
            index_path.stat().st_size if index_path.exists() else 0
        ),
    }

    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    _print_stats_human(stats)
    return 0


# ----- helpers -----


def _resolve_repo_root(target: Path | None = None) -> Path | None:
    start = (target or Path.cwd()).resolve()
    return claude_root_for(start)


def _build_embedder_or_report_error() -> "Embedder | None":
    """Instantiate FastEmbedBackend, returning ``None`` on import failure.

    Two separate try/except blocks are intentional:

    - First block: catches ``ImportError`` from ``from c3.embedding import
      FastEmbedBackend``.  This covers environments where the c3 package
      itself is partially installed or the module has a syntax error.
    - Second block: catches ``ImportError`` from ``FastEmbedBackend()``
      constructor, which triggers the lazy ``import fastembed`` inside the
      backend.  This gives a user-friendly hint to run
      ``pip install fastembed`` instead of a raw ``ModuleNotFoundError``.
      Other exceptions (network errors, model download failures) are caught
      broadly and surfaced as-is.

    CR-M-01: return type annotation added.
    CR-L-03: docstring restructured to clarify the two-block design.
    """
    try:
        from c3.embedding import FastEmbedBackend  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - defensive
        print(f"c3 recall: could not import embedding backend: {exc}", file=sys.stderr)
        return None
    try:
        return FastEmbedBackend()
    except ImportError as exc:
        print(
            "c3 recall: missing dependency 'fastembed'. Install with"
            " `pip install fastembed`.",
            file=sys.stderr,
        )
        print(f"  detail: {exc}", file=sys.stderr)
        return None
    except Exception as exc:  # pragma: no cover - network / model errors
        print(f"c3 recall: failed to load embedding model: {exc}", file=sys.stderr)
        return None


def _source_matches(record: ChunkRecord, source_filter: str) -> bool:
    if source_filter == "all":
        return True
    plural_to_singular = {
        "sessions": "session",
        "reports": "report",
        "patterns": "pattern",
        "agent-memory": "agent-memory",
    }
    target = plural_to_singular.get(source_filter, source_filter)
    return record.source_type == target


def _filter_hits(
    raw_hits: Sequence[tuple[int, float, ChunkRecord]],
    *,
    source_filter: str,
    min_score: float,
    top: int,
) -> list[dict[str, Any]]:
    # CR-L-04: return type narrowed to list[dict[str, Any]].
    # Future: replace with a TypedDict (HitResult) once the schema stabilises.
    selected: list[dict[str, Any]] = []
    for chunk_id, distance, record in raw_hits:
        if not _source_matches(record, source_filter):
            continue
        score = 1.0 - distance
        if score < min_score:
            continue
        selected.append(
            {
                "chunk_id": chunk_id,
                "score": round(score, 4),
                "distance": round(distance, 4),
                "source_type": record.source_type,
                "path": record.path,
                "chunk_label": record.chunk_id,
                "snippet": sanitize_terminal_text(record.snippet),
            }
        )
        if len(selected) >= top:
            break
    return selected


def _print_search_human(query: str, hits: Sequence[dict]) -> None:
    color = supports_color()
    if not hits:
        print(f"No matches for {query!r}.")
        return
    print(f"Top {len(hits)} match(es) for {query!r}:\n")
    for i, hit in enumerate(hits, start=1):
        header = (
            f"[{i}] score={hit['score']:.3f}  {hit['source_type']}  {hit['path']}"
        )
        if color:
            header = f"{_ANSI_BOLD}{header}{_ANSI_RESET}"
        print(header)
        label = hit["chunk_label"]
        if label:
            label_line = f"    {label}"
            print(f"{_ANSI_DIM}{label_line}{_ANSI_RESET}" if color else label_line)
        snippet = (hit["snippet"] or "").replace("\n", "\n    ")
        print(f"    {snippet}\n")


def _print_search_json(query: str, hits: Sequence[dict]) -> None:
    print(json.dumps({"query": query, "hits": list(hits)}, ensure_ascii=False, indent=2))


_STATS_TITLE = "Recall Index Statistics"


def _print_stats_human(stats: dict) -> None:
    color = supports_color()
    # CR-M-07: derive separator length from the plain title so they stay
    # in sync if the title text is ever updated.
    title = _STATS_TITLE
    if color:
        title = f"{_ANSI_BOLD}{title}{_ANSI_RESET}"
    print(title)
    print("=" * len(_STATS_TITLE))
    print(f"Total chunks: {stats['total_chunks']}")
    print("By source:")
    for source in ("session", "agent-memory", "report", "pattern"):
        count = stats["by_source"].get(source, 0)
        pct = (
            f"{(count / stats['total_chunks']) * 100:.0f}%"
            if stats["total_chunks"]
            else "0%"
        )
        print(f"  {source:13s}: {count:5d} chunks ({pct})")
    size_mb = stats["index_size_bytes"] / (1024 * 1024) if stats["index_size_bytes"] else 0.0
    print(f"Index file: {stats['index_path']} ({size_mb:.2f} MB)")
    print(f"Last rebuild: {stats.get('rebuilt_at') or '-'}")
    print(
        f"Embedding model: {stats.get('model')} ({stats.get('dim')}d)"
    )
