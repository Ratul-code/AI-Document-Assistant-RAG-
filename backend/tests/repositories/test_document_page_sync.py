"""Sync repository tests for DocumentPage.

Uses a real SQLite engine + session to exercise the bulk-replace path.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.document import Document, DocumentStatus
from app.models.document_page import DocumentPage, OcrStatus
from app.models.user import User
from app.repositories.document_page import replace_pages_for_document
from app.services.document.parser._base import ParsedPage
from app.models.document_page import TextSource


def _make_page(num: int, text: str) -> ParsedPage:
    return ParsedPage(
        page_number=num,
        raw_text=text,
        final_text=text,
        text_source=TextSource.raw,
        parser_name="test",
        parser_version="1",
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
def document_id(sync_db_session) -> str:
    user = User(
        id=uuid4(),
        email="test@example.com",
        name="Test User",
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
    sync_db_session.commit()
    return str(doc.id)


def test_replace_pages_is_idempotent(sync_db_session, document_id: str) -> None:
    doc_uuid = uuid4()
    # First insert with 3 pages
    replace_pages_for_document(
        sync_db_session, doc_uuid,
        [_make_page(1, "a" * 30), _make_page(2, "b" * 30), _make_page(3, "c" * 30)],
    )
    sync_db_session.commit()
    rows = sync_db_session.execute(
        select(DocumentPage).where(DocumentPage.document_id == doc_uuid)
    ).scalars().all()
    assert len(rows) == 3

    # Re-run with 2 pages — old 3 should be gone.
    replace_pages_for_document(
        sync_db_session, doc_uuid,
        [_make_page(1, "x" * 30), _make_page(2, "y" * 30)],
    )
    sync_db_session.commit()
    rows = sync_db_session.execute(
        select(DocumentPage)
        .where(DocumentPage.document_id == doc_uuid)
        .order_by(DocumentPage.page_number)
    ).scalars().all()
    assert len(rows) == 2
    assert [r.page_number for r in rows] == [1, 2]


def test_replace_pages_with_zero_pages_clears(sync_db_session, document_id: str) -> None:
    doc_uuid = uuid4()
    replace_pages_for_document(
        sync_db_session, doc_uuid,
        [_make_page(1, "a" * 30), _make_page(2, "b" * 30)],
    )
    sync_db_session.commit()
    n = replace_pages_for_document(sync_db_session, doc_uuid, [])
    sync_db_session.commit()
    assert n == 0
    rows = sync_db_session.execute(
        select(DocumentPage).where(DocumentPage.document_id == doc_uuid)
    ).scalars().all()
    assert rows == []


def test_pages_default_to_ocr_status_not_needed(
    sync_db_session, document_id: str
) -> None:
    doc_uuid = uuid4()
    replace_pages_for_document(
        sync_db_session, doc_uuid,
        [_make_page(1, "hello world this is a long enough line")],
    )
    sync_db_session.commit()
    row = sync_db_session.execute(
        select(DocumentPage).where(DocumentPage.document_id == doc_uuid)
    ).scalar_one()
    assert row.ocr_status == OcrStatus.not_needed
    assert row.text_source == TextSource.raw
