"""Retrieval candidate-set queries for the analysis (RAG) pipeline.

The candidate set is the set of (chunk, embedding) rows that the
retriever is allowed to rank against a user query. The rules:

* Only chunks belonging to **READY** documents (ADR-011 — `READY`
  strictly means queryable).
* Only embeddings with `status='succeeded'`.
* Always owner-scoped via JOIN on `documents.owner_id`.

This module does **not** call NumPy or do any ranking — it only returns
the rows. Ranking lives in `app.services.document.analyser`, which can
swap to a vector DB later without changing this repo's contract.

Sync by choice: matches the existing ingestion-side repos
(`document_embedding.py`, `document_chunk.py`). The analyser pulls the
rows via `run_in_sync_session` (same pattern as `process_document`)
because the in-Python cosine work is sync-friendly and shaves the
async/sync overhead at query time.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.models.document_embedding import DocumentEmbedding, EmbeddingStatus
from app.models.document_page import DocumentPage


@dataclass(frozen=True)
class CandidateRow:
    """One queryable chunk with all the fields retrieval needs.

    Frozen so the retriever can't accidentally mutate a row and so
    rows are safely hashable for debug / dedupe. Vector is a plain
    Python list to keep this layer decoupled from NumPy.
    """

    chunk_id: UUID
    document_id: UUID
    document_title: str
    page_id: UUID
    page_number: int
    chunk_index: int
    section_path: list[str]
    text: str
    vector: list[float]
    embedding_model_name: str
    embedding_dim: int


def fetch_candidate_rows(
    db: Session,
    *,
    owner_id: UUID,
    document_ids: Sequence[UUID] | None = None,
) -> list[CandidateRow]:
    """Return the queryable candidate set for one user.

    Args:
        db: Sync SQLAlchemy session. Caller owns the transaction.
        owner_id: Forces `documents.owner_id = :owner_id` in the JOIN —
                  security boundary lives here, not at the caller.
        document_ids: If provided, restrict to these document ids. The
                  list MUST be non-empty — an empty caller-side list
                  means "user selected nothing" and is rejected by the
                  analyser before reaching this function (saves a SQL
                  round trip). `None` means "all of the user's READY
                  documents".

    Returns:
        All (chunk, embedding, document_title, page_number) pairs that
        pass the status + ownership filters, in `(document_id, page_number,
        chunk_index)` order so the retriever's deterministic ranking is
        debuggable.

    Invariants:
        - Every returned row's `document_id` belongs to `owner_id`.
        - Every returned row's embedding `status='succeeded'`.
        - No row from a doc whose status is anything other than `READY`.
    """
    stmt = (
        select(
            DocumentChunk.id.label("chunk_id"),
            Document.id.label("document_id"),
            Document.title.label("document_title"),
            DocumentPage.id.label("page_id"),
            DocumentPage.page_number.label("page_number"),
            DocumentChunk.chunk_index.label("chunk_index"),
            DocumentChunk.section_path.label("section_path"),
            DocumentChunk.text.label("text"),
            DocumentEmbedding.vector.label("vector"),
            DocumentEmbedding.model_name.label("embedding_model_name"),
            DocumentEmbedding.dim.label("embedding_dim"),
        )
        .join(DocumentPage, DocumentPage.id == DocumentChunk.page_id)
        .join(Document, Document.id == DocumentPage.document_id)
        .join(DocumentEmbedding, DocumentEmbedding.chunk_id == DocumentChunk.id)
        .where(
            Document.owner_id == owner_id,
            Document.status == DocumentStatus.ready,
            DocumentEmbedding.status == EmbeddingStatus.succeeded,
        )
    )

    if document_ids is not None:
        # Convert to a tuple so SQLAlchemy inlines the IN clause
        # cleanly across SQLite / Postgres.
        stmt = stmt.where(Document.id.in_(tuple(document_ids)))

    stmt = stmt.order_by(
        Document.id.asc(),
        DocumentPage.page_number.asc(),
        DocumentChunk.chunk_index.asc(),
    )

    rows = db.execute(stmt).all()
    return [
        CandidateRow(
            chunk_id=row.chunk_id,
            document_id=row.document_id,
            document_title=row.document_title,
            page_id=row.page_id,
            page_number=row.page_number,
            chunk_index=row.chunk_index,
            section_path=list(row.section_path or []),
            text=row.text,
            vector=list(row.vector),
            embedding_model_name=row.embedding_model_name,
            embedding_dim=row.embedding_dim,
        )
        for row in rows
    ]


__all__ = ["CandidateRow", "fetch_candidate_rows"]
