"""SQLAlchemy model for the per-document activity log.

The `document_logs` table is a timeline of events for each document —
one row per "thing that happened" (download completed, page parsed,
chunking progress, batch embedded, …). The ingestion workers
(`app.tasks.document` and `app.tasks.embedding`) write rows as they
make progress; the dialog reads them via
`GET /api/v1/documents/{id}/logs` and the WS pushes new rows in real
time so the activity feed prepends them as they happen.

Schema mirrors the Alembic migration in
`alembic/versions/20260731_1030_create_document_logs.py`. The migration
created the table before the model file did, so this file's only job
is to register the table in `Base.metadata` — no DDL change.

`metadata_json` is stored as `Text` (a JSON string) per the migration,
not a native JSON column. The Pydantic schema on the wire handles the
`Text ↔ dict` round-trip via a field validator, so call sites use a
plain `dict` (or `None`).
"""

from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DocumentLogEventType(str, enum.Enum):
    """Catalog of events emitted by the ingestion pipeline.

    Values are the public contract — the frontend `DocumentLogRow`
    component maps these to icons and colors (see
    `frontend/src/components/documents/document-log-row.tsx`).
    Adding a new value is safe: unknown event_types render with a
    neutral fallback on the client. Renaming an existing value is a
    breaking change and needs a coordinated frontend update.

    The per-doc feed is intentionally minimal: one row per stage
    (uploaded, parsing, chunking, embedding). Intermediate
    breadcrumbs (`download.started`, `parse.started`, `chunking.started`,
    `embedding.started`, per-page `chunking.progress`, per-batch
    `embedding.progress`, lifecycle `ready`/`failed`) were erased so
    the activity log reads as a clean pipeline summary.

    NOTE on the value spellings: these are underscore-separated
    (`parsing_failed`, not `parsing.failed`). That matches both what
    the workers actually write into `document_logs.event_type` and
    the `case` labels the frontend switches on. Earlier revisions of
    this enum used dotted values, which never reached the wire —
    anything matching on `DocumentLogEventType.<x>.value` against
    real rows would silently fail to match.
    """

    # --- Upload phase (one row, emitted at confirm time) ---
    uploaded = "uploaded"  # /confirm landed; doc is UPLOADED

    # --- parse phase ---
    parsing = "parsing"  # N pages parsed (terminal row)
    parsing_failed = "parsing_failed"  # parse raised; doc -> FAILED

    # --- chunking phase ---
    chunking = "chunking"  # total chunks committed (terminal row)
    chunking_failed = "chunking_failed"  # chunker raised; doc -> FAILED

    # --- embedding phase ---
    embedding = "embedding"  # {succeeded, failed} (terminal row)
    embedding_failed = "embedding_failed"  # all batches failed; doc -> FAILED

    # --- Catch-all for unknown-stage failures in process_document ---
    processing_failed = "processing_failed"  # stage unknown; doc -> FAILED

    # --- Operator / out-of-band ---
    document_deleted = "document_deleted"  # user deleted the doc


class DocumentLog(Base):
    """One row per timeline event for a document.

    The composite index on `(document_id, created_at)` powers the
    per-doc activity feed; `(owner_id, created_at)` powers the
    cross-doc global feed. Both are defined in the original migration
    and re-declared here for ORM-level sanity (SQLAlchemy doesn't
    auto-discover indexes from migrations).
    """

    __tablename__ = "document_logs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    # CASCADE on document so wiping a doc also wipes its timeline.
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Redundant with document -> owner, but denormalised so the
    # global feed can filter by owner without joining documents.
    # CASCADE on user so user deletion also drops their logs.
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # String column (not a SQLAlchemy Enum) so a future addition to
    # DocumentLogEventType is a code-only change — no Alembic ALTER
    # on an enum type. Indexed for the (rare) per-type query.
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # Short human-readable message ("Parsed 24 pages", "Embedding batch 4 of 20").
    # Optional so callers that only want structured metadata can omit it.
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # JSON-serialised dict with per-event structured detail
    # ({"bytes": 1234}, {"chunks_total": 1000, "model": "..."}).
    # Stored as Text per the migration; the schema layer parses it.
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_document_logs_document_id_created_at", "document_id", "created_at"),
        Index("ix_document_logs_owner_id_created_at", "owner_id", "created_at"),
    )


__all__ = ["DocumentLog", "DocumentLogEventType"]
