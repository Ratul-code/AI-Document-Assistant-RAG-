"""End-to-end smoke tests for GET /api/v1/documents/{id}/stats.

What we verify:
  1. 401 without auth.
  2. 404 for a random UUID (no info leak).
  3. 404 for another user's document (no info leak).
  4. 200 with a zero-stats payload for a brand-new document (no
     pages yet) — the most common case in the UI before the worker
     has caught up.
  5. 200 with the right histogram + distinct model list for a doc
     with mixed embedding statuses.
  6. The response carries the same `DocumentListItem` shape the list
     endpoint returns, so the dialog can render its header without
     a second round-trip.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from uuid import UUID

os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "")

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.main import create_app
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.models.document_embedding import DocumentEmbedding, EmbeddingStatus
from app.models.document_page import DocumentPage, OcrStatus, TextSource

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


async def _seed_doc(owner_id: str, *, status=DocumentStatus.ready) -> str:
    factory = get_session_factory()
    doc_id = uuid.uuid4()
    async with factory() as session:  # type: AsyncSession
        doc = Document(
            id=doc_id,
            owner_id=UUID(owner_id),
            title="Stats Test",
            filename="f.pdf",
            mime_type="application/pdf",
            file_size_bytes=2048,
            storage_bucket="b",
            storage_path=f"{owner_id}/{doc_id}/f.pdf",
            status=status,
        )
        session.add(doc)
        await session.commit()
        return str(doc_id)


async def _seed_page_chunk_embedding(
    doc_id: str,
    *,
    page_number: int = 1,
    chunk_index: int = 0,
    ocr_status: OcrStatus = OcrStatus.not_needed,
    embedding_status: EmbeddingStatus | None = EmbeddingStatus.succeeded,
    model_name: str = "text-embedding-3-small",
) -> None:
    """Insert one page + one chunk + (optionally) one embedding row."""
    factory = get_session_factory()
    page_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    async with factory() as session:  # type: AsyncSession
        session.add(
            DocumentPage(
                id=page_id,
                document_id=UUID(doc_id),
                page_number=page_number,
                raw_text="x",
                final_text="x",
                text_source=TextSource.raw,
                ocr_status=ocr_status,
                char_count=1,
            )
        )
        await session.flush()
        session.add(
            DocumentChunk(
                id=chunk_id,
                page_id=page_id,
                chunk_index=chunk_index,
                text="x",
                token_count=1,
                char_count=1,
                needs_embedding=(embedding_status != EmbeddingStatus.succeeded),
            )
        )
        if embedding_status is not None:
            session.add(
                DocumentEmbedding(
                    id=uuid.uuid4(),
                    chunk_id=chunk_id,
                    model_name=model_name,
                    dim=4,
                    vector=[0.1, 0.2, 0.3, 0.4],
                    status=embedding_status,
                )
            )
        await session.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_stats_requires_auth() -> None:
    r = client.get(f"/api/v1/documents/{uuid.uuid4()}/stats")
    assert r.status_code == 401, r.text


def test_stats_nonexistent_returns_404() -> None:
    c = _new_client()
    _register(c, f"ne-{uuid.uuid4().hex[:8]}@x.com")
    r = c.get(f"/api/v1/documents/{uuid.uuid4()}/stats")
    assert r.status_code == 404, r.text


def test_stats_other_users_doc_returns_404() -> None:
    """User B must not be able to read user A's doc stats. 404, not
    403, so we don't leak the existence of A's document."""
    c_a = _new_client()
    c_b = _new_client()
    _register(c_a, f"a-{uuid.uuid4().hex[:8]}@x.com")
    cookie_b = _register(c_b, f"b-{uuid.uuid4().hex[:8]}@x.com")
    owner_b = _decode_sub(cookie_b)

    # B owns a doc; A asks for its stats.
    doc_id = asyncio.run(_seed_doc(owner_b))

    r = c_a.get(f"/api/v1/documents/{doc_id}/stats")
    assert r.status_code == 404, (
        f"expected 404 for cross-owner stats read, got {r.status_code}: {r.text}"
    )


def test_stats_empty_doc_returns_zeros() -> None:
    """Brand-new doc with no pages → all counts zero, empty models list.

    This is the common case for a doc in PROCESSING / UPLOADED state
    before the worker has had a chance to run. The route must return
    200 (not 404) and a zero struct so the dialog renders an
    informational 'Not parsed yet' state.
    """
    c = _new_client()
    cookie = _register(c, f"empty-{uuid.uuid4().hex[:8]}@x.com")
    owner = _decode_sub(cookie)
    doc_id = asyncio.run(_seed_doc(owner, status=DocumentStatus.processing))

    r = c.get(f"/api/v1/documents/{doc_id}/stats")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["page_count"] == 0
    assert body["chunks_total"] == 0
    assert body["chunks_embedded"] == 0
    assert body["chunks_pending"] == 0
    assert body["chunks_failed"] == 0
    assert body["pages_needing_ocr"] == 0
    assert body["pages_ocr_pending"] == 0
    assert body["pages_ocr_failed"] == 0
    assert body["embedding_models"] == []
    assert body["embedding_status_counts"] == {}

    # The header carries the same DocumentListItem shape.
    assert body["document"]["id"] == doc_id
    assert body["document"]["title"] == "Stats Test"
    assert body["document"]["status"] == "processing"


def test_stats_happy_path_mixed_statuses() -> None:
    """Doc with 3 pages, 3 chunks: 1 succeeded, 1 failed, 1 still pending
    (no embedding row). Embedding model = 'text-embedding-3-small'.
    """
    c = _new_client()
    cookie = _register(c, f"hp-{uuid.uuid4().hex[:8]}@x.com")
    owner = _decode_sub(cookie)
    doc_id = asyncio.run(_seed_doc(owner))

    # Page 1: 1 chunk, succeeded
    asyncio.run(
        _seed_page_chunk_embedding(
            doc_id,
            page_number=1,
            chunk_index=0,
            embedding_status=EmbeddingStatus.succeeded,
            model_name="text-embedding-3-small",
        )
    )
    # Page 2: 1 chunk, failed
    asyncio.run(
        _seed_page_chunk_embedding(
            doc_id,
            page_number=2,
            chunk_index=0,
            embedding_status=EmbeddingStatus.failed,
        )
    )
    # Page 3: 1 chunk, no embedding row yet (still pending)
    asyncio.run(
        _seed_page_chunk_embedding(
            doc_id,
            page_number=3,
            chunk_index=0,
            embedding_status=None,
        )
    )

    r = c.get(f"/api/v1/documents/{doc_id}/stats")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["page_count"] == 3
    assert body["chunks_total"] == 3
    assert body["chunks_embedded"] == 1
    assert body["chunks_failed"] == 1
    assert body["chunks_pending"] == 1  # 3 - 1 - 1
    assert body["embedding_models"] == ["text-embedding-3-small"]
    assert body["embedding_status_counts"] == {"succeeded": 1, "failed": 1}


def test_stats_response_includes_document_list_item() -> None:
    """The `document` field of the response must be a `DocumentListItem`
    so the dialog can render its header in one round-trip. We assert
    the field set exactly."""
    c = _new_client()
    cookie = _register(c, f"hdr-{uuid.uuid4().hex[:8]}@x.com")
    owner = _decode_sub(cookie)
    doc_id = asyncio.run(_seed_doc(owner))

    r = c.get(f"/api/v1/documents/{doc_id}/stats")
    body = r.json()
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
    assert set(body["document"].keys()) == expected, (
        f"unexpected document fields: "
        f"{set(body['document'].keys()) ^ expected}"
    )


if __name__ == "__main__":
    test_stats_requires_auth()
    print("OK: stats requires auth (401)")
    test_stats_nonexistent_returns_404()
    print("OK: stats nonexistent returns 404")
    test_stats_other_users_doc_returns_404()
    print("OK: stats cross-owner returns 404 (no leak)")
    test_stats_empty_doc_returns_zeros()
    print("OK: stats empty doc returns 200 + zeros")
    test_stats_happy_path_mixed_statuses()
    print("OK: stats happy path with mixed statuses")
    test_stats_response_includes_document_list_item()
    print("OK: stats response includes DocumentListItem")
    print("\nALL STATS TESTS PASSED")