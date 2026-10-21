"""Embedding service scaffold.

Defines the `EmbeddingModel` protocol and a deterministic placeholder
implementation. The real Google / OpenAI / local-model integration
plugs in here later — the worker talks only to `EmbeddingModel`, so
swapping providers doesn't ripple.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EmbeddingResult:
    """Output of a single embed call."""

    vector: list[float]
    dim: int
    model_name: str
    distance_metric: str = "cosine"


class EmbeddingModel(Protocol):
    """Anything the worker can call to embed a chunk of text.

    Two methods are required:
      - `embed(text)`           → embed one text. Convenience wrapper.
      - `embed_batch(texts)`    → embed many texts in a single call.
        Real models (Qwen3, OpenAI, etc.) are 5-10x faster on a batch
        than on N separate calls because the model stays "warm" in
        memory between forward passes. The stub implements it as a
        trivial loop — same correctness, just slower.

    Both methods MUST return vectors with the same dim as `self.dim`
    so they are interchangeable on disk and in queries.
    """

    model_name: str
    dim: int

    def embed(self, text: str) -> EmbeddingResult: ...

    def embed_batch(self, texts: list[str]) -> list[EmbeddingResult]: ...


class DeterministicStubModel:
    """Placeholder embedding model.

    Produces a deterministic 16-dim vector derived from a SHA-256 hash
    of the input text. Useful for local dev, tests, and CI — every run
    gives the same vector for the same input, so retrieval ranking
    behaves consistently even without a real model.

    NOT for production: replace with a real provider before going live.
    """

    model_name = "deterministic-stub-v1"
    dim = 16

    def embed(self, text: str) -> EmbeddingResult:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # Stretch the 32-byte digest into 16 floats in [0, 1).
        vector: list[float] = []
        for i in range(self.dim):
            byte = digest[i % len(digest)]
            vector.append(byte / 255.0)
        return EmbeddingResult(
            vector=vector,
            dim=self.dim,
            model_name=self.model_name,
        )

    def embed_batch(self, texts: list[str]) -> list[EmbeddingResult]:
        # Trivial loop: stub is for tests/CI, not performance.
        return [self.embed(t) for t in texts]


# Process-level singleton. Without this, every call from
# `embed_chunks_for_document` (Celery) and `/analysis/query`
# (FastAPI request handler) instantiates a fresh `Qwen3EmbeddingModel`,
# which loads a new SentenceTransformer + torch tensors onto the device.
# On MPS, the caching allocator retains those blocks across GCs, so the
# pool grows unbounded and eventually hits `MPS_HIGH_WATERMARK_RATIO`.
# Reusing a single instance keeps allocation flat for the lifetime of
# the process. Restart the worker to pick up a new model id.
_embedding_model_singleton: EmbeddingModel | None = None


def get_default_embedding_model() -> EmbeddingModel:
    """Return the embedding model the worker should use.

    Selection is driven by `Settings.embedding_provider`:
      - "stub"  → DeterministicStubModel (default; tests/CI).
      - "qwen3" → Qwen3EmbeddingModel loaded from the local Hugging
                  Face cache (downloads on first use, then cached).

    The returned instance is process-cached: subsequent calls return
    the same object so the underlying torch model is loaded into device
    memory exactly once per process. Centralized so swapping providers
    is a one-line change.
    """
    global _embedding_model_singleton
    if _embedding_model_singleton is not None:
        return _embedding_model_singleton

    # Lazy import to avoid pulling Settings at module import time —
    # tests that import only the dataclass don't need to load config.
    from app.core.config import get_settings

    settings = get_settings()
    if settings.embedding_provider == "qwen3":
        _embedding_model_singleton = Qwen3EmbeddingModel(
            model_id=settings.embedding_model_name,
            device=settings.embedding_device,
            dim=settings.embedding_dim,
        )
    else:
        _embedding_model_singleton = DeterministicStubModel()
    return _embedding_model_singleton


def reset_embedding_model_singleton() -> None:
    """Drop the cached singleton. Intended for tests that need to
    swap providers or pick up a fresh model after a settings change.
    Production code should rely on the singleton for the lifetime of
    the process.
    """
    global _embedding_model_singleton
    _embedding_model_singleton = None


class Qwen3EmbeddingModel:
    """Qwen3-Embedding model wrapped behind the EmbeddingModel protocol.

    Uses sentence-transformers, which loads a Hugging Face checkpoint
    from the local cache (~/.cache/huggingface/hub/). The first call
    to this class on a fresh machine downloads the weights (~1.2 GB);
    subsequent instantiations load from disk in a few seconds.

    The model is run with `normalize_embeddings=True`, so every output
    vector lives on the unit sphere. That makes cosine similarity
    equal to a plain dot product at query time, which is much cheaper
    to compute than the un-normalized version.

    NOTE: `sentence_transformers` and `torch` are imported lazily inside
    __init__ (not at the top of the file) so importing this module from
    tests or the FastAPI process doesn't pay the multi-second torch
    import cost. Only the Celery worker — which actually instantiates
    the model — loads them.
    """

    def __init__(self, model_id: str, device: str = "cpu", dim: int = 1024) -> None:
        # Lazy import — keeps the rest of the app boot-fast.
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_id, device=device)
        # Override class attribute with the actual loaded id so the
        # stored DocumentEmbedding.model_name matches reality.
        self.model_name = model_id
        self.dim = dim

    def _wrap(self, vector) -> EmbeddingResult:
        return EmbeddingResult(
            vector=vector.tolist(),
            dim=self.dim,
            model_name=self.model_name,
            distance_metric="cosine",
        )

    def embed(self, text: str) -> EmbeddingResult:
        return self._wrap(self._model.encode(text, normalize_embeddings=True))

    def embed_batch(self, texts: list[str]) -> list[EmbeddingResult]:
        if not texts:
            return []
        # One forward pass for the whole batch — 5-10x faster than
        # calling encode() in a loop.
        vectors = self._model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        return [self._wrap(v) for v in vectors]


__all__ = [
    "DeterministicStubModel",
    "EmbeddingModel",
    "EmbeddingResult",
    "Qwen3EmbeddingModel",
    "get_default_embedding_model",
    "reset_embedding_model_singleton",
]
