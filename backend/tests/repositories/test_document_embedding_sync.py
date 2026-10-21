"""Sync repository tests for DocumentEmbedding."""

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
from app.repositories.document_embedding import (
    EmbeddingSpec,
    list_pending_chunk_ids,
    upsert_embedding,
)


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


@pytest.fixture
def fixture_owners(sync_db_session):
    """Owner + document + page + 3 chunks ready for embedding tests."""
    user = User(
        id=uuid4(),
        email="emb@example.com",
        name="Embedding Owner",
        hashed_password="x" * 60,
    )
    sync_db_session.add(user)
    doc = Document(
        id=uuid4(),
        owner_id=user.id,
        title="t",
        filename="f",
        mime_type="text/plain",
        file_size_bytes=10,
        storage_bucket="b",
        storage_path="p",
        status=DocumentStatus.uploaded,
    )
    sync_db_session.add(doc)
    page = DocumentPage(
        id=uuid4(),
        document_id=doc.id,
        page_number=1,
        raw_text="page text",
        final_text="page text",
        text_source=TextSource.raw,
        char_count=9,
    )
    sync_db_session.add(page)
    chunks = []
    for idx in range(3):
        chunk = DocumentChunk(
            id=uuid4(),
            page_id=page.id,
            chunk_index=idx,
            text=f"chunk-{idx}",
            token_count=2,
            char_count=10,
        )
        chunks.append(chunk)
        sync_db_session.add(chunk)
    sync_db_session.commit()
    return user.id, doc.id, [c.id for c in chunks]


def _vector(seed: int) -> list[float]:
    return [float(seed), float(seed + 1), float(seed + 2)]


def test_upsert_embedding_inserts_new_row(sync_db_session, fixture_owners) -> None:
    _, _, chunk_ids = fixture_owners
    upsert_embedding(
        sync_db_session,
        EmbeddingSpec(
            chunk_id=chunk_ids[0],
            model_name="text-embedding-3-small",
            dim=3,
            vector=_vector(1),
        ),
    )
    sync_db_session.commit()
    row = sync_db_session.execute(
        select(DocumentEmbedding).where(DocumentEmbedding.chunk_id == chunk_ids[0])
    ).scalar_one()
    assert row.dim == 3
    assert row.vector == _vector(1)
    assert row.status == EmbeddingStatus.succeeded


def test_upsert_embedding_updates_existing_row(sync_db_session, fixture_owners) -> None:
    _, _, chunk_ids = fixture_owners
    upsert_embedding(
        sync_db_session,
        EmbeddingSpec(chunk_id=chunk_ids[0], model_name="m", dim=3, vector=_vector(1)),
    )
    sync_db_session.commit()
    # Re-run with different vector — should update, not insert.
    upsert_embedding(
        sync_db_session,
        EmbeddingSpec(chunk_id=chunk_ids[0], model_name="m", dim=3, vector=_vector(99)),
    )
    sync_db_session.commit()
    rows = (
        sync_db_session.execute(
            select(DocumentEmbedding).where(DocumentEmbedding.chunk_id == chunk_ids[0])
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].vector == _vector(99)


def test_list_pending_chunk_ids_returns_only_unembedded(sync_db_session, fixture_owners) -> None:
    owner_id, doc_id, chunk_ids = fixture_owners
    # Embed only the first chunk.
    upsert_embedding(
        sync_db_session,
        EmbeddingSpec(chunk_id=chunk_ids[0], model_name="m", dim=3, vector=_vector(1)),
    )
    sync_db_session.commit()
    pending = list_pending_chunk_ids(sync_db_session, doc_id, owner_id=owner_id)
    assert pending == [chunk_ids[1], chunk_ids[2]]


def test_list_pending_chunk_ids_is_owner_scoped(sync_db_session, fixture_owners) -> None:
    _, doc_id, _ = fixture_owners
    pending = list_pending_chunk_ids(sync_db_session, doc_id, owner_id=uuid4())
    assert pending == []


def test_upsert_can_record_failure_status(sync_db_session, fixture_owners) -> None:
    _, _, chunk_ids = fixture_owners
    upsert_embedding(
        sync_db_session,
        EmbeddingSpec(chunk_id=chunk_ids[0], model_name="m", dim=3, vector=_vector(1)),
        status=EmbeddingStatus.failed,
        error_message="rate limited",
    )
    sync_db_session.commit()
    row = sync_db_session.execute(
        select(DocumentEmbedding).where(DocumentEmbedding.chunk_id == chunk_ids[0])
    ).scalar_one()
    assert row.status == EmbeddingStatus.failed
    assert row.error_message == "rate limited"


def test_failed_embeddings_stay_in_pending_worklist(sync_db_session, fixture_owners) -> None:
    """A failed embedding should still appear in the pending list so the
    worker can retry later."""
    owner_id, doc_id, chunk_ids = fixture_owners
    upsert_embedding(
        sync_db_session,
        EmbeddingSpec(chunk_id=chunk_ids[0], model_name="m", dim=3, vector=_vector(1)),
        status=EmbeddingStatus.failed,
        error_message="rate limited",
    )
    sync_db_session.commit()
    pending = list_pending_chunk_ids(sync_db_session, doc_id, owner_id=owner_id)
    assert pending == [chunk_ids[0], chunk_ids[1], chunk_ids[2]]
