"""numpy ベクトル索引（cosine ブルートフォース）による ``c3 recall`` 機能の索引管理。

歴史的経緯で索引ファイルは ``.hnsw`` 拡張子を持つが、
中身は numpy の ``.npy`` ペイロード（ndarray shape (N, dim) float32）である。
hnswlib から numpy ブルートフォース検索へ移行した際にファイル名を変えると
配布先の hooks（recall_inject / recall_autorebuild）が無改修で動かなくなるため、
拡張子のみ維持してペイロードだけ numpy に切り替えている。

モジュールが担う 3 つの責務:

1. :class:`RecallIndex` — numpy ndarray による cosine ブルートフォース検索と、
   ``recall_meta.json`` へのメタデータ永続化。
   全オンディスク書き込みはアトミック（tempfile + ``os.replace``）なので、
   中断があっても直前の正常なインデックスが残る。
2. :func:`collect_sources` — C3 の ``.claude/`` ディレクトリを走査し
   :class:`SourceChunk` オブジェクトを返す。
3. :func:`is_stale` — 最新ソースの mtime とインデックスの mtime を比較し、
   CLI が再構築を促すかどうかを判断する。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

import numpy as np

from c3.recall_chunker import chunk_markdown

# Snippet length surfaced in search results / stored in recall_meta.
SNIPPET_CHARS = 300

# Source kinds. ``"all"`` is the CLI convenience alias and never appears
# in stored metadata.
SOURCE_TYPES = ("session", "agent-memory", "report", "pattern")

# Small epsilon used to avoid division-by-zero when normalising zero vectors.
_NORM_EPS = 1e-10


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
    """numpy ブルートフォース cosine 索引 + JSON メタデータ。

    索引ファイルは ``.claude/state/recall.hnsw``（拡張子は歴史的経緯で維持）に
    numpy ndarray (N, dim) float32 として保存される。検索時は query と各行を
    L2 正規化して cosine 類似度 = dot を計算し、``distance = 1 - similarity``
    を返す。
    """

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
        self._vectors: np.ndarray | None = None  # shape (N, dim) float32; None before build/load
        self._row_norms: np.ndarray | None = None  # shape (N,) float32; cached L2 norms
        self._meta = IndexMeta.empty(model=model_name, dim=dim)

    # ----- lifecycle -----

    def build(self, items: Sequence[tuple[ChunkRecord, list[float]]]) -> None:
        """Discard any existing index and rebuild from ``items`` in one shot.

        ``items`` is a sequence of ``(record, vector)`` pairs. IDs are
        assigned sequentially starting from 0 (row index = chunk id).
        Vectors are stored as-is (not normalised) to preserve fidelity
        for :meth:`get_vector`.
        """
        self._reset_meta()

        if not items:
            # 空索引: _vectors を空の (0, dim) 配列にして「ビルド済み」状態を表す。
            # search() は _meta.chunks が空であることで [] を返す。
            self._vectors = np.empty((0, self.dim), dtype=np.float32)
            self._row_norms = np.empty((0,), dtype=np.float32)
            return

        if any(len(v) != self.dim for _, v in items):
            raise ValueError(f"all vectors must have dim={self.dim}")

        vecs: list[list[float]] = []
        for record, vec in items:
            new_id = self._meta.next_id
            self._meta.next_id += 1
            vecs.append(vec)
            # CR-M-04 / SR-L-5: ensure source_hash is populated.
            if not record.source_hash:
                record.source_hash = content_hash(record.snippet)
            self._meta.chunks[str(new_id)] = record

        self._vectors = np.asarray(vecs, dtype=np.float32)
        self._row_norms = self._compute_norms(self._vectors)
        self._meta.rebuilt_at = _utcnow_iso()

    def search(
        self,
        query_vector: list[float],
        *,
        top_k: int = 5,
    ) -> list[tuple[int, float, ChunkRecord]]:
        """cosine ブルートフォース検索。``[(chunk_id, distance, record), ...]`` を返す。

        ``distance = 1 - cosine_similarity``（小さいほど類似）。
        呼び出し元は ``score = 1 - distance`` で類似度スコアに変換する
        (``cli_recall.py:516`` の ``score = 1.0 - distance`` と整合)。
        """
        if self._vectors is None or not self._meta.chunks:
            return []
        if len(query_vector) != self.dim:
            raise ValueError(f"query_vector must have dim={self.dim}")
        if self._vectors.shape[0] == 0:
            return []

        q = np.asarray(query_vector, dtype=np.float32)
        qnorm = float(np.linalg.norm(q))
        if qnorm < _NORM_EPS:
            # ゼロベクトルのクエリ: 全距離を 1.0 とみなして先頭 top_k を返す
            n = min(top_k, self._vectors.shape[0])
            results: list[tuple[int, float, ChunkRecord]] = []
            for row in range(n):
                record = self._meta.chunks.get(str(row))
                if record is not None:
                    results.append((row, 1.0, record))
            return results

        q = q / qnorm

        # cosine_sim = (M @ q) / row_norms  (ゼロノルム行は eps ガード)
        assert self._row_norms is not None
        safe_norms = np.where(self._row_norms < _NORM_EPS, _NORM_EPS, self._row_norms)
        sims = (self._vectors @ q) / safe_norms  # shape (N,)

        k = min(top_k, self._vectors.shape[0])
        if k <= 0:
            return []

        # np.argpartition で top_k 候補を取り出してから sim 降順ソート
        if k < sims.shape[0]:
            # argpartition は「上位 k 個」ではなく「最大 k 番目より大きい要素」を前半に集める
            part_idx = np.argpartition(sims, -k)[-k:]
        else:
            part_idx = np.arange(sims.shape[0])

        # sim 降順でソート
        sorted_idx = part_idx[np.argsort(sims[part_idx])[::-1]]

        results = []
        for row in sorted_idx:
            row_int = int(row)
            record = self._meta.chunks.get(str(row_int))
            if record is None:
                continue
            dist = float(1.0 - sims[row_int])
            results.append((row_int, dist, record))
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
        """``recall.hnsw`` と ``recall_meta.json`` をアトミックに書き込む。

        索引はファイルオブジェクト経由の ``np.save`` で書く（パス文字列を渡すと
        ``.npy`` が付与されるため・Windows 非 ASCII パス対策も兼ねる）。
        ``.tmp`` へ書いたあと ``_atomic_replace`` で確定し、直前のファイルを
        ``.bak`` として残す。
        """
        if self._vectors is None:
            raise RuntimeError("nothing to save; call build() first")
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)

        # [SR L-1] tmp パスを {name}.{pid}.{uuid4.hex}.tmp に一意化してプロセス間競合を防ぐ。
        # cli_update._save_version_checkpoint と同じパターン。
        _pid = os.getpid()
        _uid = uuid.uuid4().hex
        index_tmp = self.index_path.with_name(
            f"{self.index_path.name}.{_pid}.{_uid}.tmp"
        )
        # CR-E-002: clean up index_tmp if np.save raises.
        try:
            with open(index_tmp, "wb") as f:
                np.save(f, self._vectors)
        except Exception:
            try:
                index_tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        _atomic_replace(index_tmp, self.index_path)

        meta_tmp = self.meta_path.with_name(
            f"{self.meta_path.name}.{_pid}.{_uid}.tmp"
        )
        meta_tmp.write_text(
            json.dumps(self._meta.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _atomic_replace(meta_tmp, self.meta_path)

    def load(self) -> bool:
        """索引 + メタデータをディスクから読み込む。不在の場合 ``False`` を返す。

        ``np.load`` が失敗した場合（旧 hnswlib バイナリ等・形式不一致）や、
        読み込んだ ndarray の形状が不正な場合（ndim != 2 または shape[1] != dim）は
        例外を投げず ``False`` を返し、内部状態 (``_vectors`` / ``_row_norms`` / ``_meta``)
        をリセットする。``cli_recall`` の全再構築フォールバックが自己修復として機能する。
        dim / model 不一致は従来どおり ``RuntimeError`` を送出する（corrupt meta も同様）。
        失敗時は ``sys.stderr`` に簡潔な失敗理由（例外型名またはシェイプ情報）を出力する。
        内部パスは出力しない（SR-R-001 準拠）。
        """
        if not self.meta_path.exists() or not self.index_path.exists():
            return False
        payload = json.loads(self.meta_path.read_text(encoding="utf-8"))
        # SR-M-2: wrap deserialization errors so callers see a clear message.
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
        # [M-02/SR M-2] np.load に allow_pickle=False を明示（セキュリティ上の理由）。
        # numpy 1.16.3 以降デフォルト False だが、将来の変更に対して明示で固定する。
        # [M-02] except は (ValueError, OSError, EOFError) に絞り想定外例外は伝播させる。
        # numpy の AxisError は ValueError のサブクラス（MRO: ValueError, IndexError）
        # なので ValueError で捕捉される。MemoryError 等想定外は伝播してよい。
        try:
            with open(self.index_path, "rb") as f:
                self._vectors = np.load(f, allow_pickle=False)
        except (ValueError, OSError, EOFError) as exc:
            # [SR L-2] 失敗理由を stderr に出す。内部パスは出さない（SR-R-001）。
            print(
                f"[recall] index load failed ({type(exc).__name__}), will rebuild",
                file=sys.stderr,
            )
            # [L-04/L-05] _vectors / _row_norms / _meta を対称的にリセットする。
            self._vectors = None
            self._row_norms = None
            self._meta = IndexMeta.empty(model=self.model_name, dim=self.dim)
            return False

        # [H-01/SR M-1] 形状バリデーション: ndim==2 かつ shape[1]==dim であること。
        # np.load が成功しても 1D/0D/3D や列数不一致の ndarray は不正なインデックス。
        # _compute_norms を try 外で呼ぶと AxisError (ValueError のサブクラス) が
        # 呼び出し元に伝播してクラッシュするため、ここでガードする。
        if self._vectors.ndim != 2 or self._vectors.shape[1] != self.dim:
            shape_info = f"ndim={self._vectors.ndim}, shape={self._vectors.shape}"
            print(
                f"[recall] index load failed (shape mismatch: {shape_info},"
                f" expected (N, {self.dim})), will rebuild",
                file=sys.stderr,
            )
            # [L-04/L-05] 失敗時も内部状態を対称的にリセットする。
            self._vectors = None
            self._row_norms = None
            self._meta = IndexMeta.empty(model=self.model_name, dim=self.dim)
            return False

        self._row_norms = self._compute_norms(self._vectors)
        return True

    # ----- accessors used by tests / CLI -----

    @property
    def meta(self) -> IndexMeta:
        return self._meta

    def chunk_count(self) -> int:
        return len(self._meta.chunks)

    def get_vector(self, chunk_id: int) -> list[float]:
        """``chunk_id`` に対応する生ベクトルを ``list[float]`` で返す。

        未ビルド/未ロード時は ``RuntimeError``、
        範囲外 id は ``IndexError`` を送出する。
        """
        if self._vectors is None:
            raise RuntimeError(
                "index has not been built or loaded; call build() or load() first"
            )
        # numpy の IndexError をそのまま伝播させる
        return self._vectors[chunk_id].tolist()

    # ----- internals -----

    @staticmethod
    def _compute_norms(vectors: np.ndarray) -> np.ndarray:
        """各行の L2 ノルムを計算して shape (N,) の配列を返す。"""
        return np.linalg.norm(vectors, axis=1).astype(np.float32)

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


def content_hash(text: str) -> str:
    """Return the SHA-256 hex digest of *text* encoded as UTF-8.

    ``errors="replace"`` ensures arbitrary Unicode input never raises.
    This is the canonical hash used for incremental-rebuild change detection
    (``cli_recall._handle_rebuild``) and as the ``source_hash`` fallback in
    :meth:`RecallIndex.build`.
    """
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


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
    """Return ``(index_path, meta_path)`` rooted at ``repo_root``.

    索引ファイル名は歴史的経緯で ``.hnsw`` を維持する（中身は numpy ペイロード）。
    """
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
