"""Unit tests for the per-document stats aggregation helper.

The repo helper is `async def` because it's called from an async
FastAPI route. We exercise it from sync test functions via
`asyncio.run` against an in-memory aiosqlite database, which keeps
the test hermetic (no shared dev DB) and lets us pin the SQL +
aggregation logic separately from the HTTP route. The route itself
is covered end-to-end in `tests/api/test_document_stats.py`.

Cases covered:
  - empty document (no pages yet) → all zeros, no embedding models
  - happy path with mixed embedding statuses → correct histogram
  - distinct embedding model names returned sorted
  - all four ocr_status buckets break down correctly
  - cross-owner → None (so the route returns 404)
  - missing doc → None
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.base import Base
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.models.document_embedding import DocumentEmbedding, EmbeddingStatus
from app.models.document_page import DocumentPage, OcrStatus, TextSource
from app.models.user import User
from app.repositories import document_stats as stats_repo


@pytest.fixture
def session_factory():
    """Fresh in-memory aiosqlite database per test.

    Mirrors the engine the FastAPI app uses (`sqlite+aiosqlite:///:memory:`)
    but creates it in `setUp` and disposes it in `tearDown` so tests
    don't share state.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    try:
        yield factory
    finally:
        asyncio.run(engine.dispose())


def _run(coro):
    """Run an async coroutine from a sync test.

    Centralised here so each test reads cleanly. Not async-aware —
    every test is independent and doesn't share state across runs.
    """
    return asyncio.run(coro)


async def _seed_user(session, email) -> UUID:
    user = User(
        id=uuid4(),
        email=email,
        name=email.split("@")[0],
        hashed_password="x" * 60,
    )
    session.add(user)
    await session.commit()
    return user.id


async def _seed_doc(
    session,
    owner_id,
    *,
    status=DocumentStatus.ready,
    title="t",
) -> UUID:
    doc = Document(
        id=uuid4(),
        owner_id=owner_id,
        title=title,
        filename="f.pdf",
        mime_type="application/pdf",
        file_size_bytes=1024,
        storage_bucket="b",
        storage_path=f"p/{uuid4()}",
        status=status,
    )
    session.add(doc)
    await session.commit()
    return doc.id


async def _seed_page(
    session,
    doc_id,
    *,
    page_number=1,
    ocr_status=OcrStatus.not_needed,
    text_source=TextSource.raw,
) -> UUID:
    page = DocumentPage(
        id=uuid4(),
        document_id=doc_id,
        page_number=page_number,
        raw_text="hi",
        final_text="hi",
        text_source=text_source,
        ocr_status=ocr_status,
        char_count=2,
    )
    session.add(page)
    await session.commit()
    return page.id


async def _seed_chunk(session, page_id, *, chunk_index=0) -> UUID:
    chunk = DocumentChunk(
        id=uuid4(),
        page_id=page_id,
        chunk_index=chunk_index,
        text="x" * 10,
        token_count=2,
        char_count=10,
        needs_embedding=True,
    )
    session.add(chunk)
    await session.commit()
    return chunk.id


async def _seed_embedding(
    session,
    chunk_id,
    *,
    model_name="text-embedding-3-small",
    status=EmbeddingStatus.succeeded,
) -> None:
    emb = DocumentEmbedding(
        id=uuid4(),
        chunk_id=chunk_id,
        model_name=model_name,
        dim=4,
        vector=[0.1, 0.2, 0.3, 0.4],
        status=status,
    )
    session.add(emb)
    await session.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_document_returns_zeros(session_factory):
    """A document with no pages / chunks / embeddings returns a clean
    zero struct, not None — the route needs zeros, not a 404, for the
    'doc just got uploaded, nothing parsed yet' case."""

    async def body():
        async with session_factory() as session:
            owner_id = await _seed_user(session, "empty@example.com")
            doc_id = await _seed_doc(session, owner_id)
            return await stats_repo.document_stats_for(session, doc_id, owner_id=owner_id)

    stats = _run(body())
    assert stats is not None
    assert stats.page_count == 0
    assert stats.chunks_total == 0
    assert stats.chunks_embedded == 0
    assert stats.chunks_pending == 0
    assert stats.chunks_failed == 0
    assert stats.pages_needing_ocr == 0
    assert stats.pages_ocr_pending == 0
    assert stats.pages_ocr_failed == 0
    assert stats.embedding_models == []
    assert stats.embedding_status_counts == {}


def test_happy_path_with_mixed_embedding_statuses(session_factory):
    """3 chunks: 2 succeeded, 1 failed, on 2 pages. Embedding model
    name is captured once."""

    async def body():
        async with session_factory() as session:
            owner_id = await _seed_user(session, "mixed@example.com")
            doc_id = await _seed_doc(session, owner_id)
            page1 = await _seed_page(session, doc_id, page_number=1)
            page2 = await _seed_page(session, doc_id, page_number=2, ocr_status=OcrStatus.pending)
            c1 = await _seed_chunk(session, page1, chunk_index=0)
            c2 = await _seed_chunk(session, page1, chunk_index=1)
            await _seed_embedding(session, c1, status=EmbeddingStatus.succeeded)
            await _seed_embedding(session, c2, status=EmbeddingStatus.succeeded)
            c3 = await _seed_chunk(session, page2, chunk_index=0)
            await _seed_embedding(session, c3, status=EmbeddingStatus.failed)
            return await stats_repo.document_stats_for(session, doc_id, owner_id=owner_id)

    stats = _run(body())
    assert stats is not None
    assert stats.page_count == 2
    assert stats.chunks_total == 3
    assert stats.chunks_embedded == 2
    assert stats.chunks_failed == 1
    assert stats.chunks_pending == 0  # 3 - 2 - 1
    assert stats.pages_needing_ocr == 1  # page2 is pending
    assert stats.pages_ocr_pending == 1
    assert stats.pages_ocr_failed == 0
    assert stats.embedding_models == ["text-embedding-3-small"]
    assert stats.embedding_status_counts == {"succeeded": 2, "failed": 1}


def test_distinct_embedding_models_returned_sorted(session_factory):
    """Two different model names on the same doc → both listed, sorted
    alphabetically. Reproduces the rare 'mixed-model' case (e.g. a
    re-run after the operator swapped embedders)."""

    async def body():
        async with session_factory() as session:
            owner_id = await _seed_user(session, "sorted@example.com")
            doc_id = await _seed_doc(session, owner_id)
            page = await _seed_page(session, doc_id)
            c1 = await _seed_chunk(session, page, chunk_index=0)
            c2 = await _seed_chunk(session, page, chunk_index=1)
            await _seed_embedding(session, c1, model_name="z-model")
            await _seed_embedding(session, c2, model_name="a-model")
            return await stats_repo.document_stats_for(session, doc_id, owner_id=owner_id)

    stats = _run(body())
    assert stats is not None
    # Sorted alphabetically so the dialog order is stable.
    assert stats.embedding_models == ["a-model", "z-model"]


def test_ocr_status_breakdown(session_factory):
    """All four ocr_status buckets: not_needed (excluded), pending,
    processing, failed, succeeded. Asserts that
    `pages_needing_ocr` counts everything except `not_needed`."""

    async def body():
        async with session_factory() as session:
            owner_id = await _seed_user(session, "ocr@example.com")
            doc_id = await _seed_doc(session, owner_id)
            await _seed_page(session, doc_id, page_number=1, ocr_status=OcrStatus.not_needed)
            await _seed_page(session, doc_id, page_number=2, ocr_status=OcrStatus.pending)
            await _seed_page(session, doc_id, page_number=3, ocr_status=OcrStatus.processing)
            await _seed_page(session, doc_id, page_number=4, ocr_status=OcrStatus.failed)
            await _seed_page(session, doc_id, page_number=5, ocr_status=OcrStatus.succeeded)
            return await stats_repo.document_stats_for(session, doc_id, owner_id=owner_id)

    stats = _run(body())
    assert stats is not None
    assert stats.page_count == 5
    assert stats.pages_needing_ocr == 4  # everything except not_needed
    assert stats.pages_ocr_pending == 2  # pending + processing
    assert stats.pages_ocr_failed == 1


def test_cross_owner_returns_none(session_factory):
    """Doc belongs to user A; user B asks for its stats → None (route
    turns this into 404). No info leak."""

    async def body():
        async with session_factory() as session:
            owner_a = await _seed_user(session, "a@example.com")
            owner_b = await _seed_user(session, "b@example.com")
            doc_id = await _seed_doc(session, owner_a)
            return await stats_repo.document_stats_for(session, doc_id, owner_id=owner_b)

    assert _run(body()) is None


def test_missing_doc_returns_none(session_factory):
    """Random uuid that's not in the DB → None."""

    async def body():
        async with session_factory() as session:
            owner_id = await _seed_user(session, "missing@example.com")
            return await stats_repo.document_stats_for(session, uuid4(), owner_id=owner_id)

    assert _run(body()) is None


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
