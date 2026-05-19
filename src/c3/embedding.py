"""Embedding backends for the ``c3 recall`` feature.

Provides an abstract ``Embedder`` interface so the rest of the recall
code does not depend on a specific runtime, and a default
``FastEmbedBackend`` that wraps the ``fastembed`` library.

The default model is ``sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2``
(384 dim, ~220MB, Apache-2.0, ~50 languages). It does not require any
per-text role prefixes. Models from the multilingual-e5 family *do*
expect ``"query: "`` / ``"passage: "`` prefixes, so the helpers
:func:`apply_query_prefix` / :func:`apply_passage_prefix` remain in this
module for callers that want to opt into an E5 backend later. The
default backend does not apply them.
"""

from __future__ import annotations

import abc
import warnings
from typing import Iterable, Sequence

DEFAULT_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_DIM = 384

# Names of models that need the E5 ``"query: "`` / ``"passage: "`` prefix
# convention. The default backend consults this set so that an env-flip
# to e5-large continues to behave correctly without code edits.
_E5_PREFIX_MODELS = frozenset(
    {
        "intfloat/multilingual-e5-large",
        "intfloat/multilingual-e5-base",
        "intfloat/multilingual-e5-small",
    }
)

_QUERY_PREFIX = "query: "
_PASSAGE_PREFIX = "passage: "


class Embedder(abc.ABC):
    """Strategy interface for query / passage embedding.

    Implementations must return ``list[float]`` vectors so the result is
    JSON-serializable for ``recall_meta.json`` round-trips during tests.
    Production code can keep ``numpy.ndarray`` internally and convert at
    the boundary.
    """

    @property
    @abc.abstractmethod
    def model_name(self) -> str: ...

    @property
    @abc.abstractmethod
    def dim(self) -> int: ...

    @abc.abstractmethod
    def embed_query(self, text: str) -> list[float]: ...

    @abc.abstractmethod
    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...


class FastEmbedBackend(Embedder):
    """``fastembed.TextEmbedding`` adapter with E5 prefix auto-injection.

    The ``fastembed`` library is imported lazily so that importing this
    module (e.g. from unit tests that only need the prefix logic) does
    not pull in onnxruntime or download model weights.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        *,
        cache_dir: str | None = None,
    ) -> None:
        from fastembed import TextEmbedding  # noqa: PLC0415 — intentional lazy import

        self._model_name = model_name
        self._needs_e5_prefix = model_name in _E5_PREFIX_MODELS
        # fastembed >=0.6 emits a UserWarning about the MiniLM model
        # switching from CLS embedding to mean pooling. Mean pooling is
        # the modern sentence-transformers default and matches our
        # intended behaviour, so the warning is purely informational
        # and would only confuse users who see it on every CLI call.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r".*now uses mean pooling instead of CLS embedding.*",
                category=UserWarning,
            )
            self._model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
        # Probe one embedding so ``dim`` is accurate even when a non-default
        # model is selected via env. The first call is also what triggers
        # the ONNX weight download, which is unavoidable.
        sample = _first(self._model.embed(["__dim_probe__"]))
        self._dim = len(sample)

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dim(self) -> int:
        return self._dim

    def embed_query(self, text: str) -> list[float]:
        body = text or ""
        if self._needs_e5_prefix:
            body = _QUERY_PREFIX + body
        vec = _first(self._model.embed([body]))
        return list(vec)

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        bodies = [t or "" for t in texts]
        if self._needs_e5_prefix:
            bodies = [_PASSAGE_PREFIX + t for t in bodies]
        return [list(v) for v in self._model.embed(bodies)]


def _first(iterator: Iterable):
    """Return the first element of an iterator, raising if empty."""
    for item in iterator:
        return item
    raise RuntimeError("embedding iterator yielded no result")


def apply_query_prefix(text: str) -> str:
    """Public helper exposed for tests / debugging.

    Returns ``"query: " + text``. Kept here so test code does not have to
    know the constant value.
    """
    return _QUERY_PREFIX + (text or "")


def apply_passage_prefix(text: str) -> str:
    """Public helper exposed for tests / debugging.

    Returns ``"passage: " + text``.
    """
    return _PASSAGE_PREFIX + (text or "")
