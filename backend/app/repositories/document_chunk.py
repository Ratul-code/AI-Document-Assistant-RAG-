"""Sync SQLAlchemy operations on DocumentChunk.

The Celery chunker worker writes via `replace_chunks_for_page` (atomic
DELETE + INSERT) so re-runs are idempotent. Read paths are owner-scoped
via a join against `documents` so the future read endpoints can't
accidentally leak across users.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.document_page import DocumentPage


@dataclass
class ChunkSpec:
    """A chunk ready to be persisted.

    `id` is supplied by the caller so the chunker can wire `prev_chunk_id`
    / `next_chunk_id` without a round-trip to the DB. Mutable on purpose
    so the chunker can backfill `next_chunk_id` after all ids exist.
    """

    id: UUID
    page_id: UUID
    chunk_index: int
    section_path: list[str]
    text: str
    token_count: int
    char_count: int
    needs_embedding: bool = True
    prev_chunk_id: UUID | None = None
    next_chunk_id: UUID | None = None


def replace_chunks_for_page(
    db: Session,
    page_id: UUID,
    chunks: list[ChunkSpec],
) -> int:
    """Atomically replace all chunks for `page_id`.

    Idempotent: DELETE + bulk INSERT in one transaction. Returns the
    number of rows inserted.
    """
    db.execute(delete(DocumentChunk).where(DocumentChunk.page_id == page_id))

    if not chunks:
        return 0

    db.execute(insert(DocumentChunk), [_row(chunk) for chunk in chunks])
    return len(chunks)


def list_chunks_for_page(
    db: Session,
    page_id: UUID,
    *,
    owner_id: UUID,
) -> list[DocumentChunk]:
    """Return all chunks for a page, owner-scoped."""
    stmt = (
        select(DocumentChunk)
        .join(DocumentPage, DocumentPage.id == DocumentChunk.page_id)
        .join(Document, Document.id == DocumentPage.document_id)
        .where(
            DocumentChunk.page_id == page_id,
            Document.owner_id == owner_id,
        )
        .order_by(DocumentChunk.chunk_index.asc())
    )
    return list(db.execute(stmt).scalars().all())


def list_chunks_for_document(
    db: Session,
    document_id: UUID,
    *,
    owner_id: UUID,
) -> list[DocumentChunk]:
    """Return all chunks for a document, owner-scoped, in page order."""
    stmt = (
        select(DocumentChunk)
        .join(DocumentPage, DocumentPage.id == DocumentChunk.page_id)
        .join(Document, Document.id == DocumentPage.document_id)
        .where(
            DocumentPage.document_id == document_id,
            Document.owner_id == owner_id,
        )
        .order_by(
            DocumentPage.page_number.asc(),
            DocumentChunk.chunk_index.asc(),
        )
    )
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------


def _row(chunk: ChunkSpec) -> dict:
    return {
        "id": chunk.id,
        "page_id": chunk.page_id,
        "chunk_index": chunk.chunk_index,
        "section_path": chunk.section_path,
        "text": chunk.text,
        "token_count": chunk.token_count,
        "char_count": chunk.char_count,
        "needs_embedding": chunk.needs_embedding,
        "prev_chunk_id": chunk.prev_chunk_id,
        "next_chunk_id": chunk.next_chunk_id,
    }


__all__ = ["ChunkSpec", "replace_chunks_for_page", "list_chunks_for_page", "list_chunks_for_document"]
