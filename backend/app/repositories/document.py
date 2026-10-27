"""Document-related database queries.

Conventions:
- Every function that takes a `document_id` ALSO takes `owner_id` and
  filters by both. This enforces the security boundary at the data
  layer — even if a route forgets to check ownership, the query returns
  no rows for the wrong user.
- Functions do not commit; the caller (route / Celery task) controls
  the transaction boundary. The `get_db` FastAPI dependency commits on
  success, so most route handlers don't need to call commit explicitly.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.document import Document, DocumentStatus

# ----------------------------------------------------------------------
# Create
# ----------------------------------------------------------------------


async def create_document(
    db: AsyncSession,
    *,
    owner_id: UUID,
    title: str,
    filename: str,
    mime_type: str,
    file_size_bytes: int,
    storage_bucket: str,
    storage_path: str,
    document_id: UUID | None = None,
) -> Document:
    """Insert a new document row with status=PROCESSING.

    Pass `document_id` to control the row's primary key explicitly. This
    is useful when you need the id BEFORE the insert (e.g. so you can
    build a storage_path that includes it and hand that path to an
    external service like Supabase before the DB write). If omitted,
    the DB / model default generates a UUID.

    Returns the persisted Document (with id, created_at, updated_at
    populated by the DB).
    """
    doc = Document(
        id=document_id,
        owner_id=owner_id,
        title=title,
        filename=filename,
        mime_type=mime_type,
        file_size_bytes=file_size_bytes,
        storage_bucket=storage_bucket,
        storage_path=storage_path,
        status=DocumentStatus.processing,
    )
    db.add(doc)
    await db.flush()
    await db.refresh(doc)
    return doc


# ----------------------------------------------------------------------
# Read
# ----------------------------------------------------------------------


async def get_document(
    db: AsyncSession,
    document_id: UUID,
    *,
    owner_id: UUID,
) -> Document | None:
    """Fetch a document by id, scoped to a specific owner.

    Returns None if the document doesn't exist OR if it belongs to a
    different user. Callers should treat None as 404, not 403, to avoid
    leaking the existence of other users' documents.
    """
    stmt = select(Document).where(
        Document.id == document_id,
        Document.owner_id == owner_id,
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_document_any_owner(
    db: AsyncSession,
    document_id: UUID,
) -> Document | None:
    """Fetch a document by id without owner filtering.

    Use ONLY for admin / system tasks (e.g. Celery worker that has
    already verified the task payload). Never call from a user-facing
    route — use `get_document` with `owner_id` instead.
    """
    return await db.get(Document, document_id)


async def list_documents(
    db: AsyncSession,
    *,
    owner_id: UUID,
    status: DocumentStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Sequence[Document]:
    """List a user's documents, optionally filtered by status.

    Ordered by created_at DESC (newest first).
    """
    stmt = select(Document).where(Document.owner_id == owner_id)
    if status is not None:
        stmt = stmt.where(Document.status == status)
    stmt = stmt.order_by(Document.created_at.desc()).limit(limit).offset(offset)
    result = await db.execute(stmt)
    return result.scalars().all()


async def list_ready_documents(
    db: AsyncSession,
    *,
    owner_id: UUID,
) -> Sequence[Document]:
    """List the user's `READY` documents — i.e. queryable documents.

    Used by the analysis pipeline as the default scope when the client
    didn't pin `document_ids`. Per ADR-011, `READY` strictly means
    "queryable": all chunks have succeeded embeddings.

    Returns docs in stable order (id ASC) so the embedding_search
    candidate set is deterministic per call.
    """
    stmt = (
        select(Document)
        .where(
            Document.owner_id == owner_id,
            Document.status == DocumentStatus.ready,
        )
        .order_by(Document.id.asc())
    )
    result = await db.execute(stmt)
    return result.scalars().all()


# ----------------------------------------------------------------------
# Update
# ----------------------------------------------------------------------


async def update_status(
    db: AsyncSession,
    document_id: UUID,
    *,
    owner_id: UUID,
    status: DocumentStatus,
    error_message: str | None = None,
    uploaded_at: datetime | None = None,
    file_hash: str | None = None,
) -> Document | None:
    """Transition a document to a new status.

    Pass `error_message` to mark failure, `uploaded_at` to mark upload
    complete, `file_hash` if the client provided one. All optional
    fields are only written if provided.

    Returns the updated Document, or None if the document doesn't exist
    or doesn't belong to the owner.
    """
    doc = await get_document(db, document_id, owner_id=owner_id)
    if doc is None:
        return None

    doc.status = status
    if error_message is not None:
        doc.error_message = error_message
    if uploaded_at is not None:
        doc.uploaded_at = uploaded_at
    if file_hash is not None:
        doc.file_hash = file_hash

    await db.flush()
    await db.refresh(doc)
    return doc


async def mark_failed(
    db: AsyncSession,
    document_id: UUID,
    *,
    owner_id: UUID,
    error_message: str,
) -> Document | None:
    """Convenience: set status=FAILED with an error message.

    Truncates the error message to fit the column (1000 chars).
    """
    truncated = error_message[:1000]
    return await update_status(
        db,
        document_id,
        owner_id=owner_id,
        status=DocumentStatus.failed,
        error_message=truncated,
    )


async def update_document(
    db: AsyncSession,
    document_id: UUID,
    *,
    owner_id: UUID,
    title: str | None = None,
) -> Document | None:
    """Update editable user-facing attributes on a document.

    Currently only `title` is editable. The function signature uses
    `Optional` per-field so future editable attributes can be added
    without breaking existing callers — each new field is `None`-by-
    default, and `None` means "leave it alone".

    Owner-scoped via `get_document`. Returns `None` if the document
    doesn't exist OR belongs to a different user; callers should
    treat `None` as 404 (don't leak existence of other users' docs).

    Flushes but does not commit; the caller's transaction boundary
    (FastAPI dependency commits on response) decides when the change
    actually lands. Matches the rest of the repository's convention.
    """
    doc = await get_document(db, document_id, owner_id=owner_id)
    if doc is None:
        return None

    if title is not None:
        doc.title = title

    await db.flush()
    await db.refresh(doc)
    return doc


async def delete_document(
    db: AsyncSession,
    document_id: UUID,
    *,
    owner_id: UUID,
) -> bool:
    """Delete a document row, scoped to its owner.

    Returns True if a row was deleted, False if the document either
    doesn't exist or belongs to a different user. Callers should treat
    False as 404 — we deliberately do NOT distinguish "not found" from
    "wrong owner" so the endpoint doesn't leak existence of other users'
    documents.

    The DB session is flushed but not committed; the caller's
    transaction boundary (FastAPI dependency commits on response) decides
    when the delete actually lands. This matches the rest of the
    repository's convention.

    Implementation note: we use a single bulk DELETE statement with
    `(id, owner_id)` in the WHERE clause rather than load-then-delete
    via the ORM. Two reasons:
    1. Owner-scoping lives in the query itself — no chance of detaching
       a previously-loaded instance and tripping SQLAlchemy's
       lazy-loading / MissingGreenlet traps on async sessions.
    2. One round-trip instead of SELECT + DELETE, even on a miss.
    """
    stmt = (
        delete(Document)
        .where(
            Document.id == document_id,
            Document.owner_id == owner_id,
        )
        .execution_options(synchronize_session="fetch")
    )
    result = await db.execute(stmt)
    await db.flush()
    return cast(CursorResult, result).rowcount > 0


def delete_document_any_owner(db: Session, document_id: UUID) -> bool:
    """Sync delete-by-id (no owner filter) for trusted callers.

    Mirrors `delete_document` but uses the sync `Session` so the Celery
    worker can call it from `run_in_sync_session`. Use ONLY from
    trusted contexts (Celery tasks that have already verified the
    payload, admin scripts). Never call from a user-facing route —
    that path must use `delete_document` with `owner_id`.
    """
    stmt = (
        delete(Document)
        .where(Document.id == document_id)
        .execution_options(synchronize_session="fetch")
    )
    result = db.execute(stmt)
    db.flush()
    return cast(CursorResult, result).rowcount > 0


# ----------------------------------------------------------------------
# Quota / aggregation helpers
# ----------------------------------------------------------------------


async def count_user_documents(
    db: AsyncSession,
    *,
    owner_id: UUID,
    status: DocumentStatus | None = None,
) -> int:
    """How many documents this user has.

    If `status` is provided, only documents in that status are counted.
    """
    stmt = select(func.count()).select_from(Document).where(Document.owner_id == owner_id)
    if status is not None:
        stmt = stmt.where(Document.status == status)
    result = await db.execute(stmt)
    return int(result.scalar_one())


async def total_user_storage_bytes(
    db: AsyncSession,
    *,
    owner_id: UUID,
) -> int:
    """Sum of file_size_bytes across all this user's documents."""
    stmt = select(func.coalesce(func.sum(Document.file_size_bytes), 0)).where(
        Document.owner_id == owner_id
    )
    result = await db.execute(stmt)
    return int(result.scalar_one())


async def count_stuck_processing(
    db: AsyncSession,
    *,
    older_than: datetime,
) -> int:
    """How many documents have been in PROCESSING since before `older_than`.

    Used by a cleanup job to find uploads that the client never confirmed.
    Returns the count; the caller can then mark them as failed.
    """
    stmt = (
        select(func.count())
        .select_from(Document)
        .where(
            Document.status == DocumentStatus.processing,
            Document.created_at < older_than,
        )
    )
    result = await db.execute(stmt)
    return int(result.scalar_one())


async def find_stuck_processing(
    db: AsyncSession,
    *,
    older_than: datetime,
    limit: int = 100,
) -> Sequence[Document]:
    """Like count_stuck_processing but returns the actual rows.

    For a periodic Celery beat task that marks them as failed.
    """
    stmt = (
        select(Document)
        .where(
            Document.status == DocumentStatus.processing,
            Document.created_at < older_than,
        )
        .limit(limit)
    )
    result = await db.execute(stmt)
    return result.scalars().all()


async def count_stuck_learning(
    db: AsyncSession,
    *,
    older_than: datetime,
) -> int:
    """Count docs in LEARNING since before `older_than`.

    The embedding sweep task uses this to decide whether anything is
    stuck. A doc in LEARNING means parsing + chunking are done but the
    embedding worker hasn't finalized it — three things can cause this:
      * `process_document` crashed (or its send_task failed) before
        enqueueing `embed_chunks_for_document`.
      * The embedding Celery worker process died mid-run.
      * The broker dropped the embedding task message.
    All three are recoverable by re-enqueueing the embedding task;
    `_process_one_document` is idempotent (skips chunks that already
    have a succeeded embedding).
    """
    stmt = (
        select(func.count())
        .select_from(Document)
        .where(
            Document.status == DocumentStatus.learning,
            Document.updated_at < older_than,
        )
    )
    result = await db.execute(stmt)
    return int(result.scalar_one())


async def find_stuck_learning(
    db: AsyncSession,
    *,
    older_than: datetime,
    limit: int = 100,
) -> Sequence[Document]:
    """Return LEARNING docs older than `older_than`.

    Embedding sweep task uses this to find docs whose embedding work
    never finished (or whose embedding worker died mid-run). Re-running
    `embed_chunks_for_document` on these is safe and idempotent.
    """
    stmt = (
        select(Document)
        .where(
            Document.status == DocumentStatus.learning,
            Document.updated_at < older_than,
        )
        .limit(limit)
    )
    result = await db.execute(stmt)
    return result.scalars().all()
