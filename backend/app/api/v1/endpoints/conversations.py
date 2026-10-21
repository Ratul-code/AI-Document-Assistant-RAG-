"""Conversation chat endpoints.

Routes
------

* ``POST   /conversations``                       — start a new chat (with optional doc selection)
* ``GET    /conversations``                       — sidebar list (most recent first)
* ``GET    /conversations/{id}``                  — one chat's metadata + doc selection
* ``PATCH  /conversations/{id}``                  — rename, change doc selection
* ``DELETE /conversations/{id}``                  — drop chat + cascade messages
* ``GET    /conversations/{id}/messages``         — paginated history (oldest → newest)
* ``POST   /conversations/{id}/messages``         — submit a question; returns 202 + stream_id

All routes are owner-scoped via the conversation repo (returns ``None``
on cross-user access → 404 here, never 403, so we don't leak the
existence of other users' chats).

The streaming path re-uses the existing ``analysis.*`` WS envelope. The
chat UI subscribes to the existing ``/ws`` socket and filters events by
both ``stream_id`` (in-flight correlation) and ``conversation_id``
(which chat is open).
"""

# NOTE: this module intentionally OMITS `from __future__ import annotations`.
# slowapi's `@limiter.limit(...)` wraps the route handler, and FastAPI's
# `lenient_issubclass` check (which detects `Request` special parameters)
# requires real class objects, not stringified forward references. With
# `from __future__ import annotations` enabled, FastAPI treats `request:
# Request` as a query param and the route 422s on POST. See the matching
# comment in `app/api/v1/endpoints/auth.py` for the long form.

import asyncio
import logging
import uuid
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUserDep, DbDep
from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.db.session import get_session_factory
from app.repositories import conversation as conversation_repo
from app.repositories import document as document_repo
from app.schemas.analysis import AnalysisErrorCode, QueryCitation, QueryStreamStart
from app.schemas.conversation import (
    ConversationCreate,
    ConversationListItem,
    ConversationListResponse,
    ConversationMessageListResponse,
    ConversationMessageRead,
    ConversationMessageSubmit,
    ConversationRead,
    ConversationUpdate,
)
from app.services.document.analyser import (
    analyse_query_with_conversation,
)
from app.services.document.analyser.events import (
    AnalysisDoneEvent,
    AnalysisErrorEvent,
    AnalysisTokenEvent,
)
from app.services.pubsub.ws_publisher import (
    publish_analysis_done,
    publish_analysis_error,
    publish_analysis_token,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ============================================================
# Helpers
# ============================================================


async def _read_conversation(
    db: AsyncSession,
    conversation_id: UUID,
    owner_id: UUID,
) -> ConversationRead:
    """Load + 404 helper. Builds the wire-shaped `ConversationRead` with
    the resolved doc-selection list baked in."""
    conv = await conversation_repo.get_conversation(
        db, conversation_id, owner_id=owner_id
    )
    if conv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )

    if conv.scope_all_documents:
        # Don't materialise the full doc list — the frontend can show
        # "All documents" and the orchestrator expands at query time.
        document_ids: list[UUID] = []
    else:
        document_ids = await conversation_repo.list_conversation_document_ids(
            db, conversation_id=conv.id, owner_id=owner_id
        )

    return ConversationRead(
        id=conv.id,
        title=conv.title,
        scope_all_documents=conv.scope_all_documents,
        document_ids=document_ids,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        last_message_at=conv.last_message_at,
    )


async def _validate_doc_ids(
    db: AsyncSession,
    *,
    owner_id: UUID,
    document_ids: list[UUID],
) -> None:
    """Reject references to documents the caller doesn't own.

    Defence-in-depth: the orchestrator's `_resolve_scope` already
    filters, but a wrong-id write would never break retrieval and
    would surprise the user later — 404 up front.
    """
    if not document_ids:
        return
    for did in document_ids:
        doc = await document_repo.get_document(db, did, owner_id=owner_id)
        if doc is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Document {did} not found.",
            )


# ============================================================
# Per-user stream registry for conversation submissions.
# ============================================================
#
# Same single-process cap pattern as the analysis endpoint. Multi-worker
# uvicorn would need a Redis-backed registry.

_active_streams: dict[UUID, dict[UUID, asyncio.Task[None]]] = {}


def _register_stream(user_id: UUID, stream_id: UUID, task: asyncio.Task[None]) -> bool:
    user_streams = _active_streams.setdefault(user_id, {})
    cap = get_settings().analysis_max_concurrent_streams_per_user
    if len(user_streams) >= cap:
        return False
    user_streams[stream_id] = task
    return True


def _finish_stream(user_id: UUID, stream_id: UUID) -> None:
    user_streams = _active_streams.get(user_id)
    if user_streams is None:
        return
    user_streams.pop(stream_id, None)
    if not user_streams:
        _active_streams.pop(user_id, None)


def _cancel_stream(user_id: UUID, stream_id: UUID) -> bool:
    """Cancel an in-flight conversation stream by id.

    Returns True if a matching task was found and `.cancel()` issued;
    False if the stream isn't in the registry (already finished, never
    started, or owned by a different user).

    Cancellation is a server-side request, not a guarantee. The
    orchestrator honours `asyncio.CancelledError` by dropping the
    partial assistant answer — the user's question is already saved,
    but no assistant row is written for the cancelled stream. The
    `_drive` wrapper then publishes an `analysis.error` with
    code="CANCELLED" so the chat UI can surface a stopped state and
    the messages cache stays in sync.
    """
    user_streams = _active_streams.get(user_id)
    if user_streams is None:
        return False
    task = user_streams.get(stream_id)
    if task is None:
        return False
    return task.cancel()


# ============================================================
# Routes
# ============================================================


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    tags=["conversations"],
    response_model=ConversationRead,
)
@limiter.limit("60/minute")
async def create_conversation(
    request: Request,
    payload: ConversationCreate,
    current_user: CurrentUserDep,
    db: DbDep,
) -> ConversationRead:
    """Start a new chat with an optional document selection."""
    if not payload.scope_all_documents and payload.document_ids:
        await _validate_doc_ids(
            db, owner_id=current_user.id, document_ids=payload.document_ids
        )

    conv = await conversation_repo.create_conversation(
        db,
        owner_id=current_user.id,
        title=payload.title,
        scope_all_documents=payload.scope_all_documents,
    )

    if payload.document_ids:
        await conversation_repo.set_conversation_documents(
            db,
            conversation_id=conv.id,
            owner_id=current_user.id,
            document_ids=payload.document_ids,
        )

    return await _read_conversation(db, conv.id, owner_id=current_user.id)


@router.get(
    "",
    tags=["conversations"],
    response_model=ConversationListResponse,
)
@limiter.limit("120/minute")
async def list_conversations(
    request: Request,
    current_user: CurrentUserDep,
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ConversationListResponse:
    """Sidebar list — most recently active chats first."""
    items = await conversation_repo.list_conversations(
        db, owner_id=current_user.id, limit=limit, offset=offset
    )
    total = await conversation_repo.count_conversations(db, owner_id=current_user.id)

    out: list[ConversationListItem] = []
    for conv in items:
        doc_count = 0
        if not conv.scope_all_documents:
            doc_count = await conversation_repo.count_conversation_documents(
                db, conversation_id=conv.id
            )
        out.append(
            ConversationListItem(
                id=conv.id,
                title=conv.title,
                last_message_at=conv.last_message_at,
                document_count=doc_count,
            )
        )

    return ConversationListResponse(
        items=out, total=total, limit=limit, offset=offset
    )


@router.get(
    "/{conversation_id}",
    tags=["conversations"],
    response_model=ConversationRead,
)
@limiter.limit("60/minute")
async def get_conversation(
    request: Request,
    conversation_id: UUID,
    current_user: CurrentUserDep,
    db: DbDep,
) -> ConversationRead:
    return await _read_conversation(db, conversation_id, owner_id=current_user.id)


@router.patch(
    "/{conversation_id}",
    tags=["conversations"],
    response_model=ConversationRead,
)
@limiter.limit("60/minute")
async def update_conversation(
    request: Request,
    conversation_id: UUID,
    payload: ConversationUpdate,
    current_user: CurrentUserDep,
    db: DbDep,
) -> ConversationRead:
    # 404 if not owned.
    existing = await conversation_repo.get_conversation(
        db, conversation_id, owner_id=current_user.id
    )
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )

    # Validate any explicit doc selection. If the user is clearing the
    # scope (empty list + scope_all_documents stays False), the next
    # query will surface NO_DOCUMENTS_READY — by design.
    if payload.document_ids is not None and not payload.scope_all_documents:
        await _validate_doc_ids(
            db, owner_id=current_user.id, document_ids=payload.document_ids
        )

    await conversation_repo.update_conversation(
        db,
        conversation_id,
        owner_id=current_user.id,
        title=payload.title,
        scope_all_documents=payload.scope_all_documents,
    )

    if payload.document_ids is not None:
        await conversation_repo.set_conversation_documents(
            db,
            conversation_id=conversation_id,
            owner_id=current_user.id,
            document_ids=payload.document_ids,
        )

    return await _read_conversation(db, conversation_id, owner_id=current_user.id)


@router.delete(
    "/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["conversations"],
    response_class=Response,
)
@limiter.limit("60/minute")
async def delete_conversation(
    request: Request,
    conversation_id: UUID,
    current_user: CurrentUserDep,
    db: DbDep,
) -> Response:
    deleted = await conversation_repo.delete_conversation(
        db, conversation_id, owner_id=current_user.id
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{conversation_id}/messages",
    tags=["conversations"],
    response_model=ConversationMessageListResponse,
)
@limiter.limit("120/minute")
async def list_messages(
    request: Request,
    conversation_id: UUID,
    current_user: CurrentUserDep,
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
    order: Annotated[
        Literal["asc", "desc"],
        Query(
            description=(
                "Sort direction by `created_at`. Default `asc` "
                "(oldest → newest). Use `desc` for the first page of "
                "a paginated UI that loads newer-first and then "
                "prepends older pages on scroll."
            )
        ),
    ] = "asc",
) -> ConversationMessageListResponse:
    """Paginated chat history.

    Sort direction is controlled by ``order``:

      - ``asc``  (default): oldest → newest. Used by history readers
                and by paginated "load older" requests after the first
                page.
      - ``desc``: newest → oldest. Used by the first page of the
                chat history so the client can show the newest turns
                immediately, then prepend older turns on scroll.
    """
    owned = await conversation_repo.get_conversation(
        db, conversation_id, owner_id=current_user.id
    )
    if owned is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )

    rows = await conversation_repo.list_messages(
        db,
        conversation_id=conversation_id,
        owner_id=current_user.id,
        limit=limit,
        offset=offset,
        order=order,
    )
    total = await conversation_repo.count_messages(
        db, conversation_id=conversation_id, owner_id=current_user.id
    )

    items: list[ConversationMessageRead] = []
    for row in rows:
        cits: list[QueryCitation] = []
        if row.citations:
            for entry in row.citations:
                cits.append(QueryCitation.model_validate(entry))
        items.append(
            ConversationMessageRead(
                id=row.id,
                role=row.role.value if hasattr(row.role, "value") else str(row.role),
                content=row.content,
                citations=cits,
                error_code=row.error_code,  # type: ignore[arg-type]
                created_at=row.created_at,
            )
        )

    return ConversationMessageListResponse(
        items=items, total=total, limit=limit, offset=offset
    )


@router.post(
    "/{conversation_id}/messages",
    status_code=status.HTTP_202_ACCEPTED,
    tags=["conversations"],
    response_model=QueryStreamStart,
)
@limiter.limit("30/minute")
async def submit_message(
    request: Request,
    conversation_id: UUID,
    payload: ConversationMessageSubmit,
    current_user: CurrentUserDep,
    db: DbDep,
) -> QueryStreamStart:
    """Submit a question to a conversation.

    Returns ``202 Accepted`` with a ``stream_id`` immediately. The user
    row is inserted by the orchestrator *before* the first WS token is
    yielded, so it's in DB by the time the client receives anything.
    The assistant response arrives on the existing ``/ws`` socket as
    ``analysis.*`` events tagged with both ``stream_id`` and
    ``conversation_id``.

    The orchestrator persists the assistant turn on the terminal
    ``analysis.done`` (full answer + citations) or ``analysis.error``
    event (with ``error_code``, empty content). See
    ``analyse_query_with_conversation``.
    """
    # 404 if conversation not owned.
    owned = await conversation_repo.get_conversation(
        db, conversation_id, owner_id=current_user.id
    )
    if owned is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )

    # Validate any override document ids.
    if payload.document_ids is not None:
        await _validate_doc_ids(
            db, owner_id=current_user.id, document_ids=payload.document_ids
        )

    # Per-user concurrency cap — same pattern as the analysis endpoint.
    user_id = current_user.id
    stream_id = uuid.uuid4()

    cap = get_settings().analysis_max_concurrent_streams_per_user
    active = _active_streams.get(user_id, {})
    if len(active) >= cap:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "INTERNAL",
                "message": (
                    f"Too many in-flight streams (cap={cap}). "
                    "Wait for one to finish or cancel."
                ),
            },
        )

    async def _drive() -> None:
        # Hand the orchestrator a session factory, not the request
        # session. The orchestrator opens short-lived sessions for
        # each DB call so it survives across WS publish awaits —
        # sharing the request session here would trip the
        # "session is in 'prepared' state" error as soon as anything
        # in the request handler commits.
        session_factory = get_session_factory()
        try:
            async for event in analyse_query_with_conversation(
                session_factory=session_factory,
                owner_id=user_id,
                question=payload.question,
                stream_id=stream_id,
                conversation_id=conversation_id,
                document_ids=payload.document_ids,
            ):
                if isinstance(event, AnalysisTokenEvent):
                    publish_analysis_token(
                        user_id,
                        stream_id=stream_id,
                        text=event.text,
                        conversation_id=conversation_id,
                    )
                elif isinstance(event, AnalysisDoneEvent):
                    publish_analysis_done(
                        user_id,
                        stream_id=stream_id,
                        answer=event.answer,
                        citations=list(event.citations or []),
                        conversation_id=conversation_id,
                    )
                elif isinstance(event, AnalysisErrorEvent):
                    publish_analysis_error(
                        user_id,
                        stream_id=stream_id,
                        code=event.code,
                        message=event.message,
                        conversation_id=conversation_id,
                    )
        except asyncio.CancelledError:
            publish_analysis_error(
                user_id,
                stream_id=stream_id,
                code="CANCELLED",
                message="Stream cancelled by server.",
                conversation_id=conversation_id,
            )
            raise
        except Exception as exc:
            logger.exception(
                "conversation submit crashed conv_id=%s err=%s",
                conversation_id, exc,
            )
            publish_analysis_error(
                user_id,
                stream_id=stream_id,
                code="INTERNAL",
                message=str(exc)[:500],
                conversation_id=conversation_id,
            )
        finally:
            _finish_stream(user_id, stream_id)

    task = asyncio.create_task(_drive(), name=f"conv-stream-{stream_id}")
    if not _register_stream(user_id, stream_id, task):
        task.cancel()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "INTERNAL",
                "message": f"Too many in-flight streams (cap={cap}).",
            },
        )
    task.add_done_callback(lambda _t: _finish_stream(user_id, stream_id))

    logger.info(
        "conversation submit user_id=%s conv_id=%s stream_id=%s question_len=%d",
        user_id, conversation_id, stream_id, len(payload.question),
    )

    return QueryStreamStart(stream_id=stream_id)


@router.delete(
    "/{conversation_id}/messages/{stream_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["conversations"],
    response_class=Response,
)
@limiter.limit("30/minute")
async def cancel_message(
    request: Request,
    conversation_id: UUID,
    stream_id: UUID,
    current_user: CurrentUserDep,
    db: DbDep,
) -> Response:
    """Cancel an in-flight assistant stream and prevent persistence.

    Flow:
      1. Owner-scope check on the conversation (same 404 contract as
         every other endpoint — no info leak across users).
      2. Look up the registered task by `(user_id, stream_id)`. If no
         task is registered, the stream is either already finished or
         never existed — return 404 so the client can stop showing the
         stop button. We deliberately do NOT distinguish "already
         finished" from "never existed" so a client polling after a
         normal `done` event doesn't learn anything it shouldn't.
      3. Issue `task.cancel()`. The orchestrator's
         `asyncio.CancelledError` branch in `_drive` publishes an
         `analysis.error` with code="CANCELLED" and re-raises without
         persisting the assistant turn. The chat UI's existing
         `useAssistantMessageBridge` revalidates the messages cache
         on the terminal event so the cancelled turn vanishes from
         the history (the user message is still there — it was
         persisted before the LLM stream started).

    Idempotency: calling cancel twice is safe. The first call
    cancels the task and returns 204; the second call's registry
    lookup misses and returns 404. Clients that retry on flaky
    networks won't see a confusing 5xx.

    Why a server-side cancel (and not just clearing the client
    store): without this endpoint the orchestrator would run to
    completion server-side, write the assistant row, and the user
    would see a half-written answer reappear on the next page load.
    The user message stays — that was the user's input — but the
    assistant turn is dropped.
    """
    # 1. Owner-scoped conversation check. 404 on cross-owner access
    #    so we don't leak existence of other users' chats.
    owned = await conversation_repo.get_conversation(
        db, conversation_id, owner_id=current_user.id
    )
    if owned is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )

    # 2. Look up the registry. Missing means "already finished or
    #    never started" — both surface as 404 to keep the contract
    #    uniform.
    cancelled = _cancel_stream(current_user.id, stream_id)
    if not cancelled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Stream not found or already finished.",
        )

    logger.info(
        "conversation stream cancelled user_id=%s conv_id=%s stream_id=%s",
        current_user.id, conversation_id, stream_id,
    )

    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]