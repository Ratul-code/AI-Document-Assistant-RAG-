"""Sync SQLAlchemy engine + session factory for Celery workers.

FastAPI uses async SQLAlchemy (app.db.session) because FastAPI handlers
are async. Celery workers, on the other hand, run in plain sync
functions — mixing async SQLAlchemy into a sync task body is painful
and fragile. So workers get their own sync engine built from
`DATABASE_URL_SYNC`.

Why not reuse the async engine?
- AsyncSession requires an event-loop. Workers don't have one.
- `asyncio.run(session.execute(...))` works but creates an event loop
  per call, leaking asyncio state and producing surprising errors.
- Sync SQLAlchemy is the boring, correct choice for background jobs.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeVar

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_T = TypeVar("_T")


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _to_sync_url(url: str) -> str:
    """Convert an async SQLAlchemy URL to its sync equivalent.

    For SQLite, swaps `sqlite+aiosqlite:///` -> `sqlite:///`. Async
    drivers (`aiosqlite`, `psycopg` async, etc.) need an event loop and
    can't run from a sync worker thread.

    For Postgres, the same `postgresql+psycopg://` URL is already
    sync-capable (psycopg v3 is sync). Other async dialects would
    need handling here when they show up.
    """
    if url.startswith("sqlite+aiosqlite:///"):
        return "sqlite:///" + url[len("sqlite+aiosqlite:///"):]
    return url


def _build_sync_engine() -> Engine:
    settings = get_settings()
    # Try the explicit sync URL first (Postgres in prod). If that
    # fails — typically because dev has SQLite configured and the
    # Postgres URL in DATABASE_URL_SYNC is just a placeholder — fall
    # back to the effective async URL, converted to its sync form.
    candidates: list[str] = []
    if settings.database_url_sync:
        candidates.append(settings.database_url_sync)
    candidates.append(_to_sync_url(settings.effective_database_url))

    last_err: Exception | None = None
    for url in candidates:
        connect_args: dict = {}
        engine_kwargs: dict = {"future": True}
        is_sqlite = url.startswith("sqlite")
        if is_sqlite:
            connect_args["check_same_thread"] = False
        else:
            # Postgres / Supabase-specific tuning. See the async engine
            # in `app/db/session.py` for the full rationale. The same
            # four guards apply here: server-side `statement_timeout`
            # so a runaway query cannot hold a pooler connection,
            # `pool_pre_ping` so the first query through a dead pooled
            # connection transparently reconnects instead of raising,
            # `pool_recycle=3600` to keep the pool fresh against
            # the Supabase pooler's ~1 hour idle-kill, and
            # `prepare_threshold=0` to disable psycopg3's server-side
            # prepared-statement cache.
            #
            # The sync engine's current `DATABASE_URL_SYNC` points at
            # port 5432 (session-mode pooler / direct), where prepared
            # statements are session-scoped and safe — so this last
            # guard is not strictly required today. We set it anyway
            # as defense in depth: if `DATABASE_URL_SYNC` is ever
            # repointed at a transaction-mode pooler (port 6543), or
            # if a future migration moves Celery to share the async
            # URL, this engine will not start throwing
            # `DuplicatePreparedStatement: "_pg3_N" already exists`.
            connect_args["options"] = "-c statement_timeout=30000"
            # `prepare_threshold=None` (NOT 0) disables psycopg3's
            # server-side prepared-statement cache. The sync engine's
            # current `DATABASE_URL_SYNC` points at port 5432
            # (session-mode pooler / direct), where prepared
            # statements are session-scoped and safe — so this last
            # guard is not strictly required today. We set it anyway
            # as defense in depth: if `DATABASE_URL_SYNC` is ever
            # repointed at a transaction-mode pooler (port 6543), or
            # if a future migration moves Celery to share the async
            # URL, this engine will not start throwing
            # `DuplicatePreparedStatement: "_pg3_N" already exists`.
            #
            # IMPORTANT: `prepare_threshold=0` is NOT the same as
            # `None`. With `0`, psycopg3's gating check
            # (`count >= self.prepare_threshold` in
            # `_preparing.py:74`) is true on the very first execution
            # of every query, so every statement gets prepared on
            # its first run — the worst case behind a transaction
            # pooler. `None` short-circuits the check and never
            # prepares.
            connect_args["prepare_threshold"] = None
            engine_kwargs["pool_pre_ping"] = True
            engine_kwargs["pool_recycle"] = 3600
        try:
            engine = create_engine(url, connect_args=connect_args, **engine_kwargs)
            # SQLite needs `PRAGMA foreign_keys=ON` per connection for
            # `ON DELETE CASCADE` to actually fire. Without it, the
            # sync engine (used by Celery workers) silently leaves
            # orphan `document_logs` / `document_pages` / `document_chunks`
            # / `document_embeddings` rows behind when a document is
            # deleted in dev (SQLite). Postgres enforces cascades
            # natively, so this is a no-op for it.
            if is_sqlite:
                @event.listens_for(engine, "connect")
                def _enable_sqlite_fks(dbapi_connection, _connection_record):  # noqa: ANN001
                    cursor = dbapi_connection.cursor()
                    try:
                        cursor.execute("PRAGMA foreign_keys=ON")
                    finally:
                        cursor.close()
            # Liveness probe — open one connection and run SELECT 1.
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return engine
        except Exception as exc:
            last_err = exc
            continue

    raise RuntimeError(
        f"No usable sync database URL. Tried: {candidates!r}. "
        f"Last error: {last_err!r}"
    )


def get_sync_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _build_sync_engine()
    return _engine


def get_sync_session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_sync_engine(),
            expire_on_commit=False,
            class_=Session,
            autoflush=False,
        )
    return _SessionLocal


@contextmanager
def sync_session_scope() -> Iterator[Session]:
    """Yield a sync Session that commits on success, rolls back on error.

    Use this from Celery tasks like:

        with sync_session_scope() as db:
            doc = db.get(Document, doc_id)
            ...

    Prefer `run_in_sync_session(...)` when you just want to run a single
    block of work and return its result — it removes the `with` ceremony.
    """
    factory = get_sync_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run_in_sync_session(
    func: Callable[..., _T],
    *args: Any,
    **kwargs: Any,
) -> _T:
    """Run `func(db, *args, **kwargs)` inside a sync session scope.

    Opens a session, hands it to `func` as the first positional argument,
    commits on success, rolls back on exception. Returns whatever `func`
    returns. Use this when you want to skip the `with` block and just run
    a piece of work inline:

        def load_doc(db: Session, doc_id: UUID) -> Document | None:
            return db.get(Document, doc_id)

        doc = run_in_sync_session(load_doc, doc_uuid)

    Equivalent to:

        with sync_session_scope() as db:
            doc = load_doc(db, doc_uuid)
    """
    with sync_session_scope() as db:
        return func(db, *args, **kwargs)


def reset_for_tests() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
