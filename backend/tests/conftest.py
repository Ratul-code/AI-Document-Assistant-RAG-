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

import os

import pytest

# =============================================================================
# ENVIRONMENT PINS — MUST BE SET BEFORE THE APP IS IMPORTED
# =============================================================================
# `backend/.env` is BOTH the local dev config AND the production config
# on the VPS, so it carries production values (APP_ENV=prod,
# COOKIE_SECURE=true). The test suite must never inherit them.
#
# These have to be set HERE, at module scope, rather than inside the
# autouse fixture below — because several modules snapshot the settings
# at IMPORT time. `app/api/v1/endpoints/auth.py` does:
#
#     _settings = get_settings()
#
# at module level, and reads `_settings.cookie_secure` when setting the
# auth cookies. A fixture that runs per-test is too late: by then the
# module is already imported and has frozen `cookie_secure=True` into
# its module-level `_settings`.
#
# With COOKIE_SECURE=true, the TestClient (plain HTTP) sets the auth
# cookie on login but never sends it back — the `Secure` attribute
# tells the client jar to withhold it over http:// — so every
# authenticated test fails with 401 "Not authenticated".
#
# `conftest.py` is imported before any test module, so setting these
# here guarantees the app sees them at import time.
#
# "test" behaves exactly like "dev" for the fail-fast validator in
# `app/core/config.py` (it only fires for prod/production), so this
# stays permissive.
# =============================================================================
os.environ["APP_ENV"] = "test"
os.environ["COOKIE_SECURE"] = "false"

from app.core.config import get_settings  # noqa: E402
from app.core.rate_limit import limiter  # noqa: E402
from app.db.sync_session import reset_for_tests  # noqa: E402
from app.services.ai.embeddings import reset_embedding_model_singleton  # noqa: E402
from app.services.ai.rerankers import reset_reranker_singleton  # noqa: E402


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
    # opts back in via itsown per-test fixture.
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    # Re-assert the pins from module scope, in case a test (or an
    # earlier fixture) mutated the environment. `monkeypatch` undoes
    # these at the end of each test.
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("COOKIE_SECURE", "false")
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
