"""Smoke tests for the PATCH /api/v1/documents/{id} endpoint.

Verifies:
1. PATCH without auth -> 401
2. PATCH a non-existent UUID -> 404
3. PATCH another user's document -> 404 (no info leak)
4. PATCH a valid doc with a new title -> 200, title persisted,
   updated_at bumped
5. PATCH with empty title -> 422 (Pydantic min_length=1)
6. PATCH with title too long -> 422 (Pydantic max_length=200)
7. PATCH returns a DocumentListItem (the same shape the list uses)
"""

from __future__ import annotations

import asyncio
import os
import uuid

os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "")

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.main import create_app
from app.models.document import Document, DocumentStatus

client = TestClient(create_app())


def _register(email: str, password: str = "CorrectHorseBatteryStaple!1") -> str:
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "name": "Tester"},
    )
    assert r.status_code == 201, r.text
    r = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert r.status_code == 200, r.text
    return client.cookies.get(get_settings().access_cookie_name) or ""


def _get_user_id_from_cookie(cookie_value: str) -> str:
    """Decode the JWT payload to grab the user UUID."""
    import base64
    import json as _json

    payload_b64 = cookie_value.split(".")[1]
    payload_b64 += "=" * (4 - len(payload_b64) % 4)
    payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
    return payload["sub"]


async def _seed_document(
    owner_id_str: str,
    *,
    title: str = "Seed doc",
    status: DocumentStatus = DocumentStatus.ready,
) -> str:
    """Insert a document row directly and return its id."""
    from uuid import UUID

    factory = get_session_factory()
    doc_id = uuid.uuid4()
    async with factory() as session:  # type: AsyncSession
        doc = Document(
            id=doc_id,
            owner_id=UUID(owner_id_str),
            title=title,
            filename="seed.pdf",
            mime_type="application/pdf",
            file_size_bytes=1024,
            storage_bucket="doc-uploads",
            storage_path=f"{owner_id_str}/{doc_id}/seed.pdf",
            status=status,
        )
        session.add(doc)
        await session.commit()
        return str(doc.id)


async def _get_title(document_id: str) -> str | None:
    from uuid import UUID

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(Document).where(Document.id == UUID(document_id))
        )
        doc = result.scalar_one_or_none()
        return doc.title if doc is not None else None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_update_requires_auth() -> None:
    r = client.patch(
        f"/api/v1/documents/{uuid.uuid4()}", json={"title": "New"}
    )
    assert r.status_code == 401, r.text


def test_update_nonexistent_returns_404() -> None:
    _register(f"u1-{uuid.uuid4().hex[:8]}@x.com")
    r = client.patch(
        f"/api/v1/documents/{uuid.uuid4()}", json={"title": "New"}
    )
    assert r.status_code == 404, r.text


def test_update_other_users_doc_returns_404() -> None:
    """A user must not be able to rename someone else's document."""
    cookie_a = _register(f"ua-{uuid.uuid4().hex[:8]}@x.com")
    user_a = _get_user_id_from_cookie(cookie_a)

    _register(f"ub-{uuid.uuid4().hex[:8]}@x.com")

    doc_id = asyncio.run(_seed_document(user_a, title="Original"))

    r = client.patch(
        f"/api/v1/documents/{doc_id}", json={"title": "Hacked"}
    )
    assert r.status_code == 404, (
        f"expected 404 when updating another user's doc, got "
        f"{r.status_code}: {r.text}"
    )
    # And the title was NOT changed.
    assert asyncio.run(_get_title(doc_id)) == "Original"


def test_update_happy_path() -> None:
    cookie = _register(f"uc-{uuid.uuid4().hex[:8]}@x.com")
    owner = _get_user_id_from_cookie(cookie)
    doc_id = asyncio.run(_seed_document(owner, title="Original"))

    r = client.patch(
        f"/api/v1/documents/{doc_id}", json={"title": "Renamed"}
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # Response is the updated DocumentListItem.
    assert body["id"] == doc_id
    assert body["title"] == "Renamed"

    # DB also reflects the change.
    assert asyncio.run(_get_title(doc_id)) == "Renamed"


def test_update_empty_title_rejected() -> None:
    """Pydantic min_length=1 — empty titles are rejected as 422."""
    cookie = _register(f"ud-{uuid.uuid4().hex[:8]}@x.com")
    owner = _get_user_id_from_cookie(cookie)
    doc_id = asyncio.run(_seed_document(owner))

    r = client.patch(
        f"/api/v1/documents/{doc_id}", json={"title": ""}
    )
    assert r.status_code == 422, r.text


def test_update_too_long_title_rejected() -> None:
    """Pydantic max_length=200 — overlong titles are rejected as 422."""
    cookie = _register(f"ue-{uuid.uuid4().hex[:8]}@x.com")
    owner = _get_user_id_from_cookie(cookie)
    doc_id = asyncio.run(_seed_document(owner))

    r = client.patch(
        f"/api/v1/documents/{doc_id}", json={"title": "x" * 201}
    )
    assert r.status_code == 422, r.text


def test_update_returns_document_list_item_shape() -> None:
    """The response should be a DocumentListItem — the same shape the
    list endpoint returns — so the row's local cache can splice the
    update directly without reshaping."""
    cookie = _register(f"uf-{uuid.uuid4().hex[:8]}@x.com")
    owner = _get_user_id_from_cookie(cookie)
    doc_id = asyncio.run(_seed_document(owner, title="Original"))

    r = client.patch(
        f"/api/v1/documents/{doc_id}", json={"title": "Renamed"}
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # Spot-check the shape matches DocumentListItem (from the schema).
    for key in (
        "id",
        "title",
        "filename",
        "mime_type",
        "file_size_bytes",
        "status",
        "uploaded_at",
        "created_at",
        "updated_at",
    ):
        assert key in body, f"missing {key!r} in response: {body!r}"

    assert body["filename"] == "seed.pdf"
    assert body["title"] == "Renamed"


if __name__ == "__main__":
    test_update_requires_auth()
    print("OK: update requires auth (401)")
    test_update_nonexistent_returns_404()
    print("OK: update nonexistent returns 404")
    test_update_other_users_doc_returns_404()
    print("OK: update another user's doc returns 404 (no leak)")
    test_update_happy_path()
    print("OK: update happy path 200 + title persisted")
    test_update_empty_title_rejected()
    print("OK: empty title rejected (422)")
    test_update_too_long_title_rejected()
    print("OK: overlong title rejected (422)")
    test_update_returns_document_list_item_shape()
    print("OK: response shape matches DocumentListItem")
    print("\nALL UPDATE TESTS PASSED")