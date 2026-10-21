"""Shared pytest fixtures for the eval test suite.

Mirrors `backend/tests/conftest.py` exactly:

- `get_settings()` is `@lru_cache`d; the autouse fixture clears the
  cache before each test so env-level toggles take effect.
- `get_default_embedding_model()` caches the loaded model in a
  process-level singleton; the fixture drops the cache so a test
  that flips `EMBEDDING_PROVIDER` mid-run gets a fresh model.
- The sync engine and session factory are reset between tests so
  DB-URL overrides don't leak.
- The eval's own `unpatch_pubsub()` is called at teardown so a test
  that patched `ws_publisher.publish_event` doesn't leak into the
  next test.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_globals():
    """Force a fresh Settings / embedder / sync engine / pubsub patch per test."""
    # Lazy imports — keeps this conftest importable from any path the
    # test runner decides to use.
    from app.core.config import get_settings
    from app.db.sync_session import reset_for_tests
    from app.services.ai.embeddings import reset_embedding_model_singleton

    get_settings.cache_clear()
    reset_embedding_model_singleton()
    yield
    # Teardown: drop the sync engine so a later test can override
    # DATABASE_URL_SYNC and start from scratch.
    reset_for_tests()
    # Drop any `publish_event` patch installed by the eval's
    # `_db.build_hermetic_env()` so the next test sees the live Redis
    # publisher again. Imports are lazy because `_db` is optional.
    try:
        from eval.scripts._db import unpatch_pubsub

        unpatch_pubsub()
    except ImportError:
        # `_db.py` not yet implemented — fine, nothing to clean up.
        pass
