"""Sweep task tests.

Tests `sweep_stuck_learning_documents` end-to-end against an in-memory
SQLite DB. We:
  1. Seed the DB directly with a sync session (mirrors how production
     rows would land there via the API + Celery workers).
  2. Patch `get_session_factory` so the sweep reads from the *same*
     in-memory DB rather than the production-style `app.db` URL.
  3. Patch `celery_app.send_task` so the test doesn't need a live
     broker — we just assert the call signature matches what the
     embedding task expects.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.document import Document, DocumentStatus
from app.models.user import User
from app.tasks.sweep import sweep_stuck_learning_documents


@pytest.fixture
def patched_session_factory():
    """Yield an async session factory backed by an in-memory SQLite DB.

    The sweep task opens its own session via `get_session_factory()`, so
    the factory we hand it MUST be an *async* factory bound to an
    *async* engine — `app/tasks/sweep.py::_find_stuck_documents` does
    `async with session_factory() as db`. Binding an `async_sessionmaker`
    to a sync `create_engine(...)` engine raises
    `ArgumentError: AsyncEngine expected, got Engine(sqlite:///:memory:)`
    at query time.

    `StaticPool` keeps a single connection alive so the async side and
    the seeding side observe the same in-memory database; without it
    each `await` would open a fresh, empty DB and our seeded rows would
    vanish.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    # Create the schema on the async engine. `create_all` is a sync
    # DDL call, so it goes through `run_sync` on the async connection.
    import asyncio

    async def _create_schema():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create_schema())

    yield async_factory

    # Dispose the engine so the in-memory DB is torn down with the test.
    asyncio.run(engine.dispose())


def _run(coro):
    """Run an async coroutine from a sync test."""
    import asyncio

    return asyncio.run(coro)


async def _seed_doc(
    session,
    *,
    status: DocumentStatus,
    updated_at: datetime,
) -> Document:
    """Insert a user + document with a controlled `updated_at`."""
    unique = uuid4().hex[:8]
    owner = User(
        id=uuid4(),
        email=f"sweep-{unique}@example.com",
        name=f"Sweep-{unique}",
        hashed_password="x" * 60,
    )
    session.add(owner)
    doc = Document(
        id=uuid4(),
        owner_id=owner.id,
        title="t",
        filename="f",
        mime_type="text/plain",
        file_size_bytes=10,
        storage_bucket="b",
        storage_path="p",
        status=status,
    )
    session.add(doc)
    await session.flush()
    doc.updated_at = updated_at
    await session.commit()
    return doc


def test_sweep_requeues_learning_docs_older_than_threshold(
    patched_session_factory,
) -> None:
    """A LEARNING doc whose `updated_at` is older than the threshold
    must be re-enqueued to the embedding task."""
    async_factory = patched_session_factory
    now = datetime.now(UTC)

    async def _seed():
        async with async_factory() as session:
            return await _seed_doc(
                session,
                status=DocumentStatus.learning,
                updated_at=now - timedelta(minutes=60),
            )

    stuck_doc = _run(_seed())

    with (
        patch("app.tasks.sweep.get_session_factory", return_value=async_factory),
        patch("app.tasks.sweep.celery_app") as mock_celery,
    ):
        result = sweep_stuck_learning_documents(
            stuck_threshold_minutes=30,
            max_docs_per_run=100,
        )

    assert result["ok"] is True
    assert result["found"] == 1
    assert result["requeued"] == 1

    assert mock_celery.send_task.call_count == 1
    call = mock_celery.send_task.call_args
    assert call.args[0] == "app.tasks.embedding.embed_chunks_for_document"
    # The sweep passes the task args as the `args=` keyword argument,
    # not positionally — see `app/tasks/sweep.py`.
    assert call.kwargs["args"] == [str(stuck_doc.id), str(stuck_doc.owner_id)]


def test_sweep_skips_learning_docs_below_threshold(
    patched_session_factory,
) -> None:
    async_factory = patched_session_factory
    now = datetime.now(UTC)

    async def _seed():
        async with async_factory() as session:
            await _seed_doc(
                session,
                status=DocumentStatus.learning,
                updated_at=now - timedelta(minutes=5),
            )

    _run(_seed())

    with (
        patch("app.tasks.sweep.get_session_factory", return_value=async_factory),
        patch("app.tasks.sweep.celery_app") as mock_celery,
    ):
        result = sweep_stuck_learning_documents(stuck_threshold_minutes=30)

    assert result["ok"] is True
    assert result["found"] == 0
    assert result["requeued"] == 0
    mock_celery.send_task.assert_not_called()


def test_sweep_ignores_non_learning_docs(patched_session_factory) -> None:
    """READY / FAILED / UPLOADED / PROCESSING docs must never be
    re-enqueued — they're either terminal or handled by other paths."""
    async_factory = patched_session_factory
    now = datetime.now(UTC)

    async def _seed():
        async with async_factory() as session:
            for status in (
                DocumentStatus.ready,
                DocumentStatus.failed,
                DocumentStatus.uploaded,
                DocumentStatus.processing,
            ):
                await _seed_doc(
                    session,
                    status=status,
                    updated_at=now - timedelta(hours=24),
                )

    _run(_seed())

    with (
        patch("app.tasks.sweep.get_session_factory", return_value=async_factory),
        patch("app.tasks.sweep.celery_app") as mock_celery,
    ):
        result = sweep_stuck_learning_documents(stuck_threshold_minutes=30)

    assert result["found"] == 0
    assert result["requeued"] == 0
    mock_celery.send_task.assert_not_called()


def test_sweep_respects_max_docs_per_run(patched_session_factory) -> None:
    """The hard cap prevents one tick from enqueueing a thundering
    herd if a long outage leaves many stuck docs at once."""
    async_factory = patched_session_factory
    now = datetime.now(UTC)

    async def _seed():
        async with async_factory() as session:
            for _ in range(5):
                await _seed_doc(
                    session,
                    status=DocumentStatus.learning,
                    updated_at=now - timedelta(minutes=60),
                )

    _run(_seed())

    with (
        patch("app.tasks.sweep.get_session_factory", return_value=async_factory),
        patch("app.tasks.sweep.celery_app") as mock_celery,
    ):
        result = sweep_stuck_learning_documents(
            stuck_threshold_minutes=30,
            max_docs_per_run=2,
        )

    assert result["found"] == 5
    assert result["requeued"] == 2
    assert mock_celery.send_task.call_count == 2


def test_sweep_continues_when_one_enqueue_fails(
    patched_session_factory,
) -> None:
    """A single bad enqueue must not stop the rest of the sweep."""
    async_factory = patched_session_factory
    now = datetime.now(UTC)

    async def _seed():
        docs = []
        async with async_factory() as session:
            for _ in range(3):
                docs.append(
                    await _seed_doc(
                        session,
                        status=DocumentStatus.learning,
                        updated_at=now - timedelta(minutes=60),
                    )
                )
        return docs

    docs = _run(_seed())

    call_count = {"n": 0}

    def fake_send_task(name, args, **_kwargs):
        # First call raises; the rest succeed.
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("broker hiccup")
        return None

    with (
        patch("app.tasks.sweep.get_session_factory", return_value=async_factory),
        patch("app.tasks.sweep.celery_app") as mock_celery,
    ):
        mock_celery.send_task.side_effect = fake_send_task
        result = sweep_stuck_learning_documents(stuck_threshold_minutes=30)

    assert result["found"] == 3
    assert result["requeued"] == 2
    assert mock_celery.send_task.call_count == 3
    sent_ids = {call.kwargs["args"][0] for call in mock_celery.send_task.call_args_list}
    assert sent_ids == {str(d.id) for d in docs}
