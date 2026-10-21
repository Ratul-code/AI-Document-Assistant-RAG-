"""Tests for the /health/live and /health/ready endpoints.

The two endpoints have different contracts:

- `/health/live` must be cheap and dependency-free.  It must answer
  200 as long as the process and the event loop are up.

- `/health/ready` must probe DB, Redis, Celery, and the LLM, and
  return 200 iff every check is up, 503 otherwise.

We exercise both the happy path and a forced-failure path for
readiness (monkeypatched `Redis.from_url` raises) to confirm the
endpoint translates probe failures into 503 and per-check detail,
not into a 500 from an unhandled exception.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client() -> TestClient:
    """A TestClient over a fresh app instance.  No network."""
    app = create_app()
    return TestClient(app)


# --- Liveness -----------------------------------------------------------


def test_health_live_returns_ok(client: TestClient) -> None:
    """`/health/live` answers 200 with the standard liveness payload,
    independent of any external dependency."""
    resp = client.get("/api/v1/health/live")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "ai-document-analyser"
    assert "version" in body
    assert "timestamp" in body


def test_health_legacy_alias_returns_same_as_live(client: TestClient) -> None:
    """The legacy `/health` (no /live) still works as a thin alias
    for `/health/live`, so existing monitors keep working."""
    legacy = client.get("/api/v1/health")
    live = client.get("/api/v1/health/live")
    assert legacy.status_code == 200
    assert legacy.json()["status"] == live.json()["status"]


# --- Readiness: happy path ----------------------------------------------


def test_health_ready_all_up_returns_200(client: TestClient) -> None:
    """When every probe succeeds, readiness returns 200 and
    `status="ready"` with one CheckResult per dependency.

    DB: a fresh app uses SQLite in-memory fallback, so SELECT 1 works.
    Redis + Celery + LLM: real network calls — these may flap in a
    fully offline dev box.  We force them up here so the test is
    deterministic.
    """
    fake_ok = (True, "stub ok")

    with (
        patch("app.api.v1.endpoints.health._sync_redis_ping", return_value=fake_ok),
        patch("app.api.v1.endpoints.health._sync_celery_ping", return_value=fake_ok),
        patch(
            "app.api.v1.endpoints.health._async_llm_probe",
            new=_async_return_ok,
        ),
    ):
        resp = client.get("/api/v1/health/ready")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    assert set(body["checks"].keys()) == {"database", "redis", "celery", "llm"}
    for name, check in body["checks"].items():
        assert check["status"] == "up", f"{name} check should be up, got {check}"


# --- Readiness: failure paths -------------------------------------------


def test_health_ready_redis_down_returns_503(client: TestClient) -> None:
    """A redis probe failure flips the overall status to 503 and is
    reported as `status="down"` with a detail string.  Other checks
    should still complete (they run concurrently) and report their
    own status."""
    fake_ok = (True, "stub ok")
    redis_down = (False, "ConnectionError: refused")

    with (
        patch("app.api.v1.endpoints.health._sync_redis_ping", return_value=redis_down),
        patch("app.api.v1.endpoints.health._sync_celery_ping", return_value=fake_ok),
        patch(
            "app.api.v1.endpoints.health._async_llm_probe",
            new=_async_return_ok,
        ),
    ):
        resp = client.get("/api/v1/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["redis"]["status"] == "down"
    assert "ConnectionError" in body["checks"]["redis"]["detail"]
    # The other checks should still report up.
    assert body["checks"]["database"]["status"] == "up"
    assert body["checks"]["celery"]["status"] == "up"
    assert body["checks"]["llm"]["status"] == "up"


def test_health_ready_llm_down_returns_503(client: TestClient) -> None:
    """An LLM 4xx response is reported as `down` (only 2xx, 3xx, and
    401/403 count as up).  The status code propagates to 503."""
    fake_ok = (True, "stub ok")

    async def _llm_4xx(*_args, **_kwargs):
        return (False, "HTTP 500")

    with (
        patch("app.api.v1.endpoints.health._sync_redis_ping", return_value=fake_ok),
        patch("app.api.v1.endpoints.health._sync_celery_ping", return_value=fake_ok),
        patch("app.api.v1.endpoints.health._async_llm_probe", new=_llm_4xx),
    ):
        resp = client.get("/api/v1/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["checks"]["llm"]["status"] == "down"
    assert "500" in body["checks"]["llm"]["detail"]


def test_health_ready_celery_probe_raising_does_not_500(client: TestClient) -> None:
    """A probe that raises is contained by `_run_check` — the endpoint
    answers 503 (not 500) so a load-balancer probe doesn't get a
    spurious restart signal from an unhandled exception."""
    fake_ok = (True, "stub ok")

    def _celery_raises() -> tuple[bool, str | None]:
        raise RuntimeError("broker exploded")

    with (
        patch("app.api.v1.endpoints.health._sync_redis_ping", return_value=fake_ok),
        patch("app.api.v1.endpoints.health._sync_celery_ping", side_effect=_celery_raises),
        patch(
            "app.api.v1.endpoints.health._async_llm_probe",
            new=_async_return_ok,
        ),
    ):
        resp = client.get("/api/v1/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["checks"]["celery"]["status"] == "down"
    assert "RuntimeError" in body["checks"]["celery"]["detail"]


# --- Helpers ------------------------------------------------------------


async def _async_return_ok(*_args, **_kwargs):
    """Async no-op probe that always reports up.  Used as a stand-in
    for the real LLM HTTP call in the happy-path test (so the test
    never hits the network)."""
    return (True, "stub ok")
