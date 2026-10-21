"""Smoke tests for the analysis REST endpoints.

We monkeypatch the heavy bits (embedder + LLM) so the tests run
without any network. The goal is to pin the request/response shape
and the error-code → HTTP-status mapping that the frontend relies on.
"""

from __future__ import annotations

import os
import uuid
from typing import AsyncIterator

os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = ""
os.environ.setdefault("ANALYSIS_LLM_API_KEY", "sk-test")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(create_app())


# ---------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------


def _register_and_login(client: TestClient, email: str) -> None:
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "CorrectHorseBatteryStaple!1", "name": "T"},
    )
    assert r.status_code == 201, r.text
    r = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "CorrectHorseBatteryStaple!1"},
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------


def test_query_requires_auth(client) -> None:
    r = client.post("/api/v1/analysis/query", json={"question": "hi"})
    assert r.status_code == 401


# ---------------------------------------------------------------------
# Happy path with monkeypatched orchestrator
# ---------------------------------------------------------------------


def test_query_returns_answer_and_citations_when_ready(client, monkeypatch) -> None:
    _register_and_login(client, f"a-{uuid.uuid4()}@example.com")

    from app.api.v1.endpoints import analysis as endpoint_module
    from app.services.document.analyser import (
        _ErrorOutcome,
        _QueryOutcome,
    )
    from app.schemas.analysis import QueryResponse, QueryCitation

    fake_response = QueryResponse(
        answer="The capital is Paris [1].",
        citations=[
            QueryCitation(
                index=1,
                chunk_id=uuid.uuid4(),
                document_id=uuid.uuid4(),
                document_title="Geography",
                page_number=2,
                section_path=[],
                snippet="Paris is the capital.",
                score=0.93,
            )
        ],
    )

    async def fake_once(**kwargs):
        return _QueryOutcome(response=fake_response)

    monkeypatch.setattr(endpoint_module, "analyse_query_once", fake_once)
    # Also patch the alias used in the import inside `query`.
    import app.services.document.analyser as analyser_pkg
    monkeypatch.setattr(analyser_pkg, "analyse_query_once", fake_once)

    r = client.post(
        "/api/v1/analysis/query",
        json={"question": "What is the capital of France?"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "The capital is Paris [1]."
    assert len(body["citations"]) == 1
    assert body["citations"][0]["document_title"] == "Geography"
    assert body["citations"][0]["score"] == pytest.approx(0.93)


# ---------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------


def test_query_maps_no_documents_ready_to_422(client, monkeypatch) -> None:
    _register_and_login(client, f"b-{uuid.uuid4()}@example.com")

    from app.api.v1.endpoints import analysis as endpoint_module
    from app.services.document.analyser import _ErrorOutcome

    async def fake_once(**kwargs):
        return _ErrorOutcome(
            code="NO_DOCUMENTS_READY",
            message="No queryable documents are available for this user.",
        )

    monkeypatch.setattr(endpoint_module, "analyse_query_once", fake_once)
    import app.services.document.analyser as analyser_pkg
    monkeypatch.setattr(analyser_pkg, "analyse_query_once", fake_once)

    r = client.post(
        "/api/v1/analysis/query",
        json={"question": "anything"},
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["detail"]["code"] == "NO_DOCUMENTS_READY"


def test_query_maps_provider_mismatch_to_503(client, monkeypatch) -> None:
    _register_and_login(client, f"c-{uuid.uuid4()}@example.com")

    from app.api.v1.endpoints import analysis as endpoint_module
    from app.services.document.analyser import _ErrorOutcome

    async def fake_once(**kwargs):
        return _ErrorOutcome(
            code="PROVIDER_MISMATCH",
            message="Embedding dim mismatch.",
        )

    monkeypatch.setattr(endpoint_module, "analyse_query_once", fake_once)
    import app.services.document.analyser as analyser_pkg
    monkeypatch.setattr(analyser_pkg, "analyse_query_once", fake_once)

    r = client.post(
        "/api/v1/analysis/query",
        json={"question": "x"},
    )
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "PROVIDER_MISMATCH"


def test_query_maps_provider_error_to_502(client, monkeypatch) -> None:
    _register_and_login(client, f"d-{uuid.uuid4()}@example.com")

    from app.api.v1.endpoints import analysis as endpoint_module
    from app.services.document.analyser import _ErrorOutcome

    async def fake_once(**kwargs):
        return _ErrorOutcome(code="PROVIDER_ERROR", message="upstream timeout")

    monkeypatch.setattr(endpoint_module, "analyse_query_once", fake_once)
    import app.services.document.analyser as analyser_pkg
    monkeypatch.setattr(analyser_pkg, "analyse_query_once", fake_once)

    r = client.post("/api/v1/analysis/query", json={"question": "x"})
    assert r.status_code == 502, r.text


def test_query_rejects_empty_question(client) -> None:
    _register_and_login(client, f"e-{uuid.uuid4()}@example.com")
    r = client.post("/api/v1/analysis/query", json={"question": ""})
    assert r.status_code == 422
    # Pydantic validation rejects min_length=1.
    assert "question" in r.text


def test_query_rejects_oversized_question(client) -> None:
    _register_and_login(client, f"f-{uuid.uuid4()}@example.com")
    r = client.post(
        "/api/v1/analysis/query", json={"question": "x" * 5000}
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------
# Streaming-init route
# ---------------------------------------------------------------------


def test_stream_init_returns_stream_id(client, monkeypatch) -> None:
    _register_and_login(client, f"g-{uuid.uuid4()}@example.com")

    # Patch `_run_stream` so the spawned task is harmless.
    async def fake_run_stream(**kwargs):
        # Nothing to do — tests are in-process, the WS push is bypassed.
        return None

    from app.api.v1.endpoints import analysis as endpoint_module
    monkeypatch.setattr(endpoint_module, "_run_stream", fake_run_stream)

    r = client.post(
        "/api/v1/analysis/query/stream",
        json={"question": "What does the doc say?"},
    )
    # The handler returns immediately with 202 once the task is spawned.
    assert r.status_code == 202, r.text
    body = r.json()
    assert "stream_id" in body
    # stream_id parses as a UUID.
    uuid.UUID(body["stream_id"])


def test_stream_init_requires_auth(client) -> None:
    r = client.post(
        "/api/v1/analysis/query/stream",
        json={"question": "x"},
    )
    assert r.status_code == 401
