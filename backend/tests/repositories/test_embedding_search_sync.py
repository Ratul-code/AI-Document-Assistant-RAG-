"""Sync repository tests for the retrieval candidate-set query.

These tests pin three invariants from ADR-005 + ADR-011:
  * Owner-scoping is enforced in the SQL JOIN itself, not by the caller.
  * Only `Document.status='ready'` AND `EmbeddingStatus.succeeded'`
    chunks are returned.
  * `document_ids` filters correctly without disturbing the order.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.models.document_embedding import DocumentEmbedding, EmbeddingStatus
from app.models.document_page import DocumentPage, TextSource
from app.models.user import User
from app.repositories.embedding_search import fetch_candidate_rows


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


def _make_user(session, *, email: str) -> User:
    user = User(
        id=uuid4(),
        email=email,
        name="n",
        hashed_password="x" * 60,
    )
    session.add(user)
    session.flush()
    return user


def _make_doc(session, *, owner_id, status: DocumentStatus, title: str = "t") -> Document:
    doc = Document(
        id=uuid4(),
        owner_id=owner_id,
        title=title,
        filename="f",
        mime_type="text/plain",
        file_size_bytes=10,
        storage_bucket="b",
        storage_path="p",
        status=status,
    )
    session.add(doc)
    session.flush()
    return doc


def _make_page(session, *, document_id: int = 1, page_number: int = 1) -> DocumentPage:
    p = DocumentPage(
        id=uuid4(),
        document_id=document_id,
        page_number=page_number,
        raw_text="raw",
        final_text="final",
        text_source=TextSource.raw,
        char_count=5,
    )
    session.add(p)
    session.flush()
    return p


def _make_chunk(session, *, page_id, index: int = 0, text: str = "text") -> DocumentChunk:
    c = DocumentChunk(
        id=uuid4(),
        page_id=page_id,
        chunk_index=index,
        text=text,
        token_count=1,
        char_count=len(text),
    )
    session.add(c)
    session.flush()
    return c


def _make_embedding(
    session,
    *,
    chunk_id,
    status: EmbeddingStatus = EmbeddingStatus.succeeded,
    vector: list[float] | None = None,
    dim: int = 3,
) -> DocumentEmbedding:
    e = DocumentEmbedding(
        chunk_id=chunk_id,
        model_name="test-model",
        dim=dim,
        vector=vector or [0.0, 0.0, 1.0],
        status=status,
    )
    session.add(e)
    session.flush()
    return e


# ---------------------------------------------------------------------
# Owner scoping
# ---------------------------------------------------------------------


def test_returns_only_callers_rows(sync_db_session) -> None:
    user_a = _make_user(sync_db_session, email="a@example.com")
    user_b = _make_user(sync_db_session, email="b@example.com")
    doc_a = _make_doc(sync_db_session, owner_id=user_a.id, status=DocumentStatus.ready)
    doc_b = _make_doc(sync_db_session, owner_id=user_b.id, status=DocumentStatus.ready)
    page_a = _make_page(sync_db_session, document_id=doc_a.id)
    page_b = _make_page(sync_db_session, document_id=doc_b.id)
    chunk_a = _make_chunk(sync_db_session, page_id=page_a.id)
    chunk_b = _make_chunk(sync_db_session, page_id=page_b.id)
    _make_embedding(sync_db_session, chunk_id=chunk_a.id)
    _make_embedding(sync_db_session, chunk_id=chunk_b.id)
    sync_db_session.commit()

    rows = fetch_candidate_rows(sync_db_session, owner_id=user_a.id)
    assert [r.document_id for r in rows] == [doc_a.id]
    assert rows[0].chunk_id == chunk_a.id


def test_unrelated_owner_id_returns_empty(sync_db_session) -> None:
    user = _make_user(sync_db_session, email="only@example.com")
    doc = _make_doc(sync_db_session, owner_id=user.id, status=DocumentStatus.ready)
    page = _make_page(sync_db_session, document_id=doc.id)
    chunk = _make_chunk(sync_db_session, page_id=page.id)
    _make_embedding(sync_db_session, chunk_id=chunk.id)
    sync_db_session.commit()

    rows = fetch_candidate_rows(sync_db_session, owner_id=uuid4())
    assert rows == []


# ---------------------------------------------------------------------
# Status filter
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc_status,expect_empty",
    [
        (DocumentStatus.processing, True),
        (DocumentStatus.uploaded, True),
        (DocumentStatus.learning, True),
        (DocumentStatus.ready, False),
        (DocumentStatus.failed, True),
    ],
)
def test_only_ready_documents_are_queryable(sync_db_session, doc_status, expect_empty) -> None:
    user = _make_user(sync_db_session, email="x@example.com")
    doc = _make_doc(sync_db_session, owner_id=user.id, status=doc_status)
    page = _make_page(sync_db_session, document_id=doc.id)
    chunk = _make_chunk(sync_db_session, page_id=page.id)
    _make_embedding(sync_db_session, chunk_id=chunk.id)
    sync_db_session.commit()

    rows = fetch_candidate_rows(sync_db_session, owner_id=user.id)
    assert (len(rows) == 0) is expect_empty


@pytest.mark.parametrize(
    "emb_status,expect_empty",
    [
        (EmbeddingStatus.pending, True),
        (EmbeddingStatus.processing, True),
        (EmbeddingStatus.failed, True),
        (EmbeddingStatus.skipped, True),
        (EmbeddingStatus.succeeded, False),
    ],
)
def test_only_succeeded_embeddings_are_queryable(sync_db_session, emb_status, expect_empty) -> None:
    user = _make_user(sync_db_session, email="e@example.com")
    doc = _make_doc(sync_db_session, owner_id=user.id, status=DocumentStatus.ready)
    page = _make_page(sync_db_session, document_id=doc.id)
    chunk = _make_chunk(sync_db_session, page_id=page.id)
    _make_embedding(sync_db_session, chunk_id=chunk.id, status=emb_status)
    sync_db_session.commit()

    rows = fetch_candidate_rows(sync_db_session, owner_id=user.id)
    assert (len(rows) == 0) is expect_empty


# ---------------------------------------------------------------------
# document_ids filter
# ---------------------------------------------------------------------


def test_document_ids_filter_intersects(sync_db_session) -> None:
    user = _make_user(sync_db_session, email="m@example.com")
    doc_1 = _make_doc(sync_db_session, owner_id=user.id, status=DocumentStatus.ready, title="D1")
    doc_2 = _make_doc(sync_db_session, owner_id=user.id, status=DocumentStatus.ready, title="D2")
    doc_3 = _make_doc(sync_db_session, owner_id=user.id, status=DocumentStatus.ready, title="D3")
    for doc in (doc_1, doc_2, doc_3):
        page = _make_page(sync_db_session, document_id=doc.id)
        chunk = _make_chunk(sync_db_session, page_id=page.id)
        _make_embedding(sync_db_session, chunk_id=chunk.id)
    sync_db_session.commit()

    # Ask only for docs 1 and 3.
    rows = fetch_candidate_rows(
        sync_db_session,
        owner_id=user.id,
        document_ids=[doc_1.id, doc_3.id],
    )
    returned_doc_ids = {r.document_id for r in rows}
    assert returned_doc_ids == {doc_1.id, doc_3.id}


def test_document_ids_other_owner_returns_empty(sync_db_session) -> None:
    user_a = _make_user(sync_db_session, email="x1@example.com")
    user_b = _make_user(sync_db_session, email="x2@example.com")
    doc_a = _make_doc(sync_db_session, owner_id=user_a.id, status=DocumentStatus.ready)
    doc_b = _make_doc(sync_db_session, owner_id=user_b.id, status=DocumentStatus.ready)
    for doc in (doc_a, doc_b):
        page = _make_page(sync_db_session, document_id=doc.id)
        chunk = _make_chunk(sync_db_session, page_id=page.id)
        _make_embedding(sync_db_session, chunk_id=chunk.id)
    sync_db_session.commit()

    # user_a asks for both docs; only doc_a is theirs.
    rows = fetch_candidate_rows(
        sync_db_session,
        owner_id=user_a.id,
        document_ids=[doc_a.id, doc_b.id],
    )
    assert {r.document_id for r in rows} == {doc_a.id}


# ---------------------------------------------------------------------
# Row shape
# ---------------------------------------------------------------------


def test_candidate_row_includes_metadata_for_citations(sync_db_session) -> None:
    user = _make_user(sync_db_session, email="row@example.com")
    doc = _make_doc(sync_db_session, owner_id=user.id, status=DocumentStatus.ready, title="Alpha")
    page = _make_page(sync_db_session, document_id=doc.id, page_number=4)
    chunk = _make_chunk(sync_db_session, page_id=page.id, index=2, text="hello world")
    _make_embedding(
        sync_db_session,
        chunk_id=chunk.id,
        vector=[0.1, 0.2, 0.3],
        dim=3,
    )
    sync_db_session.commit()

    rows = fetch_candidate_rows(sync_db_session, owner_id=user.id)
    assert len(rows) == 1
    r = rows[0]
    assert r.document_title == "Alpha"
    assert r.page_number == 4
    assert r.chunk_index == 2
    assert r.text == "hello world"
    assert r.vector == [0.1, 0.2, 0.3]
    assert r.embedding_dim == 3
    assert r.embedding_model_name == "test-model"
