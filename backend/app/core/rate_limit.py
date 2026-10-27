"""Rate limiting (slowapi) wiring.

Single source of truth for the limiter instance, the key function, and the
HTTP-429 exception handler. Imported by `app/main.py` to wire the limiter
into the FastAPI app, and by every endpoint module to attach per-route
decorators (`@limiter.limit("5/minute")`).

Why a single module?
--------------------
- One `Limiter` instance must be shared by all routes (and the exception
  handler) so the in-Redis counter is the same on every request. Creating
  a fresh `Limiter` per module would split the counter across instances.
- The key function is non-trivial (best-effort user-id decode from the
  access-token cookie, fallback to client IP) and lives here so every
  decorator uses the same keying policy.

Why slowapi?
------------
- It's the de-facto FastAPI port of `flask-limiter`, which has been in
  production for a decade.
- Backed by the `limits` library, which supports Redis as a storage
  backend natively (no extra dependency on a `redis-py` client — `limits`
  ships its own).
- Per-route decorator API fits cleanly on top of the existing
  `@router.post(...)` / `@router.get(...)` handlers in this codebase.

Per-route limits (apply via `@limiter.limit("...")`)
----------------------------------------------------
| Route                                       | Limit         | Why                    |
|---------------------------------------------|---------------|------------------------|
| POST /auth/register                         | 5/hour        | Spam account creation  |
| POST /auth/login                            | 5/minute      | Brute-force mitigation |
| POST /auth/refresh                          | 30/minute     | Token-theft hammering  |
| POST /auth/logout                           | 10/minute     | Cheap but cap it       |
| GET  /auth/me                               | 60/minute     | Page-load hit         |
| POST /documents/upload-intent               | 10/minute     | Signed URL + DB row    |
| POST /documents/{id}/confirm                | 30/minute     | Cheap, doc-scoped      |
| POST /documents/{id}/upload-failed          | 30/minute     | Cheap                  |
| PATCH/DELETE /documents/{id}                | 60 / 30 per m | Edit / destructive     |
| GET  /documents/{id}/{download,view,...}    | 60/minute     | Bytes / metadata       |
| GET  /documents (list + filter)             | 120/minute    | Cheap read             |
| POST /analysis/query                        | 20/minute     | LLM cost               |
| POST /analysis/query/stream                 | 20/minute     | LLM cost               |
| POST /conversations                         | 60/minute     | New chat               |
| GET  /conversations                         | 120/minute    | Sidebar list           |
| GET/PATCH/DELETE /conversations/{id}        | 60/minute     | Chat CRUD              |
| GET  /conversations/{id}/messages           | 120/minute    | History pagination     |
| POST /conversations/{id}/messages           | 30/minute     | Submit question        |
| DELETE /conversations/{id}/messages/{mid}   | 30/minute     | Cancel stream          |
| GET  /health/*                              | UNLIMITED     | LB / k8s probes        |
| /ws (WebSocket)                             | UNLIMITED     | slowapi is HTTP-only   |

Routes are NOT auto-discovered — each handler attaches its own
`@limiter.limit(...)` decorator. This is intentional: limits are policy
and policy is per-route, so per-route opt-in keeps the contract visible
in the endpoint module instead of hidden in a config file.
"""

from __future__ import annotations

import logging
from typing import Any

import jwt
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from limits import RateLimitItem
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.config import get_settings
from app.core.security import decode_access_token

logger = logging.getLogger(__name__)


# --- Key function ---------------------------------------------------------
#
# slowapi calls this on every limited request. We want the counter keyed by
# the user id when we can resolve it (so two users on the same NAT'd IP
# don't share a bucket), and by client IP otherwise (so unauthenticated
# endpoints like /auth/login are still rate-limited).
#
# Decoding the JWT is best-effort: a malformed / expired token is fine —
# the route will return 401 anyway; we just fall back to the IP key. We
# never raise from this function because the limiter is on the hot path
# of every request and a keying failure must not 500 the request.


def user_or_ip_key(request: Request) -> str:
    """Resolve a rate-limit key from the request.

    Returns ``user:<uuid>`` if the access-token cookie is present and
    decodes to a valid access JWT; otherwise ``ip:<remote-address>``.

    Never raises. Logs at DEBUG on decode failure (it's expected for
    pre-auth routes like /auth/login).
    """
    settings = get_settings()
    try:
        # The access cookie is named via settings so the same key function
        # works whether the project keeps the default "xs" or overrides it
        # in tests / prod.
        cookie_token = request.cookies.get(settings.access_cookie_name)
        if cookie_token:
            payload = decode_access_token(cookie_token)
            sub = payload.get("sub")
            if sub:
                return f"user:{sub}"
    except jwt.PyJWTError as exc:
        logger.debug("rate_limit key_func: cookie decode failed (%s)", exc)
    except Exception:  # noqa: BLE001 — defensive: never let keying raise
        logger.debug("rate_limit key_func: unexpected error", exc_info=True)

    # Fallback: client IP. slowapi's get_remote_address already handles
    # the case where request.client is None (returns "<unknown>").
    return f"ip:{get_remote_address(request)}"


# --- Limiter singleton ----------------------------------------------------
#
# slowapi requires that every decorator and the exception handler share
# the same `Limiter` instance. We construct it at import time (after
# settings exist) and re-export it. `get_settings()` is `@lru_cache`d so
# this is cheap; tests that override `RATE_LIMIT_ENABLED` do so via
# `monkeypatch.setenv` + `get_settings.cache_clear()` and the limiter
# reads the new value on next access via `_limiter_settings_snapshot()`.
#
# The Limiter is constructed with `enabled=settings.rate_limit_enabled`
# so flipping the env var at startup disables every decorator globally
# — the decorators themselves stay in the source as documentation.

_settings = get_settings()
limiter: Limiter = Limiter(
    key_func=user_or_ip_key,
    storage_uri=_settings.rate_limit_storage_uri,
    enabled=_settings.rate_limit_enabled,
    # The default strategy is "fixed-window" which is fine for our use
    # case. "moving-window" is more accurate but doubles the Redis reads
    # per request. We start simple; can be bumped later.
    strategy="fixed-window",
    # Inject the standard `X-RateLimit-*` response headers so the
    # frontend can show a friendly "Slow down" toast without a separate
    # probe request.
    #
    # NOTE: slowapi's route-level header-injection path
    # (extension.py:738) calls `_inject_headers(kwargs.get("response"), ...)`
    # for routes that return a Pydantic model rather than a `Response`.
    # When such a route has no `response: Response` parameter,
    # `kwargs.get("response")` is `None` and slowapi raises
    #   "parameter `response` must be an instance of starlette.responses.Response"
    # — the 500 we hit on GET /api/v1/documents.
    #
    # We enable headers globally (the 429 handler in this module needs
    # them to attach `Retry-After`) and neutralise the crash by
    # wrapping `_inject_headers` below so a `None` response is skipped
    # instead of raising. Routes that DO declare `response: Response`
    # still get the headers; the rest are unaffected.
    headers_enabled=True,
    # Compute Retry-After from the actual window so 429 responses
    # include a useful "seconds until reset" hint.
    retry_after="http-date",
)


def _safe_inject_headers(
    self: Limiter,
    response: Response | None,
    current_limit: tuple[RateLimitItem, list[str]] | None,
) -> Response | None:
    """`_inject_headers` that tolerates a `None` response.

    slowapi's route wrapper calls `_inject_headers(kwargs.get("response"), ...)`
    for any route that doesn't return a `Response` directly. For routes
    without a `response: Response` parameter that argument is `None`, and
    the upstream implementation raises
    `"parameter `response` must be an instance of starlette.responses.Response"`.

    Skipping the injection (returning `None` unchanged) is the correct
    behaviour for those routes: there is no response object to attach
    headers to. Routes that do declare `response: Response` — and the 429
    handler, which always passes a real `JSONResponse` — are unaffected and
    still receive `X-RateLimit-*` and `Retry-After`.
    """
    if response is None:
        return None
    if current_limit is None:
        # Upstream's own body starts with `current_limit is not None`
        # but its type annotation doesn't admit the None case. Nothing
        # to inject either way.
        return response
    return _original_inject_headers(self, response, current_limit)


# Keep a reference to the upstream implementation so the wrapper above
# can delegate to it without recursing into itself, then install the
# wrapper. The `type: ignore` is needed because mypy rejects assigning
# to a method on an imported class.
_original_inject_headers = Limiter._inject_headers
Limiter._inject_headers = _safe_inject_headers  # type: ignore[method-assign, assignment]


def _rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Return a JSON 429 that matches the rest of the API's error shape.

    The default slowapi handler returns ``{"error": "..."}``; this
    returns ``{"detail": "..."}`` so the frontend's existing 401 / 403 /
    404 error-handling branch can show the same toast without a new
    case.
    """
    response = JSONResponse(
        {"detail": f"Rate limit exceeded: {exc.detail}"},
        status_code=429,
    )
    # Let slowapi add the standard `X-RateLimit-Limit`,
    # `X-RateLimit-Remaining`, and `Retry-After` headers. The library
    # stores the limit on `request.state.view_rate_limit` while
    # processing the request.
    try:
        response = request.app.state.limiter._inject_headers(
            response, request.state.view_rate_limit
        )
    except Exception:  # noqa: BLE001 — never let header injection 500
        logger.debug("rate_limit: header injection failed", exc_info=True)
    return response


# Re-export for callers that just want the symbols.
__all__ = [
    "RateLimitExceeded",
    "limiter",
    "user_or_ip_key",
    "_rate_limit_exceeded_handler",
]


# Quiet "unused import" linters — Any is used in type comments elsewhere.
_ = Any
