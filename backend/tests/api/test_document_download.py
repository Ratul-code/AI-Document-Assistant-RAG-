"""End-to-end tests for GET /api/v1/documents/{id}/download and /view.

What we verify:
  1. 401 without auth (both endpoints).
  2. 404 for a non-existent UUID (both endpoints) — no info leak.
  3. 404 for another user's document (both endpoints) — no info leak.
  4. /download returns 200 with `Content-Disposition: attachment` and
     the sanitised original filename.
  5. /view returns 200 with `Content-Disposition: inline` and the
     original mime type.
  6. /download returns 409 for a doc in `processing` or `failed`.
  7. /view returns 409 for a doc in `processing` or `failed`.
  8. The storage download is called with the right (bucket, path) so
     the route reads the file the row actually points at.
  9. Filename sanitisation runs server-side (CRLF / quote stripping).

The Supabase storage client is a module-level singleton accessed via
`get_supabase_storage()`. We monkeypatch that function on the
endpoints module to return a fake whose `download_file` records its
arguments and returns canned bytes — no real network call ever
happens.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from uuid import UUID

os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "")

from fastapi.testclient import TestClient

from app.api.v1.endpoints import documents as documents_endpoint
from app.core.config import get_settings
from app.db.session import get_session_factory
from app.main import create_app
from app.models.document import Document, DocumentStatus


client = TestClient(create_app())


def _new_client() -> TestClient:
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Supabase storage fake
# ---------------------------------------------------------------------------


@dataclass
class FakeStorage:
    """In-memory stand-in for the Supabase storage client.

    `download_file` records its arguments and returns whatever bytes
    were queued for the matching (bucket, path). Tests can inspect
    `.calls` to confirm the route read the right object.
    """

    responses: dict[tuple[str, str], bytes] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    raise_on_download: Exception | None = None

    def download_file(self, bucket: str, storage_path: str) -> bytes:
        self.calls.append((bucket, storage_path))
        if self.raise_on_download is not None:
            raise self.raise_on_download
        return self.responses[(bucket, storage_path)]


def _patch_storage(monkeypatch, fake: FakeStorage) -> None:
    """Swap the endpoint's `get_supabase_storage` for our fake.

    The endpoints module imports the factory at module top-level, so
    we patch the bound name inside the endpoint module specifically —
    monkeypatching the underlying services module would leave the
    endpoint's import unchanged.
    """
    monkeypatch.setattr(
        documents_endpoint,
        "get_supabase_storage",
        lambda: fake,
    )


# ---------------------------------------------------------------------------
# Auth + seeding helpers
# ---------------------------------------------------------------------------


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


async def _seed_doc(
    owner_id: str,
    *,
    status: DocumentStatus = DocumentStatus.ready,
    filename: str = "report.pdf",
    mime_type: str = "application/pdf",
    bucket: str = "doc-uploads",
) -> str:
    factory = get_session_factory()
    doc_id = uuid.uuid4()
    async with factory() as session:  # type: ignore[arg-type]
        doc = Document(
            id=doc_id,
            owner_id=UUID(owner_id),
            title="Download Test",
            filename=filename,
            mime_type=mime_type,
            file_size_bytes=42,
            storage_bucket=bucket,
            storage_path=f"{owner_id}/{doc_id}",
            status=status,
        )
        session.add(doc)
        await session.commit()
        return str(doc_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_download_requires_auth() -> None:
    r = client.get(f"/api/v1/documents/{uuid.uuid4()}/download")
    assert r.status_code == 401, r.text


def test_view_requires_auth() -> None:
    r = client.get(f"/api/v1/documents/{uuid.uuid4()}/view")
    assert r.status_code == 401, r.text


def test_download_nonexistent_returns_404(monkeypatch) -> None:
    _patch_storage(monkeypatch, FakeStorage())
    c = _new_client()
    _register(c, f"ne-{uuid.uuid4().hex[:8]}@x.com")
    r = c.get(f"/api/v1/documents/{uuid.uuid4()}/download")
    assert r.status_code == 404, r.text


def test_view_nonexistent_returns_404(monkeypatch) -> None:
    _patch_storage(monkeypatch, FakeStorage())
    c = _new_client()
    _register(c, f"ne2-{uuid.uuid4().hex[:8]}@x.com")
    r = c.get(f"/api/v1/documents/{uuid.uuid4()}/view")
    assert r.status_code == 404, r.text


def test_download_other_users_doc_returns_404(monkeypatch) -> None:
    """User B must not be able to download user A's document. 404, not
    403, so we don't leak the existence of A's document."""
    fake = FakeStorage()
    _patch_storage(monkeypatch, fake)
    c_a = _new_client()
    c_b = _new_client()
    _register(c_a, f"a-{uuid.uuid4().hex[:8]}@x.com")
    cookie_b = _register(c_b, f"b-{uuid.uuid4().hex[:8]}@x.com")
    owner_b = _decode_sub(cookie_b)

    doc_id = asyncio.run(_seed_doc(owner_b))
    r = c_a.get(f"/api/v1/documents/{doc_id}/download")
    assert r.status_code == 404, (
        f"expected 404 for cross-owner download, got {r.status_code}: {r.text}"
    )
    # Critical: storage was never queried — the auth check happens
    # before the storage call.
    assert fake.calls == [], (
        f"storage was called for a cross-owner request: {fake.calls}"
    )


def test_view_other_users_doc_returns_404(monkeypatch) -> None:
    fake = FakeStorage()
    _patch_storage(monkeypatch, fake)
    c_a = _new_client()
    c_b = _new_client()
    _register(c_a, f"a2-{uuid.uuid4().hex[:8]}@x.com")
    cookie_b = _register(c_b, f"b2-{uuid.uuid4().hex[:8]}@x.com")
    owner_b = _decode_sub(cookie_b)

    doc_id = asyncio.run(_seed_doc(owner_b))
    r = c_a.get(f"/api/v1/documents/{doc_id}/view")
    assert r.status_code == 404, r.text
    assert fake.calls == [], f"storage was called cross-owner: {fake.calls}"


def test_download_happy_path_returns_attachment(monkeypatch) -> None:
    """A READY doc owned by the caller streams its bytes with
    `Content-Disposition: attachment` and the original filename."""
    fake = FakeStorage()
    _patch_storage(monkeypatch, fake)
    c = _new_client()
    cookie = _register(c, f"dl-{uuid.uuid4().hex[:8]}@x.com")
    owner = _decode_sub(cookie)
    doc_id = asyncio.run(_seed_doc(owner, filename="quarterly report.pdf"))
    storage_path = f"{owner}/{doc_id}"
    payload = b"%PDF-1.4\n% fake bytes"
    fake.responses[("doc-uploads", storage_path)] = payload

    r = c.get(f"/api/v1/documents/{doc_id}/download")
    assert r.status_code == 200, r.text
    assert r.content == payload
    assert r.headers["content-type"].startswith("application/pdf")
    # The header carries the original filename in quotes (single header
    # for simple ASCII; browsers handle RFC 5987 only for non-ASCII).
    assert r.headers["content-disposition"] == (
        f'attachment; filename="quarterly report.pdf"'
    )
    # Storage was hit with the doc's (bucket, path).
    assert fake.calls == [("doc-uploads", storage_path)]


def test_view_happy_path_returns_inline(monkeypatch) -> None:
    """A READY doc owned by the caller streams its bytes with
    `Content-Disposition: inline` and the original mime type."""
    fake = FakeStorage()
    _patch_storage(monkeypatch, fake)
    c = _new_client()
    cookie = _register(c, f"v-{uuid.uuid4().hex[:8]}@x.com")
    owner = _decode_sub(cookie)
    doc_id = asyncio.run(_seed_doc(owner, mime_type="text/plain"))
    storage_path = f"{owner}/{doc_id}"
    payload = b"Hello, world.\n"
    fake.responses[("doc-uploads", storage_path)] = payload

    r = c.get(f"/api/v1/documents/{doc_id}/view")
    assert r.status_code == 200, r.text
    assert r.content == payload
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["content-disposition"] == (
        f'inline; filename="report.pdf"'
    )
    assert fake.calls == [("doc-uploads", storage_path)]


def test_download_processing_returns_409(monkeypatch) -> None:
    """A doc still in `processing` (the PUT hasn't landed) has no
    file in storage to serve. 409, not 200 with a 404-from-storage."""
    fake = FakeStorage()
    _patch_storage(monkeypatch, fake)
    c = _new_client()
    cookie = _register(c, f"p-{uuid.uuid4().hex[:8]}@x.com")
    owner = _decode_sub(cookie)
    doc_id = asyncio.run(_seed_doc(owner, status=DocumentStatus.processing))

    r = c.get(f"/api/v1/documents/{doc_id}/download")
    assert r.status_code == 409, r.text
    assert "processing" in r.json()["detail"].lower()
    # 409 fires before the storage call — we never ask Supabase for a
    # file we know doesn't exist yet.
    assert fake.calls == []


def test_view_failed_returns_409(monkeypatch) -> None:
    fake = FakeStorage()
    _patch_storage(monkeypatch, fake)
    c = _new_client()
    cookie = _register(c, f"f-{uuid.uuid4().hex[:8]}@x.com")
    owner = _decode_sub(cookie)
    doc_id = asyncio.run(_seed_doc(owner, status=DocumentStatus.failed))

    r = c.get(f"/api/v1/documents/{doc_id}/view")
    assert r.status_code == 409, r.text
    assert fake.calls == []


def test_download_learning_status_succeeds(monkeypatch) -> None:
    """`learning` (embeddings in progress) is still a valid time to
    download the original file — the file is fully present, only the
    embeddings are still being computed."""
    fake = FakeStorage()
    _patch_storage(monkeypatch, fake)
    c = _new_client()
    cookie = _register(c, f"lrn-{uuid.uuid4().hex[:8]}@x.com")
    owner = _decode_sub(cookie)
    doc_id = asyncio.run(_seed_doc(owner, status=DocumentStatus.learning))
    storage_path = f"{owner}/{doc_id}"
    fake.responses[("doc-uploads", storage_path)] = b"learning-doc"

    r = c.get(f"/api/v1/documents/{doc_id}/download")
    assert r.status_code == 200, r.text
    assert r.content == b"learning-doc"


def test_download_filename_sanitised(monkeypatch) -> None:
    """CRLF and double-quote in the filename must be neutralised so
    they can't inject additional Content-Disposition headers.

    The defence is structural: any character that can break out of a
    quoted-string (CR, LF, ", ;) is replaced with `_`. The rest of
    the name is preserved as-is so the user can still recognise the
    file when it lands on disk. We assert (a) no CRLF in the header
    at all, (b) no bare `"` inside the filename value (it would
    close the quoted-string early), and (c) the route produced 200.
    """
    fake = FakeStorage()
    _patch_storage(monkeypatch, fake)
    c = _new_client()
    cookie = _register(c, f"san-{uuid.uuid4().hex[:8]}@x.com")
    owner = _decode_sub(cookie)
    # \r\n + a stray " — would be a header-injection vector if passed
    # through unsanitised. The route must replace these with `_`.
    evil_name = 'evil"\r\nX-Injected: yes.txt'
    doc_id = asyncio.run(_seed_doc(owner, filename=evil_name))
    storage_path = f"{owner}/{doc_id}"
    fake.responses[("doc-uploads", storage_path)] = b"x"

    r = c.get(f"/api/v1/documents/{doc_id}/download")
    assert r.status_code == 200, r.text
    cd = r.headers["content-disposition"]
    # (a) No CRLF anywhere in the header.
    assert "\r" not in cd and "\n" not in cd, (
        f"CRLF leaked into Content-Disposition: {cd!r}"
    )
    # (b) The unescaped `"` (which would close the quoted-string) is
    # gone — the header has exactly ONE `"` pair surrounding the value.
    # Split off the `attachment; filename="` prefix and the trailing `"`
    # to inspect only the value.
    prefix = 'attachment; filename="'
    assert cd.startswith(prefix)
    assert cd.endswith('"')
    value = cd[len(prefix):-1]
    assert '"' not in value, (
        f'unescaped quote inside filename value: {value!r}'
    )


def test_download_storage_failure_returns_502(monkeypatch) -> None:
    """A storage-layer exception (missing object, network, auth) is
    translated to 502 with a generic message — no Supabase internals
    leak to the client."""
    fake = FakeStorage(raise_on_download=RuntimeError("supabase down"))
    _patch_storage(monkeypatch, fake)
    c = _new_client()
    cookie = _register(c, f"sf-{uuid.uuid4().hex[:8]}@x.com")
    owner = _decode_sub(cookie)
    doc_id = asyncio.run(_seed_doc(owner))

    r = c.get(f"/api/v1/documents/{doc_id}/download")
    assert r.status_code == 502, r.text
    # Generic message — the supabase internals must not appear.
    assert "supabase" not in r.json()["detail"].lower()
