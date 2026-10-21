"""End-to-end smoke tests for GET /api/v1/documents.

What we verify:
1. 401 without auth.
2. Empty list for a brand-new user.
3. Seeded docs appear, newest first.
4. ?status= filter returns only matching docs and correct total.
5. Pagination via limit/offset works correctly.
6. Users only see their own documents (no cross-user leakage).
7. limit > 100 is rejected (422).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from uuid import UUID

os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = ""

from datetime import UTC

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.main import create_app
from app.models.document import Document, DocumentStatus

client = TestClient(create_app())


def _new_client() -> TestClient:
    """Fresh TestClient with its own cookie jar.

    Tests for "user A can't see user B's docs" need to issue requests
    as different users in the same test, which requires separate
    cookie jars.
    """
    return TestClient(create_app())


def _register(client: TestClient, email: str) -> str:
    """Register + login on the given client; return the access_token cookie."""
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


async def _seed(
    owner_id: str,
    *,
    status: DocumentStatus = DocumentStatus.ready,
    title: str = "Seed",
    filename: str = "seed.pdf",
    created_offset_seconds: int = 0,
) -> str:
    """Insert a document row and return its id.

    `created_offset_seconds` lets us create docs with different
    created_at values for ordering tests. Positive = older.
    """
    from datetime import datetime, timedelta, timezone

    factory = get_session_factory()
    async with factory() as session:
        doc = Document(
            id=uuid.uuid4(),
            owner_id=UUID(owner_id),
            title=title,
            filename=filename,
            mime_type="application/pdf",
            file_size_bytes=1024,
            storage_bucket="doc-uploads",
            storage_path=f"{owner_id}/{uuid.uuid4()}/{filename}",
            status=status,
        )
        session.add(doc)
        await session.commit()
        if created_offset_seconds:
            # Backdate created_at so we get deterministic ordering
            await session.execute(
                Document.__table__.update()
                .where(Document.id == doc.id)
                .values(created_at=datetime.now(UTC) - timedelta(seconds=created_offset_seconds))
            )
            await session.commit()
        return str(doc.id)


def test_list_requires_auth() -> None:
    r = client.get("/api/v1/documents")
    assert r.status_code == 401, r.text


def test_list_empty_for_new_user() -> None:
    c = _new_client()
    _register(c, f"empty-{uuid.uuid4().hex[:8]}@x.com")
    r = c.get("/api/v1/documents")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"items": [], "total": 0, "limit": 50, "offset": 0}


def test_list_returns_only_own_documents() -> None:
    """User A lists → sees only their doc. Then we log in as B and confirm
    B sees only theirs. Two separate TestClients keeps the cookie jars
    distinct."""
    client_a = _new_client()
    client_b = _new_client()
    cookie_a = _register(client_a, f"a-{uuid.uuid4().hex[:8]}@x.com")
    cookie_b = _register(client_b, f"b-{uuid.uuid4().hex[:8]}@x.com")
    owner_a = _decode_sub(cookie_a)
    owner_b = _decode_sub(cookie_b)

    a_doc_id = asyncio.run(_seed(owner_a, title="A's doc"))
    b_doc_id = asyncio.run(_seed(owner_b, title="B's doc"))

    # User A's list: only their own doc, total=1
    r = client_a.get("/api/v1/documents")
    body = r.json()
    ids = [item["id"] for item in body["items"]]
    assert a_doc_id in ids
    assert b_doc_id not in ids
    assert body["total"] == 1

    # User B's list: only their own doc, total=1
    r = client_b.get("/api/v1/documents")
    body = r.json()
    ids = [item["id"] for item in body["items"]]
    assert b_doc_id in ids
    assert a_doc_id not in ids
    assert body["total"] == 1


def test_list_orders_newest_first() -> None:
    c = _new_client()
    cookie = _register(c, f"order-{uuid.uuid4().hex[:8]}@x.com")
    owner = _decode_sub(cookie)

    # Create one "old" and one "new" doc. Newer should come first.
    asyncio.run(_seed(owner, title="old", created_offset_seconds=600))
    asyncio.run(_seed(owner, title="new", created_offset_seconds=0))

    r = c.get("/api/v1/documents")
    body = r.json()
    titles = [item["title"] for item in body["items"]]
    assert titles == ["new", "old"], f"expected newest-first, got {titles}"


def test_list_status_filter() -> None:
    c = _new_client()
    cookie = _register(c, f"flt-{uuid.uuid4().hex[:8]}@x.com")
    owner = _decode_sub(cookie)

    # 2 READY, 1 FAILED, 1 PROCESSING
    asyncio.run(_seed(owner, status=DocumentStatus.ready, title="r1"))
    asyncio.run(_seed(owner, status=DocumentStatus.ready, title="r2"))
    asyncio.run(_seed(owner, status=DocumentStatus.failed, title="f1", filename="f.pdf"))
    asyncio.run(_seed(owner, status=DocumentStatus.processing, title="p1", filename="p.pdf"))

    # No filter: all 4
    r = c.get("/api/v1/documents")
    assert r.json()["total"] == 4

    # status=ready: 2
    r = c.get("/api/v1/documents?status=ready")
    body = r.json()
    assert body["total"] == 2
    assert all(item["status"] == "ready" for item in body["items"])

    # status=failed: 1, error_message is included
    r = c.get("/api/v1/documents?status=failed")
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "f1"
    # Schema allows error_message to be null even on FAILED rows;
    # just assert the field is present.
    assert "error_message" in body["items"][0]

    # status=processing: 1
    r = c.get("/api/v1/documents?status=processing")
    assert r.json()["total"] == 1

    # status=uploaded: 0
    r = c.get("/api/v1/documents?status=uploaded")
    body = r.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_list_pagination() -> None:
    c = _new_client()
    cookie = _register(c, f"pg-{uuid.uuid4().hex[:8]}@x.com")
    owner = _decode_sub(cookie)

    # 7 docs, all READY
    for i in range(7):
        asyncio.run(_seed(owner, title=f"doc-{i}"))

    # Page 1: limit=3, offset=0
    r = c.get("/api/v1/documents?limit=3&offset=0")
    body = r.json()
    assert body["total"] == 7
    assert body["limit"] == 3
    assert body["offset"] == 0
    assert len(body["items"]) == 3

    # Page 2: limit=3, offset=3
    r = c.get("/api/v1/documents?limit=3&offset=3")
    body = r.json()
    assert body["total"] == 7
    assert body["offset"] == 3
    assert len(body["items"]) == 3

    # Page 3: only 1 left
    r = c.get("/api/v1/documents?limit=3&offset=6")
    body = r.json()
    assert body["total"] == 7
    assert len(body["items"]) == 1

    # Past the end: empty items, total still 7
    r = c.get("/api/v1/documents?limit=3&offset=100")
    body = r.json()
    assert body["total"] == 7
    assert body["items"] == []


def test_list_limit_too_high_rejected() -> None:
    c = _new_client()
    _register(c, f"hi-{uuid.uuid4().hex[:8]}@x.com")
    r = c.get("/api/v1/documents?limit=500")
    assert r.status_code == 422, r.text


def test_list_limit_too_low_rejected() -> None:
    c = _new_client()
    _register(c, f"lo-{uuid.uuid4().hex[:8]}@x.com")
    r = c.get("/api/v1/documents?limit=0")
    assert r.status_code == 422, r.text


def test_list_response_shape() -> None:
    """Verify the exact field set we expose to the client."""
    c = _new_client()
    cookie = _register(c, f"shape-{uuid.uuid4().hex[:8]}@x.com")
    owner = _decode_sub(cookie)
    asyncio.run(_seed(owner, status=DocumentStatus.ready, title="shape-check"))

    r = c.get("/api/v1/documents")
    body = r.json()
    assert body["total"] == 1
    item = body["items"][0]
    # Expected fields
    expected = {
        "id",
        "title",
        "filename",
        "mime_type",
        "file_size_bytes",
        "status",
        "error_message",
        "uploaded_at",
        "created_at",
        "updated_at",
    }
    assert set(item.keys()) == expected, f"unexpected fields: {set(item.keys()) ^ expected}"
    # Internal fields must NOT leak
    for forbidden in ("storage_bucket", "storage_path", "owner_id", "file_hash"):
        assert forbidden not in item, f"leaked internal field: {forbidden}"


if __name__ == "__main__":
    test_list_requires_auth()
    print("OK: list requires auth (401)")
    test_list_empty_for_new_user()
    print("OK: empty list for new user")
    test_list_returns_only_own_documents()
    print("OK: only own documents returned (no cross-user leak)")
    test_list_orders_newest_first()
    print("OK: ordering is newest-first")
    test_list_status_filter()
    print("OK: ?status= filter works, total respects filter")
    test_list_pagination()
    print("OK: pagination via limit/offset works")
    test_list_limit_too_high_rejected()
    print("OK: limit > 100 rejected (422)")
    test_list_limit_too_low_rejected()
    print("OK: limit < 1 rejected (422)")
    test_list_response_shape()
    print("OK: response shape matches spec, no internal fields leaked")
    print("\nALL LIST-ENDPOINT TESTS PASSED")
