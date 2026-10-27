"""Sync Redis publisher for WebSocket events.

Used by the Celery worker (and any other sync code path) to push
server-side events to the FastAPI process(es). The worker is sync
(Celery workers run in a thread pool unless explicitly made async), so
we use the sync `redis.Redis` client.

Wire format (per-worker channel `ws:events:{worker_id}`):
    {
      "type": "<WsEventType>",
      "data": { ... },          # event-specific payload
      "ts":   "<iso8601 utc>"
    }

Multi-process (B3) fanout:
    The publisher enumerates live FastAPI workers via a Redis SET
    (`ws:workers`, maintained by a per-worker keepalive task in the
    FastAPI lifespan) and PUBLISHes one copy of the envelope to each
    worker's private channel. Each worker then dispatches to its own
    in-process `ConnectionManager.broadcast` — sockets are delivered
    regardless of which FastAPI process the user is connected to.

    When `ws_b3_enabled` is False, the publisher falls back to the v1
    single global channel behavior (one PUBLISH, every subscriber
    receives it, each routes by user_id).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any
from uuid import UUID

from redis import Redis

from app.core.config import get_settings
from app.models.document_log import DocumentLog
from app.schemas.document_log import DocumentLogItem
from app.schemas.ws_events import (
    analysis_done_event,
    analysis_error_event,
    analysis_token_event,
    analysis_user_message_event,
    document_embedding_progress_event,
    document_log_event,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Worker-list cache (B3 only)
#
# `SMEMBERS ws:workers` is one round-trip per publish. For the
# analysis-token hot path (one publish per LLM delta, possibly
# 50-100/sec) that's wasteful. Cache the worker list for
# `ws_worker_keepalive_s` (default 20s) — long enough to amortize the
# round trip, short enough that a dead worker is excluded within one
# cache window. The cache is per-process; multiple Celery processes
# each maintain their own view, which is fine.
# ---------------------------------------------------------------------


_worker_list_cache: list[str] | None = None
_worker_list_cache_at: float = 0.0
_worker_list_lock = threading.Lock()


def _get_live_workers(client: Redis) -> list[str]:
    """Return the current list of live worker_ids, cached for the
    configured keepalive interval.

    On any Redis error, return the previous cache (possibly stale) so
    publish doesn't fail. If the cache is empty AND Redis fails, return
    an empty list — the caller will publish zero messages and log.
    """
    global _worker_list_cache, _worker_list_cache_at
    settings = get_settings()
    ttl = float(settings.ws_worker_keepalive_s)
    now = time.monotonic()

    with _worker_list_lock:
        if _worker_list_cache is not None and (now - _worker_list_cache_at) < ttl:
            return list(_worker_list_cache)

    # Fetch outside the lock — SMEMBERS is slow vs. the lock window.
    #
    # redis-py's inline types for `smembers()` return `set[bytes | str]`
    # because the `decode_responses` flag is a *runtime* constructor
    # argument that the type system can't narrow on. `_get_client()`
    # always builds the client with `decode_responses=True`, so members
    # are guaranteed `str` here. Decode defensively anyway: it costs
    # nothing (the happy path hits the `isinstance` fast branch) and
    # keeps this correct if a caller ever passes a differently-configured
    # client.
    try:
        members = client.smembers(settings.ws_worker_registry_key)
        ids = (
            sorted(m.decode("utf-8") if isinstance(m, bytes) else m for m in members)
            if members
            else []
        )
    except Exception as exc:
        logger.warning("ws publisher: SMEMBERS %s failed: %s", settings.ws_worker_registry_key, exc)
        with _worker_list_lock:
            return list(_worker_list_cache) if _worker_list_cache is not None else []

    with _worker_list_lock:
        _worker_list_cache = ids
        _worker_list_cache_at = now
    return list(ids)


def _get_client() -> Redis:
    """Build a fresh sync Redis client.

    Cheap to construct; the underlying connection pool is reused per
    process. Workers are short-lived relative to FastAPI so we don't
    bother with a module-level singleton.
    """
    return Redis.from_url(get_settings().redis_url, decode_responses=True)


def publish_event(user_id: UUID, envelope: dict[str, Any]) -> int:
    """Publish a WS envelope to every live FastAPI worker (B3) or to
    the single global channel (v1 fallback).

    Args:
        user_id: Recipient user. Kept in the signature for back-compat
                 with every existing caller. In B3 mode it is NOT sent
                 in the payload (the subscriber no longer routes by
                 user_id — every worker calls manager.broadcast). In v1
                 mode it rides in the payload as before.
        envelope: The WsEvent.model_dump(mode="json") dict. Must already
                  include `type`, `data`, and `ts`.

    Returns:
        Number of subscribers the message was delivered to (sum across
        workers in B3 mode). 0 means no FastAPI process is running or
        Redis was unreachable. Best-effort — never raises.

    Note:
        Best-effort. If Redis is unreachable we log and return 0 — the
        Celery task itself must not fail because the WS push failed.
    """
    settings = get_settings()
    envelope_type = envelope.get("type")

    try:
        client = _get_client()

        if not settings.ws_b3_enabled:
            # v1 fallback: single global channel, per-user routing.
            payload = {"user_id": str(user_id), **envelope}
            channel = settings.ws_pubsub_channel
            delivered = client.publish(channel, json.dumps(payload))
            logger.info(
                "ws publish (v1) channel=%s user_id=%s type=%s delivered=%d",
                channel,
                user_id,
                envelope_type,
                delivered,
            )
            return int(delivered)

        # B3: fan out to every live worker.
        worker_ids = _get_live_workers(client)
        if not worker_ids:
            logger.debug(
                "ws publish (b3): no live workers in %s; envelope dropped",
                settings.ws_worker_registry_key,
            )
            return 0

        # Serialize once; the same JSON is sent to every worker.
        body = json.dumps(envelope)
        total_delivered = 0
        for wid in worker_ids:
            channel = settings.ws_pubsub_channel_pattern.format(worker_id=wid)
            try:
                delivered = client.publish(channel, body)
                total_delivered += int(delivered)
            except Exception as exc:
                # One bad channel must not abort the rest of the fanout.
                logger.warning(
                    "ws publish (b3) failed channel=%s worker_id=%s err=%s",
                    channel,
                    wid,
                    exc,
                )
        logger.info(
            "ws publish (b3) workers=%d total_delivered=%d type=%s",
            len(worker_ids),
            total_delivered,
            envelope_type,
        )
        return total_delivered
    except Exception as exc:
        logger.warning(
            "ws publish failed (redis unreachable?) user_id=%s err=%s",
            user_id,
            exc,
        )
        return 0


# ---------------------------------------------------------------------
# Document log + progress helpers
#
# The Celery ingestion workers (`app.tasks.document`,
# `app.tasks.embedding`) write log rows to the DB and push a matching
# WS event so the activity feed in the dialog updates in real time.
# The publish path is identical to every other helper: build a typed
# envelope, hand it to `publish_event`, return the subscriber count
# (0 in dev with no FastAPI process is fine).
# ---------------------------------------------------------------------


def publish_document_log(
    user_id: UUID,
    *,
    document_id: UUID,
    log_row: DocumentLog,
) -> int:
    """Push a `document.log` event for a freshly-committed log row.

    `log_row` is the SQLAlchemy row as it stands AFTER the worker's
    transaction committed — `id`, `created_at`, and `metadata_json`
    are all populated. We round-trip through `DocumentLogItem` so
    the on-wire shape is exactly what the REST endpoint would return
    (same field set, same JSON encoding for `metadata`), letting the
    client splice the row into its REST cache with no shape conversion.
    """
    item = DocumentLogItem.model_validate(log_row)
    envelope = document_log_event(
        document_id=document_id,
        log=item.model_dump(mode="json"),
    ).model_dump(mode="json")
    return publish_event(user_id, envelope)


def publish_embedding_progress(
    user_id: UUID,
    *,
    document_id: UUID,
    phase: str,
    pages_total: int | None = None,
    pages_done: int | None = None,
    chunks_total: int | None = None,
    chunks_done: int | None = None,
    failed_total: int = 0,
    model_name: str | None = None,
) -> int:
    """Push a `document.embedding_progress` event with pipeline counters.

    See `document_embedding_progress_event` for the field contract.
    """
    envelope = document_embedding_progress_event(
        document_id=document_id,
        phase=phase,
        pages_total=pages_total,
        pages_done=pages_done,
        chunks_total=chunks_total,
        chunks_done=chunks_done,
        failed_total=failed_total,
        model_name=model_name,
    ).model_dump(mode="json")
    return publish_event(user_id, envelope)


# ---------------------------------------------------------------------
# Analysis helpers (ADR-014)
#
# Thin wrappers that build the typed envelope, then delegate to the
# generic publish_event(). Empty token deltas are filtered out so we
# don't fan out noisy zero-byte messages.
# ---------------------------------------------------------------------


def publish_analysis_token(
    user_id: UUID,
    *,
    stream_id: UUID,
    text: str,
    conversation_id: UUID | None = None,
) -> int:
    """One token delta. Empty strings are skipped (returns 0, no publish)."""
    if not text:
        return 0
    envelope = analysis_token_event(
        stream_id=stream_id, text=text, conversation_id=conversation_id
    ).model_dump(mode="json")
    return publish_event(user_id, envelope)


def publish_analysis_user_message(
    user_id: UUID,
    *,
    stream_id: UUID,
    conversation_id: UUID,
    message: dict[str, Any],
) -> int:
    """One user turn just committed to the DB, before the LLM stream.

    The frontend uses this to show the user's bubble + a typing indicator
    immediately, instead of waiting for the whole answer to land. The
    payload mirrors `ConversationMessageRead` so the client can splice
    it into its cache without a refetch.
    """
    envelope = analysis_user_message_event(
        stream_id=stream_id,
        conversation_id=conversation_id,
        message=message,
    ).model_dump(mode="json")
    return publish_event(user_id, envelope)


def publish_analysis_done(
    user_id: UUID,
    *,
    stream_id: UUID,
    answer: str,
    citations: list[dict[str, Any]],
    conversation_id: UUID | None = None,
) -> int:
    envelope = analysis_done_event(
        stream_id=stream_id,
        answer=answer,
        citations=citations,
        conversation_id=conversation_id,
    ).model_dump(mode="json")
    return publish_event(user_id, envelope)


def publish_analysis_error(
    user_id: UUID,
    *,
    stream_id: UUID,
    code: str,
    message: str,
    conversation_id: UUID | None = None,
) -> int:
    envelope = analysis_error_event(
        stream_id=stream_id,
        code=code,
        message=message,
        conversation_id=conversation_id,
    ).model_dump(mode="json")
    return publish_event(user_id, envelope)
