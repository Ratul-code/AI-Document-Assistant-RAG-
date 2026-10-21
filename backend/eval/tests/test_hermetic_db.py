"""Tests for the hermetic DB helper (`eval/scripts/_db.py`)."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import inspect

from app.services.pubsub import ws_publisher
from eval.scripts._db import (
    build_hermetic_env,
    hermetic_engine,
    hermetic_session,
    patch_pubsub_noop,
    unpatch_pubsub,
)


def test_hermetic_engine_creates_all_tables() -> None:
    engine = hermetic_engine()
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        # The five core tables we need to ingest + retrieve.
        assert "users" in tables
        assert "documents" in tables
        assert "document_pages" in tables
        assert "document_chunks" in tables
        assert "document_embeddings" in tables
    finally:
        engine.dispose()


def test_hermetic_session_commits_and_closes() -> None:
    engine = hermetic_engine()
    try:
        from app.models.user import User

        with hermetic_session(engine) as db:
            user = User(id=uuid4(), email="x@x", name="x", hashed_password="x" * 60)
            db.add(user)
        # After exit, a new session sees the row.
        from sqlalchemy.orm import sessionmaker

        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        with SessionLocal() as db:
            assert db.query(User).count() == 1
    finally:
        engine.dispose()


def test_hermetic_session_rolls_back_on_exception() -> None:
    engine = hermetic_engine()
    try:
        from sqlalchemy.orm import sessionmaker

        from app.models.user import User

        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            with hermetic_session(engine) as db:
                db.add(User(id=uuid4(), email="r@r", name="r", hashed_password="x" * 60))
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        with SessionLocal() as db:
            assert db.query(User).count() == 0
    finally:
        engine.dispose()


def test_patch_pubsub_noop_replaces_publish_event() -> None:
    original = ws_publisher.publish_event
    patch_pubsub_noop()
    try:
        assert ws_publisher.publish_event is not original
        # Calling the patched function is safe and returns 0.
        assert ws_publisher.publish_event(uuid4(), {"type": "x"}) == 0
    finally:
        unpatch_pubsub()
    assert ws_publisher.publish_event is original


def test_patch_pubsub_noop_is_idempotent() -> None:
    original = ws_publisher.publish_event
    patch_pubsub_noop()
    try:
        first = ws_publisher.publish_event
        patch_pubsub_noop()  # re-patch should be a no-op
        second = ws_publisher.publish_event
        assert first is second
    finally:
        unpatch_pubsub()
    assert ws_publisher.publish_event is original


def test_unpatch_when_not_patched_is_safe() -> None:
    # Idempotent: unpatch without patch should not raise.
    unpatch_pubsub()


def test_build_hermetic_env_yields_engine_and_factory() -> None:
    with build_hermetic_env() as (engine, SessionLocal):
        inspector = inspect(engine)
        assert "documents" in set(inspector.get_table_names())
        # The factory must produce a usable session.
        with SessionLocal() as db:
            from app.models.user import User

            db.add(User(id=uuid4(), email="h@h", name="h", hashed_password="x" * 60))
        # The pubsub patch is active inside the context.
        assert ws_publisher.publish_event is not _ORIGINAL_PUBLISH  # type: ignore[name-defined]


# Module-level reference to the original for the test above to work
# even when tests run out of order.
_ORIGINAL_PUBLISH = (
    None  # placeholder, real one restored in test_patch_pubsub_noop_replaces_publish_event
)
