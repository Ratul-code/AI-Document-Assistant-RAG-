"""SQLAlchemy model for chunked document text.

One row per chunk. Chunks are produced by `chunker.build_chunks_for_page`
from a single `DocumentPage`. The chunker respects heading boundaries
inside the page (when the parser emitted them) and stamps each chunk
with a `section_path` so retrieval can boost matches by section.

`needs_embedding` is a fast boolean for the embedding worker to scan
without joining to the embeddings table. Source-of-truth for the
lifecycle lives on `DocumentEmbedding.status`.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.types import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DocumentChunk(Base):
    """A chunk of text belonging to a parsed page."""

    __tablename__ = "document_chunks"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    # Owner-scoped via the parent page -> document -> user chain. CASCADE
    # ensures wiping a document wipes its chunks too.
    page_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_pages.id", ondelete="CASCADE"),
        nullable=False,
    )

    # 0-indexed position within the page. Combined with `page_id` this
    # is unique so a chunk's index is stable across re-runs.
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)

    # Section breadcrumb captured at chunking time. Stored as TEXT[] on
    # Postgres (queryable / indexable) and as JSON on SQLite (dev/tests).
    # Empty for PDFs (the parser doesn't emit headings there).
    section_path: Mapped[list[str]] = mapped_column(
        ARRAY(Text).with_variant(JSON(), "sqlite"),
        nullable=False,
        default=list,
    )

    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Set on every chunk by default; flipped to False once an embedding
    # row exists. Lets the embedding worker pull a fast worklist
    # without joining the embeddings table.
    needs_embedding: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )

    # Optional linkage to the next chunk in the same page (for
    # overlap-aware retrieval). NULL for the last chunk in a page.
    prev_chunk_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="SET NULL"),
        nullable=True,
    )
    next_chunk_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("uq_document_chunks_page_index", "page_id", "chunk_index", unique=True),
        Index("ix_document_chunks_page_id", "page_id"),
    )


__all__ = ["DocumentChunk"]

