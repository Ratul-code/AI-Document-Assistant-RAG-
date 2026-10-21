"""SQLAlchemy model for messages in a conversation.

One row per turn (one user question OR one assistant answer). Two rows
make one exchange. We never persist partial streaming tokens — the
assistant row is written when the orchestrator's `AnalysisDoneEvent`
fires (or an `AnalysisErrorEvent` for the error variant).

`citations` is JSON so the same payload the REST endpoint returned also
sits in the DB — re-opening a chat just re-reads these rows.

`error_code` is only set on assistant rows that resulted in an error
(NO_DOCUMENTS_READY, PROVIDER_ERROR, ...). It powers a per-bubble retry
UI on the frontend.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base
from app.models.conversation import MessageRole

if TYPE_CHECKING:
    from app.models.conversation import Conversation


class ConversationMessage(Base):
    """A single message in a conversation — either from the user or the assistant."""

    __tablename__ = "conversation_messages"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )

    role: Mapped[MessageRole] = mapped_column(
        Enum(MessageRole, name="message_role"),
        nullable=False,
    )

    # Full text. For user messages: the question they typed (≤2000 chars,
    # already validated by QueryRequest). For assistant messages: the
    # concatenated answer produced by the LLM stream.
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Citation list for assistant messages. None/[] for user messages.
    # JSONB on Postgres (queryable), plain JSON on SQLite for dev/tests.
    citations: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
        default=None,
    )

    # Optional assistant-side error. Maps onto AnalysisErrorCode values
    # (NO_DOCUMENTS_READY / PROVIDER_MISMATCH / PROVIDER_ERROR / CANCELLED
    # / INTERNAL / UNAUTHORIZED). NULL on successful turns.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships.
    conversation: Mapped[Conversation] = relationship(back_populates="messages", lazy="raise")

    __table_args__ = (
        # Chat-history fetch: SELECT ... WHERE conversation_id=? ORDER BY created_at.
        Index(
            "ix_conversation_messages_conversation_created_at",
            "conversation_id",
            "created_at",
        ),
    )


__all__ = ["ConversationMessage"]
