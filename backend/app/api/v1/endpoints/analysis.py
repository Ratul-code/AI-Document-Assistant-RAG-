"""Retrieval / QA (RAG) endpoints.

Two routes:

* `POST /analysis/query`        — non-streaming REST answer. Returns
  `QueryResponse` directly. Used by tests and as a fallback when the
  client can't keep a WebSocket open.

* `POST /analysis/query/stream` — streaming-init. Returns
  `QueryStreamStart{stream_id}` immediately (HTTP 202). The actual
  tokens arrive on the existing `/ws` socket as `analysis.token`,
  `analysis.done`, or `analysis.error` events tagged with the same
  `stream_id`. The background task runs as a fire-and-forget asyncio
  task on the FastAPI event loop.

The handler-side error mapping lives in `_http_status_for_code` so
both routes share the same status-code policy.
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
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status

from app.api.dependencies import CurrentUserDep, DbDep
from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.schemas.analysis import (
    QueryRequest,
    QueryResponse,
    QueryStreamStart,
)
from app.services.document.analyser import (
    AnalysisDoneEvent,
    AnalysisErrorEvent,
    AnalysisEvent,
    AnalysisTokenEvent,
    analyse_query,
    analyse_query_once,
)
from app.services.pubsub.ws_publisher import (
    publish_analysis_done,
    publish_analysis_error,
    publish_analysis_token,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------
# HTTP status mapping — shared by both routes.
# ---------------------------------------------------------------------

_STATUS_FOR_CODE: dict[str, int] = {
    "NO_DOCUMENTS_READY": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "PROVIDER_MISMATCH": status.HTTP_503_SERVICE_UNAVAILABLE,
    "PROVIDER_ERROR": status.HTTP_502_BAD_GATEWAY,
    # CANCELLED is rare on the non-streaming REST path (only if the
    # client closed mid-flight). 503 surfaces "service is busy / gone"
    # without misleadingly claiming an HTTP-level client error.
    "CANCELLED": status.HTTP_503_SERVICE_UNAVAILABLE,
    "INTERNAL": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "UNAUTHORIZED": status.HTTP_401_UNAUTHORIZED,
}


def _http_status_for_code(code: str) -> int:
    return _STATUS_FOR_CODE.get(code, status.HTTP_500_INTERNAL_SERVER_ERROR)


def _detail_for(code: str, message: str) -> dict[str, str]:
    """Stable JSON shape for REST errors so the frontend can render
    them without free-text parsing."""
    return {"code": code, "message": message}


# ---------------------------------------------------------------------
# Per-user stream registry — best-effort cancellation.
# ---------------------------------------------------------------------

# Maps (user_id, stream_id) -> asyncio.Task. Tasks are removed when
# they complete normally. Capped at `analysis_max_concurrent_streams_per_user`
# per user. Single-process only — multi-worker uvicorn would need a
# Redis-backed registry, but that's not in scope here.

_active_streams: dict[UUID, dict[UUID, asyncio.Task[None]]] = {}


def _register_stream(user_id: UUID, stream_id: UUID, task: asyncio.Task[None]) -> bool:
    """Add the task to the registry. Returns False if the user already
    has the maximum number of in-flight streams; the caller should
    refuse the request in that case."""
    user_streams = _active_streams.setdefault(user_id, {})
    cap = get_settings().analysis_max_concurrent_streams_per_user
    if len(user_streams) >= cap:
        return False
    user_streams[stream_id] = task
    return True


def _finish_stream(user_id: UUID, stream_id: UUID, task: asyncio.Task[None]) -> None:
    user_streams = _active_streams.get(user_id)
    if user_streams is None:
        return
    user_streams.pop(stream_id, None)
    if not user_streams:
        _active_streams.pop(user_id, None)


# ---------------------------------------------------------------------
# Streaming-init background task.
# ---------------------------------------------------------------------


async def _run_stream(
    *,
    user_id: UUID,
    stream_id: UUID,
    db,
    question: str,
    document_ids: list[UUID] | None,
    conversation_id: UUID | None = None,
) -> None:
    """Drive `analyse_query` and translate each `AnalysisEvent` to a
    WS envelope. Catches all exceptions so a mid-stream failure
    publishes `analysis.error` instead of dying silently.

    CancelledError is allowed to propagate: the caller (the FastAPI
    route or the WS onclose handler) decides when to cancel."""
    try:
        async for event in analyse_query(
            db_async=db,
            owner_id=user_id,
            question=question,
            document_ids=document_ids,
            stream_id=stream_id,
            conversation_id=conversation_id,
        ):
            await _dispatch_event(user_id=user_id, stream_id=stream_id, event=event)
    except asyncio.CancelledError:
        logger.info("analysis stream cancelled user_id=%s stream_id=%s", user_id, stream_id)
        # Best-effort terminal event so a reconnecting client knows
        # the stream ended, not just went silent.
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
            "analysis stream crashed user_id=%s stream_id=%s", user_id, stream_id
        )
        publish_analysis_error(
            user_id,
            stream_id=stream_id,
            code="INTERNAL",
            message=str(exc)[:500],
            conversation_id=conversation_id,
        )


async def _dispatch_event(
    *, user_id: UUID, stream_id: UUID, event: AnalysisEvent
) -> None:
    """Translate one in-process event to a WS publish."""
    cid = getattr(event, "conversation_id", None)
    if isinstance(event, AnalysisTokenEvent):
        publish_analysis_token(
            user_id, stream_id=stream_id, text=event.text, conversation_id=cid
        )
    elif isinstance(event, AnalysisDoneEvent):
        # `citations` here is `list[dict] | None` — `model_dump(mode="json")`
        # on each QueryCitation would be ideal, but events store dicts
        # so the publisher round-trip is lossless.
        publish_analysis_done(
            user_id,
            stream_id=stream_id,
            answer=event.answer,
            citations=list(event.citations or []),
            conversation_id=cid,
        )
    elif isinstance(event, AnalysisErrorEvent):
        publish_analysis_error(
            user_id,
            stream_id=stream_id,
            code=event.code,
            message=event.message,
            conversation_id=cid,
        )


# ---------------------------------------------------------------------
# Routes.
# ---------------------------------------------------------------------


@router.post(
    "/query",
    status_code=status.HTTP_200_OK,
    tags=["analysis"],
    response_model=QueryResponse,
)
@limiter.limit("20/minute")
async def query(
    request: Request,
    payload: QueryRequest,
    current_user: CurrentUserDep,
    db: DbDep,
) -> QueryResponse:
    """Non-streaming REST answer.

    Same retrieval pipeline as the streaming path; the LLM is drained
    to completion before responding. Useful for tests, REST clients,
    and as a fallback when the WebSocket is unavailable.
    """
    document_ids = payload.document_ids  # may be None or list
    stream_id = uuid.uuid4()

    outcome = await analyse_query_once(
        db_async=db,
        owner_id=current_user.id,
        question=payload.question,
        document_ids=document_ids,
        stream_id=stream_id,
        conversation_id=payload.conversation_id,
    )

    # `analyse_query_once` returns a sum type (`_QueryOutcome` for
    # success, `_ErrorOutcome` for failure). Local imports are fine
    # because Python's symbol table resolves them at call time.
    from app.services.document.analyser import _ErrorOutcome, _QueryOutcome

    if isinstance(outcome, _QueryOutcome):
        # Also push a single `analysis.done` so any open WS session
        # sees the result (in case the client issued both REST and WS).
        try:
            publish_analysis_done(
                current_user.id,
                stream_id=stream_id,
                answer=outcome.response.answer,
                citations=[
                    c.model_dump(mode="json") for c in outcome.response.citations
                ],
            )
        except Exception:
            logger.exception("analysis: REST-fallback WS push failed")
        return outcome.response

    # Failure path. Map the error code to the right HTTP status so the
    # frontend can render a typed error message without free-text parsing.
    assert isinstance(outcome, _ErrorOutcome)
    http_status = _http_status_for_code(outcome.code)
    raise HTTPException(
        status_code=http_status,
        detail=_detail_for(outcome.code, outcome.message or "Analysis failed."),
    )


@router.post(
    "/query/stream",
    status_code=status.HTTP_202_ACCEPTED,
    tags=["analysis"],
    response_model=QueryStreamStart,
)
@limiter.limit("20/minute")
async def query_stream(
    request: Request,
    payload: QueryRequest,
    current_user: CurrentUserDep,
    db: DbDep,
) -> QueryStreamStart:
    """Streaming-init: returns a `stream_id` immediately. Tokens
    arrive on the existing `/ws` socket as `analysis.*` events
    correlated by `stream_id`. See ADR-012."""
    stream_id = uuid.uuid4()
    user_id = current_user.id

    # Per-user concurrency cap.
    cap = get_settings().analysis_max_concurrent_streams_per_user
    active = _active_streams.get(user_id, {})
    if len(active) >= cap:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_detail_for(
                "INTERNAL",
                f"Too many in-flight analysis streams (cap={cap}). "
                "Wait for one to finish or cancel.",
            ),
        )

    task = asyncio.create_task(
        _run_stream(
            user_id=user_id,
            stream_id=stream_id,
            db=db,
            question=payload.question,
            document_ids=payload.document_ids,
            conversation_id=payload.conversation_id,
        ),
        name=f"analysis-stream-{stream_id}",
    )

    if not _register_stream(user_id, stream_id, task):
        task.cancel()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_detail_for(
                "INTERNAL",
                f"Too many in-flight analysis streams (cap={cap}).",
            ),
        )

    # Clean up the registry when the task finishes (success, error, or cancel).
    def _on_done(t: asyncio.Task[None]) -> None:
        _finish_stream(user_id, stream_id, t)

    task.add_done_callback(_on_done)

    logger.info(
        "analysis stream started user_id=%s stream_id=%s question_len=%d",
        user_id, stream_id, len(payload.question),
    )

    return QueryStreamStart(stream_id=stream_id)


__all__ = ["router"]
