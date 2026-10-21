"""Shared pytest fixtures.

Settings cache invalidation: `get_settings()` is `@lru_cache`d, so
the first test that imports the app locks in the env vars at that
moment. Subsequent tests that override env vars (e.g.
`test_upload_intent.py` clearing Supabase creds) would see stale
cached values. The autouse fixture below clears the cache before each
test, letting test-level env mutation take effect cleanly.

Embedding model singleton: `get_default_embedding_model()` caches the
loaded model for the lifetime of the process to avoid re-loading torch
weights between requests (and exhausting MPS memory). Tests that toggle
`EMBEDDING_PROVIDER` between cases must drop that cache too, otherwise
the first provider sticks for the rest of the run.

Reranker singleton: same pattern as the embedding model — the loaded
cross-encoder is cached for the process lifetime. Tests that toggle
`ANALYSIS_RERANK_PROVIDER` between cases drop it explicitly.

Rerank default: every test starts with the rerank DISABLED (and the
provider set to "none"), regardless of what's in `.env`. The cross-
encoder requires `sentence_transformers` which isn't always installed
in CI / dev venvs, and most analyser tests don't exercise the rerank
path. Tests in `test_rerank.py` opt back in via `monkeypatch.setenv`.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.db.sync_session import reset_for_tests
from app.services.ai.embeddings import reset_embedding_model_singleton
from app.services.ai.rerankers import reset_reranker_singleton


@pytest.fixture(autouse=True)
def _reset_settings_cache(monkeypatch):
    """Force a fresh Settings read for every test, with rerank off."""
    # Force the rerank master switch off and the provider to the
    # identity no-op. This is independent of what's in `.env` (the
    # user may have rerank enabled in their dev .env for live demos).
    # Tests that exercise the rerank path override these via their
    # own `monkeypatch.setenv("ANALYSIS_RERANK_ENABLED", "true")`.
    monkeypatch.setenv("ANALYSIS_RERANK_ENABLED", "false")
    monkeypatch.setenv("ANALYSIS_RERANK_PROVIDER", "none")
    # Disable rate limiting by default so existing tests that hit an
    # endpoint >N times in a row (e.g. analysis tests) don't get
    # throttled. The dedicated `tests/api/test_rate_limit.py` suite
    # opts back in via its own per-test fixture.
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    get_settings.cache_clear()
    # Mirror the env-var on the singleton Limiter — slowapi reads
    # `limiter.enabled` directly (not via settings), so we have to
    # flip the attribute too. Rate-limit tests override this in
    # their own fixtures.
    limiter.enabled = False
    reset_embedding_model_singleton()
    reset_reranker_singleton()
    yield
    # Also reset the sync engine cache so DB-URL changes between tests
    # don't keep pointing at the old engine.
    reset_for_tests()
