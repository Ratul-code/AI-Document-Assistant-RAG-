"""Sync SQLAlchemy operations on DocumentPage.

Used by the Celery `process_document` task via `run_in_sync_session`.
Read paths are owner-scoped so the future `/documents/{id}/pages`
endpoints can use them without leaking other users' data.

Conventions (mirrors `app/repositories/document.py`):
- Functions that take `document_id` for reads ALSO take `owner_id` and
  filter by both. This enforces the security boundary at the data
  layer.
- Functions do not commit; the caller controls the transaction.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.document_page import DocumentPage, OcrStatus, TextSource
from app.services.document.parser._base import ParsedPage


def replace_pages_for_document(
    db: Session,
    document_id: UUID,
    pages: list[ParsedPage],
) -> int:
    """Atomically replace all pages for `document_id`.

    DELETE existing rows, bulk-INSERT the new ones in the same
    transaction. Idempotent on Celery retry regardless of how the
    page count changed across runs.

    Returns the number of rows inserted.
    """
    db.execute(delete(DocumentPage).where(DocumentPage.document_id == document_id))

    if not pages:
        return 0

    db.execute(insert(DocumentPage), [_row(document_id, page) for page in pages])
    return len(pages)


def list_pages(
    db: Session,
    document_id: UUID,
    *,
    owner_id: UUID,
) -> list[DocumentPage]:
    """Return all pages for a document, owner-scoped.

    Filters by both `document_id` AND `documents.owner_id` in a single
    query so we never accidentally return rows for a different owner's
    document.
    """
    stmt = (
        select(DocumentPage)
        .join(Document, Document.id == DocumentPage.document_id)
        .where(
            DocumentPage.document_id == document_id,
            Document.owner_id == owner_id,
        )
        .order_by(DocumentPage.page_number.asc())
    )
    return list(db.execute(stmt).scalars().all())


def get_page(
    db: Session,
    page_id: UUID,
    *,
    owner_id: UUID,
) -> DocumentPage | None:
    """Fetch a single page by id, owner-scoped."""
    stmt = (
        select(DocumentPage)
        .join(Document, Document.id == DocumentPage.document_id)
        .where(
            DocumentPage.id == page_id,
            Document.owner_id == owner_id,
        )
    )
    return db.execute(stmt).scalar_one_or_none()


# ---------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------


def _row(document_id: UUID, page: ParsedPage) -> dict:
    return {
        "document_id": document_id,
        "page_number": page.page_number,
        "raw_text": page.raw_text,
        "ocr_text": page.ocr_text,
        "final_text": page.final_text,
        "text_source": page.text_source or TextSource.raw,
        "ocr_status": OcrStatus.not_needed,
        "parser_name": page.parser_name,
        "parser_version": page.parser_version,
        "ocr_engine": page.ocr_engine,
        "ocr_confidence": page.ocr_confidence,
        "char_count": len(page.final_text),
        "page_metadata": page.page_metadata,
    }
