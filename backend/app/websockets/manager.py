"""In-memory WebSocket connection manager.

Keeps a mapping of user_id -> list[WebSocket] so the server can push
messages to all of a user's open connections (e.g. multiple browser tabs).

Single-process only. If you ever run uvicorn with --workers > 1, swap this
out for a Redis-pub/sub backed manager — the public API stays the same.

Threading / async notes:
    All methods are async because WebSocket.send_text() is async. The
    internal dict mutations are fast (microseconds) and only happen during
    connect/disconnect, so we don't need a lock.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any
from uuid import UUID

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Tracks active WebSocket connections grouped by user_id."""

    def __init__(self) -> None:
        # user_id -> set of WebSocket objects.
        # Using a set (not list) so we don't double-send if the same WS
        # somehow gets registered twice.
        self._connections: dict[UUID, set[WebSocket]] = defaultdict(set)
        # Track all sockets across users so we can broadcast / shutdown.
        self._all: set[WebSocket] = set()
        # Per-connection lock so concurrent sends from the same socket
        # don't interleave bytes.
        self._send_locks: dict[WebSocket, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, user_id: UUID, websocket: WebSocket) -> None:
        """Register an already-accepted WebSocket under user_id."""
        async with self._lock:
            self._connections[user_id].add(websocket)
            self._all.add(websocket)
            self._send_locks[websocket] = asyncio.Lock()
        logger.info(
            "ws connected user_id=%s total_for_user=%d total=%d",
            user_id,
            len(self._connections[user_id]),
            len(self._all),
        )

    async def disconnect(self, user_id: UUID, websocket: WebSocket) -> None:
        """Remove a WebSocket from tracking. Safe to call twice."""
        async with self._lock:
            self._connections[user_id].discard(websocket)
            if not self._connections[user_id]:
                del self._connections[user_id]
            self._all.discard(websocket)
            self._send_locks.pop(websocket, None)
        logger.info(
            "ws disconnected user_id=%s total=%d",
            user_id,
            len(self._all),
        )

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send_to_user(self, user_id: UUID, message: dict[str, Any]) -> int:
        """Send a JSON message to every connection belonging to user_id.

        Returns the number of connections the message was delivered to.
        Connections that fail to receive (closed, broken) are removed.
        """
        # Snapshot the set under the lock, then send outside the lock so
        # one slow socket doesn't block connect/disconnect.
        async with self._lock:
            sockets = list(self._connections.get(user_id, set()))

        if not sockets:
            logger.debug("ws send_to_user: no active connections for %s", user_id)
            return 0

        delivered = 0
        dead: list[WebSocket] = []
        for ws in sockets:
            lock = self._send_locks.get(ws)
            if lock is None:
                continue
            try:
                async with lock:
                    await ws.send_json(message)
                delivered += 1
            except Exception as exc:
                # Broken pipe, client closed mid-send, etc. Mark for cleanup.
                logger.warning("ws send failed (%s); will drop connection", exc)
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections[user_id].discard(ws)
                    self._all.discard(ws)
                    self._send_locks.pop(ws, None)
                if user_id in self._connections and not self._connections[user_id]:
                    del self._connections[user_id]

        return delivered

    async def broadcast(self, message: dict[str, Any]) -> int:
        """Send to every connected socket regardless of user.

        Sockets that fail to receive (closed, broken) are dropped from
        the registry, matching the cleanup pattern in `send_to_user`.
        Without this, a single dead socket would persist in `_all`
        forever and block the per-socket lock for every subsequent
        broadcast — a real leak once the B3 subscriber dispatches
        every received event here.
        """
        async with self._lock:
            sockets = list(self._all)
        delivered = 0
        dead: list[WebSocket] = []
        for ws in sockets:
            lock = self._send_locks.get(ws)
            if lock is None:
                continue
            try:
                async with lock:
                    await ws.send_json(message)
                delivered += 1
            except Exception as exc:
                logger.warning("ws broadcast send failed (%s); will drop connection", exc)
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._all.discard(ws)
                    self._send_locks.pop(ws, None)
                    # A socket can only belong to one user, but we
                    # walk _connections defensively so a future
                    # refactor that allows a socket to be in multiple
                    # users' sets still cleans up correctly.
                    for _user_id, user_sockets in self._connections.items():
                        user_sockets.discard(ws)
                    # Tidy: drop users with no sockets left.
                    empty_users = [u for u, s in self._connections.items() if not s]
                    for u in empty_users:
                        del self._connections[u]
        return delivered

    # ------------------------------------------------------------------
    # Introspection (handy for tests / debug)
    # ------------------------------------------------------------------

    def is_connected(self, user_id: UUID) -> bool:
        return bool(self._connections.get(user_id))

    def total_connections(self) -> int:
        return len(self._all)

    def connection_count_for(self, user_id: UUID) -> int:
        return len(self._connections.get(user_id, set()))

    async def shutdown(self) -> None:
        """Close all sockets. Used on app shutdown."""
        async with self._lock:
            sockets = list(self._all)
            self._all.clear()
            self._connections.clear()
            self._send_locks.clear()
        for ws in sockets:
            try:
                await ws.close()
            except Exception:
                pass


# Module-level singleton — same pattern as get_settings().
_manager: ConnectionManager | None = None


def get_connection_manager() -> ConnectionManager:
    """Return the process-wide ConnectionManager instance."""
    global _manager
    if _manager is None:
        _manager = ConnectionManager()
    return _manager
