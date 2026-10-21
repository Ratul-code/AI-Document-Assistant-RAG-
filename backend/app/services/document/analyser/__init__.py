"""Retrieval / answer orchestration for the analysis (RAG) pipeline.

Top-level public surface:

* `rank_top_rows(candidates, query_vector, *, top_k, min_cosine)` —
  pure NumPy top-K cosine ranking. Testable in isolation, no I/O.
* `analyse_query(...)` — async generator. Embeds the question, ranks,
  calls the LLM, and yields in-process `AnalysisEvent`s.
* `analyse_query_once(...)` — drains the iterator and returns a
  `QueryOutcome` (a small sum type: `_QueryOutcome` for success,
  `_ErrorOutcome` for failure with an `AnalysisErrorCode`).
* `analyse_query_with_conversation(...)` — orchestration entry point
  for the chat UI. When `conversation_id` is provided:

    1. Persist the user turn immediately (before retrieval runs).
    2. Resolve the conversation's saved document scope.
    3. Drive `analyse_query`, threading `conversation_id` into every
       yielded event.
    4. Persist the assistant turn on `AnalysisDoneEvent`
       (full answer + citations) or on terminal `AnalysisErrorEvent`
       (with `error_code` set, content left empty).
    5. Never persist partial streams — a disconnect / cancellation
       leaves no assistant row.

  When `conversation_id` is `None`, this falls through to the legacy
  one-shot behaviour so REST clients don't pay the persistence cost.

The handler boundary (`api/v1/endpoints/{analysis,conversation}.py`)
translates each `AnalysisEvent` into the appropriate WS envelope / REST
response.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Sequence
from uuid import UUID

from app.core.config import get_settings
from app.db.sync_session import run_in_sync_session
from app.models.conversation import MessageRole
from app.repositories import conversation as conversation_repo
from app.repositories import document as document_repo
from app.repositories.embedding_search import CandidateRow, fetch_candidate_rows
from app.schemas.analysis import QueryCitation, QueryResponse
from app.services.ai.embeddings import get_default_embedding_model
from app.services.ai.llm import LLMMessage, get_default_llm_provider, get_rewrite_llm_provider
from app.services.ai.rerankers import Reranker, get_default_reranker
from app.services.document.analyser.chat_history import fetch_recent_turns
from app.services.document.analyser.events import (
    AnalysisDoneEvent,
    AnalysisErrorEvent,
    AnalysisEvent,
    AnalysisTokenEvent,
)
from app.services.document.analyser.prompt import build_messages, trim_snippet
from app.services.document.analyser.query_rewrite import (
    rewrite_query_for_retrieval,
)
from app.services.pubsub.ws_publisher import publish_analysis_user_message

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Result type for `analyse_query_once` — sum type over success/error.
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class _QueryOutcome:
    """Success: a fully assembled `QueryResponse`."""

    response: QueryResponse


@dataclass(frozen=True)
class _ErrorOutcome:
    """Failure: machine-readable code + message."""

    code: str  # AnalysisErrorCode value
    message: str = ""


# Public alias. The endpoint inspects with `isinstance(outcome, ...)`.
QueryOutcome = _QueryOutcome | _ErrorOutcome


# ---------------------------------------------------------------------
# Ranking — pure NumPy, no I/O.
# ---------------------------------------------------------------------


def rank_top_rows(
    candidates: Sequence[CandidateRow],
    query_vector: Sequence[float],
    *,
    top_k: int,
    min_cosine: float = 0.0,
) -> list[tuple[CandidateRow, float]]:
    """Cosine-rank `candidates` against `query_vector`, descending.

    Returns up to `top_k` `(row, score)` tuples. Empty input → empty
    output. `min_cosine` filters out near-zero or negative scores
    (set to 0.0 to disable; the default keeps everything positive).

    Invariants:
        - Same model must have produced `query_vector` and the row vectors
          (i.e. same `dim`). Caller verifies dim equality before calling.
        - Score is cosine, range [-1.0, 1.0]. Ties broken by document_id
          then page_number then chunk_index for determinism (the SQL
          fetch already returns rows in that order; we only need a
          stable secondary sort if a vector store returns a different
          order later).
    """
    if not candidates:
        return []
    if top_k <= 0:
        return []

    # Lazy import — keeps numpy off the import path of tests that
    # only exercise prompt construction.
    import numpy as np

    q = np.asarray(query_vector, dtype=np.float32)
    q_norm = float(np.linalg.norm(q))
    if q_norm == 0.0 or not math.isfinite(q_norm):
        return []
    q_unit = q / q_norm

    # Stack all candidate vectors. Cheap even for thousands of rows;
    # sub-millisecond on modern hardware.
    matrix = np.asarray([r.vector for r in candidates], dtype=np.float32)
    if matrix.size == 0:
        return []
    row_norms = np.linalg.norm(matrix, axis=1)
    # Avoid division by zero for any zero-vector row (degenerate input
    # from a stub or zero-filled test fixture) — those get cosine 0.
    safe_norms = np.where(row_norms == 0.0, 1.0, row_norms)
    unit = matrix / safe_norms[:, None]
    scores = unit @ q_unit  # shape (n,)

    # Order: by score DESC, then (doc_id, page_number, chunk_index)
    # ASC for stability. We pre-attach the row indices so numpy's
    # argsort can be tweaked in one pass.
    if min_cosine > 0.0:
        mask = scores >= min_cosine
        if not mask.any():
            return []
        # Index with the mask; the scores / unit arrays stay aligned.
        idx_pool = np.flatnonzero(mask)
        pool_scores = scores[idx_pool]
        # Build secondary keys lazily for the pool only.
        secondary = [
            (str(r.document_id), r.page_number, r.chunk_index)
            for i, r in enumerate(candidates)
            if i in set(idx_pool.tolist())
        ]
        order = sorted(
            range(len(idx_pool)),
            key=lambda j: (-float(pool_scores[j]), secondary[j]),
        )
        picked: list[tuple[CandidateRow, float]] = []
        for j in order[:top_k]:
            i = int(idx_pool[j])
            picked.append((candidates[i], float(pool_scores[j])))
        return picked

    # No min_cosine gate. Order everything by score DESC, then secondary.
    secondary_all = [
        (str(r.document_id), r.page_number, r.chunk_index)
        for r in candidates
    ]
    order = sorted(
        range(len(candidates)),
        key=lambda i: (-float(scores[i]), secondary_all[i]),
    )
    return [
        (candidates[i], float(scores[i])) for i in order[:top_k]
    ]


# ---------------------------------------------------------------------
# Mode dispatcher — picks dense vs hybrid based on Settings.
# (ADR-018)
# ---------------------------------------------------------------------


def rank_top_rows_with_mode(
    candidates: Sequence[CandidateRow],
    query_vector: Sequence[float],
    *,
    mode: str,
    top_k: int,
    min_cosine: float = 0.0,
    query_text: str | None = None,
    hybrid_dense_k: int = 15,
    hybrid_bm25_k: int = 15,
    hybrid_rrf_k: int = 60,
) -> list[tuple[CandidateRow, float]]:
    """Pick the retriever based on `mode`.

    `mode == "dense"`:
        Today's NumPy cosine top-K (no extra deps).

    `mode == "hybrid"` (default in Settings):
        dense top-`hybrid_dense_k` + BM25 top-`hybrid_bm25_k` fused
        via Reciprocal Rank Fusion with constant `hybrid_rrf_k`, then
        take the final `top_k`. BM25 needs raw query text, so when
        `mode == "hybrid"` callers MUST pass `query_text`.

    Returns the same `(CandidateRow, score)` shape as `rank_top_rows`
    so downstream code (citations, prompt construction, scoring) is
    mode-agnostic.
    """
    if mode == "dense":
        return rank_top_rows(
            candidates,
            query_vector,
            top_k=top_k,
            min_cosine=min_cosine,
        )

    if mode == "hybrid":
        if query_text is None:
            # Refuse loudly — BM25 can't run without query text, and
            # silently falling back to dense would hide a misconfiguration.
            raise ValueError(
                "rank_top_rows_with_mode(mode='hybrid') requires `query_text` "
                "(BM25 leg is lexical)."
            )
        # Lazy import — keeps the dep surface small for callers that
        # only ever use the dense path.
        from app.services.document.retrieval import hybrid_retrieve
        return hybrid_retrieve(
            candidates,
            query_vector,
            query_text,
            dense_top_k=hybrid_dense_k,
            bm25_top_k=hybrid_bm25_k,
            rrf_k=hybrid_rrf_k,
            top_k=top_k,
            min_cosine=min_cosine,
        )

    raise ValueError(
        f"Unknown retrieval mode {mode!r}; expected 'dense' or 'hybrid'."
    )


# ---------------------------------------------------------------------
# Reranker wrapper (ADR-019).
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class RerankResult:
    """Outcome of a single rerank call: reordered rows plus the metadata
    that decides what's shown to the user.

    `top_rows` carries the reranker-decided ordering. `original_scores`
    is parallel to `top_rows` and gives the retriever's score (cosine
    or RRF) for the same row — populated only when rerank actually
    changed the order, so the citation payload can show both. The
    `provider` field is the reranker's self-identifier (e.g.
    ``"cross_encoder:BAAI/bge-reranker-base"``) so a frontend can
    badge the answer as such; ``None`` means the retriever decided.
    """

    top_rows: list[tuple[CandidateRow, float]]
    # `None` when rerank did not run (provider is NoneReranker or
    # rerank is disabled). Otherwise parallel to `top_rows`.
    original_scores: list[float] | None
    provider: str | None


def rerank_top_rows(
    ranked: Sequence[tuple[CandidateRow, float]],
    query: str,
    *,
    reranker: Reranker,
    top_k: int,
    pool_multiplier: int = 3,
    timeout_s: float = 5.0,
) -> RerankResult:
    """Reorder a retriever-produced candidate pool with a cross-encoder.

    Pipeline:
        1. Slice `ranked[:top_k * pool_multiplier]` to form the
           pre-rerank pool. Capped at ``len(ranked)``.
        2. Call ``reranker.rerank(query, [row.text for row in pool],
           top_k=top_k)`` and map the returned indices back to rows.
        3. Wall-clock cap (``timeout_s``) is enforced by the caller via
           ``asyncio.wait_for`` — here we trust the reranker to honour
           the timeout policy of the calling event loop. If the
           reranker raises, the caller decides what to do (today:
           fall back to the pre-rerank order).

    When ``reranker`` is a :class:`NoneReranker` (or ``top_k <= 0`` or
    the pool is empty) this returns the input slice unchanged with
    ``original_scores=None`` and ``provider=None`` — the orchestrator
    treats it as a no-op.
    """
    if not ranked or top_k <= 0:
        return RerankResult(top_rows=[], original_scores=None, provider=None)

    # No-op path: NoneReranker returns identity ordering with score=0;
    # we'd lose the original retriever score if we used those. Detect
    # the identity case by name and pass through cleanly.
    if getattr(reranker, "name", "") == "none":
        sliced = list(ranked[:top_k])
        return RerankResult(
            top_rows=[(row, score) for row, score in sliced],
            original_scores=None,
            provider=None,
        )

    pool_size = min(len(ranked), top_k * max(1, pool_multiplier))
    pool = list(ranked[:pool_size])
    documents = [row.text for row, _ in pool]
    reranked = reranker.rerank(query, documents, top_k=top_k)

    # Map back: original index → row + retriever score.
    original_scores: list[float] = []
    top_rows: list[tuple[CandidateRow, float]] = []
    for original_idx, rerank_score in reranked:
        if 0 <= original_idx < len(pool):
            row, original_score = pool[original_idx]
            top_rows.append((row, float(rerank_score)))
            original_scores.append(float(original_score))

    return RerankResult(
        top_rows=top_rows,
        original_scores=original_scores,
        provider=reranker.name,
    )


async def rerank_top_rows_async(
    ranked: Sequence[tuple[CandidateRow, float]],
    query: str,
    *,
    reranker: Reranker,
    top_k: int,
    pool_multiplier: int = 3,
    timeout_s: float = 5.0,
) -> RerankResult:
    """Async wrapper around :func:`rerank_top_rows`.

    Runs the (CPU-bound) rerank in a worker thread so it doesn't pin
    the FastAPI event loop, and enforces a hard wall-clock cap. On
    timeout the orchestrator falls back to the pre-rerank order with
    a warning — a slow reranker must never fail the query (ADR-019).
    """
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: rerank_top_rows(
                    ranked,
                    query,
                    reranker=reranker,
                    top_k=top_k,
                    pool_multiplier=pool_multiplier,
                ),
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "rerank_top_rows_async: timed out after %.2fs; "
            "falling back to pre-rerank order (pool=%d, top_k=%d).",
            timeout_s, min(len(ranked), top_k * max(1, pool_multiplier)), top_k,
        )
        # Fallback: keep the retriever's ordering, drop everything
        # past `top_k`. `provider=None` so the citation payload says
        # "no rerank decided this order".
        sliced = list(ranked[:top_k])
        return RerankResult(
            top_rows=[(row, score) for row, score in sliced],
            original_scores=None,
            provider=None,
        )
    except Exception as exc:  # noqa: BLE001 — defensive: rerank never fails the query
        logger.warning(
            "rerank_top_rows_async: reranker raised %s; falling back to "
            "pre-rerank order.",
            exc,
        )
        sliced = list(ranked[:top_k])
        return RerankResult(
            top_rows=[(row, score) for row, score in sliced],
            original_scores=None,
            provider=None,
        )


# ---------------------------------------------------------------------
# Citation assembly — turns (row, score) → QueryCitation dict.
# ---------------------------------------------------------------------


def _snippet_for_response(text: str, max_chars: int) -> str:
    """Trim a chunk for the citation list. Delegates to ``trim_snippet``
    so the prompt's snippet and the citation's snippet are byte-for-
    byte the same logic (sentence-aware boundary, head+tail windowing,
    honest omission marker). Both surfaces stay in lockstep."""
    return trim_snippet(text or "", max_chars=max_chars)


def _build_citations(
    top_rows: Sequence[tuple[CandidateRow, float]],
    *,
    snippet_chars_for_response: int,
    original_scores: Sequence[float] | None = None,
    rerank_provider: str | None = None,
) -> list[QueryCitation]:
    """Build the `QueryCitation` list — used by both the WS payload
    (dumped via `model_dump(mode="json")`) and the REST response.

    When ``original_scores`` is supplied (parallel to ``top_rows``) and
    the rerank provider is non-None, every citation carries
    ``original_score`` (the retriever's score before rerank) and
    ``rerank=True`` (plus the provider name). When they're ``None`` /
    empty, citations look identical to the v1 payload — the rerank
    feature is opt-in and contract-additive.
    """
    citations: list[QueryCitation] = []
    for i, (row, score) in enumerate(top_rows, start=1):
        kwargs: dict[str, Any] = dict(
            index=i,
            chunk_id=row.chunk_id,
            document_id=row.document_id,
            document_title=row.document_title,
            page_number=row.page_number + 1 if row.page_number is not None else None,
            section_path=list(row.section_path or []),
            snippet=_snippet_for_response(row.text, snippet_chars_for_response),
            score=score,
        )
        if (
            original_scores is not None
            and rerank_provider is not None
            and i - 1 < len(original_scores)
        ):
            kwargs["original_score"] = original_scores[i - 1]
            kwargs["rerank"] = True
            kwargs["rerank_provider"] = rerank_provider
        citations.append(QueryCitation(**kwargs))
    return citations


# ---------------------------------------------------------------------
# Scope resolution — shared by the streaming path and the REST fallback.
# ---------------------------------------------------------------------


async def _resolve_scope(
    db_async: Any,
    *,
    owner_id: UUID,
    document_ids: Sequence[UUID] | None,
) -> list[UUID] | None:
    """Return the effective list of document ids to search, or `None`
    to mean "all of the user's READY docs".

    Empty list (after the caller's intent resolution) returns `[]`
    so the orchestrator can raise `NO_DOCUMENTS_READY` without an
    extra DB round trip — calling `fetch_candidate_rows([])` would
    also yield no rows, but we want to short-circuit with a clear
    error code.
    """
    if document_ids is not None:
        # Caller has been explicit. An empty list means "no selection";
        # the route layer already produced NO_DOCUMENTS_READY upstream,
        # but we double-check defensively. Crucially: we don't touch
        # the DB when the caller has been explicit — saves a round
        # trip and lets tests run without a real session.
        return list(document_ids)

    docs = await document_repo.list_ready_documents(db_async, owner_id=owner_id)
    return [doc.id for doc in docs]


# ---------------------------------------------------------------------
# Orchestration — the public surface called by handlers.
# ---------------------------------------------------------------------


async def analyse_query(
    *,
    db_async: Any,
    owner_id: UUID,
    question: str,
    document_ids: Sequence[UUID] | None,
    stream_id: UUID,
    conversation_id: UUID | None = None,
    llm_provider: Any | None = None,
    rewrite_llm_provider: Any | None = None,
    embedding_model: Any | None = None,
    history: Sequence[LLMMessage] | None = None,
) -> AsyncIterator[AnalysisEvent]:
    """Async generator yielding `AnalysisEvent`s for one analysis.

    The endpoint / Celery background task iterates this; for each
    `token` event it publishes a WS envelope. The terminal `done`
    or `error` event is the last thing yielded.

    Args:
        db_async: AsyncSession from the FastAPI `get_db` dependency.
        owner_id: Current user. Used for owner-scoped SQL.
        question: The user's question text. Already validated by the
            route layer (1..2000 chars).
        document_ids: Caller-pinned scope, or None for "all READY docs".
        stream_id: Used for tagging events so the client can correlate.
        conversation_id: If set, threads through every yielded event
            so the WS envelope and the chat UI can reconcile.
            Persistence is handled by `analyse_query_with_conversation`,
            not here.
        llm_provider / embedding_model: optional overrides for tests.
        rewrite_llm_provider: optional override for the query-rewrite
            LLM. Defaults to ``get_rewrite_llm_provider()`` which
            reads ``analysis_query_rewrite_*`` settings. Kept
            separate from ``llm_provider`` so tests can stub the
            rewrite without touching the answer-gen model.
            See ADR-021.
        history: Pre-rendered chat history (oldest → newest). When
            provided, the prompt carries a <CHAT_HISTORY> block
            between the system prompt and the current user message,
            and the orchestrator also fires a query-rewrite call
            (gated by settings.analysis_query_rewrite_enabled) to
            build a self-contained query for the retriever. The
            conversational orchestrator populates this from
            ``fetch_recent_turns``; the REST fallback passes None.

    Notes:
        * Uses `run_in_sync_session(...)` to fetch the candidate rows
          because the SQL join is naturally sync and Cosine uses NumPy
          in pure Python. The handler's DB transaction stays in the
          async session; the sync session is short-lived and discarded.
        * Cancellation: `asyncio.CancelledError` from the for-loop
          above propagates into `provider.stream()` which closes the
          upstream `httpx` request via its context manager.
    """
    settings = get_settings()

    # 1. Resolve scope.
    scope = await _resolve_scope(db_async, owner_id=owner_id, document_ids=document_ids)
    if not scope:
        yield AnalysisErrorEvent(
            code="NO_DOCUMENTS_READY",
            message="No queryable documents are available for this user.",
            conversation_id=conversation_id,
        )
        return

    # 2. Fire TWO things concurrently (ADR-021):
    #    (a) the candidate fetch on a worker thread (SQL is sync),
    #    (b) the query rewrite on the event loop (a small LLM call).
    #    In the steady state both finish around the same time and the
    #    rewrite lands inside the embedding call's own wait, so the
    #    wall-clock cost over v1 is ~0ms. If the rewrite is disabled
    #    or there's no history, we skip (b) entirely and only (a) runs.
    candidates_task = asyncio.create_task(
        asyncio.to_thread(
            run_in_sync_session,
            fetch_candidate_rows,
            owner_id=owner_id,
            document_ids=scope,
        )
    )
    rewrite_task: asyncio.Task[str] | None = None
    rewrite_enabled = (
        settings.analysis_query_rewrite_enabled
        and settings.analysis_query_rewrite_max_history_turns > 0
        and bool(history)
    )
    if rewrite_enabled:
        rewrite_provider = rewrite_llm_provider or get_rewrite_llm_provider()
        rewrite_task = asyncio.create_task(
            rewrite_query_for_retrieval(
                history=history or [],
                question=question,
                llm_provider=rewrite_provider,
                max_history_turns=settings.analysis_query_rewrite_max_history_turns,
            )
        )

    candidates = await candidates_task
    if not candidates:
        # Cancel any in-flight rewrite so it doesn't leak.
        if rewrite_task is not None and not rewrite_task.done():
            rewrite_task.cancel()
        yield AnalysisErrorEvent(
            code="NO_DOCUMENTS_READY",
            message="Selected documents have no embedded chunks.",
            conversation_id=conversation_id,
        )
        return

    # 3. Embed the question. We use the same provider the embedding
    #    worker used, so the vectors are commensurable.
    model = embedding_model or get_default_embedding_model()
    stored_dim = candidates[0].embedding_dim
    if model.dim != stored_dim:
        if rewrite_task is not None and not rewrite_task.done():
            rewrite_task.cancel()
        yield AnalysisErrorEvent(
            code="PROVIDER_MISMATCH",
            message=(
                f"Embedding model dim ({model.dim}) does not match stored "
                f"embeddings (dim={stored_dim}). Re-embed documents or "
                "switch to a matching model."
            ),
            conversation_id=conversation_id,
        )
        return

    # 3a. Await the rewrite with a wall-clock cap. The helper itself
    #     is fail-soft (returns the raw question on any provider
    #     error) — we only enforce the deadline here. On timeout we
    #     fall back to the raw question and log; a slow rewrite must
    #     never block a query.
    if rewrite_task is not None:
        try:
            query_text = await asyncio.wait_for(
                rewrite_task,
                timeout=settings.analysis_query_rewrite_timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "analyse_query: query rewrite timed out after %.2fs; "
                "falling back to raw question. conv_id=%s",
                settings.analysis_query_rewrite_timeout_s,
                conversation_id,
            )
            query_text = question
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "analyse_query: query rewrite raised %s; falling back. "
                "conv_id=%s",
                exc, conversation_id,
            )
            query_text = question
    else:
        query_text = question

    query_result = await asyncio.to_thread(model.embed, query_text)
    query_vec = query_result.vector

    # 4. Top-K via the configured retriever mode (ADR-018).
    #    Default is `hybrid` (dense + BM25 → RRF); `dense` is the
    #    legacy v1 path for safe fallback. The dispatcher is shape-
    #    compatible with `rank_top_rows` so downstream code is mode-
    #    agnostic. `query_text` is only used in `hybrid` mode; we pass
    #    it unconditionally so the dispatcher's own validation can
    #    catch a misconfiguration (hybrid + missing query_text).
    #
    #    The retriever surfaces a pool sized to
    #    `analysis_top_k * analysis_rerank_pool_multiplier` so the
    #    reranker has headroom. When the reranker is OFF this is
    #    equivalent to "give me top-K" — the multiplier is wasted work
    #    that the post-rerank slice throws away. ADR-019 calls that
    #    out as acceptable (the multiplier defaults to 3 only matters
    #    when rerank is on, and rank_top_rows truncates internally
    #    when there are fewer candidates than the pool cap).
    retrieval_mode = settings.analysis_retrieval_mode
    pool_cap = (
        settings.analysis_top_k * settings.analysis_rerank_pool_multiplier
        if settings.analysis_rerank_enabled
        else settings.analysis_top_k
    )
    ranked = await asyncio.to_thread(
        rank_top_rows_with_mode,
        candidates,
        query_vec,
        mode=retrieval_mode,
        top_k=pool_cap,
        min_cosine=settings.analysis_min_cosine,
        query_text=query_text,
        hybrid_dense_k=settings.analysis_hybrid_dense_k,
        hybrid_bm25_k=settings.analysis_hybrid_bm25_k,
        hybrid_rrf_k=settings.analysis_hybrid_rrf_k,
    )
    if not ranked:
        yield AnalysisErrorEvent(
            code="NO_DOCUMENTS_READY",
            message="No chunks passed the relevance threshold.",
            conversation_id=conversation_id,
        )
        return

    # 4b. Rerank (ADR-019). Opt-in via env. On timeout or any reranker
    #     failure, the helper falls back to the pre-rerank order so
    #     a flaky model never breaks a query.
    rerank_result: RerankResult | None = None
    if settings.analysis_rerank_enabled:
        reranker = get_default_reranker()
        t0 = time.monotonic()
        rerank_result = await rerank_top_rows_async(
            ranked,
            query_text,  # rewritten when history is present (ADR-021)
            reranker=reranker,
            top_k=settings.analysis_rerank_top_k,
            pool_multiplier=settings.analysis_rerank_pool_multiplier,
            timeout_s=settings.analysis_rerank_timeout_s,
        )
        latency_ms = (time.monotonic() - t0) * 1000.0
        logger.info(
            "analyse_query: rerank stream_id=%s provider=%s pool=%d kept=%d latency_ms=%.1f",
            stream_id, rerank_result.provider, len(ranked), len(rerank_result.top_rows),
            latency_ms,
        )
        top_rows = rerank_result.top_rows
        if not top_rows:
            # Rerank returned nothing — treat as a no-chunks-pass case
            # so the client gets a clean error rather than a confusing
            # empty prompt.
            yield AnalysisErrorEvent(
                code="NO_DOCUMENTS_READY",
                message="No chunks passed reranking.",
                conversation_id=conversation_id,
            )
            return
    else:
        top_rows = ranked[: settings.analysis_top_k]

    # 5. Build the prompt and call the LLM.
    messages = build_messages(
        question=question,
        top_rows=[row for row, _score in top_rows],
        snippet_chars=settings.analysis_snippet_chars,
        history=history,
    )
    provider = llm_provider or get_default_llm_provider()

    # 6. Stream tokens. Build the answer as we go so the terminal
    #    `done` event carries the full text without a second pass.
    answer_parts: list[str] = []
    try:
        async for token in provider.stream(messages):
            answer_parts.append(token)
            yield AnalysisTokenEvent(text=token, conversation_id=conversation_id)
    except asyncio.CancelledError:
        logger.info("analyse_query: cancelled stream_id=%s", stream_id)
        # Surface CANCELLED to the WS so the client can render a
        # "stopped" state if it reconnects later; the response
        # itself is discarded.
        yield AnalysisErrorEvent(
            code="CANCELLED",
            message="Stream cancelled by client.",
            conversation_id=conversation_id,
        )
        return
    except Exception as exc:
        logger.exception(
            "analyse_query: LLM provider failed stream_id=%s err=%s",
            stream_id, exc,
        )
        yield AnalysisErrorEvent(
            code="PROVIDER_ERROR",
            message=str(exc)[:500],
            conversation_id=conversation_id,
        )
        return

    answer = "".join(answer_parts)
    citations = _build_citations(
        top_rows,
        snippet_chars_for_response=settings.analysis_snippet_chars,
        original_scores=(
            rerank_result.original_scores if rerank_result is not None else None
        ),
        rerank_provider=(
            rerank_result.provider if rerank_result is not None else None
        ),
    )
    # `AnalysisDoneEvent.citations` is `list[dict] | None` — the WS
    # publisher needs JSON-serialisable payloads; `analyse_query_once`
    # re-hydrates via `QueryCitation.model_validate(...)` for the
    # REST path. Convert once here so both paths see the same shape.
    citations_payload = [c.model_dump(mode="json") for c in citations]
    yield AnalysisDoneEvent(
        answer=answer,
        citations=citations_payload,
        conversation_id=conversation_id,
    )


async def analyse_query_once(
    *,
    db_async: Any,
    owner_id: UUID,
    question: str,
    document_ids: Sequence[UUID] | None,
    stream_id: UUID,
    conversation_id: UUID | None = None,
    llm_provider: Any | None = None,
    rewrite_llm_provider: Any | None = None,
    embedding_model: Any | None = None,
    history: Sequence[LLMMessage] | None = None,
) -> "QueryOutcome":
    """Drain `analyse_query` and return either a `QueryResponse` or an
    `ErrorCode` constant. Cancellation is surfaced as `CANCELLED`.

    `llm_provider` and `embedding_model` overrides exist for tests.
    """
    answer_parts: list[str] = []
    citations: list[QueryCitation] = []
    error_code: str | None = None

    async for event in analyse_query(
        db_async=db_async,
        owner_id=owner_id,
        question=question,
        document_ids=document_ids,
        stream_id=stream_id,
        conversation_id=conversation_id,
        llm_provider=llm_provider,
        rewrite_llm_provider=rewrite_llm_provider,
        embedding_model=embedding_model,
        history=history,
    ):
        if isinstance(event, AnalysisTokenEvent):
            answer_parts.append(event.text)
        elif isinstance(event, AnalysisDoneEvent):
            # `AnalysisDoneEvent.citations` is `list[dict] | None` —
            # the in-process shape carries dicts for WS payload symmetry.
            # Re-hydrate to `QueryCitation` for the REST response.
            answer_parts = [event.answer]
            for entry in event.citations or []:
                citations.append(QueryCitation.model_validate(entry))
        elif isinstance(event, AnalysisErrorEvent):
            error_code = event.code
            break

    if error_code is not None:
        return _ErrorOutcome(code=error_code, message="Analysis failed.")
    return _QueryOutcome(
        response=QueryResponse(answer="".join(answer_parts), citations=citations)
    )


async def analyse_query_with_conversation(
    *,
    session_factory: Any | None = None,
    db_async: Any | None = None,
    owner_id: UUID,
    question: str,
    stream_id: UUID,
    conversation_id: UUID,
    document_ids: Sequence[UUID] | None = None,
    llm_provider: Any | None = None,
    rewrite_llm_provider: Any | None = None,
    embedding_model: Any | None = None,
) -> AsyncIterator[AnalysisEvent]:
    """Chat-aware orchestration: persists turns around the analysis stream.

    Behaviour:

    1. Resolve the effective scope. If ``document_ids`` is provided, it
       overrides the conversation's saved selection (one-off experiment
       on top of the saved chat). Otherwise we read the conversation's
       ``ConversationDocument`` rows — or expand to all READY docs if
       the conversation has ``scope_all_documents=True``.
    2. Insert the user message immediately (before retrieval). If the
       conversation doesn't exist or doesn't belong to the user, yield
       ``UNAUTHORIZED`` and stop — never write a row to the wrong chat.
    3. Fetch the most recent non-error turns (settings
       ``analysis_chat_history_*``) into a ``list[LLMMessage]`` so
       the prompt can carry a ``<CHAT_HISTORY>`` block. Failure here
       is non-fatal — the orchestrator falls back to the chat-less
       prompt and a warning is logged.
    4. Drive ``analyse_query`` with ``conversation_id`` threaded through.
       Yield every event unchanged so the caller can publish WS tokens.
    5. On terminal events, persist the assistant row:
       * ``AnalysisDoneEvent`` → full answer + citations, no error.
       * ``AnalysisErrorEvent`` → empty content + the error code, so
         the chat UI can show a red bubble with a retry button.
       * On cancellation (``CancelledError`` before a terminal event),
         the assistant row is intentionally NOT written — partial
         streams are dropped (see chat-history design doc).
    6. Bumps ``Conversation.last_message_at`` on every successful
       user or assistant insert via the repo helper.

    The user's title is auto-set to the first 60 chars of the question
    if the conversation doesn't already have one.

    Session model:
        This generator **owns its DB sessions**, scoped to itself, so
        nothing leaks from the FastAPI request-scoped session. Each
        ``async with session_factory()`` opens a short-lived
        ``AsyncSession`` that auto-closes and rolls back on exception.
        Never reuse a request-scoped session across an async generator
        that yields control back to the event loop — once that request
        commits (e.g. to publish a WS event) the session enters the
        'prepared' state and any further ``db.execute()`` raises
        ``InvalidRequestError`` (this was the bug that prompted the
        refactor).

    Args:
        session_factory: an ``async_sessionmaker[AsyncSession]`` used
            to open per-chunk sessions inside the generator. Required
            for the chat UI flow.
        db_async: **Legacy** — a single AsyncSession to use for every
            DB call. Kept so older callers (and unit tests that pass
            ``None`` or a fake session) don't break. New code should
            pass ``session_factory`` instead.
    """
    # Resolver that picks a fresh session per call when a factory is
    # available, otherwise falls back to the legacy single-session
    # path. ``None`` means "no DB available" (used by tests).
    async def _open_db():
        if session_factory is not None:
            return session_factory()
        if db_async is not None:
            # Adapter: turn a single session into a context manager so
            # the ``async with`` pattern below stays uniform. The
            # session is NOT closed — that's the caller's job.
            return _NullCtx(db_async)
        return _NullCtx(None)

    # --- 1. Resolve scope + 2. Persist user turn --------------------------
    # `async with db.begin():` opens a transaction that auto-commits on
    # success and rolls back on exception. Without it the session
    # closes without committing (SQLAlchemy 2.x defaults to
    # autobegin=False on a fresh session) and the inserts silently
    # vanish — which is exactly the bug that prompted this fix.
    async with await _open_db() as db:
        async with db.begin():
            conv = await conversation_repo.get_conversation(
                db, conversation_id, owner_id=owner_id
            )
            if conv is None:
                yield AnalysisErrorEvent(
                    code="UNAUTHORIZED",
                    message="Conversation not found.",
                    conversation_id=conversation_id,
                )
                return

            effective_doc_ids = await _resolve_conversation_scope(
                db, owner_id=owner_id, conv=conv, document_ids_override=document_ids
            )
            if not effective_doc_ids:
                # Saved scope is empty AND caller didn't override. Persist
                # the user turn so the question is visible, then yield
                # NO_DOCUMENTS_READY so the UI can prompt the user to pick
                # docs.
                user_msg = await conversation_repo.insert_message(
                    db,
                    conversation_id=conversation_id,
                    owner_id=owner_id,
                    role=MessageRole.user,
                    content=question,
                )
                if user_msg is not None:
                    _publish_user_message(
                        owner_id, stream_id, conversation_id, user_msg
                    )
                if conv.title is None:
                    await _autotitle(db, conv, question)
                yield AnalysisErrorEvent(
                    code="NO_DOCUMENTS_READY",
                    message=(
                        "This conversation has no documents selected. "
                        "Pick at least one or enable 'All documents'."
                    ),
                    conversation_id=conversation_id,
                )
                return

            user_msg = await conversation_repo.insert_message(
                db,
                conversation_id=conversation_id,
                owner_id=owner_id,
                role=MessageRole.user,
                content=question,
            )
            if user_msg is not None:
                _publish_user_message(
                    owner_id, stream_id, conversation_id, user_msg
                )
            if conv.title is None:
                await _autotitle(db, conv, question)

    # --- 3. Drive analyse_query and persist terminal events --------------
    # Note: each yield to the caller (which awaits WS publish, sleeps,
    # etc.) gives the event loop a chance to run other coroutines.
    # Sharing a session across that boundary is unsafe; the factory
    # gives us a fresh one per chunk below.
    #
    # `analyse_query` itself only does ONE DB call (scope resolution)
    # before yielding the LLM token stream — the streaming portion
    # never touches the DB. So one short-lived session covers it.
    answer_parts: list[str] = []
    terminal_done: AnalysisDoneEvent | None = None
    terminal_error: AnalysisErrorEvent | None = None
    chat_history_messages: list[LLMMessage] = []

    # Fetch prior turns before driving analyse_query. We use a
    # dedicated short-lived session so a slow / failing history
    # fetch never blocks the streaming path — a failure here just
    # degrades to the chat-less prompt.
    settings = get_settings()
    if settings.analysis_chat_history_enabled and settings.analysis_chat_history_max_turns > 0:
        try:
            async with await _open_db() as history_db:
                chat_history_messages = await fetch_recent_turns(
                    history_db,
                    conversation_id=conversation_id,
                    owner_id=owner_id,
                    max_turns=settings.analysis_chat_history_max_turns,
                    max_assistant_tokens=settings.analysis_chat_history_max_assistant_tokens,
                )
        except Exception as exc:  # noqa: BLE001 — defensive: history never breaks a query
            logger.warning(
                "analyse_query_with_conversation: history fetch failed; "
                "continuing chat-less. err=%s",
                exc,
            )
            chat_history_messages = []

    try:
        # `analyse_query` only reads from the DB (scope resolution);
        # no writes happen here, so we don't need `db.begin()` — just
        # a fresh session scoped to the LLM stream.
        async with await _open_db() as db:
            async for event in analyse_query(
                db_async=db,
                owner_id=owner_id,
                question=question,
                document_ids=effective_doc_ids,
                stream_id=stream_id,
                conversation_id=conversation_id,
                llm_provider=llm_provider,
                rewrite_llm_provider=rewrite_llm_provider,
                embedding_model=embedding_model,
                history=chat_history_messages,
            ):
                if isinstance(event, AnalysisTokenEvent):
                    answer_parts.append(event.text)
                    yield event
                elif isinstance(event, AnalysisDoneEvent):
                    terminal_done = event
                    yield event
                    break
                elif isinstance(event, AnalysisErrorEvent):
                    terminal_error = event
                    yield event
                    break
    except asyncio.CancelledError:
        # Client disconnected mid-stream. Drop the partial answer —
        # the user message is already saved so the chat history still
        # shows the question, but no orphan fragment is persisted.
        logger.info(
            "analyse_query_with_conversation: cancelled conv_id=%s stream_id=%s",
            conversation_id, stream_id,
        )
        raise

    # --- 4. Persist the assistant turn -----------------------------------
    if terminal_done is not None or terminal_error is not None:
        # Same commit-or-vanish fix as the pre-stream block: the session
        # must be inside a transaction or the inserts never reach the DB.
        async with await _open_db() as db:
            async with db.begin():
                if terminal_done is not None:
                    await conversation_repo.insert_message(
                        db,
                        conversation_id=conversation_id,
                        owner_id=owner_id,
                        role=MessageRole.assistant,
                        content=terminal_done.answer,
                        citations=list(terminal_done.citations or []),
                    )
                elif terminal_error is not None:
                    await conversation_repo.insert_message(
                        db,
                        conversation_id=conversation_id,
                        owner_id=owner_id,
                        role=MessageRole.assistant,
                        content="",  # error bubble is rendered from error_code, not text
                        citations=None,
                        error_code=terminal_error.code,
                    )


async def _resolve_conversation_scope(
    db: Any,
    *,
    owner_id: UUID,
    conv: Any,
    document_ids_override: Sequence[UUID] | None,
) -> list[UUID]:
    """Pick the document scope for one query under a conversation.

    - ``document_ids_override`` (non-None) wins, even if empty (means
      "user picked nothing this turn" → returns ``[]`` so the caller
      surfaces NO_DOCUMENTS_READY).
    - Else if ``conv.scope_all_documents`` → owner's full READY set.
    - Else → ``ConversationDocument`` rows for this chat.

    ``db`` may be a real ``AsyncSession`` (production), ``None`` (legacy
    single-session callers / unit tests), or a fake session. The repo
    helpers handle ``None``-ish inputs gracefully — for example the
    unit-test fakes ignore ``db`` entirely.
    """
    if document_ids_override is not None:
        return list(document_ids_override)
    if conv.scope_all_documents:
        docs = await document_repo.list_ready_documents(db, owner_id=owner_id)
        return [doc.id for doc in docs]
    return await conversation_repo.list_conversation_document_ids(
        db, conversation_id=conv.id, owner_id=owner_id
    )


class _NullCtx:
    """Async context manager that yields a single object without closing it.

    Used when a caller passes a pre-existing ``AsyncSession`` (legacy
    path) or ``None`` (no-DB path). Lets the generator code use a
    uniform ``async with await _open_db() as db:`` regardless of which
    session strategy the caller chose.

    ``__aenter__`` also returns a context-manager-like object so the
    subsequent ``async with db.begin():`` is a no-op for ``None`` /
    fake-session callers. For a real session it returns ``db`` so the
    inner ``async with db.begin():`` works as expected.
    """

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        if self._value is None:
            # No DB — return a no-op context so the caller's
            # `async with db.begin():` is skipped harmlessly.
            return _NoOpCtx()
        # Real session — return it so `async with db.begin():` works.
        return self._value

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _NoOpCtx:
    """No-op async context manager that mimics an ``AsyncSession``.

    Used for the legacy no-DB path (unit tests pass ``db_async=None``).
    Mirrors just enough of the session API the orchestrator touches so
    the same code path works for both real sessions (``begin()``
    commits, ``flush()`` flushes) and fake/None callers (everything is
    a no-op).
    """

    def begin(self) -> "_NoOpCtx":
        return self

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> "_NoOpCtx":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


async def _autotitle(db_async: Any, conv: Any, question: str) -> None:
    """Set the conversation title from the first question (truncated)."""
    snippet = question.strip().replace("\n", " ")
    if len(snippet) > 60:
        snippet = snippet[:59].rstrip() + "…"
    conv.title = snippet
    # `updated_at` may be unset on a freshly-created conv that hasn't
    # been refreshed yet; fall back to UTC.
    tz = getattr(conv.updated_at, "tzinfo", None)
    now = datetime.now(tz=tz) if tz is not None else datetime.now(tz=timezone.utc)
    conv.updated_at = now
    if db_async is not None:
        await db_async.flush()


def _publish_user_message(
    owner_id: UUID,
    stream_id: UUID,
    conversation_id: UUID,
    msg: Any,
) -> None:
    """Push a `analysis.user_message` WS event for a just-persisted user row.

    Called inside the orchestrator right after `insert_message(role=user)`
    succeeds — the row already has an `id` and `created_at` from
    `db.flush()`. The frontend uses this to splice the user's bubble into
    its chat-history cache and show a typing indicator while the LLM
    stream runs.

    Best-effort: a WS-publish failure never rolls back the DB write.
    `publish_analysis_user_message` already swallows Redis errors and
    logs them, so a missing pub/sub only delays the UI update — the
    user can still see the message via the next refetch.

    Defensive construction: the repo can return either a real SQLAlchemy
    row (with `created_at` populated by `db.refresh()`) or a unit-test
    fake (SimpleNamespace without `created_at`). We build the payload
    attribute-by-attribute so missing fields become safe defaults
    instead of pydantic ValidationErrors.
    """
    payload = {
        "id": str(msg.id),
        "role": msg.role.value if hasattr(msg.role, "value") else str(msg.role),
        "content": msg.content,
        "citations": list(msg.citations) if getattr(msg, "citations", None) else [],
        "error_code": getattr(msg, "error_code", None),
        "created_at": (
            msg.created_at.isoformat()
            if getattr(msg, "created_at", None) is not None
            else datetime.now(timezone.utc).isoformat()
        ),
    }
    publish_analysis_user_message(
        owner_id,
        stream_id=stream_id,
        conversation_id=conversation_id,
        message=payload,
    )


# Re-export the underlying types the handlers/tests need.
__all__ = [
    "QueryOutcome",
    "RerankResult",
    "analyse_query",
    "analyse_query_once",
    "analyse_query_with_conversation",
    "rank_top_rows",
    "rank_top_rows_with_mode",
    "rerank_top_rows",
    "rerank_top_rows_async",
]
