"""Tests for the request-ID middleware and the logging filter.

Three contracts are exercised end-to-end through `TestClient`:

1. **Generation** — when the incoming request carries no `X-Request-ID`
   header, the middleware mints one and the response carries it.
2. **Propagation** — when the incoming request does carry a
   well-formed `X-Request-ID`, the middleware trusts it and echoes
   the SAME value on the response (standard LB-tracing pattern).
3. **Log correlation** — a log line emitted *inside* a request handler
   picks up the request ID via the `RequestIdFilter` on the root
   handler, so an operator can `grep <id> app.log` and find the
   entire trail for that one request.

These tests do not rely on a real DB / Redis / Celery. `/health/live`
is dependency-free (per its docstring) and is the cheapest possible
target; the readiness probe (used in the log-correlation test) is
patched to deterministically trigger a logged failure so we don't
have to spin up real services.
"""

from __future__ import annotations

import logging
import re
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.request_id import (
    RequestIdFilter,
    get_request_id,
    reset_request_id,
    set_request_id,
)
from app.main import create_app


# --- Helpers ------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    """A TestClient over a fresh app instance. No network."""
    app = create_app()
    return TestClient(app)


# Regex for a freshly-minted ID. The middleware uses
# `secrets.token_urlsafe(6)` which yields 8 base64url characters.
# Anchored so a partial-match (e.g. substring inside a longer value)
# can't pass the test.
_MINTED_RE = re.compile(r"^[A-Za-z0-9_\-]{8,16}$")


# --- 1. Generation ------------------------------------------------------


def test_request_id_generated_when_absent(client: TestClient) -> None:
    """A request without an `X-Request-ID` header gets a minted one
    on the response. The minted value is a short, URL-safe, opaque
    string — not empty, not the literal header name, not the
    request path."""
    resp = client.get("/api/v1/health/live")

    assert resp.status_code == 200
    rid = resp.headers.get("X-Request-ID")
    assert rid is not None, "response missing X-Request-ID header"
    assert rid != "", "X-Request-ID header is empty"
    assert _MINTED_RE.match(rid), (
        f"minted request id {rid!r} does not look like a token_urlsafe value"
    )


# --- 2. Propagation -----------------------------------------------------


def test_request_id_echoed_when_supplied(client: TestClient) -> None:
    """If the caller supplies a well-formed X-Request-ID (the
    standard LB / mesh pattern), the response carries the same
    value. This is the whole point of propagation: the upstream
    tracing layer mints the ID once and the same ID is visible on
    every hop's response."""
    supplied = "trace-from-loadbalancer-001"
    resp = client.get(
        "/api/v1/health/live",
        headers={"X-Request-ID": supplied},
    )

    assert resp.status_code == 200
    assert resp.headers.get("X-Request-ID") == supplied


def test_request_id_malformed_value_is_replaced(client: TestClient) -> None:
    """A supplied value that fails the safety check (e.g. contains
    a newline, is absurdly long, or is too short) is replaced with
    a freshly-minted ID. This prevents log forging and memory
    pressure from a hostile client."""
    # Newline injection attempt: `a\nFAKE LOG LINE` would otherwise
    # be stamped into the log filter and break the log format.
    hostile = "abc\n2026-10-15 INFO attacker forged line"
    resp = client.get(
        "/api/v1/health/live",
        headers={"X-Request-ID": hostile},
    )

    assert resp.status_code == 200
    rid = resp.headers.get("X-Request-ID")
    assert rid is not None
    assert rid != hostile, "hostile value was NOT replaced"
    assert "\n" not in rid
    assert _MINTED_RE.match(rid), f"replacement id {rid!r} is not a valid token"


# --- 3. Log correlation -------------------------------------------------


def test_request_id_appears_on_log_records(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A log line emitted from inside a request handler carries the
    same request ID as the response header. This is the test that
    proves log correlation actually works end-to-end — the
    `RequestIdFilter` is installed, the `ContextVar` is set by the
    middleware, and the formatter renders the ID.

    We trigger a logged line by hitting `/health/ready` with the
    redis probe patched to fail. The handler emits
    `logger.warning("readiness: %s raised ...")` for the failed
    check, which goes through the root handler and is captured by
    `caplog`.
    """
    # configure_logging() is already called by create_app() (via
    # main.create_app → configure_logging()). But we call it again
    # to be explicit and so the test is self-contained if the
    # app-init order changes.
    configure_logging()

    redis_down = (False, "ConnectionError: refused")
    redis_ok = (True, "stub ok")
    with caplog.at_level(logging.WARNING, logger="app.api.v1.endpoints.health"), \
         patch("app.api.v1.endpoints.health._sync_redis_ping", return_value=redis_down), \
         patch("app.api.v1.endpoints.health._sync_celery_ping", return_value=redis_ok), \
         patch(
             "app.api.v1.endpoints.health._async_llm_probe",
             new=_async_return_ok,
         ):
        supplied = "corr-test-001"
        resp = client.get(
            "/api/v1/health/ready",
            headers={"X-Request-ID": supplied},
        )

    assert resp.status_code == 503  # redis is "down" → 503
    assert resp.headers.get("X-Request-ID") == supplied

    # At least one warning record was captured with our request id
    # stamped on it. The middleware's "request started" line is
    # not what we want — we want the LINE EMITTED FROM INSIDE THE
    # HANDLER, which proves the ContextVar propagated into the
    # service / route code.
    matching = [
        r for r in caplog.records
        if getattr(r, "request_id", None) == supplied
    ]
    assert matching, (
        "no log record carried the request_id; the filter / "
        "ContextVar plumbing is broken. All captured records: "
        f"{[(r.name, getattr(r, 'request_id', None)) for r in caplog.records]}"
    )
    # The health handler logs at WARNING when a check raises /
    # fails, so we should see at least one warning-level match.
    assert any(r.levelno == logging.WARNING for r in matching)


# --- 4. Direct unit tests for the helpers ------------------------------
#
# These are independent of the FastAPI app — they pin down the
# ContextVar / filter behaviour in isolation so a future refactor
# can't silently break the contract.


def test_request_id_context_var_round_trip() -> None:
    """set_request_id() makes the value visible to get_request_id();
    reset_request_id(token) restores the previous value (or empty
    default if there was none)."""
    assert get_request_id() == "", "default should be empty string"
    token = set_request_id("rid-A")
    try:
        assert get_request_id() == "rid-A"
        token_inner = set_request_id("rid-B")
        try:
            assert get_request_id() == "rid-B"
        finally:
            reset_request_id(token_inner)
        # After the inner reset, the outer value is back.
        assert get_request_id() == "rid-A"
    finally:
        reset_request_id(token)
    assert get_request_id() == "", "outer reset failed"


def test_request_id_filter_stamps_dash_when_no_id() -> None:
    """When the ContextVar is empty (the Celery / startup case),
    the filter stamps '-' (not None, not ''), so the log format
    string can always render %(request_id)s without KeyError and
    the rendered line is still self-explanatory."""
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname="", lineno=0,
        msg="hi", args=(), exc_info=None,
    )
    # No ID set — get_request_id() returns the default "".
    assert get_request_id() == ""
    flt = RequestIdFilter()
    assert flt.filter(record) is True
    assert record.request_id == "-", (
        f"filter should stamp '-' as a sentinel; got {record.request_id!r}"
    )


def test_request_id_filter_stamps_real_id() -> None:
    """When the ContextVar holds a real ID, the filter stamps it
    verbatim onto the record."""
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname="", lineno=0,
        msg="hi", args=(), exc_info=None,
    )
    token = set_request_id("rid-XYZ")
    try:
        flt = RequestIdFilter()
        flt.filter(record)
        assert record.request_id == "rid-XYZ"
    finally:
        reset_request_id(token)


# --- 5. Config plumbing -------------------------------------------------


def test_request_id_settings_have_sane_defaults() -> None:
    """The default config has request_id_enabled=True and the
    standard `X-Request-ID` header. If a future PR flips either
    default, every existing log-correlation assumption breaks, so
    this test pins them down."""
    settings = get_settings()
    assert settings.request_id_enabled is True
    assert settings.request_id_header == "X-Request-ID"


# --- Async no-op probe (same shape as test_health.py) -----------------


async def _async_return_ok(*_args, **_kwargs):
    """Async no-op probe for the LLM dependency. Returns the same
    `(_async_return_ok)` shape used by `tests/api/test_health.py`
    so the readiness endpoint's `_run_check` flow runs end-to-end
    without hitting the network."""
    return (True, "stub ok")
