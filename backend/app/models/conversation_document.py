"""Join table: which documents ground a given conversation.

Composite PK `(conversation_id, document_id)` so:

- A document can be added to many conversations.
- A conversation can select many documents.
- The pair is naturally unique; re-adding a doc is a no-op.

Cascades:
- Conversation deleted → rows here are removed (parent CASCADE).
- Document deleted  → rows here are removed (parent CASCADE).
  Past messages keep their `citations[].document_id` references
  intentionally — see ADR (planned) on conversation scope.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.conversation import Conversation
    from app.models.document import Document


class ConversationDocument(Base):
    """A document selected as part of a conversation's retrieval scope."""

    __tablename__ = "conversation_documents"

    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,
    )

    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships.
    conversation: Mapped["Conversation"] = relationship(
        back_populates="documents", lazy="raise"
    )
    document: Mapped["Document"] = relationship(lazy="raise")

    __table_args__ = (
        # Reverse lookup: which conversations use this document?
        # Cheap for "warn user before delete" UX later.
        Index("ix_conversation_documents_document_id", "document_id"),
    )


__all__ = ["ConversationDocument"]