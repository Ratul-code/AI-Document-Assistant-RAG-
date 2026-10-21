"""End-to-end smoke tests for GET /api/v1/documents/{id}/logs.

What we verify:
  1. 401 without auth.
  2. 404 for a random UUID (no info leak).
  3. 404 for another user's document (no info leak).
  4. 200 + empty list for a brand-new document with no log rows yet.
  5. 200 + paginated list with the right shape for a doc with rows.
  6. Pagination math (`total`, `limit`, `offset`) matches what we wrote.
  7. The response field set exactly matches `DocumentLogListResponse`
     so the frontend's typed consumer doesn't break.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "")

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.main import create_app
from app.models.document import Document, DocumentStatus
from app.models.document_log import DocumentLog
from app.models.user import User
from app.repositories import document_log as log_repo

client = TestClient(create_app())


def _new_client() -> TestClient:
    return TestClient(create_app())


def _register(client: TestClient, email: str) -> str:
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
    cookie = client.cookies.get(get_settings().access_cookie_name)
    assert cookie
    return cookie


def _decode_sub(cookie_value: str) -> str:
    import base64
    import json as _json

    payload_b64 = cookie_value.split(".")[1]
    payload_b64 += "=" * (4 - len(payload_b64) % 4)
    return _json.loads(base64.urlsafe_b64decode(payload_b64))["sub"]


async def _seed_doc_and_logs(owner_id: str, *, num_logs: int = 3) -> tuple[str, list[str]]:
    """Insert one document + N log rows for it. Returns (doc_id, log_ids)."""
    factory = get_session_factory()
    doc_id = uuid.uuid4()
    owner_uuid = UUID(owner_id)
    log_ids: list[str] = []
    async with factory() as session:  # type: AsyncSession
        doc = Document(
            id=doc_id,
            owner_id=owner_uuid,
            title="Logs Test",
            filename="f.pdf",
            mime_type="application/pdf",
            file_size_bytes=2048,
            storage_bucket="b",
            storage_path=f"{owner_id}/{doc_id}/f.pdf",
            status=DocumentStatus.learning,
        )
        session.add(doc)
        await session.commit()

    # Seed log rows with explicit timestamps for deterministic ordering.
    base = datetime.now(UTC)
    factory2 = get_session_factory()
    async with factory2() as session:  # type: AsyncSession
        for i in range(num_logs):
            row = DocumentLog(
                document_id=doc_id,
                owner_id=owner_uuid,
                event_type="embedding.progress",
                message=f"batch {i}",
                metadata_json=json.dumps({"batch_index": i}),
                created_at=base + timedelta(seconds=i),
            )
            session.add(row)
            await session.flush()
            log_ids.append(str(row.id))
        await session.commit()
    return str(doc_id), log_ids


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_logs_requires_auth() -> None:
    r = client.get(f"/api/v1/documents/{uuid.uuid4()}/logs")
    assert r.status_code == 401, r.text


def test_logs_nonexistent_returns_404() -> None:
    c = _new_client()
    _register(c, f"ne-{uuid.uuid4().hex[:8]}@x.com")
    r = c.get(f"/api/v1/documents/{uuid.uuid4()}/logs")
    assert r.status_code == 404, r.text


def test_logs_other_users_doc_returns_404() -> None:
    """User B must not be able to read user A's doc logs. 404, not 403,
    so we don't leak the existence of A's document."""
    c_a = _new_client()
    c_b = _new_client()
    _register(c_a, f"a-{uuid.uuid4().hex[:8]}@x.com")
    cookie_b = _register(c_b, f"b-{uuid.uuid4().hex[:8]}@x.com")
    owner_b = _decode_sub(cookie_b)

    doc_id, _ = asyncio.run(_seed_doc_and_logs(owner_b, num_logs=2))

    r = c_a.get(f"/api/v1/documents/{doc_id}/logs")
    assert (
        r.status_code == 404
    ), f"expected 404 for cross-owner logs read, got {r.status_code}: {r.text}"


def test_logs_empty_doc_returns_empty_list() -> None:
    """Brand-new doc with no log rows → 200 + items=[] + total=0."""
    c = _new_client()
    cookie = _register(c, f"empty-{uuid.uuid4().hex[:8]}@x.com")
    owner = _decode_sub(cookie)
    doc_id, _ = asyncio.run(_seed_doc_and_logs(owner, num_logs=0))

    r = c.get(f"/api/v1/documents/{doc_id}/logs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"items": [], "total": 0, "limit": 20, "offset": 0}


def test_logs_happy_path_with_rows() -> None:
    """Doc with 3 log rows → 200 + 3 items, newest first."""
    c = _new_client()
    cookie = _register(c, f"hp-{uuid.uuid4().hex[:8]}@x.com")
    owner = _decode_sub(cookie)
    doc_id, log_ids = asyncio.run(_seed_doc_and_logs(owner, num_logs=3))

    r = c.get(f"/api/v1/documents/{doc_id}/logs")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["total"] == 3
    assert body["limit"] == 20
    assert body["offset"] == 0
    assert len(body["items"]) == 3

    # Newest first: i=2 is the last-inserted row.
    messages = [item["message"] for item in body["items"]]
    assert messages == ["batch 2", "batch 1", "batch 0"]

    # Each item carries the expected field set; `metadata` is a dict
    # on the wire (the DB column is a JSON string, but the schema
    # does the round-trip via a field validator).
    expected_keys = {"id", "document_id", "event_type", "message", "metadata", "created_at"}
    for item in body["items"]:
        assert set(item.keys()) == expected_keys
        assert item["document_id"] == doc_id
        assert item["event_type"] == "embedding.progress"
        assert item["metadata"] == {"batch_index": int(item["message"].split()[-1])}


def test_logs_pagination_respects_limit_and_offset() -> None:
    """`limit=2, offset=1` returns the 2nd and 3rd rows (newest-first)."""
    c = _new_client()
    cookie = _register(c, f"pg-{uuid.uuid4().hex[:8]}@x.com")
    owner = _decode_sub(cookie)
    doc_id, _ = asyncio.run(_seed_doc_and_logs(owner, num_logs=5))

    r = c.get(f"/api/v1/documents/{doc_id}/logs?limit=2&offset=1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert [it["message"] for it in body["items"]] == ["batch 3", "batch 2"]


def test_logs_response_field_set_is_stable() -> None:
    """The response shape must match `DocumentLogListResponse` exactly
    so the frontend's typed consumer doesn't break."""
    c = _new_client()
    cookie = _register(c, f"fs-{uuid.uuid4().hex[:8]}@x.com")
    owner = _decode_sub(cookie)
    doc_id, _ = asyncio.run(_seed_doc_and_logs(owner, num_logs=1))

    r = c.get(f"/api/v1/documents/{doc_id}/logs")
    body = r.json()
    assert set(body.keys()) == {"items", "total", "limit", "offset"}


def test_logs_metadata_field_is_dict_not_string() -> None:
    """The DB stores `metadata_json` as a JSON string; the API must
    hand the client a `dict` so the frontend doesn't have to parse
    a string per row. This is the `DocumentLogItem._coerce_metadata`
    field validator's job."""
    c = _new_client()
    cookie = _register(c, f"md-{uuid.uuid4().hex[:8]}@x.com")
    owner = _decode_sub(cookie)
    doc_id, _ = asyncio.run(_seed_doc_and_logs(owner, num_logs=1))

    r = c.get(f"/api/v1/documents/{doc_id}/logs")
    item = r.json()["items"][0]
    # Must be a dict, not a string.
    assert isinstance(item["metadata"], dict)
    assert item["metadata"] == {"batch_index": 0}


if __name__ == "__main__":
    test_logs_requires_auth()
    print("OK: logs requires auth (401)")
    test_logs_nonexistent_returns_404()
    print("OK: logs nonexistent returns 404")
    test_logs_other_users_doc_returns_404()
    print("OK: logs cross-owner returns 404 (no leak)")
    test_logs_empty_doc_returns_empty_list()
    print("OK: logs empty doc returns 200 + empty list")
    test_logs_happy_path_with_rows()
    print("OK: logs happy path with rows")
    test_logs_pagination_respects_limit_and_offset()
    print("OK: logs pagination respects limit/offset")
    test_logs_response_field_set_is_stable()
    print("OK: logs response field set is stable")
    test_logs_metadata_field_is_dict_not_string()
    print("OK: logs metadata is a dict, not a string")
    print("\nALL LOGS TESTS PASSED")
