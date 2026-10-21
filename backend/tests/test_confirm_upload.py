"""Smoke test for the /documents/{id}/confirm endpoint.

We manually INSERT a document row (bypassing the upload-intent flow
because Supabase isn't configured here), then test confirm.

What we verify:
1. Confirm without auth -> 401
2. Confirm with another user's document -> 404 (no info leak)
3. Confirm a valid PROCESSING document -> 200 + status=UPLOADED
4. Double-confirm the same document -> 200 (idempotent)
5. Confirm non-existent UUID -> 404
"""

from __future__ import annotations

import asyncio
import os
import uuid

os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = ""

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_engine, get_session_factory
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
    # base64url padding
    payload_b64 += "=" * (4 - len(payload_b64) % 4)
    payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
    return payload["sub"]


async def _seed_document(
    owner_id_str: str, status: DocumentStatus = DocumentStatus.processing
) -> str:
    """Insert a document row directly and return its id."""
    from uuid import UUID

    factory = get_session_factory()
    async with factory() as session:  # type: AsyncSession
        doc = Document(
            id=uuid.uuid4(),
            owner_id=UUID(owner_id_str),
            title="Seed doc",
            filename="seed.pdf",
            mime_type="application/pdf",
            file_size_bytes=1024,
            storage_bucket="doc-uploads",
            storage_path=f"{owner_id_str}/{uuid.uuid4()}/seed.pdf",
            status=status,
        )
        session.add(doc)
        await session.commit()
        return str(doc.id)


def test_confirm_requires_auth() -> None:
    r = client.post(f"/api/v1/documents/{uuid.uuid4()}/confirm")
    assert r.status_code == 401, r.text


def test_confirm_nonexistent_returns_404() -> None:
    _register(f"u1-{uuid.uuid4().hex[:8]}@x.com")
    r = client.post(f"/api/v1/documents/{uuid.uuid4()}/confirm")
    assert r.status_code == 404, r.text


def test_confirm_other_users_doc_returns_404() -> None:
    """A user must not be able to confirm someone else's document."""
    # User A
    cookie_a = _register(f"ua-{uuid.uuid4().hex[:8]}@x.com")
    user_a = _get_user_id_from_cookie(cookie_a)

    # User B
    _register(f"ub-{uuid.uuid4().hex[:8]}@x.com")

    # A seeds a document
    doc_id = asyncio.run(_seed_document(user_a))

    # B tries to confirm A's document
    r = client.post(f"/api/v1/documents/{doc_id}/confirm")
    assert (
        r.status_code == 404
    ), f"expected 404 when confirming another user's doc, got {r.status_code}: {r.text}"


def test_confirm_happy_path() -> None:
    cookie = _register(f"uc-{uuid.uuid4().hex[:8]}@x.com")
    owner = _get_user_id_from_cookie(cookie)
    doc_id = asyncio.run(_seed_document(owner))

    r = client.post(f"/api/v1/documents/{doc_id}/confirm")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["document_id"] == doc_id
    assert body["status"] == "uploaded"
    assert body["uploaded_at"] is not None


def test_confirm_is_idempotent() -> None:
    cookie = _register(f"ud-{uuid.uuid4().hex[:8]}@x.com")
    owner = _get_user_id_from_cookie(cookie)
    doc_id = asyncio.run(_seed_document(owner))

    # First confirm
    r1 = client.post(f"/api/v1/documents/{doc_id}/confirm")
    assert r1.status_code == 200

    # Second confirm — idempotent, still 200
    r2 = client.post(f"/api/v1/documents/{doc_id}/confirm")
    assert r2.status_code == 200, r2.text


def test_confirm_ready_doc_returns_409() -> None:
    """A document already in READY shouldn't be re-confirmable."""
    cookie = _register(f"ue-{uuid.uuid4().hex[:8]}@x.com")
    owner = _get_user_id_from_cookie(cookie)
    doc_id = asyncio.run(_seed_document(owner, status=DocumentStatus.ready))

    r = client.post(f"/api/v1/documents/{doc_id}/confirm")
    assert r.status_code == 409, r.text


if __name__ == "__main__":
    test_confirm_requires_auth()
    print("OK: confirm requires auth (401)")
    test_confirm_nonexistent_returns_404()
    print("OK: confirm nonexistent doc returns 404")
    test_confirm_other_users_doc_returns_404()
    print("OK: confirm another user's doc returns 404 (no leak)")
    test_confirm_happy_path()
    print("OK: confirm happy path returns 200 / uploaded")
    test_confirm_is_idempotent()
    print("OK: double-confirm is idempotent")
    test_confirm_ready_doc_returns_409()
    print("OK: confirm on READY doc returns 409")
    print("\nALL CONFIRM TESTS PASSED")
