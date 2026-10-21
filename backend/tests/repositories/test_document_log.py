"""Unit tests for the document_log repository helpers.

Covers:
  - `add_log` writes a row with the right column shape (metadata
    JSON-encoded, message optional)
  - `add_log` is non-fatal on a non-serialisable metadata dict
  - `list_logs_for_document` returns rows newest-first, owner-scoped,
    paginated
  - `count_logs_for_document` matches the list's filter exactly
  - Cross-owner reads return empty (no info leak)

The write path (`add_log`) is sync; the read paths
(`list_logs_for_document`, `count_logs_for_document`) are async. We
exercise each in its native session flavour against an in-memory
SQLite engine, with the read-path tests seeding rows directly via
`DocumentLog(...)` so the test stays within a single engine.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.document import Document, DocumentStatus
from app.models.document_log import DocumentLog
from app.models.user import User
from app.repositories import document_log as log_repo

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_session():
    """Sync in-memory SQLite — used to test the `add_log` write path.

    Mirrors how the Celery workers call `add_log` (sync `Session`).
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def async_session_factory():
    """Async in-memory SQLite — used to test the read path."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    try:
        yield factory
    finally:
        asyncio.run(engine.dispose())


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_sync(session, *, owner_email: str = "owner@example.com") -> tuple[UUID, UUID]:
    """Insert one user + one document via the sync session; return (owner_id, doc_id).

    The doc is seeded in `learning` because the test only exercises
    the log surface; the doc's pipeline state doesn't matter.
    """
    user = User(
        id=uuid4(),
        email=owner_email,
        name="Owner",
        hashed_password="x" * 60,
    )
    session.add(user)
    doc = Document(
        id=uuid4(),
        owner_id=user.id,
        title="Log test",
        filename="f.pdf",
        mime_type="application/pdf",
        file_size_bytes=1024,
        storage_bucket="b",
        storage_path=f"p/{uuid4()}",
        status=DocumentStatus.learning,
    )
    session.add(doc)
    session.commit()
    return user.id, doc.id


async def _seed_async(factory, owner_email: str) -> tuple[UUID, UUID]:
    async with factory() as session:
        user = User(
            id=uuid4(),
            email=owner_email,
            name="Owner",
            hashed_password="x" * 60,
        )
        session.add(user)
        doc = Document(
            id=uuid4(),
            owner_id=user.id,
            title="Log test",
            filename="f.pdf",
            mime_type="application/pdf",
            file_size_bytes=1024,
            storage_bucket="b",
            storage_path=f"p/{uuid4()}",
            status=DocumentStatus.learning,
        )
        session.add(doc)
        await session.commit()
        return user.id, doc.id


async def _seed_log_async(
    factory,
    *,
    document_id: UUID,
    owner_id: UUID,
    message: str,
    created_at: datetime,
    event_type: str = "embedding.progress",
    metadata: dict | None = None,
) -> None:
    """Insert one `DocumentLog` row with an explicit `created_at`.

    Direct `session.add(DocumentLog(...))` is used (not `add_log`) so
    the test can pin `created_at` for deterministic ordering — the
    production write path uses `server_default=func.now()` which on
    SQLite truncates to second precision and produces ties.
    """
    async with factory() as session:
        row = DocumentLog(
            document_id=document_id,
            owner_id=owner_id,
            event_type=event_type,
            message=message,
            metadata_json=json.dumps(metadata) if metadata else None,
            created_at=created_at,
        )
        session.add(row)
        await session.commit()


# ---------------------------------------------------------------------------
# add_log tests (sync, mirrors production)
# ---------------------------------------------------------------------------


def test_add_log_writes_row_with_metadata(sync_session) -> None:
    """`add_log` persists the row, JSON-encodes metadata, returns the id."""
    owner_id, doc_id = _seed_sync(sync_session)

    log_id = log_repo.add_log(
        sync_session,
        document_id=doc_id,
        owner_id=owner_id,
        event_type="chunking.progress",
        message="Page 1/3 chunked",
        metadata={"page_index": 1, "pages_total": 3, "chunks_so_far": 12},
    )
    sync_session.commit()

    assert log_id is not None
    row = sync_session.execute(select(DocumentLog).where(DocumentLog.id == log_id)).scalar_one()
    assert row.event_type == "chunking.progress"
    assert row.message == "Page 1/3 chunked"
    # The metadata column is Text (JSON string), not a native dict.
    assert json.loads(row.metadata_json) == {
        "page_index": 1,
        "pages_total": 3,
        "chunks_so_far": 12,
    }


def test_add_log_with_no_message_or_metadata(sync_session) -> None:
    """A minimal call (just event_type) still writes a valid row."""
    owner_id, doc_id = _seed_sync(sync_session)

    log_id = log_repo.add_log(
        sync_session,
        document_id=doc_id,
        owner_id=owner_id,
        event_type="ready",
    )
    sync_session.commit()

    row = sync_session.execute(select(DocumentLog).where(DocumentLog.id == log_id)).scalar_one()
    assert row.message is None
    assert row.metadata_json is None


def test_add_log_swallows_non_serialisable_metadata(sync_session) -> None:
    """A metadata dict that `json.dumps` can't encode even with
    `default=str` (e.g. a self-referencing structure) must not break
    the worker — the row gets written with NULL `metadata_json` and a
    warning is logged. The `default=str` fallback handles most odd
    objects via `__str__`, but a circular reference still raises a
    `ValueError`, so this is the one path that exercises the
    repo's `try/except` guard."""
    owner_id, doc_id = _seed_sync(sync_session)

    recursive: dict = {}
    recursive["self"] = recursive  # type: ignore[assignment]

    log_id = log_repo.add_log(
        sync_session,
        document_id=doc_id,
        owner_id=owner_id,
        event_type="embedding.progress",
        message="batch 1",
        metadata=recursive,
    )
    sync_session.commit()

    assert log_id is not None
    row = sync_session.execute(select(DocumentLog).where(DocumentLog.id == log_id)).scalar_one()
    # The repo falls back to NULL rather than raising — the row is
    # still there for the activity feed.
    assert row.metadata_json is None


# ---------------------------------------------------------------------------
# list / count tests (async, mirrors production read path)
# ---------------------------------------------------------------------------


def test_list_logs_newest_first(async_session_factory) -> None:
    """3 rows with distinct timestamps → list returns them newest first."""

    async def body():
        owner_id, doc_id = await _seed_async(async_session_factory, "newest@example.com")
        base = datetime.now(UTC)
        # 1s apart so the ordering is unambiguous even on SQLite's
        # second-resolution timestamps.
        for i, msg in enumerate(["page 0", "page 1", "page 2"]):
            await _seed_log_async(
                async_session_factory,
                document_id=doc_id,
                owner_id=owner_id,
                message=msg,
                created_at=base + timedelta(seconds=i),
                metadata={"page_index": i},
            )

        async with async_session_factory() as session:
            rows = await log_repo.list_logs_for_document(
                session,
                document_id=doc_id,
                owner_id=owner_id,
            )
            return [r.message for r in rows]

    assert _run(body()) == ["page 2", "page 1", "page 0"]


def test_list_logs_paginates(async_session_factory) -> None:
    """`limit` + `offset` clamp the page; the list respects both."""

    async def body():
        owner_id, doc_id = await _seed_async(async_session_factory, "page@example.com")
        base = datetime.now(UTC)
        for i in range(5):
            await _seed_log_async(
                async_session_factory,
                document_id=doc_id,
                owner_id=owner_id,
                message=f"batch {i}",
                created_at=base + timedelta(seconds=i),
                metadata={"batch_index": i},
            )

        async with async_session_factory() as session:
            page1 = await log_repo.list_logs_for_document(
                session,
                document_id=doc_id,
                owner_id=owner_id,
                limit=2,
                offset=0,
            )
            page2 = await log_repo.list_logs_for_document(
                session,
                document_id=doc_id,
                owner_id=owner_id,
                limit=2,
                offset=2,
            )
            return [r.message for r in page1], [r.message for r in page2]

    p1, p2 = _run(body())
    assert p1 == ["batch 4", "batch 3"]
    assert p2 == ["batch 2", "batch 1"]


def test_list_logs_is_owner_scoped(async_session_factory) -> None:
    """User B asking for user A's logs gets nothing — no info leak."""

    async def body():
        owner_a, doc_a = await _seed_async(async_session_factory, "a@example.com")
        await _seed_log_async(
            async_session_factory,
            document_id=doc_a,
            owner_id=owner_a,
            message="ready",
            created_at=datetime.now(UTC),
            event_type="ready",
        )

        async with async_session_factory() as session:
            rows = await log_repo.list_logs_for_document(
                session,
                document_id=doc_a,
                owner_id=uuid4(),  # not A
            )
            return rows

    assert _run(body()) == []


def test_count_logs_matches_list_filter(async_session_factory) -> None:
    """`count_logs_for_document` uses the same WHERE clause as the list
    helper, so a paginated client can trust the total."""

    async def body():
        owner_id, doc_id = await _seed_async(async_session_factory, "cnt@example.com")
        base = datetime.now(UTC)
        for i in range(4):
            await _seed_log_async(
                async_session_factory,
                document_id=doc_id,
                owner_id=owner_id,
                message=f"b{i}",
                created_at=base + timedelta(seconds=i),
            )

        async with async_session_factory() as session:
            total = await log_repo.count_logs_for_document(
                session, document_id=doc_id, owner_id=owner_id
            )
            page = await log_repo.list_logs_for_document(
                session,
                document_id=doc_id,
                owner_id=owner_id,
                limit=2,
                offset=0,
            )
            return total, len(page)

    total, page_size = _run(body())
    assert total == 4
    assert page_size == 2


def test_count_logs_for_other_user_is_zero(async_session_factory) -> None:
    """`count_logs_for_document` returns 0 for cross-owner access, so
    the paginated response reports total=0 instead of leaking the real
    count of A's logs."""

    async def body():
        owner_a, doc_a = await _seed_async(async_session_factory, "co-a@example.com")
        await _seed_log_async(
            async_session_factory,
            document_id=doc_a,
            owner_id=owner_a,
            message="ready",
            created_at=datetime.now(UTC),
            event_type="ready",
        )

        async with async_session_factory() as session:
            return await log_repo.count_logs_for_document(
                session,
                document_id=doc_a,
                owner_id=uuid4(),
            )

    assert _run(body()) == 0


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
