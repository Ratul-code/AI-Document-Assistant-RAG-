"""Smoke tests for the DELETE /api/v1/documents/{id} endpoint.

Verifies:
1. DELETE without auth -> 401
2. DELETE a non-existent UUID -> 404
3. DELETE another user's document -> 404 (no info leak)
4. DELETE a valid READY document -> 204; row gone, page/chunk/embedding
   cascade rows gone
5. DELETE the same document twice -> second call 404 (idempotency /
   no info leak on already-deleted)
6. DELETE works in any status (processing / failed / uploaded / ready)
7. DELETE a document also wipes its `document_logs` rows (the activity
   log must not be left with orphan entries after the parent document
   is gone — the global / per-doc activity feed should drop them too)

The DELETE handler relies on FK ON DELETE CASCADE for chunks/pages/
embeddings cleanup. These tests verify the cascade is wired up
correctly on the live SQLite dev DB.
"""

from __future__ import annotations

import asyncio
import os
import uuid

os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "")

from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.db.session import get_engine, get_session_factory
from app.main import create_app
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.models.document_embedding import DocumentEmbedding
from app.models.document_log import DocumentLog
from app.models.document_page import DocumentPage

# SQLite does NOT enforce foreign-key cascades by default. Without
# `PRAGMA foreign_keys=ON`, deleting the `documents` row leaves its
# pages / chunks / embeddings orphaned — even though the migrations
# declare `ON DELETE CASCADE`. The production deployment (Postgres)
# enforces cascades automatically, but the dev SQLite doesn't.
#
# The cascade contract is part of what we're verifying in the
# `test_delete_happy_path_cascades` test below, so we attach an
# event listener that turns on FK enforcement on every new SQLite
# connection for the lifetime of this test module.
if get_settings().effective_database_url.startswith("sqlite"):
    _engine = get_engine()

    @event.listens_for(_engine.sync_engine, "connect")
    def _enable_sqlite_fks(dbapi_conn, _conn_record):  # pragma: no cover
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


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


async def _seed_document_with_cascade(
    owner_id_str: str, status: DocumentStatus = DocumentStatus.ready
) -> tuple[str, str, str, str]:
    """Insert a document plus a page, chunk, and embedding row.

    Returns (document_id, page_id, chunk_id, embedding_id).
    """
    from uuid import UUID

    factory = get_session_factory()
    doc_id = uuid.uuid4()
    page_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    embedding_id = uuid.uuid4()

    async with factory() as session:  # type: AsyncSession
        doc = Document(
            id=doc_id,
            owner_id=UUID(owner_id_str),
            title="Seed doc",
            filename="seed.pdf",
            mime_type="application/pdf",
            file_size_bytes=1024,
            storage_bucket="doc-uploads",
            storage_path=f"{owner_id_str}/{doc_id}/seed.pdf",
            status=status,
        )
        session.add(doc)
        await session.flush()

        page = DocumentPage(
            id=page_id,
            document_id=doc_id,
            page_number=1,
            final_text="hello world",
            text_source="raw",
            ocr_status="not_needed",
            char_count=11,
        )
        session.add(page)
        await session.flush()

        chunk = DocumentChunk(
            id=chunk_id,
            page_id=page_id,
            chunk_index=0,
            section_path=[],
            text="hello world",
            token_count=2,
            char_count=11,
            needs_embedding=False,
        )
        session.add(chunk)
        await session.flush()

        emb = DocumentEmbedding(
            id=embedding_id,
            chunk_id=chunk_id,
            model_name="stub",
            dim=16,
            vector=[0.0] * 16,
            status="succeeded",
        )
        session.add(emb)
        await session.commit()

    return str(doc_id), str(page_id), str(chunk_id), str(embedding_id)


async def _row_exists(model, row_id: str) -> bool:
    from uuid import UUID

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(select(model).where(model.id == UUID(row_id)))
        return result.scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_delete_requires_auth() -> None:
    r = client.delete(f"/api/v1/documents/{uuid.uuid4()}")
    assert r.status_code == 401, r.text


def test_delete_nonexistent_returns_404() -> None:
    _register(f"d1-{uuid.uuid4().hex[:8]}@x.com")
    r = client.delete(f"/api/v1/documents/{uuid.uuid4()}")
    assert r.status_code == 404, r.text


def test_delete_other_users_doc_returns_404() -> None:
    """A user must not be able to delete someone else's document."""
    cookie_a = _register(f"da-{uuid.uuid4().hex[:8]}@x.com")
    user_a = _get_user_id_from_cookie(cookie_a)

    _register(f"db-{uuid.uuid4().hex[:8]}@x.com")

    doc_id = asyncio.run(_seed_document_with_cascade(user_a, DocumentStatus.ready))[0]

    r = client.delete(f"/api/v1/documents/{doc_id}")
    assert r.status_code == 404, (
        f"expected 404 when deleting another user's doc, got " f"{r.status_code}: {r.text}"
    )


def test_delete_happy_path_cascades() -> None:
    """DELETE on a READY doc returns 204, removes the row AND all
    cascaded derived rows (page, chunk, embedding)."""
    cookie = _register(f"dc-{uuid.uuid4().hex[:8]}@x.com")
    owner = _get_user_id_from_cookie(cookie)
    doc_id, page_id, chunk_id, emb_id = asyncio.run(
        _seed_document_with_cascade(owner, DocumentStatus.ready)
    )

    # Sanity: rows exist before delete.
    assert asyncio.run(_row_exists(Document, doc_id))
    assert asyncio.run(_row_exists(DocumentPage, page_id))
    assert asyncio.run(_row_exists(DocumentChunk, chunk_id))
    assert asyncio.run(_row_exists(DocumentEmbedding, emb_id))

    r = client.delete(f"/api/v1/documents/{doc_id}")
    assert r.status_code == 204, r.text
    assert r.content == b"", f"204 must have empty body, got {r.content!r}"

    # After delete: every row is gone.
    assert not asyncio.run(_row_exists(Document, doc_id)), "doc not deleted"
    assert not asyncio.run(_row_exists(DocumentPage, page_id)), "page not cascaded"
    assert not asyncio.run(_row_exists(DocumentChunk, chunk_id)), "chunk not cascaded"
    assert not asyncio.run(_row_exists(DocumentEmbedding, emb_id)), "embedding not cascaded"


def test_delete_is_idempotent_second_returns_404() -> None:
    """Deleting the same doc twice returns 404 on the second call —
    we don't leak existence, and the end state the user cares about
    ('row is gone') is already true."""
    cookie = _register(f"dd-{uuid.uuid4().hex[:8]}@x.com")
    owner = _get_user_id_from_cookie(cookie)
    doc_id, *_ = asyncio.run(_seed_document_with_cascade(owner, DocumentStatus.ready))

    r1 = client.delete(f"/api/v1/documents/{doc_id}")
    assert r1.status_code == 204, r1.text

    r2 = client.delete(f"/api/v1/documents/{doc_id}")
    assert r2.status_code == 404, r2.text


def test_delete_works_in_any_status() -> None:
    """The endpoint must accept DELETE regardless of the doc's status.
    Previously-uploaded, still-processing, failed docs all need to be
    deletable from the UI."""
    for status in (
        DocumentStatus.processing,
        DocumentStatus.uploaded,
        DocumentStatus.learning,
        DocumentStatus.failed,
        DocumentStatus.ready,
    ):
        cookie = _register(f"ds-{status.value}-{uuid.uuid4().hex[:8]}@x.com")
        owner = _get_user_id_from_cookie(cookie)
        doc_id, *_ = asyncio.run(_seed_document_with_cascade(owner, status))
        r = client.delete(f"/api/v1/documents/{doc_id}")
        assert r.status_code == 204, (
            f"DELETE on status={status.value!r} returned {r.status_code}: " f"{r.text}"
        )


async def _seed_document_with_logs(owner_id_str: str, log_count: int = 3) -> tuple[str, list[str]]:
    """Insert a document plus a small batch of `document_logs` rows.

    Used by `test_delete_wipes_document_logs` to assert that deleting
    the parent document also removes its activity-log entries. The
    event_type values mirror what the real pipeline emits so the test
    exercises the same row shape the cascade / explicit delete fires
    against in production.

    Returns (document_id, [log_id, ...]) so the test can sanity-check
    rows exist before the delete and vanish after.
    """
    from uuid import UUID

    factory = get_session_factory()
    doc_id = uuid.uuid4()
    event_types = ["uploaded", "parsing", "chunking", "embedding", "ready"]
    log_ids: list[str] = []

    async with factory() as session:  # type: AsyncSession
        doc = Document(
            id=doc_id,
            owner_id=UUID(owner_id_str),
            title="Seed doc with logs",
            filename="seed.pdf",
            mime_type="application/pdf",
            file_size_bytes=1024,
            storage_bucket="doc-uploads",
            storage_path=f"{owner_id_str}/{doc_id}/seed.pdf",
            status=DocumentStatus.ready,
        )
        session.add(doc)
        await session.flush()

        for i in range(log_count):
            session.add(
                DocumentLog(
                    document_id=doc_id,
                    owner_id=UUID(owner_id_str),
                    event_type=event_types[i % len(event_types)],
                    message=f"event {i}",
                    metadata_json=None,
                )
            )
        await session.flush()
        # Re-query so we have the assigned ids in a stable order.
        rows = (
            (await session.execute(select(DocumentLog).where(DocumentLog.document_id == doc_id)))
            .scalars()
            .all()
        )
        log_ids = [str(r.id) for r in rows]
        await session.commit()

    return str(doc_id), log_ids


async def _log_count_for_doc(doc_id_str: str) -> int:
    """How many `document_logs` rows remain for a given document.

    Owner-scope is intentionally omitted because once the document is
    gone the rows are too — there's no surviving `owner_id` lookup
    to filter against. The cascade in dev (SQLite) and the explicit
    `delete_logs_for_document` helper both remove all rows for the
    `document_id`, regardless of `owner_id`.
    """
    from uuid import UUID

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(DocumentLog).where(DocumentLog.document_id == UUID(doc_id_str))
        )
        return len(result.scalars().all())


def test_delete_wipes_document_logs() -> None:
    """Deleting a document must also remove its `document_logs` rows.

    The activity log is the per-doc + global feed the user sees; if
    deleting a doc left orphan log rows behind, the global feed would
    show "Document deleted" events forever (or, worse, events pointing
    at a doc id the user can no longer navigate to).

    The fix is belt-and-braces:
      - `PRAGMA foreign_keys=ON` is now issued on every SQLite
        connection (engine `connect` listener) so the schema's
        `ON DELETE CASCADE` on `document_logs.document_id` actually
        fires in dev.
      - The DELETE endpoint also calls
        `document_log_repo.delete_logs_for_document(...)` explicitly
        so the cleanup is guaranteed even if a future migration
        changes the cascade behaviour.
    """
    cookie = _register(f"dl-{uuid.uuid4().hex[:8]}@x.com")
    owner = _get_user_id_from_cookie(cookie)

    doc_id, log_ids = asyncio.run(_seed_document_with_logs(owner, log_count=3))

    # Sanity: all 3 log rows are in the DB before the delete. If this
    # assertion fails the test setup itself is broken — the rest of
    # the assertions become meaningless.
    assert len(log_ids) == 3
    assert (
        asyncio.run(_log_count_for_doc(doc_id)) == 3
    ), "fixture failed to insert the expected log rows"

    r = client.delete(f"/api/v1/documents/{doc_id}")
    assert r.status_code == 204, r.text

    # After delete: every log row is gone, AND the parent document
    # is gone (defence-in-depth: a regression that only drops logs
    # but keeps the document would still be visible to the user).
    assert asyncio.run(_log_count_for_doc(doc_id)) == 0, (
        "document_logs were not wiped on document delete — the global "
        "activity feed would still show events for a deleted doc"
    )
    assert not asyncio.run(_row_exists(Document, doc_id)), "parent document not deleted"


if __name__ == "__main__":
    test_delete_requires_auth()
    print("OK: delete requires auth (401)")
    test_delete_nonexistent_returns_404()
    print("OK: delete nonexistent returns 404")
    test_delete_other_users_doc_returns_404()
    print("OK: delete another user's doc returns 404 (no leak)")
    test_delete_happy_path_cascades()
    print("OK: delete happy path 204 + cascades")
    test_delete_is_idempotent_second_returns_404()
    print("OK: double delete returns 404")
    test_delete_works_in_any_status()
    print("OK: delete works in every status")
    print("\nALL DELETE TESTS PASSED")
