"""Sync SQLAlchemy operations on DocumentEmbedding.

The embedding worker pulls a worklist of `pending` rows, flips them to
`processing`, writes the vector, and flips them to `succeeded` (or
`failed` / `skipped`). Reads are scoped through `document_chunks ->
document_pages -> documents` so they never leak across owners.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.document_embedding import DocumentEmbedding, EmbeddingStatus
from app.models.document_page import DocumentPage


@dataclass(frozen=True)
class EmbeddingSpec:
    """Embedding payload ready to persist."""

    chunk_id: UUID
    model_name: str
    dim: int
    vector: list[float]
    distance_metric: str | None = None
    confidence: float | None = None


def upsert_embedding(
    db: Session,
    spec: EmbeddingSpec,
    *,
    status: EmbeddingStatus = EmbeddingStatus.succeeded,
    error_message: str | None = None,
) -> UUID:
    """Insert an embedding row, or update the existing one for the chunk.

    One embedding per chunk — re-running the worker on the same chunk
    updates in place instead of duplicating.
    """
    existing = db.execute(
        select(DocumentEmbedding).where(DocumentEmbedding.chunk_id == spec.chunk_id)
    ).scalar_one_or_none()

    if existing is None:
        db.execute(
            insert(DocumentEmbedding),
            [
                {
                    "chunk_id": spec.chunk_id,
                    "model_name": spec.model_name,
                    "dim": spec.dim,
                    "vector": spec.vector,
                    "distance_metric": spec.distance_metric,
                    "status": status,
                    "confidence": spec.confidence,
                    "error_message": error_message,
                }
            ],
        )
        # Return the row id by re-querying.
        return db.execute(
            select(DocumentEmbedding.id).where(
                DocumentEmbedding.chunk_id == spec.chunk_id
            )
        ).scalar_one()

    db.execute(
        update(DocumentEmbedding)
        .where(DocumentEmbedding.id == existing.id)
        .values(
            model_name=spec.model_name,
            dim=spec.dim,
            vector=spec.vector,
            distance_metric=spec.distance_metric,
            status=status,
            confidence=spec.confidence,
            error_message=error_message,
        )
    )
    return existing.id


def list_pending_chunk_ids(
    db: Session, document_id: UUID, *, owner_id: UUID, limit: int = 100
) -> list[UUID]:
    """Return chunk ids for a document that still need an embedding.

    Owner-scoped via JOIN document_pages -> documents. Skips chunks
    that already have a succeeded embedding.
    """
    stmt = (
        select(DocumentChunk.id)
        .join(DocumentPage, DocumentPage.id == DocumentChunk.page_id)
        .join(Document, Document.id == DocumentPage.document_id)
        .outerjoin(
            DocumentEmbedding,
            DocumentEmbedding.chunk_id == DocumentChunk.id,
        )
        .where(
            DocumentPage.document_id == document_id,
            Document.owner_id == owner_id,
            DocumentChunk.needs_embedding.is_(True),
            (DocumentEmbedding.id.is_(None))
            | (DocumentEmbedding.status != EmbeddingStatus.succeeded),
        )
        .order_by(
            DocumentPage.page_number.asc(),
            DocumentChunk.chunk_index.asc(),
        )
        .limit(limit)
    )
    return [row[0] for row in db.execute(stmt).all()]


__all__ = [
    "EmbeddingSpec",
    "list_pending_chunk_ids",
    "upsert_embedding",
]
