"""SQLAlchemy model for document chunk embeddings.

One row per embedding. The vector itself is stored as a JSON array of
floats (not a native `pgvector` type) so the model is portable across
Postgres and SQLite; a future migration can swap in `pgvector` without
changing the worker contract. The worker writes/reads via the repo.

Status lifecycle:
    pending -> processing -> succeeded | failed | skipped

`pending` is set when the embedding worker claims a chunk; `succeeded`
once the vector is persisted; `failed` / `skipped` are terminal.
"""

from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.types import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class EmbeddingStatus(str, enum.Enum):
    """Lifecycle of the embedding pipeline for a single chunk."""

    pending = "pending"
    processing = "processing"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"


class DocumentEmbedding(Base):
    """Embedding row produced by the embedding worker."""

    __tablename__ = "document_embeddings"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    chunk_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Identifier of the model that produced the embedding
    # (e.g. "text-embedding-3-small"). Lets us invalidate embeddings
    # when the model changes.
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Vector dimensionality, captured so the worker can sanity-check
    # the model output.
    dim: Mapped[int] = mapped_column(Integer, nullable=False)

    # The embedding vector. JSON on both Postgres + SQLite for
    # portability — replace with `pgvector` later.
    vector: Mapped[list[float]] = mapped_column(JSON, nullable=False)

    # Worker-reported distance metric (e.g. "cosine"). May be NULL
    # until the first successful run.
    distance_metric: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )

    status: Mapped[EmbeddingStatus] = mapped_column(
        Enum(EmbeddingStatus, name="embedding_status"),
        nullable=False,
        default=EmbeddingStatus.pending,
        index=True,
    )

    # Optional engine-side quality score (cosine sim of best hit, etc.).
    # NULL on failure / skipped rows.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)

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
        Index("uq_document_embeddings_chunk", "chunk_id", unique=True),
    )


__all__ = ["DocumentEmbedding", "EmbeddingStatus"]
