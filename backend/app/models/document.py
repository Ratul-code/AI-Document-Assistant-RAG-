"""SQLAlchemy model for uploaded documents.

This is the MINIMAL schema for the upload pipeline. In a later phase we'll
add DocumentSection, Chunk, and Table tables for structured storage.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class DocumentStatus(str, enum.Enum):
    """Lifecycle states for a document.

    Transitions:
        processing  → uploaded   (client successfully completed upload to Supabase)
        uploaded    → learning   (Celery task picked up the file)
        learning    → ready      (AI processing finished)
        any state   → failed     (an error occurred; error_message will be set)
    """

    processing = "processing"  # upload to Supabase in progress
    uploaded = "uploaded"      # file fully stored in Supabase, awaiting processing
    learning = "learning"      # AI processing (Celery task running)
    ready = "ready"            # ready for queries
    failed = "failed"          # error; see error_message


class Document(Base):
    """An uploaded document owned by a user."""

    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    # Ownership
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # File metadata (what the user uploaded)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Storage location (Supabase)
    storage_bucket: Mapped[str] = mapped_column(String(100), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)

    # Lifecycle
    #
    # `native_enum=False, length=20` keeps `status` as a VARCHAR(20) on
    # Postgres and a TEXT CHECK on SQLite — identical to the column
    # produced by the `20260726_1740_create_documents` migration and to
    # the other enums in the project (`DocumentPage.ocr_status`,
    # `DocumentEmbedding.status`). Native Postgres ENUM types require an
    # `ALTER TYPE ... ADD VALUE` round-trip every time a new value is
    # added — this VARCHAR approach lets you add a value with a plain
    # `ALTER TABLE ... DROP CONSTRAINT ... ADD CONSTRAINT ...` migration.
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(
            DocumentStatus,
            name="document_status",
            native_enum=False,
            length=20,
        ),
        nullable=False,
        default=DocumentStatus.processing,
        index=True,
    )
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Timestamps.
    #
    # `created_at` and `updated_at` are stamped at the application
    # layer (Python `default=`) AND have a DB-side `server_default=`
    # for the same reason `Conversation.last_message_at` does (see
    # that model for the full rationale):
    #
    #   - The Python default is required for SQLite, which has no
    #     DDL syntax to ALTER an existing column's DEFAULT in
    #     place — its `CURRENT_TIMESTAMP` returns a *naive* UTC
    #     value, and the app code can't rely on it always arriving
    #     tz-aware across dialects. Stamping in Python with
    #     `datetime.now(timezone.utc)` guarantees the value is
    #     tz-aware on the way out, which is what the Pydantic
    #     schemas and (downstream) the frontend relative-time
    #     formatter expect.
    #   - The DB default remains for Postgres so DB-level tooling
    #     (COPY, raw SQL inserts) still produces a valid timestamp
    #     without knowing the application code.
    #
    # `uploaded_at` is stamped by the `confirm_upload` route with
    # `datetime.now(timezone.utc)` — already correct.
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationship (lazy-loaded; explicit so back-populates could be set on User)
    owner: Mapped["User"] = relationship(back_populates="documents", lazy="raise")