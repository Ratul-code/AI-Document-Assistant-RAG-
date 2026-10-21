"""WebSocket route: /ws

Auth:
    The browser sends the access_token cookie automatically during the
    WS upgrade handshake. We validate it the same way REST endpoints do —
    by decoding the JWT. If invalid / missing, we close the socket with
    a 1008 policy-violation code BEFORE accepting the connection.

Lifecycle:
    1. Validate cookie -> resolve user_id
    2. accept() the connection
    3. register with ConnectionManager under user_id
    4. spawn a heartbeat task that sends a protocol-level PING every
       _HEARTBEAT_INTERVAL_SECONDS; track the time of the last received
       client frame (browser auto-PONGs count)
    5. main loop: receive() — handles PONGs and any client messages
    6. on disconnect / heartbeat timeout: unregister and exit
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from uuid import UUID

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from app.core.config import get_settings
from app.core.security import decode_access_token
from app.websockets.manager import get_connection_manager

logger = logging.getLogger(__name__)

router = APIRouter()

# How often the heartbeat task sends a protocol PING.
_HEARTBEAT_INTERVAL_SECONDS = 3

# If we don't see any client frame (PONG or app message) within this
# window after our PING, we treat the connection as dead. Browsers
# auto-reply with PONG, so this fires only on truly dead clients.
_HEARTBEAT_TIMEOUT_SECONDS = 5


def _user_id_from_cookie(websocket: WebSocket) -> str | None:
    """Decode the access_token cookie. Returns user_id (str) or None."""
    settings = get_settings()
    raw_token = websocket.cookies.get(settings.access_cookie_name)
    if not raw_token:
        return None
    try:
        payload = decode_access_token(raw_token)
    except (jwt.ExpiredSignatureError, jwt.PyJWTError):
        return None
    return payload.get("sub")


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Main WebSocket endpoint. One connection per browser tab."""
    manager = get_connection_manager()

    # 1. Auth
    user_id_str = _user_id_from_cookie(websocket)
    if not user_id_str:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        logger.info("ws rejected: no/invalid access_token cookie")
        return

    try:
        user_id = UUID(user_id_str)
    except ValueError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # 2. Accept
    await websocket.accept()

    # 3. Register
    await manager.connect(user_id, websocket)
    logger.info("ws accepted user_id=%s", user_id)

    try:
        await websocket.send_json(
            {"type": "connected", "user_id": str(user_id)}
        )
    except Exception:
        await manager.disconnect(user_id, websocket)
        return

    # 4. Liveness tracking. The receive loop updates `last_seen` on
    #    every client frame; the heartbeat task checks it on every tick.
    #    Using time.monotonic() — immune to clock skew.
    last_seen = time.monotonic()
    timed_out = asyncio.Event()

    async def heartbeat_loop() -> None:
        """Periodically PING the client; signal a timeout if no reply."""
        nonlocal last_seen
        try:
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
                if time.monotonic() - last_seen > _HEARTBEAT_TIMEOUT_SECONDS:
                    logger.info(
                        "ws heartbeat timeout user_id=%s (>%ds silent)",
                        user_id,
                        _HEARTBEAT_TIMEOUT_SECONDS,
                    )
                    timed_out.set()
                    # Force the receive loop to wake up and exit.
                    try:
                        await websocket.close(
                            code=status.WS_1011_GOING_AWAY
                        )
                    except Exception:
                        pass
                    return
                try:
                    # Browser replies to a PING frame with a PONG at the
                    # protocol level — no client code needed. If the
                    # send fails, the socket itself is broken; the
                    # receive loop will see a disconnect.
                    await websocket.send({"type": "websocket.ping"})
                    logger.info(
                        "ws ping sent user_id=%s last_seen=%.1fs ago",
                        user_id,
                        time.monotonic() - last_seen,
                    )
                except Exception as exc:
                    logger.debug("ws heartbeat send failed: %s", exc)
                    return
        except asyncio.CancelledError:
            return

    heartbeat_task = asyncio.create_task(heartbeat_loop())

    # 5. Receive loop. Updates `last_seen` on every client frame so the
    #    heartbeat task can confirm liveness without a separate handshake.
    try:
        while True:
            msg = await websocket.receive()
            last_seen = time.monotonic()

            if msg.get("type") == "websocket.disconnect":
                return

            if msg.get("type") == "websocket.pong":
                continue

            if msg.get("type") == "websocket.receive":
                text = msg.get("text") or msg.get("bytes")
                if not text:
                    continue
                # Application-level ping/pong for testing/debug only.
                # The browser does not send these — liveness uses the
                # protocol PING/PONG above.
                try:
                    payload = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    continue
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("ws loop error user_id=%s err=%s", user_id, exc)
    finally:
        heartbeat_task.cancel()
        await manager.disconnect(user_id, websocket)
