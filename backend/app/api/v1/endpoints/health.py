"""Health-check endpoints: liveness and readiness.

Two endpoints with distinct contracts, matching the Kubernetes /
container-orchestrator convention:

- `GET /health/live`  — liveness.  Process is up and the event loop is
  responsive.  Used by the orchestrator to decide whether to RESTART
  the container.  Must NOT touch external dependencies — if the DB is
  down we should still report "live" so the orchestrator restarts the
  *right* container, not the API.

- `GET /health/ready` — readiness.  Every external dependency this
  process needs to serve real traffic is reachable.  Used by the
  orchestrator / load balancer to decide whether to ROUTE TRAFFIC to
  the container.  Touches DB (SELECT 1), Redis (PING), Celery
  (round-trip the existing `app.tasks.document.ping` task), and the
  LLM (GET {base_url}/models with a short timeout).

The legacy `GET /health` is kept as a thin alias for `/health/live`
so existing monitors that hit `/health` keep working.

Each check is bounded with its own short timeout so a single
misbehaving dependency cannot stall the whole endpoint.  Every check
is fail-soft: a failure is reported as `status: "down"` with a
diagnostic string, never as a raised exception.  The overall HTTP
status is 200 when every check is "up" and 503 otherwise, so a
load-balancer health probe can route on it directly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_llm_api_key, get_settings
from app.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

# Per-check timeouts. Kept tight so the endpoint stays fast even when
# a dependency is wedged — a readiness probe should answer in
# well under a second.
_DB_TIMEOUT_S = 2.0
_REDIS_TIMEOUT_S = 1.0
_CELERY_TIMEOUT_S = 3.0
_LLM_TIMEOUT_S = 3.0

# App version. Mirrors the value used in `app.main.create_app()` —
# kept in sync manually so we don't have to import the FastAPI app
# (which would create circular-import risk in some test setups).
_APP_VERSION = "0.1.0"
_SERVICE_NAME = "ai-document-analyser"


class HealthResponse(BaseModel):
    """Schema for the basic liveness response."""

    status: str
    service: str
    version: str
    timestamp: str


class CheckResult(BaseModel):
    """Result of a single dependency probe."""

    status: str  # "up" | "down" | "skipped"
    latency_ms: float | None = None
    detail: str | None = None


class ReadinessResponse(BaseModel):
    """Schema for the readiness response — one CheckResult per dep."""

    status: str  # "ready" | "not_ready"
    service: str
    version: str
    timestamp: str
    checks: dict[str, CheckResult]


# --- Helpers ------------------------------------------------------------


async def _run_check(
    name: str,
    probe: Callable[[], Awaitable[tuple[bool, str | None]]],
    timeout_s: float,
) -> CheckResult:
    """Run a single probe under a wall-clock cap, never raise.

    Returns a `CheckResult` with `status="up"` on success and
    `status="down"` (with a diagnostic `detail`) on failure or
    timeout.  Latency is reported in milliseconds.
    """
    t0 = time.monotonic()
    try:
        ok, detail = await asyncio.wait_for(probe(), timeout=timeout_s)
    except TimeoutError:
        elapsed = (time.monotonic() - t0) * 1000
        logger.warning("readiness: %s timed out after %.0fms", name, elapsed)
        return CheckResult(
            status="down",
            latency_ms=round(elapsed, 1),
            detail=f"timeout after {timeout_s:.1f}s",
        )
    except Exception as exc:  # noqa: BLE001 — never let a probe raise
        elapsed = (time.monotonic() - t0) * 1000
        logger.warning("readiness: %s raised %s: %s", name, type(exc).__name__, exc)
        return CheckResult(
            status="down",
            latency_ms=round(elapsed, 1),
            detail=f"{type(exc).__name__}: {exc}",
        )
    elapsed = (time.monotonic() - t0) * 1000
    return CheckResult(
        status="up" if ok else "down",
        latency_ms=round(elapsed, 1),
        detail=detail,
    )


def _sync_redis_ping(url: str) -> tuple[bool, str | None]:
    """Sync Redis PING — wrapped in `to_thread` by the caller.

    Uses the sync `redis` client because Celery + the WS publisher
    already depend on it, so no new dependency.  The probe is
    intentionally minimal: one PING, no retries.
    """
    try:
        client = Redis.from_url(url, socket_connect_timeout=1.0)
        try:
            pong = client.ping()
            return (bool(pong), None)
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001 — best-effort close
                pass
    except RedisError as exc:
        return (False, f"{type(exc).__name__}: {exc}")


def _sync_celery_ping() -> tuple[bool, str | None]:
    """Send the existing `app.tasks.document.ping` task and wait for
    the worker to echo 'pong'.  This proves both the broker (Redis)
    and a live worker process are healthy.

    Imported lazily to avoid forcing the Celery app to import during
    pure liveness probes.
    """
    try:
        from app.tasks.celery import celery_app

        async_result = celery_app.send_task("app.tasks.document.ping")
        # 2.5s here so it stays under the wrapping `_CELERY_TIMEOUT_S`
        # — the wrapping `wait_for` is the real safety net.
        value = async_result.get(timeout=2.5, propagate=True)
        return (value == "pong", f"got {value!r}" if value != "pong" else None)
    except Exception as exc:  # noqa: BLE001 — never let a probe raise
        return (False, f"{type(exc).__name__}: {exc}")


async def _async_llm_probe(base_url: str, api_key: str | None) -> tuple[bool, str | None]:
    """Hit `{base_url}/models` with a short timeout.

    Most OpenAI-compatible gateways expose this endpoint without
    authentication (or with a permissive 401/403 that still proves
    reachability).  We treat any 2xx-3xx response as "up".  A 401/403
    also counts as up (the LLM is reachable, the key is just wrong)
    — surfacing the key issue is the orchestrator's job, not the
    readiness probe's.
    """
    url = f"{base_url.rstrip('/')}/models"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=_LLM_TIMEOUT_S) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return (False, f"{type(exc).__name__}: {exc}")
    if 200 <= resp.status_code < 400:
        return (True, f"HTTP {resp.status_code}")
    if resp.status_code in (401, 403):
        # Reachable, but the key is rejected.  Don't fail readiness
        # on auth — that's an operator-config issue, not an outage.
        return (True, f"HTTP {resp.status_code} (auth — key rejected)")
    return (False, f"HTTP {resp.status_code}")


# --- Endpoints ----------------------------------------------------------


@router.get("/health", response_model=HealthResponse, tags=["health"])
@router.get("/health/live", response_model=HealthResponse, tags=["health"])
async def health_check() -> HealthResponse:
    """Liveness probe — process is up and the event loop is responsive.

    Intentionally cheap.  No DB / Redis / LLM calls.  An orchestrator
    should restart the container only if THIS endpoint fails.
    """
    return HealthResponse(
        status="ok",
        service=_SERVICE_NAME,
        version=_APP_VERSION,
        timestamp=datetime.now(UTC).isoformat(),
    )


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    tags=["health"],
    responses={
        200: {"description": "All dependencies reachable."},
        503: {"description": "At least one dependency is down."},
    },
)
async def readiness_check(
    response: Response, db: AsyncSession = Depends(get_db)
) -> ReadinessResponse:
    """Readiness probe — every external dependency is reachable.

    Probes, in order, with bounded timeouts:

    1. **database** — `SELECT 1` over the configured async engine.
    2. **redis**    — `PING` on the WS pub/sub URL (the same URL the
       app actually uses; the Celery broker is on a different DB but
       the same Redis instance, so this is sufficient).
    3. **celery**   — round-trip the `app.tasks.document.ping` task
       through the configured broker + result backend.
    4. **llm**      — `GET {base_url}/models` against the configured
       OpenAI-compatible gateway.

    All four probes run concurrently.  The overall response is 200
    iff every check is `up`, 503 otherwise — so a load-balancer
    health probe can route on the status code directly.
    """
    settings = get_settings()
    api_key = get_llm_api_key()

    async def _db_probe() -> tuple[bool, str | None]:
        result = await db.execute(text("SELECT 1"))
        row = result.scalar_one()
        return (row == 1, None)

    async def _redis_probe() -> tuple[bool, str | None]:
        return await asyncio.to_thread(_sync_redis_ping, settings.redis_url)

    async def _celery_probe() -> tuple[bool, str | None]:
        return await asyncio.to_thread(_sync_celery_ping)

    async def _llm_probe() -> tuple[bool, str | None]:
        return await _async_llm_probe(settings.analysis_llm_base_url, api_key)

    db_result, redis_result, celery_result, llm_result = await asyncio.gather(
        _run_check("database", _db_probe, _DB_TIMEOUT_S),
        _run_check("redis", _redis_probe, _REDIS_TIMEOUT_S),
        _run_check("celery", _celery_probe, _CELERY_TIMEOUT_S),
        _run_check("llm", _llm_probe, _LLM_TIMEOUT_S),
    )

    checks: dict[str, CheckResult] = {
        "database": db_result,
        "redis": redis_result,
        "celery": celery_result,
        "llm": llm_result,
    }
    all_up = all(c.status == "up" for c in checks.values())
    if not all_up:
        # 503 so a load-balancer / k8s readiness probe can route on it.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        logger.warning(
            "readiness: not ready — %s",
            {k: v.status for k, v in checks.items()},
        )

    return ReadinessResponse(
        status="ready" if all_up else "not_ready",
        service=_SERVICE_NAME,
        version=_APP_VERSION,
        timestamp=datetime.now(UTC).isoformat(),
        checks=checks,
    )
