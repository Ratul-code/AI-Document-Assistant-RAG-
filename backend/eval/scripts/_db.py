"""Hermetic DB + pubsub helpers for the eval scripts.

The eval must not touch the dev `app.db` and must not require a live
Redis. This module provides:

* `hermetic_engine()` — a fresh in-memory SQLite with the full
  SQLAlchemy schema (`Base.metadata.create_all`).
* `persisted_engine(path)` — a SQLite file engine, used by the
  `ingest_corpus.py` → "run" split so the seeded corpus survives
  across CLI invocations. The schema is created if absent.
* `hermetic_session()` — a context manager that yields a sync
  Session bound to the hermetic engine.
* `patch_pubsub_noop()` / `unpatch_pubsub()` — replaces
  `app.services.pubsub.ws_publisher.publish_event` with a no-op for
  the duration of an eval run. The Celery workers and
  `_process_one_document` call `publish_event` indirectly through
  the status-event helpers; a Redis-less env would otherwise log
  warnings and slow the run down.
* `build_hermetic_env()` — context manager that bundles both: a
  fresh engine + a pubsub patch, torn down on exit.

The patch is **process-local** via `monkeypatch`-style attribute
swaps; it is reverted by `unpatch_pubsub()` (also called by the
autouse test fixture at teardown, so a test that forgets to clean
up doesn't leak into the next one).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

# IMPORTANT: importing the model modules here is what actually registers
# the table definitions on `Base.metadata`. Without these imports,
# `Base.metadata.create_all()` runs against an empty metadata and the
# SQL store has zero tables.
from app.db.base import Base  # noqa: F401
from app.models.document import Document, DocumentStatus  # noqa: F401
from app.models.document_chunk import DocumentChunk  # noqa: F401
from app.models.document_embedding import DocumentEmbedding, EmbeddingStatus  # noqa: F401
from app.models.document_page import DocumentPage  # noqa: F401
from app.models.user import User  # noqa: F401
from app.services.pubsub import ws_publisher

_PATCHED: bool = False
_ORIGINAL_PUBLISH: object | None = None


def hermetic_engine() -> Engine:
    """Build a fresh in-memory SQLite engine and create all tables.

    Each call returns a new engine — useful for tests that want a
    guaranteed-empty DB. The eval scripts use a single engine for
    the lifetime of a CLI invocation via `build_hermetic_env()`.

    Uses ``StaticPool`` so the in-memory database persists across
    connections — without it, every new connection opens a private
    in-memory database and ``Base.metadata.create_all`` happens on a
    connection that no later session can read from.
    """
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def persisted_engine(path: Path) -> Engine:
    """Build a SQLite file engine at `path` and ensure the schema exists.

    Used by `ingest_corpus.py` to seed the corpus once and persist it
    to disk so subsequent `run_retrieval_eval.py` invocations can
    read it without re-seeding. The parent directory is created if
    missing. If the file exists and already has tables, this is a
    no-op for the schema (no DROP) — so a populated DB is safe to
    point a fresh engine at.

    SQLite + a single-process eval runner is fine with the default
    pool. We do not need `StaticPool` because every connection reads
    the same on-disk file. `check_same_thread=False` lets SQLAlchemy
    share the engine across thread boundaries (eval scripts open
    sessions sequentially, but Celery sync helpers sometimes cross
    threads).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine


@contextmanager
def hermetic_session(engine: Engine) -> Iterator[Session]:
    """Yield a sync Session bound to `engine`. Commits on exit.

    Mirrors the `sync_session_scope()` helper in
    `app/db/sync_session.py` but does not consult the cached
    `get_sync_engine()` — the eval wants an explicit, hermetic
    engine every time.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _noop_publish_event(user_id: UUID, envelope: dict) -> int:  # pragma: no cover
    """Drop-in for `publish_event` during a hermetic eval run."""
    return 0


def patch_pubsub_noop() -> None:
    """Replace `ws_publisher.publish_event` with a no-op.

    Idempotent: re-patching is a no-op so a re-entry of the context
    manager doesn't stack patches.
    """
    global _PATCHED, _ORIGINAL_PUBLISH
    if _PATCHED:
        return
    _ORIGINAL_PUBLISH = ws_publisher.publish_event
    ws_publisher.publish_event = _noop_publish_event  # type: ignore[assignment]
    _PATCHED = True


def unpatch_pubsub() -> None:
    """Restore the original `publish_event` (if a patch is active)."""
    global _PATCHED, _ORIGINAL_PUBLISH
    if not _PATCHED:
        return
    assert _ORIGINAL_PUBLISH is not None
    ws_publisher.publish_event = _ORIGINAL_PUBLISH  # type: ignore[assignment]
    _PATCHED = False
    _ORIGINAL_PUBLISH = None


@contextmanager
def build_hermetic_env() -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    """Bundle: fresh in-memory SQLite + pubsub no-op.

    Yields (engine, SessionLocal) so the caller can open as many
    sessions as it needs. On exit, disposes the engine and unpatches
    pubsub.

    Usage:
        with build_hermetic_env() as (engine, SessionLocal):
            with SessionLocal() as db:
                ...
    """
    patch_pubsub_noop()
    engine = hermetic_engine()
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    try:
        yield engine, SessionLocal
    finally:
        engine.dispose()
        unpatch_pubsub()


__all__ = [
    "build_hermetic_env",
    "hermetic_engine",
    "hermetic_session",
    "patch_pubsub_noop",
    "persisted_engine",
    "unpatch_pubsub",
]
