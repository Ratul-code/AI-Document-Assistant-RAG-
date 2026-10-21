"""Conversation / message database queries.

Conventions (matching the rest of the repository layer):

- Every function that takes a ``conversation_id`` ALSO takes ``owner_id``
  and filters by both. This is the security boundary — never call
  without the owner filter from a user-facing route.
- Reads are async (FastAPI routes go through ``AsyncSession``).
- The Celery embedding / analytics tasks would use the sync variants
  (``run_in_sync_session``); they're defined here for completeness, even
  though v1 doesn't enqueue conversation work to Celery.
- Functions do not commit; the caller controls the transaction.
- Inserts that should bump the conversation's ``last_message_at`` do so
  on the same flush — never split "insert message" from "update
  timestamp" across transactions or pagination reads will be racy.

The doc-selection join table is in a sibling file
(``conversation_documents.py``) but lives in the same logical repo.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Sequence
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.conversation import Conversation, MessageRole
from app.models.conversation_document import ConversationDocument
from app.models.conversation_message import ConversationMessage


# ============================================================
# Conversation CRUD — async (HTTP routes)
# ============================================================


async def create_conversation(
    db: AsyncSession,
    *,
    owner_id: UUID,
    title: str | None = None,
    scope_all_documents: bool = False,
) -> Conversation:
    """Insert a fresh ``conversations`` row and return it.

    The associated ``conversation_documents`` rows (if any) are written
    via ``set_conversation_documents``. Kept separate so the caller can
    pass in a list it just validated.

    ``last_message_at`` defaults via the DB column default to the
    creation timestamp; we refresh so callers see the real value.
    """
    conv = Conversation(
        owner_id=owner_id,
        title=title,
        scope_all_documents=scope_all_documents,
    )
    db.add(conv)
    await db.flush()
    await db.refresh(conv)
    return conv


async def get_conversation(
    db: AsyncSession,
    conversation_id: UUID,
    *,
    owner_id: UUID,
) -> Conversation | None:
    """Fetch one conversation, owner-scoped.

    Returns ``None`` for both "doesn't exist" and "owned by someone
    else" — callers should treat ``None`` as 404 to avoid leaking the
    existence of other users' chats.
    """
    stmt = select(Conversation).where(
        Conversation.id == conversation_id,
        Conversation.owner_id == owner_id,
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_conversations(
    db: AsyncSession,
    *,
    owner_id: UUID,
    limit: int = 50,
    offset: int = 0,
) -> Sequence[Conversation]:
    """Sidebar list: most recently active chats first."""
    stmt = (
        select(Conversation)
        .where(Conversation.owner_id == owner_id)
        .order_by(Conversation.last_message_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    return result.scalars().all()


async def count_conversations(
    db: AsyncSession,
    *,
    owner_id: UUID,
) -> int:
    stmt = select(func.count()).select_from(Conversation).where(
        Conversation.owner_id == owner_id
    )
    result = await db.execute(stmt)
    return int(result.scalar_one())


async def update_conversation(
    db: AsyncSession,
    conversation_id: UUID,
    *,
    owner_id: UUID,
    title: str | None = None,
    scope_all_documents: bool | None = None,
) -> Conversation | None:
    """Patch metadata on a conversation.

    Both fields are optional; ``None`` means "leave unchanged". Only the
    fields explicitly passed are written. Document selection is owned by
    ``set_conversation_documents`` to keep the join-table logic separate.
    """
    conv = await get_conversation(db, conversation_id, owner_id=owner_id)
    if conv is None:
        return None

    values: dict[str, Any] = {}
    if title is not None:
        conv.title = title
        values["title"] = title
    if scope_all_documents is not None:
        conv.scope_all_documents = scope_all_documents
        values["scope_all_documents"] = scope_all_documents

    if values:
        values["updated_at"] = datetime.now(tz=conv.updated_at.tzinfo)
        await db.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(**values)
        )
        await db.flush()
        await db.refresh(conv)
    return conv


async def delete_conversation(
    db: AsyncSession,
    conversation_id: UUID,
    *,
    owner_id: UUID,
) -> bool:
    """Bulk-delete a conversation by id+owner. Returns ``True`` iff a row was removed.

    Messages + doc-selection rows cascade. Same single-statement pattern
    as ``delete_document`` so we never trip the async lazy-loader.
    """
    stmt = (
        delete(Conversation)
        .where(
            Conversation.id == conversation_id,
            Conversation.owner_id == owner_id,
        )
        .execution_options(synchronize_session="fetch")
    )
    result = await db.execute(stmt)
    await db.flush()
    return result.rowcount > 0


# ============================================================
# Document selection (join table)
# ============================================================


async def set_conversation_documents(
    db: AsyncSession,
    *,
    conversation_id: UUID,
    owner_id: UUID,
    document_ids: Sequence[UUID],
) -> None:
    """Replace the document selection for a conversation.

    Validates that the conversation exists AND belongs to ``owner_id``
    first. The previous selection is wiped via a single DELETE, then a
    bulk INSERT writes the new set. Composite PK prevents duplicate rows
    in the same call.

    On Postgres we use ``ON CONFLICT DO NOTHING`` to make a re-add a
    no-op rather than an integrity error. On SQLite the bulk INSERT
    simply ignores the PK conflict.
    """
    conv = await get_conversation(db, conversation_id, owner_id=owner_id)
    if conv is None:
        return

    # Wipe existing selection.
    await db.execute(
        delete(ConversationDocument).where(
            ConversationDocument.conversation_id == conversation_id
        )
    )

    if not document_ids:
        return

    rows = [
        {"conversation_id": conversation_id, "document_id": did}
        for did in dict.fromkeys(document_ids)  # dedupe, preserve order
    ]

    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        stmt = (
            pg_insert(ConversationDocument)
            .values(rows)
            .on_conflict_do_nothing(
                index_elements=["conversation_id", "document_id"]
            )
        )
    else:
        stmt = pg_insert(ConversationDocument).values(rows)

    await db.execute(stmt)
    await db.flush()


async def list_conversation_document_ids(
    db: AsyncSession,
    *,
    conversation_id: UUID,
    owner_id: UUID,
) -> list[UUID]:
    """The doc selection for a conversation (or ``[]`` if all-docs).

    The empty-list caller case is handled by the caller checking
    ``Conversation.scope_all_documents`` separately.
    """
    conv = await get_conversation(db, conversation_id, owner_id=owner_id)
    if conv is None:
        return []

    stmt = select(ConversationDocument.document_id).where(
        ConversationDocument.conversation_id == conversation_id
    )
    result = await db.execute(stmt)
    return [row[0] for row in result.all()]


async def count_conversation_documents(
    db: AsyncSession,
    *,
    conversation_id: UUID,
) -> int:
    """One-query count for sidebar "📎 N docs" badges."""
    stmt = select(func.count()).select_from(ConversationDocument).where(
        ConversationDocument.conversation_id == conversation_id
    )
    result = await db.execute(stmt)
    return int(result.scalar_one())


# ============================================================
# Messages
# ============================================================


async def insert_message(
    db: AsyncSession,
    *,
    conversation_id: UUID,
    owner_id: UUID,
    role: MessageRole,
    content: str,
    citations: list[dict[str, Any]] | None = None,
    error_code: str | None = None,
) -> ConversationMessage | None:
    """Insert one turn + bump ``last_message_at`` in the SAME flush.

    Validates ownership before insert. Returns ``None`` if the
    conversation doesn't exist or belongs to someone else.

    The timestamp bump lives in this function so every code path that
    writes a message (user submit, orchestrator persistence, batch
    backfill) updates the sidebar sort key consistently.
    """
    conv = await get_conversation(db, conversation_id, owner_id=owner_id)
    if conv is None:
        return None

    msg = ConversationMessage(
        conversation_id=conversation_id,
        role=role,
        content=content,
        citations=citations,
        error_code=error_code,
    )
    db.add(msg)

    # SQLAlchemy will pick up the FK relationship and set
    # `Conversation.last_message_at` server-side via the column default;
    # but we want it to reflect *this* message's timestamp, so set it
    # in Python now and flush.
    conv.last_message_at = datetime.now(tz=conv.last_message_at.tzinfo)
    conv.updated_at = conv.last_message_at

    await db.flush()
    await db.refresh(msg)
    return msg


async def list_messages(
    db: AsyncSession,
    *,
    conversation_id: UUID,
    owner_id: UUID,
    limit: int = 200,
    offset: int = 0,
    order: Literal["asc", "desc"] = "asc",
) -> Sequence[ConversationMessage]:
    """Ordered history: oldest → newest by default, owner-scoped.

    The ``order`` argument controls sort direction:

      - ``"asc"``  — oldest → newest (default; used by history readers
                     that want natural reading order, and by paginated
                     "load older" requests beyond the first page).
      - ``"desc"`` — newest → oldest (used by the first page of the
                     chat history, so the client can render the
                     newest turns immediately and then prepend older
                     ones via "load more" without an extra round trip).

    The route should always check the conversation exists for the owner
    via ``get_conversation`` before calling this — but we also re-check
    here so a misused caller doesn't leak another user's messages.
    """
    conv = await get_conversation(db, conversation_id, owner_id=owner_id)
    if conv is None:
        return []

    direction = (
        ConversationMessage.created_at.desc()
        if order == "desc"
        else ConversationMessage.created_at.asc()
    )
    stmt = (
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation_id)
        .order_by(direction)
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    return result.scalars().all()


async def count_messages(
    db: AsyncSession,
    *,
    conversation_id: UUID,
    owner_id: UUID,
) -> int:
    conv = await get_conversation(db, conversation_id, owner_id=owner_id)
    if conv is None:
        return 0
    stmt = select(func.count()).select_from(ConversationMessage).where(
        ConversationMessage.conversation_id == conversation_id
    )
    result = await db.execute(stmt)
    return int(result.scalar_one())


# ============================================================
# Sync variants — for trusted Celery / admin scripts.
# ============================================================
#
# Routes are async-only; these exist so future background jobs (e.g.
# nightly summarisation, retraction tools) can use the sync session
# pattern established in `embedding_search.py`. Never call from a route.


def create_conversation_sync(
    db: Session,
    *,
    owner_id: UUID,
    title: str | None = None,
    scope_all_documents: bool = False,
) -> Conversation:
    conv = Conversation(
        owner_id=owner_id,
        title=title,
        scope_all_documents=scope_all_documents,
    )
    db.add(conv)
    db.flush()
    db.refresh(conv)
    return conv


def delete_conversation_any_owner_sync(db: Session, conversation_id: UUID) -> bool:
    stmt = (
        delete(Conversation)
        .where(Conversation.id == conversation_id)
        .execution_options(synchronize_session="fetch")
    )
    result = db.execute(stmt)
    db.flush()
    return result.rowcount > 0


__all__ = [
    "count_conversation_documents",
    "count_conversations",
    "count_messages",
    "create_conversation",
    "create_conversation_sync",
    "delete_conversation",
    "delete_conversation_any_owner_sync",
    "get_conversation",
    "insert_message",
    "list_conversation_document_ids",
    "list_conversations",
    "list_messages",
    "set_conversation_documents",
    "update_conversation",
]