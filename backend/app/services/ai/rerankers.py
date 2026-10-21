"""Reranker service (ADR-019).

Defines the `Reranker` Protocol and a no-op identity implementation.
The real cross-encoder provider uses sentence-transformers'
``CrossEncoder`` so it shares the same model-cache directory as the
embedding model and picks up no new heavy dependency.

Pipeline position:
    SQL pre-filter → candidates
        → embed query
        → rank_top_rows_with_mode(...)  # dense or hybrid (RRF)
        → rerank_top_rows(...)          # THIS MODULE — opt-in via env
        → prompt + LLM

The orchestrator only knows the ``Reranker`` Protocol. Adding a new
provider (hosted API, GPU model, etc.) means a new class below plus a
branch in :func:`get_default_reranker` — same shape as the embedding
singleton.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class Reranker(Protocol):
    """Anything the orchestrator can call to reorder a candidate pool.

    Implementations MUST be stateless across calls: each ``rerank``
    invocation is independent and returns a fresh ordering. Process-
    level caching of the loaded model lives in the factory, not on
    the Protocol.

    Methods:
        - ``rerank(query, documents, top_k)`` → ``list[tuple[int, float]]``.
          Returns up to ``top_k`` ``(original_index, score)`` tuples in
          descending score order. ``original_index`` refers to the
          position the document had in the input list, so the caller
          can map the ordering back to its own rows.

    Attributes:
        - ``name``: short identifier persisted on logs and citation
          payloads so operators can see which reranker decided the
          order. Keep it stable and lowercase.
    """

    name: str

    def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_k: int,
    ) -> list[tuple[int, float]]: ...


class NoneReranker:
    """Identity reranker.

    Used when ``analysis_rerank_enabled=False`` (and by tests). Returns
    the input order unchanged with a constant score of 0.0 — the
    caller falls back to the retriever's ordering.
    """

    name = "none"

    def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_k: int,
    ) -> list[tuple[int, float]]:
        if not documents:
            return []
        n = min(top_k, len(documents))
        # Constant score — the original retriever's score is what the
        # caller already carries; this is just a no-op ordering.
        return [(i, 0.0) for i in range(n)]


class CrossEncoderReranker:
    """Cross-encoder reranker via sentence-transformers.

    Loads a Hugging Face cross-encoder checkpoint (default
    ``BAAI/bge-reranker-base``) and runs ``predict`` over the
    ``(query, document)`` pairs in batches. Cross-encoders score a
    query-document pair jointly, which is more accurate but slower
    than bi-encoder cosine — that's why this is a *rerank* stage over
    a pre-filtered pool rather than the first-stage retriever.

    NOTE: ``sentence_transformers`` and ``torch`` are imported lazily
    inside ``__init__`` (not at the top of the file) so importing this
    module from tests or the FastAPI process doesn't pay the multi-
    second torch import cost. Only the request that actually triggers
    a rerank loads them.
    """

    def __init__(
        self,
        model_id: str,
        *,
        device: str = "cpu",
        max_length: int = 512,
        batch_size: int = 16,
    ) -> None:
        # Lazy import — keeps the rest of the app boot-fast.
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(model_id, max_length=max_length, device=device)
        self._batch_size = max(1, batch_size)
        self.name = f"cross_encoder:{model_id}"

    def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_k: int,
    ) -> list[tuple[int, float]]:
        if not documents:
            return []
        if top_k <= 0:
            return []

        # ``CrossEncoder.predict`` returns one score per (query, doc)
        # pair, in the input order. We sort indices by descending
        # score and return the top-K.
        pairs = [(query, doc) for doc in documents]
        scores = self._model.predict(
            pairs,
            batch_size=self._batch_size,
            show_progress_bar=False,
        )
        # `scores` is a numpy array on most installs; coerce to a
        # plain list of floats so the rest of the pipeline never
        # needs to know about numpy types.
        scores_list = [float(s) for s in scores]
        ranked_indices = sorted(
            range(len(scores_list)),
            key=lambda i: scores_list[i],
            reverse=True,
        )
        n = min(top_k, len(ranked_indices))
        return [(int(ranked_indices[i]), scores_list[i]) for i in range(n)]


# Process-level singleton — same pattern as the embedding model.
# Without this, every /analysis/query request would re-load the
# cross-encoder from disk (~300 MB), and on MPS the caching allocator
# would retain freed blocks across GCs (the same leak the embedding
# singleton fixes; see progress.md "MPS memory leak in embedding
# pipeline"). Reusing one instance keeps allocation flat for the
# lifetime of the process. Restart the worker to pick up a new id.
_reranker_singleton: Reranker | None = None


def get_default_reranker() -> Reranker:
    """Return the reranker the orchestrator should use.

    Selection is driven by ``Settings.analysis_rerank_provider``:

      - ``"none"`` → :class:`NoneReranker` (identity, for tests and
        when the feature is off but the caller still asks).
      - ``"cross_encoder"`` → :class:`CrossEncoderReranker` loaded
        from the local Hugging Face cache (downloads on first use).

    The returned instance is process-cached. Subsequent calls return
    the same object so the underlying torch model is loaded into
    device memory exactly once per process.

    Failure mode: if the cross-encoder can't be constructed (e.g.
    ``sentence_transformers`` / ``torch`` aren't installed in this
    Python), we **do not** raise — we fall back to a :class:`NoneReranker`
    and log a loud one-shot warning. This used to propagate the
    ``ModuleNotFoundError`` and silently produce pre-rerank order
    for every query, which is the worst possible failure mode for
    an opt-in feature (the operator has no signal that rerank is
    dead).
    """
    global _reranker_singleton
    if _reranker_singleton is not None:
        return _reranker_singleton

    # Lazy import to avoid pulling Settings at module import time.
    from app.core.config import get_settings

    settings = get_settings()
    if settings.analysis_rerank_provider == "none":
        _reranker_singleton = NoneReranker()
        return _reranker_singleton

    # Default to cross_encoder. Provider name is a Literal in
    # config, so anything that lands here is "cross_encoder".
    try:
        _reranker_singleton = CrossEncoderReranker(
            model_id=settings.analysis_rerank_model,
            device=settings.analysis_rerank_device,
            max_length=settings.analysis_rerank_max_length,
            batch_size=settings.analysis_rerank_batch_size,
        )
    except Exception as exc:  # noqa: BLE001 — defensive: reranker is opt-in
        logger.warning(
            "get_default_reranker: failed to construct "
            "CrossEncoderReranker (model=%s, device=%s): %s: %s. "
            "Falling back to NoneReranker — every query will get "
            "the retriever's pre-rerank order until you install "
            "sentence-transformers and restart the process.",
            settings.analysis_rerank_model,
            settings.analysis_rerank_device,
            type(exc).__name__, exc,
        )
        _reranker_singleton = NoneReranker()
    return _reranker_singleton


def reset_reranker_singleton() -> None:
    """Drop the cached singleton. Intended for tests that need to
    swap providers or pick up a fresh model after a settings change.
    Production code should rely on the singleton for the lifetime of
    the process.
    """
    global _reranker_singleton
    _reranker_singleton = None


__all__ = [
    "CrossEncoderReranker",
    "NoneReranker",
    "Reranker",
    "get_default_reranker",
    "reset_reranker_singleton",
]