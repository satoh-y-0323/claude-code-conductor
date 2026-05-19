"""Tests for src/c3/embedding.py.

The unit tests cover prefix injection and contract verification without
loading the real fastembed model. A separate ``@pytest.mark.slow`` test
exercises the full FastEmbedBackend end-to-end.
"""

from __future__ import annotations

from typing import Sequence

import pytest

from c3.embedding import (
    DEFAULT_DIM,
    DEFAULT_MODEL_NAME,
    Embedder,
    FastEmbedBackend,
    apply_passage_prefix,
    apply_query_prefix,
)


def test_query_prefix() -> None:
    assert apply_query_prefix("hello") == "query: hello"


def test_passage_prefix() -> None:
    assert apply_passage_prefix("hello") == "passage: hello"


def test_prefix_helpers_handle_empty_string() -> None:
    assert apply_query_prefix("") == "query: "
    assert apply_passage_prefix("") == "passage: "


def test_default_model_is_multilingual_minilm() -> None:
    assert DEFAULT_MODEL_NAME == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    assert DEFAULT_DIM == 384


def test_embedder_is_abstract() -> None:
    with pytest.raises(TypeError):
        Embedder()  # type: ignore[abstract]


class _FakeFastEmbedModel:
    """Stand-in for ``fastembed.TextEmbedding`` used by tests."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts):
        batch = list(texts)
        self.calls.append(batch)
        # Return a 3-D unit-ish vector; tests only check shape & wiring.
        return [[float(len(t)), 0.0, 0.0] for t in batch]


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> tuple[FastEmbedBackend, _FakeFastEmbedModel]:
    """Build a FastEmbedBackend with the real ``fastembed`` swapped out."""
    fake = _FakeFastEmbedModel()

    class _FakeFastEmbedModule:
        TextEmbedding = lambda *args, **kwargs: fake  # noqa: E731

    import sys
    monkeypatch.setitem(sys.modules, "fastembed", _FakeFastEmbedModule())
    backend = FastEmbedBackend()
    return backend, fake


def test_fast_embed_backend_default_does_not_inject_prefix(fake_backend) -> None:
    backend, fake = fake_backend
    backend.embed_query("こんにちは")
    # The default MiniLM model does not need E5 prefixes.
    # The first probe call ("__dim_probe__") happens during __init__,
    # so the most recent call is the embed_query payload.
    assert fake.calls[-1] == ["こんにちは"]


def test_fast_embed_backend_default_passages_no_prefix(fake_backend) -> None:
    backend, fake = fake_backend
    backend.embed_passages(["a", "b"])
    assert fake.calls[-1] == ["a", "b"]


def test_fast_embed_backend_empty_passages_skips_model_call(fake_backend) -> None:
    backend, fake = fake_backend
    fake.calls.clear()  # drop the dim probe call
    assert backend.embed_passages([]) == []
    assert fake.calls == []


def test_fast_embed_backend_empty_query_unchanged(fake_backend) -> None:
    backend, fake = fake_backend
    backend.embed_query("")
    assert fake.calls[-1] == [""]


def test_fast_embed_backend_reports_model_name(fake_backend) -> None:
    backend, _ = fake_backend
    assert backend.model_name == DEFAULT_MODEL_NAME
    assert backend.dim == 3  # The fake embedding returns 3-D vectors


def test_fast_embed_backend_e5_model_injects_prefixes(monkeypatch: pytest.MonkeyPatch) -> None:
    """E5-family models still get ``query: `` / ``passage: `` prefixes."""
    import sys
    fake = _FakeFastEmbedModel()

    class _FakeFastEmbedModule:
        TextEmbedding = lambda *args, **kwargs: fake  # noqa: E731

    monkeypatch.setitem(sys.modules, "fastembed", _FakeFastEmbedModule())
    backend = FastEmbedBackend(model_name="intfloat/multilingual-e5-large")
    backend.embed_query("hi")
    backend.embed_passages(["a", "b"])
    # The probe call (no prefix) + query call + passages call.
    assert fake.calls[-1] == ["passage: a", "passage: b"]
    assert fake.calls[-2] == ["query: hi"]


def test_fast_embed_backend_returns_lists(fake_backend) -> None:
    backend, _ = fake_backend
    vec = backend.embed_query("hi")
    assert isinstance(vec, list)
    assert all(isinstance(x, float) for x in vec)
    vecs = backend.embed_passages(["a", "bb"])
    assert isinstance(vecs, list)
    assert all(isinstance(v, list) for v in vecs)


@pytest.mark.slow
def test_real_fast_embed_backend_smoke() -> None:
    """Smoke test against the actual fastembed model.

    Skipped by default (slow marker). Run via ``pytest -m slow`` or
    ``pytest -m ''``. Requires network access on first run to download
    the model weights (~220MB for the default MiniLM model).
    """
    fastembed = pytest.importorskip("fastembed")
    del fastembed
    backend = FastEmbedBackend()
    vec = backend.embed_query("テスト")
    assert len(vec) == DEFAULT_DIM
    passages = backend.embed_passages(["これはテストです", "another test"])
    assert len(passages) == 2
    assert all(len(v) == DEFAULT_DIM for v in passages)


class _CustomEmbedder(Embedder):
    """Concrete Embedder used to verify the ABC contract compiles."""

    @property
    def model_name(self) -> str:
        return "custom"

    @property
    def dim(self) -> int:
        return 4

    def embed_query(self, text: str) -> list[float]:
        return [0.0, 0.0, 0.0, 0.0]

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0, 0.0, 0.0, 0.0] for _ in texts]


def test_embedder_abc_can_be_subclassed() -> None:
    e = _CustomEmbedder()
    assert e.model_name == "custom"
    assert e.dim == 4
    assert len(e.embed_query("x")) == 4
    assert e.embed_passages(["a", "b"]) == [[0.0] * 4, [0.0] * 4]
