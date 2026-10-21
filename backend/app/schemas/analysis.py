"""Pydantic schemas for the retrieval / QA (RAG) pipeline.

The pipeline has three request/response shapes on the wire:

* `QueryRequest` — what the client sends.
* `QueryResponse` — non-streaming REST answer (also embedded as the
  `analysis.done` payload for the streaming path).
* `QueryStreamStart` — what the streaming-init REST call returns
  immediately so the client has a `stream_id` to correlate WS events.

Citation models live here too so the REST response and the WS payload
share the exact same shape.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------
# Citation — appears in the response and in `analysis.done` events.
# ---------------------------------------------------------------------


class QueryCitation(BaseModel):
    """A single source backing a fragment of the assistant's answer.

    `index` is the 1-based marker the answer text references (`[1]`,
    `[2]`, ...). `score` is the score that decided this row's place in
    the final top-K — that's the cosine score when the reranker is off
    (ADR-018) and the cross-encoder score when the reranker is on
    (ADR-019). `original_score` and `rerank` are populated only when the
    reranker actually reranked this query; older clients can ignore
    them safely.
    """

    index: Annotated[int, Field(ge=1)]
    chunk_id: UUID
    document_id: UUID
    document_title: str
    page_number: int | None = None
    section_path: list[str] = Field(default_factory=list)
    snippet: str
    score: float
    # Filled in only when a reranker re-ordered the candidates
    # (ADR-019). When `rerank=False` (the default), the citation came
    # straight out of the retriever — `score` IS the retriever score.
    original_score: float | None = None
    rerank: bool = False
    # Identifier of the reranker provider that produced this ordering
    # (e.g. ``"cross_encoder:BAAI/bge-reranker-base"``). Useful for
    # debugging and for portfolio demos that want to show "this answer
    # was reranked by X". None means the retriever decided the order.
    rerank_provider: str | None = None


# ---------------------------------------------------------------------
# Request / response (non-streaming).
# ---------------------------------------------------------------------


class QueryRequest(BaseModel):
    """Body of `POST /analysis/query` and `POST /analysis/query/stream`.

    `document_ids`:
      - `None` (omitted) → search ALL of the caller's `READY` documents.
      - Empty list `[]`   → caller explicitly selected nothing; the
        server returns `NO_DOCUMENTS_READY` without doing any work.
      - Non-empty list   → search exactly these docs (each still must
        be `READY` and owned by the caller; anything else is filtered).

    `conversation_id` (NEW):
      - `None` (omitted) → no persistence; matches the legacy single-shot
        analysis behaviour. Use this for ad-hoc / one-off questions.
      - UUID → persist the user + assistant turns to this conversation
        and tag the WS `analysis.*` events with the conversation id so
        the chat UI can reconcile the stream with the open chat.
        The user must own the conversation or the server returns 404.
    """

    question: Annotated[str, Field(min_length=1, max_length=2000)]
    document_ids: list[UUID] | None = Field(default=None)
    conversation_id: UUID | None = Field(default=None)


class QueryResponse(BaseModel):
    """The non-streaming REST answer.

    Same shape as the `analysis.done` WS payload so a client that
    misses the stream can fall back to the REST endpoint and render
    identical UI.
    """

    answer: str
    citations: list[QueryCitation] = Field(default_factory=list)


class QueryStreamStart(BaseModel):
    """Returned immediately by `POST /analysis/query/stream`.

    The client subscribes to the WebSocket and correlates events whose
    `stream_id` matches this value. HTTP 202 Accepted is the expected
    status — work happens asynchronously and the client waits on the
    WS channel.
    """

    stream_id: UUID


# ---------------------------------------------------------------------
# Error codes — surfaced as `analysis.error` payload `code` and as the
# REST fallback's machine-readable error code.
# ---------------------------------------------------------------------


AnalysisErrorCode = Literal[
    "NO_DOCUMENTS_READY",  # caller has no READY docs (or selected none)
    "PROVIDER_MISMATCH",  # query-time embedding dim != stored dim
    "PROVIDER_ERROR",  # LLM call failed (timeout, 5xx, rate limit)
    "CANCELLED",  # client disconnected mid-stream
    "INTERNAL",  # unexpected server error
    "UNAUTHORIZED",  # auth failed (defence-in-depth; should never reach here)
]


# Public re-exports for convenient imports elsewhere in the app.
__all__ = [
    "AnalysisErrorCode",
    "QueryCitation",
    "QueryRequest",
    "QueryResponse",
    "QueryStreamStart",
]
