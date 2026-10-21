"""Async Redis subscriber for WebSocket events.

Started once per FastAPI process from the lifespan handler. It opens an
async Redis connection and SUBSCRIBEs to either:

  * (B3, default) this process's per-worker channel
    `ws:events:{worker_id}`. The publisher enumerates a Redis SET
    (`ws:workers`) of live worker_ids maintained by a per-worker
    keepalive task and PUBLISHes one copy of the envelope to each live
    worker's channel. Each worker then dispatches to its own
    in-process `ConnectionManager.broadcast` — sockets are delivered
    regardless of which FastAPI process the user is connected to.

  * (v1 fallback, when `ws_b3_enabled=False`) the single global
    `ws:events` channel, routing by `user_id` in the payload (the
    pre-migration behavior).

In both modes the envelope shape on the wire is the same
`{type, data, ts}` dict produced by `WsEvent.model_dump(mode="json")`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import get_settings
from app.websockets.manager import get_connection_manager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Parsing — convert the raw Redis payload into the envelope dict.
# Kept separate from dispatch so the parser is easy to unit-test in
# isolation. In B3 mode the publisher no longer puts a `user_id` field
# in the payload, so we just return the parsed dict as-is (after
# validation). In v1 fallback mode the dict still has a `user_id` key
# that the receiver ignores.
# ---------------------------------------------------------------------


def parse_message(raw: object) -> dict[str, Any] | None:
    """Decode the raw pubsub payload into an envelope dict, or None.

    Returns None (and logs why) for any of:
        - non-string payload
        - bad JSON
        - payload that is not a dict
    """
    if not isinstance(raw, str):
        logger.warning("ws subscriber: unexpected payload type=%s", type(raw))
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("ws subscriber: bad json payload=%r", raw[:200])
        return None
    if not isinstance(payload, dict):
        logger.warning("ws subscriber: payload is not a dict type=%s", type(payload))
        return None
    return payload


# ---------------------------------------------------------------------
# Subscriber
# ---------------------------------------------------------------------


class WsSubscriber:
    """Long-lived subscriber task for the WS event channel."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._redis: Redis | None = None

    def _resolve_channel(self) -> str:
        """Return the channel name this subscriber should SUBSCRIBE to.

        B3 mode → `ws:events:{worker_id}` (per-worker private channel).
        v1 fallback → `ws:events` (single global channel).
        """
        settings = get_settings()
        if settings.ws_b3_enabled:
            return settings.ws_pubsub_channel_pattern.format(worker_id=settings.worker_id)
        return settings.ws_pubsub_channel

    async def start(self) -> None:
        """Spawn the background subscriber. Idempotent."""
        if self._task is not None and not self._task.done():
            logger.debug("ws subscriber already running")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="ws-pubsub-subscriber")
        logger.info(
            "ws subscriber started channel=%s",
            self._resolve_channel(),
        )

    async def stop(self) -> None:
        """Signal the subscriber to exit and close the Redis connection."""
        self._stop.set()
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                logger.debug("ws subscriber: redis close error", exc_info=True)
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                self._task.cancel()
                logger.warning("ws subscriber: cancelled after 5s timeout")
            except Exception:
                logger.debug("ws subscriber: task ended with error", exc_info=True)
        self._task = None
        logger.info("ws subscriber stopped")

    async def _run(self) -> None:
        """Subscribe loop. Reconnects with backoff on transient errors."""
        settings = get_settings()
        backoff = 1.0
        max_backoff = 30.0

        while not self._stop.is_set():
            channel = self._resolve_channel()
            try:
                self._redis = Redis.from_url(settings.redis_url, decode_responses=True)
                pubsub = self._redis.pubsub()
                await pubsub.subscribe(channel)
                logger.info("ws subscriber: subscribed channel=%s", channel)
                backoff = 1.0  # reset after successful connect

                async for message in pubsub.listen():
                    if self._stop.is_set():
                        break
                    if message is None:
                        continue
                    if message.get("type") != "message":
                        # 'subscribe' ack and similar — ignore.
                        continue
                    await self._dispatch(message.get("data"))

            except RedisConnectionError as exc:
                logger.warning(
                    "ws subscriber: redis connection error (%s); retrying in %.1fs",
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "ws subscriber: unexpected error (%s); retrying in %.1fs",
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
            finally:
                if self._redis is not None:
                    try:
                        await self._redis.aclose()
                    except Exception:
                        pass
                    self._redis = None

    async def _dispatch(self, raw: object) -> None:
        """Parse one message and deliver to all of this worker's sockets.

        In B3 mode the per-worker channel is private, so every message
        we receive is intended for our sockets. The in-process
        ConnectionManager handles the actual fanout (and the per-socket
        send lock that prevents inter-byte interleaving with the
        heartbeat task).
        """
        event = parse_message(raw)
        if event is None:
            return

        event_type = event.get("type")
        try:
            delivered = await get_connection_manager().broadcast(event)
        except Exception:
            logger.exception(
                "ws subscriber: broadcast failed type=%s",
                event_type,
            )
            return
        logger.info(
            "ws subscriber: delivered type=%s sockets=%d",
            event_type,
            delivered,
        )


# Module-level singleton — one subscriber per FastAPI process.
_subscriber: WsSubscriber | None = None


def get_ws_subscriber() -> WsSubscriber:
    """Return the process-wide WsSubscriber instance."""
    global _subscriber
    if _subscriber is None:
        _subscriber = WsSubscriber()
    return _subscriber
