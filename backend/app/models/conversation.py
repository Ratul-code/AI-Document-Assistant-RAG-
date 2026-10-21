"""SQLAlchemy model for chat conversations.

A conversation groups a thread of Q&A turns (`ConversationMessage`) and
pins a document-selection scope to that thread. Two ways to express the
scope:

- `scope_all_documents=True`  → ground every turn on ALL of the owner's
  READY documents (resolved at query time, not snapshotted here).
- `scope_all_documents=False` → ground only on the rows in
  `ConversationDocument` for this conversation.

The chosen shape (a boolean + a join table) keeps the common case
("chat with one or two docs") explicit and queryable, and lets the
all-docs case skip the join.

`last_message_at` is bumped on every new message; the chat sidebar uses
it (with `owner_id`) for "sort by recent".
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class MessageRole(str, enum.Enum):
    """Who produced a message in a conversation."""

    user = "user"
    assistant = "assistant"


class Conversation(Base):
    """A chat thread owned by one user."""

    __tablename__ = "conversations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    # Ownership. CASCADE so deleting a user removes their conversations.
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Auto-derived from the first user question (truncated) and later
    # editable via PATCH /conversations/{id}.
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # When True, retrieval for this conversation ignores the
    # `ConversationDocument` join table and searches every READY doc the
    # owner has at query time.
    scope_all_documents: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Denormalised "most recent activity" timestamp. Updated by the
    # message-write path on each new `ConversationMessage` insert.
    #
    # Both a Python-side `default=` and a DB-side `server_default=`
    # are set:
    #  - Python default: required for SQLite, which has no DDL syntax
    #    to ALTER an existing column's DEFAULT and whose server_default
    #    is therefore not applied at insert time. Without this the
    #    NOT NULL constraint trips the moment we try to create a row.
    #  - DB default: required for Postgres so DB-level tooling (COPY,
    #    raw SQL inserts) still produces a valid timestamp without
    #    knowing the application code.
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
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

    # Relationships. lazy="raise" so a missing eager-load is loud rather
    # than silently N+1'ing.
    owner: Mapped[User] = relationship(back_populates="conversations", lazy="raise")
    documents: Mapped[list[ConversationDocument]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        lazy="raise",
    )
    messages: Mapped[list[ConversationMessage]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        lazy="raise",
        order_by="ConversationMessage.created_at",
    )

    __table_args__ = (
        # Sidebar listing — fetch the most recent N chats for the user.
        Index(
            "ix_conversations_owner_last_message_at",
            "owner_id",
            "last_message_at",
        ),
    )


__all__ = ["Conversation", "MessageRole"]
