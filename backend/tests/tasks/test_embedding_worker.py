"""Embedding worker tests.

The worker runs against an in-memory SQLite database. We test the
`_process_one_document` helper directly rather than through Celery so
the test stays fast and synchronous.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.models.document_embedding import DocumentEmbedding, EmbeddingStatus
from app.models.document_page import DocumentPage, TextSource
from app.models.user import User
from app.repositories.document_embedding import EmbeddingSpec, upsert_embedding
from app.services.ai.embeddings import (
    DeterministicStubModel,
    get_default_embedding_model,
)
from app.tasks.embedding import _process_one_document


@pytest.fixture
def sync_db_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed(session, chunk_texts: list[str]):
    """Insert a user + document + page + chunks. Returns (owner_id, document_id).

    The doc is seeded in `learning` because that's the state
    `process_document` leaves it in: chunking is done, embeddings are
    still pending. The embedding worker is responsible for flipping it
    to `ready` (or `failed`) once chunks are processed.
    """
    user = User(
        id=uuid4(),
        email="emb-task@example.com",
        name="Embedding Worker",
        hashed_password="x" * 60,
    )
    session.add(user)
    doc = Document(
        id=uuid4(),
        owner_id=user.id,
        title="t",
        filename="f",
        mime_type="text/plain",
        file_size_bytes=10,
        storage_bucket="b",
        storage_path="p",
        status=DocumentStatus.learning,
    )
    session.add(doc)
    page = DocumentPage(
        id=uuid4(),
        document_id=doc.id,
        page_number=1,
        raw_text="hello",
        final_text="hello",
        text_source=TextSource.raw,
        char_count=5,
    )
    session.add(page)
    chunks = []
    for idx, text in enumerate(chunk_texts):
        chunk = DocumentChunk(
            id=uuid4(),
            page_id=page.id,
            chunk_index=idx,
            text=text,
            token_count=2,
            char_count=len(text),
        )
        chunks.append(chunk)
        session.add(chunk)
    session.commit()
    return user.id, doc.id


def test_stub_model_is_deterministic() -> None:
    model = DeterministicStubModel()
    a = model.embed("hello world")
    b = model.embed("hello world")
    assert a.vector == b.vector
    assert a.dim == 16
    assert a.model_name == "deterministic-stub-v1"


def test_stub_model_differs_for_different_text() -> None:
    model = DeterministicStubModel()
    a = model.embed("hello world")
    b = model.embed("completely different text")
    assert a.vector != b.vector


def test_get_default_returns_stub() -> None:
    model = get_default_embedding_model()
    assert model.model_name == "deterministic-stub-v1"


def test_process_one_document_embeds_pending_chunks(sync_db_session) -> None:
    owner_id, doc_id = _seed(sync_db_session, ["chunk one", "chunk two", "chunk three"])

    result = _process_one_document(sync_db_session, doc_id, owner_id)
    sync_db_session.commit()
    assert result["succeeded"] == 3
    assert result["failed"] == 0

    rows = sync_db_session.execute(
        select(DocumentEmbedding).join(
            DocumentChunk, DocumentChunk.id == DocumentEmbedding.chunk_id
        )
    ).scalars().all()
    assert len(rows) == 3
    for row in rows:
        assert row.status == EmbeddingStatus.succeeded
        assert row.dim == 16

    # All chunks should now have needs_embedding=False.
    chunks = sync_db_session.execute(select(DocumentChunk)).scalars().all()
    assert all(c.needs_embedding is False for c in chunks)

    # The doc must have been flipped LEARNING -> READY by the worker,
    # because at least one chunk succeeded.
    doc = sync_db_session.get(Document, doc_id)
    assert doc is not None
    assert doc.status == DocumentStatus.ready


def test_process_one_document_flips_to_failed_when_every_chunk_fails(
    sync_db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All chunks fail to embed -> doc ends in FAILED, not READY."""

    class _AlwaysFailModel:
        model_name = "always-fail-test"
        dim = 16

        def embed_batch(self, texts: list[str]) -> list:
            raise RuntimeError("simulated provider outage")

        def embed(self, text: str):  # pragma: no cover - batch path is hit first
            raise RuntimeError("simulated provider outage")

    monkeypatch.setattr(
        "app.tasks.embedding.get_default_embedding_model", _AlwaysFailModel
    )

    owner_id, doc_id = _seed(sync_db_session, ["a", "b"])

    result = _process_one_document(sync_db_session, doc_id, owner_id)
    sync_db_session.commit()
    assert result["succeeded"] == 0
    assert result["failed"] == 2

    doc = sync_db_session.get(Document, doc_id)
    assert doc is not None
    assert doc.status == DocumentStatus.failed
    assert doc.error_message is not None
    assert "simulated provider outage" in doc.error_message


def test_process_one_document_skips_already_embedded(sync_db_session) -> None:
    owner_id, doc_id = _seed(sync_db_session, ["a", "b"])
    # Pre-embed one chunk.
    chunks = sync_db_session.execute(
        select(DocumentChunk).order_by(DocumentChunk.chunk_index)
    ).scalars().all()

    upsert_embedding(
        sync_db_session,
        EmbeddingSpec(
            chunk_id=chunks[0].id,
            model_name="deterministic-stub-v1",
            dim=16,
            vector=[0.0] * 16,
        ),
    )
    sync_db_session.commit()
    # Mark the first chunk as already embedded so the worker skips it.
    chunks[0].needs_embedding = False
    sync_db_session.commit()

    result = _process_one_document(sync_db_session, doc_id, owner_id)
    sync_db_session.commit()
    # Only the second chunk should have been embedded.
    assert result["succeeded"] == 1


def test_process_one_document_is_owner_scoped(sync_db_session) -> None:
    """A different owner_id sees zero pending chunks."""
    real_owner, doc_id = _seed(sync_db_session, ["only chunk"])

    other_owner = uuid4()
    result = _process_one_document(sync_db_session, doc_id, other_owner)
    assert result["succeeded"] == 0
    assert result["failed"] == 0


def test_process_one_document_with_no_pending_chunks(sync_db_session) -> None:
    owner_id, doc_id = _seed(sync_db_session, [])

    result = _process_one_document(sync_db_session, doc_id, owner_id)
    sync_db_session.commit()
    assert result["succeeded"] == 0
    assert result["failed"] == 0
    # Zero-chunk doc — worker has nothing to flip; status stays LEARNING.
    doc = sync_db_session.get(Document, doc_id)
    assert doc is not None
    assert doc.status == DocumentStatus.learning
