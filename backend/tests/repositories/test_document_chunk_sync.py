"""Sync repository tests for DocumentChunk."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.models.document_page import DocumentPage, TextSource
from app.models.user import User
from app.repositories.document_chunk import (
    ChunkSpec,
    list_chunks_for_page,
    replace_chunks_for_page,
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
    """Owner + document + page ready for chunk tests."""
    user = User(
        id=uuid4(),
        email="chunk@example.com",
        name="Chunk Owner",
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
    sync_db_session.commit()
    return user.id, doc.id, page.id


def _spec(page_id, idx, *, text="hello", section=None, prev=None, next_=None):
    return ChunkSpec(
        id=uuid4(),
        page_id=page_id,
        chunk_index=idx,
        section_path=section or [],
        text=text,
        token_count=2,
        char_count=len(text),
        prev_chunk_id=prev,
        next_chunk_id=next_,
    )


def test_replace_chunks_is_idempotent(sync_db_session, fixture_owners) -> None:
    _, _, page_id = fixture_owners
    # First insert 3 chunks.
    replace_chunks_for_page(
        sync_db_session,
        page_id,
        [
            _spec(page_id, 0, text="a " * 30),
            _spec(page_id, 1, text="b " * 30),
            _spec(page_id, 2, text="c " * 30),
        ],
    )
    sync_db_session.commit()
    rows = (
        sync_db_session.execute(select(DocumentChunk).where(DocumentChunk.page_id == page_id))
        .scalars()
        .all()
    )
    assert len(rows) == 3

    # Re-run with 1 chunk — old 3 should be gone.
    replace_chunks_for_page(sync_db_session, page_id, [_spec(page_id, 0)])
    sync_db_session.commit()
    rows = (
        sync_db_session.execute(select(DocumentChunk).where(DocumentChunk.page_id == page_id))
        .scalars()
        .all()
    )
    assert len(rows) == 1


def test_chunks_default_to_needing_embedding(sync_db_session, fixture_owners) -> None:
    _, _, page_id = fixture_owners
    replace_chunks_for_page(sync_db_session, page_id, [_spec(page_id, 0)])
    sync_db_session.commit()
    row = sync_db_session.execute(
        select(DocumentChunk).where(DocumentChunk.page_id == page_id)
    ).scalar_one()
    assert row.needs_embedding is True


def test_section_path_round_trips(sync_db_session, fixture_owners) -> None:
    _, _, page_id = fixture_owners
    section = ["Chapter 1", "Section 1.1"]
    replace_chunks_for_page(
        sync_db_session,
        page_id,
        [_spec(page_id, 0, section=section)],
    )
    sync_db_session.commit()
    row = sync_db_session.execute(
        select(DocumentChunk).where(DocumentChunk.page_id == page_id)
    ).scalar_one()
    assert row.section_path == section


def test_list_chunks_for_page_orders_by_index(sync_db_session, fixture_owners) -> None:
    _, _, page_id = fixture_owners
    replace_chunks_for_page(
        sync_db_session,
        page_id,
        [
            _spec(page_id, 0, text="z"),
            _spec(page_id, 1, text="a"),
            _spec(page_id, 2, text="m"),
        ],
    )
    sync_db_session.commit()
    rows = list_chunks_for_page(sync_db_session, page_id, owner_id=fixture_owners[0])
    assert [r.chunk_index for r in rows] == [0, 1, 2]


def test_list_chunks_respects_owner(sync_db_session, fixture_owners) -> None:
    _, _, page_id = fixture_owners
    replace_chunks_for_page(sync_db_session, page_id, [_spec(page_id, 0)])
    sync_db_session.commit()
    rows = list_chunks_for_page(sync_db_session, page_id, owner_id=uuid4())
    assert rows == []
