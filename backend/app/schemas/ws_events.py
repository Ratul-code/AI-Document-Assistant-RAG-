"""Typed envelopes for server-pushed WebSocket events.

Why an envelope
---------------
We want the server to push one event when something changes (e.g. a
document finishes uploading) and have the client decide what to do —
show a toast, invalidate the document-list cache, etc. The server
should NOT need to know which screens the user has open or which
React Query keys to invalidate.

So every push is wrapped in a small typed envelope:

    {
      "type": "document.status",   # event discriminator
      "data": { ... },             # event-specific payload
      "ts":   "2026-07-31T18:31:31Z" # server-side emit time (UTC ISO 8601)
    }

Adding a new event = add a new entry to `WsEventType` and write a small
builder. The wire format and client dispatch table stay unchanged.

The client-side counterpart should mirror this — a TypeScript discriminated
union keyed on `type`. See the frontend snippet in the project README
for the matching shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class WsEventType(str, Enum):
    """Discriminator for server→client WebSocket events.

    Add new event types here as features land. The client dispatcher
    is a switch on `type` — adding a case is the entire frontend change
    needed for a new event.
    """

    # A document's lifecycle status changed (processing → uploaded →
    # ready, or → failed). Includes the new status and any error
    # message so the UI can update badges / show inline errors / toasts.
    document_status = "document.status"

    # A new entry was added to the per-document activity log. The
    # payload mirrors `DocumentLogItem` so the client can prepend it
    # to the activity feed without a refetch.
    document_log = "document.log"

    # Per-batch embedding progress: phase + counters. Pushed after
    # each batch commits and at the start/end of the embedding phase.
    # Drives the progress bars in the detail dialog; carries counters
    # only, no text — the `document.log` event carries the human-
    # readable narration.
    document_embedding_progress = "document.embedding_progress"

    # RAG answer streaming — one event per token delta. The full
    # message is reconstructed client-side and finalised by `analysis.done`.
    # Pair with `stream_id` so multiple in-flight streams coexist.
    analysis_token = "analysis.token"
    # User question persisted to the DB right after `insert_message`,
    # BEFORE the LLM stream starts. Lets the chat UI show the user's
    # bubble + a typing indicator immediately instead of waiting for the
    # whole answer to land. `message` mirrors `ConversationMessageRead`
    # so the client can drop it into its cache without refetching.
    analysis_user_message = "analysis.user_message"
    # Terminal event for a stream: carries the whole answer + citations.
    analysis_done = "analysis.done"
    # Terminal failure for a `stream_id`. `code` matches AnalysisErrorCode.
    analysis_error = "analysis.error"


# ---------------------------------------------------------------------
# Discriminated payload models — one per WsEventType value
# ---------------------------------------------------------------------

class DocumentStatusEventData(BaseModel):
    """Payload for `document.status` events.

    Used by the doc list to update a single row's badge / error message
    without a full refetch.
    """

    document_id: UUID
    title: str
    status: str  # DocumentStatus.value
    error_message: str | None = None


class DocumentLogEventData(BaseModel):
    """Payload for `document.log` events.

    `log` mirrors `DocumentLogItem.model_dump(mode="json")` so the
    client can splice the new row into the activity feed without a
    refetch. Kept as a free-form `dict` so the inner schema can grow
    (new fields on `DocumentLogItem`) without bumping the WS contract.
    """

    document_id: UUID
    log: dict[str, Any]


class DocumentEmbeddingProgressEventData(BaseModel):
    """Payload for `document.embedding_progress` events.

    Carries counters only — no narration, no `message`. The matching
    `document.log` event (if any) carries the human-readable line;
    this event is what the progress bars subscribe to.

    Field semantics:
      - `phase`: which pipeline step is being reported. The dialog
        lights up the matching segment in its phase strip.
      - `pages_total` / `pages_done`: only set during `chunking`.
      - `chunks_total` / `chunks_done`: set during `chunking` (cumulative
        across all pages) and `embedding` (cumulative across batches).
      - `failed_total`: cumulative failed chunks during `embedding`.
      - `model_name`: set on `embedding` events that need it (start,
        per-batch, terminal).
    """

    document_id: UUID
    phase: str  # "parsing" | "chunking" | "embedding"
    pages_total: int | None = None
    pages_done: int | None = None
    chunks_total: int | None = None
    chunks_done: int | None = None
    failed_total: int = 0
    model_name: str | None = None


# ---------------------------------------------------------------------
# Analysis event payloads (ADR-014).
# ---------------------------------------------------------------------


class AnalysisTokenEventData(BaseModel):
    """Payload for `analysis.token` events.

    `text` is one token delta from the LLM — concatenate across events
    for the same `stream_id` to reconstruct the answer. Empty deltas
    (`text == ""`) are filtered by the publisher; the client should
    still tolerate them defensively.

    `conversation_id` is set when the query was submitted under a
    persistent conversation (`QueryRequest.conversation_id`). The chat
    UI uses it to reconcile the stream with the open chat — both fields
    are present so clients can correlate on either.
    """

    stream_id: UUID
    conversation_id: UUID | None = None
    text: str


class AnalysisUserMessageEventData(BaseModel):
    """Payload for `analysis.user_message` events.

    Emitted by the orchestrator RIGHT AFTER the user turn is committed
    to the DB, before the LLM stream starts. `message` mirrors
    `ConversationMessageRead` so the client can splice it into its
    chat-history cache without a refetch.

    `stream_id` is included so the client can correlate this with the
    in-flight stream (and ignore events from other tabs / sessions).
    """

    model_config = ConfigDict(from_attributes=True)

    stream_id: UUID
    conversation_id: UUID
    message: dict[str, Any]


class AnalysisDoneEventData(BaseModel):
    """Payload for `analysis.done` events.

    Mirrors `QueryResponse` so a client that misses intermediate tokens
    can still hydrate the answer + Sources panel from this single event.
    """

    stream_id: UUID
    conversation_id: UUID | None = None
    answer: str
    citations: list[dict[str, Any]] = Field(default_factory=list)


class AnalysisErrorEventData(BaseModel):
    """Payload for `analysis.error` events (terminal for a `stream_id`).

    `code` matches `AnalysisErrorCode` so the client can render a
    human-readable message. `message` is operator-readable detail.
    """

    stream_id: UUID
    conversation_id: UUID | None = None
    code: str  # AnalysisErrorCode.value
    message: str


# ---------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------

class WsEvent(BaseModel):
    """What the server pushes on the wire.

    `type` tells the client which handler to invoke; `data` is the
    event-specific payload. `ts` is the server's emit time so the
    client can order/dedupe without trusting its own clock.
    """

    type: WsEventType
    data: dict[str, Any]
    ts: Annotated[datetime, Field(default_factory=lambda: datetime.now(timezone.utc))]


# ---------------------------------------------------------------------
# Builders — keep call sites short and type-safe
# ---------------------------------------------------------------------

def document_status_event(
    *,
    document_id: UUID,
    status: str,
    title: str,
    error_message: str | None = None,
) -> WsEvent:
    return WsEvent(
        type=WsEventType.document_status,
        data=DocumentStatusEventData(
            document_id=document_id,
            title=title,
            status=status,
            error_message=error_message,
        ).model_dump(mode="json"),
    )


def document_log_event(
    *,
    document_id: UUID,
    log: dict[str, Any],
) -> WsEvent:
    """One new log row for a document.

    `log` should be the `DocumentLogItem.model_dump(mode="json")` payload
    for the row — i.e. exactly what the REST `GET /documents/{id}/logs`
    endpoint would return for it. Keeping the shapes identical means
    the client can splice the WS row into its REST cache without any
    shape conversion.
    """
    return WsEvent(
        type=WsEventType.document_log,
        data=DocumentLogEventData(
            document_id=document_id,
            log=log,
        ).model_dump(mode="json"),
    )


def document_embedding_progress_event(
    *,
    document_id: UUID,
    phase: str,
    pages_total: int | None = None,
    pages_done: int | None = None,
    chunks_total: int | None = None,
    chunks_done: int | None = None,
    failed_total: int = 0,
    model_name: str | None = None,
) -> WsEvent:
    """Pipeline progress counters for a document.

    Fires:
      - Once when parsing completes (`phase="parsing"`).
      - Once per page during chunking (`phase="chunking"`).
      - Once at start, once per batch, and once at end during
        embedding (`phase="embedding"`).

    The progress-bar UI subscribes to this event; the activity feed
    subscribes to `document.log` for narration.
    """
    return WsEvent(
        type=WsEventType.document_embedding_progress,
        data=DocumentEmbeddingProgressEventData(
            document_id=document_id,
            phase=phase,
            pages_total=pages_total,
            pages_done=pages_done,
            chunks_total=chunks_total,
            chunks_done=chunks_done,
            failed_total=failed_total,
            model_name=model_name,
        ).model_dump(mode="json"),
    )


# ---------------------------------------------------------------------
# Analysis event builders (ADR-014)
# ---------------------------------------------------------------------


def analysis_token_event(
    *,
    stream_id: UUID,
    text: str,
    conversation_id: UUID | None = None,
) -> WsEvent:
    """One token delta. Empty strings are filtered by the publisher."""
    return WsEvent(
        type=WsEventType.analysis_token,
        data=AnalysisTokenEventData(
            stream_id=stream_id,
            conversation_id=conversation_id,
            text=text,
        ).model_dump(mode="json"),
    )


def analysis_user_message_event(
    *,
    stream_id: UUID,
    conversation_id: UUID,
    message: dict[str, Any],
) -> WsEvent:
    """One user turn just committed to the DB.

    `message` should be a `ConversationMessageRead.model_dump(mode="json")`
    payload — the client can splice it directly into its chat-history
    cache. Kept as a free-form `dict` here so we don't bind the WS
    envelope to a specific Pydantic model version; if
    `ConversationMessageRead` grows new fields later, both layers pick
    them up automatically.
    """
    return WsEvent(
        type=WsEventType.analysis_user_message,
        data=AnalysisUserMessageEventData(
            stream_id=stream_id,
            conversation_id=conversation_id,
            message=message,
        ).model_dump(mode="json"),
    )


def analysis_done_event(
    *,
    stream_id: UUID,
    answer: str,
    citations: list[dict[str, Any]],
    conversation_id: UUID | None = None,
) -> WsEvent:
    return WsEvent(
        type=WsEventType.analysis_done,
        data=AnalysisDoneEventData(
            stream_id=stream_id,
            conversation_id=conversation_id,
            answer=answer,
            citations=citations,
        ).model_dump(mode="json"),
    )


def analysis_error_event(
    *,
    stream_id: UUID,
    code: str,
    message: str,
    conversation_id: UUID | None = None,
) -> WsEvent:
    return WsEvent(
        type=WsEventType.analysis_error,
        data=AnalysisErrorEventData(
            stream_id=stream_id,
            conversation_id=conversation_id,
            code=code,
            message=message,
        ).model_dump(mode="json"),
    )
