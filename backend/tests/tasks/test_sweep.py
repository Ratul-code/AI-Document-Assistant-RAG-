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

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.document import Document, DocumentStatus
from app.models.user import User
from app.tasks.sweep import sweep_stuck_learning_documents


@pytest.fixture
def sync_db_session():
    """Yield a sync session bound to an in-memory SQLite DB."""
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
def patched_session_factory(sync_db_session):
    """Make the sweep's `get_session_factory()` return an async session
    bound to the *same* in-memory DB the test is seeding.

    SQLite in-memory + aiosqlite lets multiple connections see the
    same DB only when they share a URI like `file:test?mode=memory&...`
    OR when we serialize access through a single connection. The
    simplest reliable thing for tests is to expose an AsyncSession
    backed by a *sync* connection — i.e. use `sessionmaker` from the
    async side and run on the same thread.

    We use `async_sessionmaker(class_=AsyncSession, sync_session=...)`
    via the trick below: open the in-memory DB with `StaticPool` so a
    single connection is reused across threads/loops, then wrap it in
    an async session.
    """
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    # Use the SAME engine as the sync fixture so writes via sync_db_session
    # are visible to the async factory.
    sync_db_session.close()
    new_sync = sessionmaker(bind=engine, expire_on_commit=False)()
    # Recreate rows from the original session in the new one (the
    # original is closed). We do this by re-running the test's seed
    # later via a hook — instead, simpler: rebind sync_db_session too.
    sync_db_session.bind = engine
    sync_db_session.close()
    # Reopen the session on the shared engine.
    fresh = sessionmaker(bind=engine, expire_on_commit=False)()
    # Replace the original fixture's session with the shared one.
    # Tests have already seeded against the *original* session; since
    # we re-create the schema on the new engine, the seed is gone.
    # So tests must seed via this `fresh` session. To keep tests
    # simple, return both.
    async_factory = async_sessionmaker(
        bind=engine, expire_on_commit=False
    )

    yield fresh, async_factory


def _seed_doc(
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
    session.flush()
    doc.updated_at = updated_at
    session.commit()
    return doc


def test_sweep_requeues_learning_docs_older_than_threshold(
    patched_session_factory,
) -> None:
    """A LEARNING doc whose `updated_at` is older than the threshold
    must be re-enqueued to the embedding task."""
    sync_session, async_factory = patched_session_factory
    now = datetime.now(timezone.utc)
    stuck_doc = _seed_doc(
        sync_session,
        status=DocumentStatus.learning,
        updated_at=now - timedelta(minutes=60),
    )

    with patch("app.tasks.sweep.get_session_factory", return_value=async_factory), \
         patch("app.tasks.sweep.celery_app") as mock_celery:
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
    assert call.args[1] == [str(stuck_doc.id), str(stuck_doc.owner_id)]


def test_sweep_skips_learning_docs_below_threshold(
    patched_session_factory,
) -> None:
    sync_session, async_factory = patched_session_factory
    now = datetime.now(timezone.utc)
    _seed_doc(
        sync_session,
        status=DocumentStatus.learning,
        updated_at=now - timedelta(minutes=5),
    )

    with patch("app.tasks.sweep.get_session_factory", return_value=async_factory), \
         patch("app.tasks.sweep.celery_app") as mock_celery:
        result = sweep_stuck_learning_documents(stuck_threshold_minutes=30)

    assert result["ok"] is True
    assert result["found"] == 0
    assert result["requeued"] == 0
    mock_celery.send_task.assert_not_called()


def test_sweep_ignores_non_learning_docs(patched_session_factory) -> None:
    """READY / FAILED / UPLOADED / PROCESSING docs must never be
    re-enqueued — they're either terminal or handled by other paths."""
    sync_session, async_factory = patched_session_factory
    now = datetime.now(timezone.utc)
    for status in (
        DocumentStatus.ready,
        DocumentStatus.failed,
        DocumentStatus.uploaded,
        DocumentStatus.processing,
    ):
        _seed_doc(
            sync_session,
            status=status,
            updated_at=now - timedelta(hours=24),
        )

    with patch("app.tasks.sweep.get_session_factory", return_value=async_factory), \
         patch("app.tasks.sweep.celery_app") as mock_celery:
        result = sweep_stuck_learning_documents(stuck_threshold_minutes=30)

    assert result["found"] == 0
    assert result["requeued"] == 0
    mock_celery.send_task.assert_not_called()


def test_sweep_respects_max_docs_per_run(patched_session_factory) -> None:
    """The hard cap prevents one tick from enqueueing a thundering
    herd if a long outage leaves many stuck docs at once."""
    sync_session, async_factory = patched_session_factory
    now = datetime.now(timezone.utc)
    for _ in range(5):
        _seed_doc(
            sync_session,
            status=DocumentStatus.learning,
            updated_at=now - timedelta(minutes=60),
        )

    with patch("app.tasks.sweep.get_session_factory", return_value=async_factory), \
         patch("app.tasks.sweep.celery_app") as mock_celery:
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
    sync_session, async_factory = patched_session_factory
    now = datetime.now(timezone.utc)
    docs = [
        _seed_doc(
            sync_session,
            status=DocumentStatus.learning,
            updated_at=now - timedelta(minutes=60),
        )
        for _ in range(3)
    ]

    call_count = {"n": 0}

    def fake_send_task(name, args, **_kwargs):
        # First call raises; the rest succeed.
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("broker hiccup")
        return None

    with patch("app.tasks.sweep.get_session_factory", return_value=async_factory), \
         patch("app.tasks.sweep.celery_app") as mock_celery:
        mock_celery.send_task.side_effect = fake_send_task
        result = sweep_stuck_learning_documents(stuck_threshold_minutes=30)

    assert result["found"] == 3
    assert result["requeued"] == 2
    assert mock_celery.send_task.call_count == 3
    sent_ids = {
        call.args[1][0]
        for call in mock_celery.send_task.call_args_list
    }
    assert sent_ids == {str(d.id) for d in docs}
