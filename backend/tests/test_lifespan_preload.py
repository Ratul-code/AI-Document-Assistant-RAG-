"""Tests for the model-preload behavior in the FastAPI lifespan.

The lifespan eagerly warms the embedding-model and reranker singletons
so the first `/analysis/query` request doesn't pay the multi-second
torch / checkpoint load cost (and so the first request doesn't trip
the MPS caching allocator). These tests pin three things:

1. With the reranker enabled, both `get_default_embedding_model` and
   `get_default_reranker` are called during startup and the singletons
   are populated for the rest of the request lifetime.
2. With the reranker disabled, the reranker factory is NOT called —
   the switch is the gate.
3. If a model preload raises, the app still finishes startup and the
   singleton is left in the factory's fallback state. A startup-time
   model failure must not block the API from booting.

We drive the lifespan directly via ``app.router.lifespan_context``
(no network, no Redis dependency for the preload itself — the WS
subscriber is started after the preload and tolerates Redis being
down via its reconnect loop).
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.core.config import get_settings
from app.services.ai import embeddings as embeddings_pkg
from app.services.ai import rerankers as rerankers_pkg
from app.services.ai.embeddings import (
    DeterministicStubModel,
    reset_embedding_model_singleton,
)
from app.services.ai.rerankers import (
    NoneReranker,
    reset_reranker_singleton,
)


def _run_lifespan(app) -> None:
    """Drive one full lifespan cycle (startup + shutdown) sync-style
    so the assertions can read singleton state on the way out."""

    async def _drive() -> None:
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(_drive())


def test_lifespan_preloads_embedding_model_when_rerank_disabled(monkeypatch) -> None:
    """Embedding-model preload happens regardless of the rerank switch —
    every `/analysis/query` request needs it."""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "stub")
    monkeypatch.setenv("ANALYSIS_RERANK_ENABLED", "false")
    monkeypatch.setenv("ANALYSIS_RERANK_PROVIDER", "none")
    get_settings.cache_clear()
    reset_embedding_model_singleton()
    reset_reranker_singleton()

    import app.main as main_pkg
    from app.main import create_app

    app = create_app()

    embed_calls: list[int] = []
    original_embed_factory = embeddings_pkg.get_default_embedding_model

    def _spy_embed_factory():
        embed_calls.append(1)
        return original_embed_factory()

    # Patch where the lifespan looks it up — at the import in `app.main`.
    monkeypatch.setattr(main_pkg, "get_default_embedding_model", _spy_embed_factory)

    _run_lifespan(app)

    # Embedding factory called exactly once during startup.
    assert (
        len(embed_calls) == 1
    ), f"expected embedding factory to be called once at startup, got {len(embed_calls)}"

    # Singleton should be populated from the prior startup.
    model = embeddings_pkg.get_default_embedding_model()
    assert isinstance(model, DeterministicStubModel)


def test_lifespan_preloads_reranker_when_enabled(monkeypatch) -> None:
    """When `analysis_rerank_enabled=True`, the reranker factory is
    called during startup and the singleton is populated."""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "stub")
    monkeypatch.setenv("ANALYSIS_RERANK_ENABLED", "true")
    monkeypatch.setenv("ANALYSIS_RERANK_PROVIDER", "none")
    get_settings.cache_clear()
    reset_embedding_model_singleton()
    reset_reranker_singleton()

    import app.main as main_pkg
    from app.main import create_app

    app = create_app()

    rerank_calls: list[int] = []
    original_rerank_factory = rerankers_pkg.get_default_reranker

    def _spy_rerank_factory():
        rerank_calls.append(1)
        return original_rerank_factory()

    monkeypatch.setattr(main_pkg, "get_default_reranker", _spy_rerank_factory)

    _run_lifespan(app)

    assert len(rerank_calls) == 1, (
        f"expected reranker factory to be called once at startup when enabled, "
        f"got {len(rerank_calls)}"
    )

    # Singleton is populated with the identity no-op (provider=none).
    rr = rerankers_pkg.get_default_reranker()
    assert isinstance(rr, NoneReranker)


def test_lifespan_skips_reranker_preload_when_disabled(monkeypatch) -> None:
    """When `analysis_rerank_enabled=False`, the reranker factory is
    NOT called during startup — no point loading a model that's
    gated off."""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "stub")
    monkeypatch.setenv("ANALYSIS_RERANK_ENABLED", "false")
    monkeypatch.setenv("ANALYSIS_RERANK_PROVIDER", "none")
    get_settings.cache_clear()
    reset_embedding_model_singleton()
    reset_reranker_singleton()

    import app.main as main_pkg
    from app.main import create_app

    app = create_app()

    rerank_calls: list[int] = []
    original_rerank_factory = rerankers_pkg.get_default_reranker

    def _spy_rerank_factory():
        rerank_calls.append(1)
        return original_rerank_factory()

    monkeypatch.setattr(main_pkg, "get_default_reranker", _spy_rerank_factory)

    _run_lifespan(app)

    assert rerank_calls == [], (
        f"expected reranker factory NOT to be called when disabled, "
        f"but it was called {len(rerank_calls)} time(s)"
    )


def test_lifespan_does_not_crash_when_embedding_preload_raises(monkeypatch, caplog) -> None:
    """A startup-time embedding preload failure must NOT crash the
    lifespan — the API has to boot so health checks and the docs
    surface stay reachable. The factory itself is the right place to
    enforce the hard fallback, but the lifespan also guards with a
    try/except so any unexpected raise (e.g. import-time failure) is
    contained."""

    monkeypatch.setenv("EMBEDDING_PROVIDER", "stub")
    monkeypatch.setenv("ANALYSIS_RERANK_ENABLED", "false")
    monkeypatch.setenv("ANALYSIS_RERANK_PROVIDER", "none")
    get_settings.cache_clear()
    reset_embedding_model_singleton()
    reset_reranker_singleton()

    import app.main as main_pkg
    from app.main import create_app

    app = create_app()

    def _boom():
        raise RuntimeError("simulated preload failure (e.g. checkpoint unreadable)")

    monkeypatch.setattr(main_pkg, "get_default_embedding_model", _boom)

    with caplog.at_level(logging.WARNING, logger="app.main"):
        # This must not raise.
        _run_lifespan(app)

    assert any(
        "embedding model preload failed" in rec.message
        and "simulated preload failure" in rec.message
        for rec in caplog.records
    ), f"Expected loud 'preload failed' warning, got: " f"{[r.message for r in caplog.records]}"
