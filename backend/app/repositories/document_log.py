"""Sync + async SQLAlchemy operations on the document activity log.

Two consumers with two different session flavours:

  - Celery workers (`app.tasks.document`, `app.tasks.embedding`) call
    `add_log(...)` with a sync `Session` so they can write log rows
    in the same transaction as the chunk / embedding work they
    describe. Committing together means a chunk insert and its log
    row become visible atomically — no race where the WS push
    references a log that hasn't committed yet.

  - The async FastAPI route `GET /api/v1/documents/{id}/logs` reads
    via `list_logs_for_document` / `count_logs_for_document` with an
    `AsyncSession`, so the HTTP path stays async-native.

Owner-scope: every read filters by `owner_id` (in addition to
`document_id`) so a future caller can't accidentally leak across
users. `add_log` is internal — only the workers call it and they
already have the owner_id from the document they just loaded.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.document_log import DocumentLog

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Sync write path (Celery workers)
# ---------------------------------------------------------------------


def add_log(
    db: Session,
    *,
    document_id: UUID,
    owner_id: UUID,
    event_type: str,
    message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> UUID | None:
    """Insert one log row and return its id.

    The caller owns the transaction (the worker uses `run_in_sync_session`,
    which commits on exit). We `flush()` so the new row is queryable
    within the same transaction — the embedding worker's batch loop
    needs the row visible to its next `list_pending_chunk_ids` call.

    `metadata` is JSON-encoded into the `metadata_json` `Text` column
    per the original migration. We swallow encode errors and write
    NULL instead: a log row is observability, not authoritative state,
    and a non-serialisable dict must not break the worker.

    Returns the new row's id, or None if the insert silently failed.
    """
    try:
        metadata_json = (
            json.dumps(metadata, default=str, separators=(",", ":"))
            if metadata is not None
            else None
        )
    except (TypeError, ValueError):
        logger.warning(
            "add_log: metadata not JSON-serialisable, storing NULL event_type=%s",
            event_type,
        )
        metadata_json = None

    row = DocumentLog(
        document_id=document_id,
        owner_id=owner_id,
        event_type=event_type,
        message=message,
        metadata_json=metadata_json,
    )
    db.add(row)
    db.flush()
    return row.id


# ---------------------------------------------------------------------
# Async write path (FastAPI route)
# ---------------------------------------------------------------------


async def add_log_async(
    db: AsyncSession,
    *,
    document_id: UUID,
    owner_id: UUID,
    event_type: str,
    message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> UUID | None:
    """Insert one log row using the caller's AsyncSession and return its id.

    Use this from FastAPI endpoints that already hold an `AsyncSession`
    (the standard `DbDep`). Two reasons this exists alongside the sync
    `add_log`:

      1. **Same-transaction commit.** `add_log` (sync) opens a fresh
         session bound to a DIFFERENT engine (`app.db.sync_session`,
         used by Celery workers). If the endpoint has just inserted
         the document row using the async engine, that row is uncommitted
         when this function runs. A separate sync session cannot see
         the uncommitted row, so the `document_logs.document_id`
         FOREIGN KEY fails — and the endpoint's outer `try/except`
         silently swallows the failure. The log row never lands in
         the DB, and the user sees "LOG 0" in the detail dialog even
         after the doc is fully ready.

         Using the same AsyncSession means the log row flushes into
         the same transaction as the document row and the FK is
         satisfied.

      2. **SQLite write-lock collision.** SQLite serialises writes
         and only one connection at a time can hold the write lock.
         The async engine and sync engine are TWO distinct connection
         pools. While the async session holds a connection mid-request,
         a concurrent `run_in_sync_session` call from the same request
         either blocks on `busy_timeout` or errors with
         `database is locked` — again, silently swallowed by the
         endpoint's outer handler.

    Returns the new row's id, or None if the insert silently failed.
    """
    try:
        metadata_json = (
            json.dumps(metadata, default=str, separators=(",", ":"))
            if metadata is not None
            else None
        )
    except (TypeError, ValueError):
        logger.warning(
            "add_log_async: metadata not JSON-serialisable, storing NULL " "event_type=%s",
            event_type,
        )
        metadata_json = None

    row = DocumentLog(
        document_id=document_id,
        owner_id=owner_id,
        event_type=event_type,
        message=message,
        metadata_json=metadata_json,
    )
    db.add(row)
    await db.flush()
    return row.id


# ---------------------------------------------------------------------
# Async read path (FastAPI route)
# ---------------------------------------------------------------------


async def list_logs_for_document(
    db: AsyncSession,
    *,
    document_id: UUID,
    owner_id: UUID,
    limit: int = 20,
    offset: int = 0,
) -> list[DocumentLog]:
    """Return one page of log rows for the document, newest first.

    Owner-scope is enforced by the `WHERE owner_id = :owner_id` clause
    even though `document_id` already implies a single document. The
    redundant filter is cheap insurance against a future caller passing
    a `document_id` that was looked up without owner-scope.

    Index hit: the composite `(document_id, created_at)` index from
    the original migration. Newest-first is `ORDER BY created_at DESC`
    so the activity feed shows the latest event at the top.
    """
    stmt = (
        select(DocumentLog)
        .where(
            DocumentLog.document_id == document_id,
            DocumentLog.owner_id == owner_id,
        )
        .order_by(DocumentLog.created_at.desc(), DocumentLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await db.execute(stmt)).scalars().all())


async def count_logs_for_document(
    db: AsyncSession,
    *,
    document_id: UUID,
    owner_id: UUID,
) -> int:
    """Count all log rows for one document, owner-scoped.

    Used to populate the `total` field of the paginated response so
    the client can render "Showing 1–20 of N" without a second call.
    Same WHERE clause as `list_logs_for_document` so the count
    matches the list exactly.
    """
    stmt = (
        select(func.count())
        .select_from(DocumentLog)
        .where(
            DocumentLog.document_id == document_id,
            DocumentLog.owner_id == owner_id,
        )
    )
    return int((await db.execute(stmt)).scalar_one())


async def delete_logs_for_document(
    db: AsyncSession,
    *,
    document_id: UUID,
    owner_id: UUID,
) -> int:
    """Delete every log row for one document, owner-scoped.

    Defence-in-depth for `DELETE /documents/{id}`. The DB schema declares
    `ON DELETE CASCADE` on `document_logs.document_id`, so a Postgres
    backend (or a SQLite backend with `PRAGMA foreign_keys=ON`) handles
    the cleanup automatically. This helper exists so the application
    layer explicitly removes the rows regardless of the backend's FK
    enforcement state — if a future migration changes the cascade
    behaviour, or if the connection pool ever opens a session without
    the pragma, the activity log never gets orphaned.

    Owner-scope is enforced in the WHERE clause even though
    `document_id` is unique — cheap insurance against a future caller
    passing a `document_id` looked up without owner filtering.

    Returns the number of rows deleted (0 if there were no logs).
    """
    stmt = (
        delete(DocumentLog)
        .where(
            DocumentLog.document_id == document_id,
            DocumentLog.owner_id == owner_id,
        )
        .execution_options(synchronize_session="fetch")
    )
    result = await db.execute(stmt)
    # A DELETE/UPDATE statement always yields a CursorResult, but the
    # AsyncSession.execute() signature is typed as the broader Result[Any].
    # Cast to narrow so `.rowcount` type-checks.
    return int(cast(CursorResult, result).rowcount or 0)


__all__ = [
    "add_log",
    "add_log_async",
    "count_logs_for_document",
    "delete_logs_for_document",
    "list_logs_for_document",
]
