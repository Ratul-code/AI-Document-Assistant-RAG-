"""HTTP middleware collection.

Currently houses `RequestIDMiddleware`; this is also the natural home
for any future cross-cutting HTTP middleware (e.g. an access-log
middleware, a request-size cap, etc.) so we don't end up with a
`middleware_request_id.py` / `middleware_access_log.py` / ...
proliferation.

Why a request ID
----------------
Production-grade HTTP services tag every request with a short unique
ID and propagate that ID to:

1. the **response** — as the `X-Request-ID` header, so a user reporting
   "I got an error" can quote the ID and the operator can find the
   exact log trail in one grep.
2. the **logs** — every log line emitted while the request is being
   processed carries the same ID, via the `RequestIdFilter` on the
   root handler (see `app.core.request_id`).
3. downstream **services** — if the client supplied a header
   (common behind a load balancer or a frontend correlation layer),
   we trust their value and reuse it; otherwise we mint our own.
   This is the standard `X-Request-ID` propagation pattern.

Order of middleware
-------------------
In `app/main.py` this middleware is registered **after** `CORS` and
**before** `SlowAPIMiddleware`. That order is deliberate:

- After CORS: a CORS preflight (`OPTIONS`) should not consume a
  request ID slot in our logs (it never reaches the route handler).
  Putting CORS first means the request ID is only minted for the
  actual request.
- Before slowapi / auth: every downstream layer — including the
  rate-limiter's 429 response and any 401 from auth — should carry
  the same `X-Request-ID` header on its response, so the client gets
  the same ID for the success path, the throttle path, and the auth
  path. Starlette's `add_middleware` is LIFO, so to run "after
  CORS / before slowapi" we register this middleware AFTER slowapi
  is added (see `app/main.py`).
"""

from __future__ import annotations

import re
import secrets
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.config import get_settings
from app.core.request_id import (
    get_request_id,
    reset_request_id,
    set_request_id,
)

# Whitelist for caller-supplied request IDs. We accept the upstream
# value ONLY if it is short, URL-safe, and contains no whitespace /
# control chars — a malicious client could otherwise inject newlines
# into our log lines (log forging) or absurdly long strings (memory
# pressure on the log handler). Any value that doesn't match falls
# back to a freshly-minted ID.
#
# 8 to 128 chars covers the common formats: 8-char short id
# (secrets.token_urlsafe(6)), 32-char UUID-hex without dashes, 36-char
# UUID, 22-char base64url, 64-char SHA-256, etc. Anything outside
# that range is almost certainly garbage or an attack.
_VALID_INCOMING = re.compile(r"^[A-Za-z0-9_\-]{8,128}$")

# Length of the freshly-generated ID. secrets.token_urlsafe(6) returns
# 8 base64url chars (6 bytes of entropy ≈ 48 bits — collision-safe
# within a process lifetime, and a single human can copy it off a
# support ticket without typos).
_GENERATED_LEN = 6


def _generate_request_id() -> str:
    """Mint a short, URL-safe, opaque request ID."""
    return secrets.token_urlsafe(_GENERATED_LEN)


def _resolve_request_id(incoming: Optional[str]) -> str:
    """Return the incoming header value if it looks safe; else mint one.

    `getattr(request.headers, ...)` style is verbose; this is the
    single place the validation policy lives.
    """
    if incoming and _VALID_INCOMING.match(incoming):
        return incoming
    return _generate_request_id()


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Stamp every request with a stable ID and echo it on the response.

    Behaviour
    ---------
    - If the request carries a well-formed `X-Request-ID` (or whatever
      `settings.request_id_header` is set to), reuse it. This is the
      standard propagation pattern for tracing across an LB / mesh.
    - Otherwise, mint a new 8-char URL-safe ID with `secrets`.
    - Store it on `request.state.request_id` for handlers that want
      it (most won't need to — the `ContextVar` covers them).
    - Set it on the module-level `ContextVar` so the logging filter
      stamps it onto every log record emitted during this request.
    - On the response, set the same header so the client can quote it.

    The `ContextVar` is reset in a `finally` block so a request that
    raises or that runs through a buggy downstream middleware cannot
    leak its ID into the next request that happens to land on the
    same asyncio task.

    Disabled by `settings.request_id_enabled = false` only in
    benchmarking scenarios where every microsecond of overhead
    matters; off in normal dev and prod.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        # Snapshot the settings at middleware construction. This is
        # the same pattern slowapi uses in `app/core/rate_limit.py`
        # — `get_settings()` is `@lru_cache`d so this is cheap, and
        # tests that override env vars do so via
        # `get_settings.cache_clear()` + `monkeypatch.setenv` BEFORE
        # `create_app()` is called, so a fresh app picks up the new
        # values.
        settings = get_settings()
        self._header_name: str = settings.request_id_header
        self._enabled: bool = settings.request_id_enabled

    async def dispatch(self, request: Request, call_next) -> Response:
        if not self._enabled:
            # No-op fast path. We still attach an attribute so handler
            # code that does `getattr(request.state, "request_id", "")`
            # doesn't have to special-case the disabled state.
            request.state.request_id = ""
            return await call_next(request)

        request_id = _resolve_request_id(
            request.headers.get(self._header_name)
        )
        # Stash for handlers that want it. Naming it `request_id` (not
        # the full header name) keeps handler code clean.
        request.state.request_id = request_id
        # Push into the ContextVar so the logging filter can pick it
        # up from anywhere in the call tree.
        token = set_request_id(request_id)
        try:
            response = await call_next(request)
        finally:
            # Restore even on exception. The exception will be
            # re-raised by Starlette's exception-handler middleware
            # further up the stack; we just clean up our ambient
            # state here.
            reset_request_id(token)

        # Set the response header. Done after the handler so the
        # header is on the response no matter what the handler did —
        # including error responses built inside the handler.
        response.headers[self._header_name] = request_id
        return response


__all__ = [
    "RequestIDMiddleware",
    "get_request_id",
]
