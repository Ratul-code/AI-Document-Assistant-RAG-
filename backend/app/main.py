"""FastAPI application entry point."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis
from slowapi.middleware import SlowAPIMiddleware

from app.api.v1.router import api_router_v1
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.middleware import RequestIDMiddleware
from app.core.rate_limit import (
    RateLimitExceeded,
    _rate_limit_exceeded_handler,
    limiter,
)
from app.db.session import get_engine, dispose_engine
from app.services.ai.embeddings import get_default_embedding_model
from app.services.ai.rerankers import get_default_reranker
from app.services.pubsub.ws_subscriber import get_ws_subscriber
from app.websockets.manager import get_connection_manager
from app.websockets.routes import router as ws_router

logger = logging.getLogger(__name__)


# Dev convenience: load `.env` from the backend root into os.environ
# on startup so secrets referenced via `os.environ.get(...)` (notably
# `ANALYSIS_LLM_API_KEY` in `app.core.config.get_llm_api_key`) work
# without requiring the dev to `export` them in their shell.
#
# Safety:
#  - `override=False` — a real shell env var always wins over `.env`,
#    so production deployments that pass secrets via the orchestrator
#    are unaffected.
#  - We only load if the file actually exists; production images that
#    don't ship `.env` are a no-op.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_BACKEND_ROOT / ".env", override=False)


async def _worker_registry_keepalive(
    stop: asyncio.Event,
) -> None:
    """Keep this worker's id in the ws:workers SET alive.

    Runs as a background task. Refreshes every `ws_worker_keepalive_s`
    with a TTL of `ws_worker_registry_ttl_s` so a hard-crashed worker
    expires on its own (no SREM on clean shutdown either, but the
    finally-block below does a best-effort SREM). All Redis failures
    are logged and swallowed — the app must not crash if Redis blips.

    No-op when `ws_b3_enabled=False` (v1 single-channel mode doesn't
    need a worker registry).
    """
    settings = get_settings()
    if not settings.ws_b3_enabled:
        logger.info("[startup] ws b3 disabled; worker registry keepalive skipped")
        return

    client = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        while not stop.is_set():
            try:
                await client.sadd(
                    settings.ws_worker_registry_key, settings.worker_id
                )
                await client.expire(
                    settings.ws_worker_registry_key,
                    settings.ws_worker_registry_ttl_s,
                )
            except Exception as exc:
                logger.warning(
                    "ws worker registry refresh failed: %s", exc,
                )
            # Sleep in short slices so shutdown is responsive.
            sleep_for = float(settings.ws_worker_keepalive_s)
            slept = 0.0
            while slept < sleep_for and not stop.is_set():
                await asyncio.sleep(min(0.5, sleep_for - slept))
                slept += 0.5
    finally:
        try:
            await client.srem(
                settings.ws_worker_registry_key, settings.worker_id
            )
        except Exception:
            logger.debug("ws worker registry SREM on shutdown failed",
                         exc_info=True)
        try:
            await client.aclose()
        except Exception:
            logger.debug("ws worker registry client close failed",
                         exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown."""
    settings = get_settings()
    engine = get_engine()
    print(
        f"[startup] env={settings.app_env}\n"
        f"[startup] engine ready: {engine.url}\n"
        f"db={'postgres' if settings.is_postgres else 'sqlite'}\n"
        f"[startup] worker_id={settings.worker_id} "
        f"ws_b3={settings.ws_b3_enabled} "
        f"ws_channel={settings.ws_pubsub_channel_pattern.format(worker_id=settings.worker_id) if settings.ws_b3_enabled else settings.ws_pubsub_channel}"
    )

    # Eagerly warm the singletons used by the analysis hot path so the
    # first `/analysis/query` (or `process_document` Celery task in the
    # same process) doesn't pay the multi-second torch / checkpoint
    # load cost. Without this, request N=1 is slow and the request
    # that triggers the load triggers a one-shot MPS caching allocator
    # spike (see ADR-019 + progress.md "MPS memory leak in embedding
    # pipeline"). The constructors themselves are wrapped in
    # try/except inside the factories — they fall back to a no-op
    # identity implementation on construction failure and log a
    # warning. We never raise from this preload: a missing model
    # shouldn't block the API from booting.
    t0 = time.monotonic()
    try:
        embed_model = get_default_embedding_model()
        logger.info(
            "[startup] embedding model preloaded: %s (dim=%d) in %.2fs",
            embed_model.model_name, embed_model.dim, time.monotonic() - t0,
        )
    except Exception as exc:  # noqa: BLE001 — defensive: startup must not crash
        logger.warning(
            "[startup] embedding model preload failed (%s: %s); "
            "the first /analysis/query request will pay the load cost.",
            type(exc).__name__, exc,
        )

    if settings.analysis_rerank_enabled:
        t1 = time.monotonic()
        try:
            reranker = get_default_reranker()
            logger.info(
                "[startup] reranker preloaded: %s in %.2fs",
                reranker.name, time.monotonic() - t1,
            )
        except Exception as exc:  # noqa: BLE001 — defensive: rerank is opt-in
            logger.warning(
                "[startup] reranker preload failed (%s: %s); "
                "queries will fall back to retriever order until restart.",
                type(exc).__name__, exc,
            )

    # Start the Redis pub/sub subscriber so any WS event the worker
    # publishes is fanned out to this process's connected sockets.
    await get_ws_subscriber().start()

    # Start the worker-registry keepalive AFTER the subscriber so the
    # worker only advertises itself once it can actually receive.
    keepalive_stop = asyncio.Event()
    keepalive_task: asyncio.Task[None] | None = None
    if settings.ws_b3_enabled:
        keepalive_task = asyncio.create_task(
            _worker_registry_keepalive(keepalive_stop),
            name="ws-worker-registry-keepalive",
        )

    yield

    # Stop the keepalive FIRST so the publisher stops fanning out to
    # this channel before we tear down the subscriber.
    keepalive_stop.set()
    if keepalive_task is not None:
        try:
            await asyncio.wait_for(keepalive_task, timeout=2.0)
        except asyncio.TimeoutError:
            keepalive_task.cancel()
            logger.warning("ws worker registry keepalive: cancelled after 2s timeout")
        except Exception:
            logger.debug("ws worker registry keepalive ended with error",
                         exc_info=True)

    # Stop the subscriber so no in-flight messages try to write to
    # sockets we're about to close.
    await get_ws_subscriber().stop()

    # Close all WebSocket connections cleanly on shutdown.
    await get_connection_manager().shutdown()
    await dispose_engine()
    print("[shutdown] engine disposed")


def create_app() -> FastAPI:
    """Application factory."""
    settings = get_settings()

    # Wire the root logger before anything else logs so app-level
    # `logger.info(...)` calls (e.g. in the WebSocket route) actually
    # print — uvicorn only configures its own loggers, leaving ours at
    # WARNING with no handlers.
    configure_logging()

    app = FastAPI(
        title="AI Document Analyser",
        version="0.1.0",
        description="Backend for the AI Document Analyser learning project.",
        lifespan=lifespan,
    )

    # CORS: allow the frontends listed in settings to call this API from a browser.
    # In dev, defaults cover localhost:3000 (CRA) and :5173 (Vite). In production,
    # set CORS_ORIGINS in your environment to your real frontend origin(s).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=settings.cors_allow_methods_list,
        allow_headers=settings.cors_allow_headers_list,
    )

    # Rate limiting (slowapi). The limiter instance lives on
    # `app.state.limiter` — the same place slowapi's middleware and
    # `@limiter.limit(...)` decorators expect to find it. The exception
    # handler turns `RateLimitExceeded` into a JSON 429 with the same
    # `{"detail": "..."}` shape the rest of the API uses.
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    # Request ID middleware: stamps every request with a short
    # unique ID, echoes it on the response, and propagates it into
    # every log line. Registered LAST so Starlette's LIFO middleware
    # stack makes it the OUTERMOST layer — that way the request ID
    # is set before CORS / SlowAPI run (so their log lines and
    # response headers are also correlated) and the response header
    # is written last. See `app/core/middleware.py` for the full
    # rationale on middleware ordering.
    app.add_middleware(RequestIDMiddleware)

    # Mount all v1 endpoints under /api/v1
    app.include_router(api_router_v1, prefix="/api/v1")

    # WebSocket endpoint: /ws (cookie auth via access_token)
    app.include_router(ws_router)

    # NOTE: the old debug-only `/db-check` endpoint was removed when
    # `/api/v1/health/ready` (and the existing `/api/v1/health/live`)
    # were wired up. The new readiness probe supersedes it: it covers
    # DB + Redis + Celery + LLM with bounded timeouts and a proper
    # 200/503 status code, suitable as a load-balancer health check.

    return app


app = create_app()