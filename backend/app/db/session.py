"""Async SQLAlchemy engine, session factory, and FastAPI dependency."""

from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _build_engine() -> AsyncEngine:
    settings = get_settings()
    url = settings.effective_database_url

    connect_args: dict = {}
    engine_kwargs: dict = {}
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        connect_args["check_same_thread"] = False
    else:
        # Postgres / Supabase-specific tuning.
        #
        # - `statement_timeout=30s` is a Postgres server-side guardrail
        #   that cancels any query that runs longer than 30 seconds. It
        #   is set via the connection `options` parameter, which the
        #   psycopg driver forwards as a libpq connection option. Without
        #   it a single stuck query can hold a connection from the
        #   Supabase pooler (the pooler is sized for short transactions)
        #   and starve the rest of the app.
        #
        # - `pool_pre_ping=True` issues a cheap `SELECT 1` before
        #   handing out a pooled connection. The Supabase pooler (and
        #   PgBouncer in front of Postgres) silently drops idle
        #   connections after ~1 hour; without pre-ping the first query
        #   through a dead connection raises `OperationalError: server
        #   closed the connection unexpectedly` and the user sees a 500.
        #   Pre-ping turns that into a transparent reconnect.
        #
        # - `pool_recycle=3600` is belt-and-braces with pre-ping — it
        #   forces SQLAlchemy to discard connections older than 1 hour
        #   from the pool so they are never handed out again, even if
        #   the pooler hasn't killed them yet.
        #
        # - `prepare_threshold=0` disables psycopg3's server-side
        #   prepared-statement cache. This is REQUIRED behind
        #   PgBouncer / Supavisor in **transaction** pooling mode
        #   (port 6543): the pooler multiplexes many client
        #   transactions over a single backend connection, and a
        #   `PREPARE name AS ...` issued by one client transaction
        #   collides with the same name from another, surfacing as
        #   `psycopg.errors.DuplicatePreparedStatement: prepared
        #   statement "_pg3_0" already exists`. The cost is a tiny
        #   per-query re-plan (~microseconds) which is invisible
        #   compared to network latency. The sync engine doesn't need
        #   this — `DATABASE_URL_SYNC` points at the direct port
        #   (5432) where prepared statements are session-scoped and
        #   safe.
        #
        #   We pass this as a `connect_args` entry (not an engine
        #   kwarg) because psycopg3's `prepare_threshold` is a
        #   `connect()` parameter, not a SQLAlchemy engine option.
        #   SQLAlchemy 2.0.51 + psycopg3 3.x does not expose it as
        #   a top-level engine kwarg, so threading it through
        #   `connect_args` is the only path.
        connect_args["options"] = "-c statement_timeout=30000"
        # `prepare_threshold=None` (NOT 0) disables psycopg3's
        # server-side prepared-statement cache. This is REQUIRED behind
        # PgBouncer / Supavisor in **transaction** pooling mode (port
        # 6543): the pooler multiplexes many client transactions over
        # a single backend connection, and a `PREPARE name AS ...`
        # issued by one client transaction collides with the same
        # name from another, surfacing as
        # `psycopg.errors.DuplicatePreparedStatement: prepared
        # statement "_pg3_0" already exists`. The cost is a tiny
        # per-query re-plan (~microseconds) which is invisible
        # compared to network latency. The sync engine doesn't need
        # this — `DATABASE_URL_SYNC` points at the direct port
        # (5432) where prepared statements are session-scoped and
        # safe.
        #
        # IMPORTANT: `prepare_threshold=0` is NOT the same as `None`.
        # Looking at psycopg3's `_preparing.py:74`, the gating check
        # is `count >= self.prepare_threshold` — with `0`, `count` is
        # 0 on the first execution so the predicate is ALWAYS true and
        # every query is prepared on its very first run, which is the
        # worst case behind a transaction pooler. `None` short-
        # circuits the check at line 63 and never prepares.
        #
        # We pass this as a `connect_args` entry (not an engine
        # kwarg) because psycopg3's `prepare_threshold` is a
        # `connect()` parameter, not a SQLAlchemy engine option.
        # SQLAlchemy 2.0.51 + psycopg3 3.x does not expose it as
        # a top-level engine kwarg, so threading it through
        # `connect_args` is the only path.
        connect_args["prepare_threshold"] = None
        engine_kwargs["pool_pre_ping"] = True
        engine_kwargs["pool_recycle"] = 3600

    # NOTE: do NOT pass `echo=...` here. SQLAlchemy's `echo=True` attaches
    # its own private StreamHandler to the `sqlalchemy.engine` logger
    # that bypasses the root logger's handlers — so it can produce
    # duplicates even when our `configure_logging()` quiets that
    # logger. To see SQL traces, instead set
    # `logging.getLogger("sqlalchemy.engine").setLevel(logging.DEBUG)`.
    engine = create_async_engine(
        url,
        echo=False,
        connect_args=connect_args,
        **engine_kwargs,
    )

    # SQLite does NOT enforce foreign-key constraints (including
    # `ON DELETE CASCADE`) unless `PRAGMA foreign_keys=ON` is issued
    # on every connection. Our migrations declare cascades on
    # `document_logs.document_id`, `document_pages.document_id`,
    # `document_chunks.page_id`, `document_embeddings.chunk_id`, and
    # `conversation_documents.document_id` — but without this pragma
    # those cascades are silently bypassed in dev, leaving orphan rows
    # when a document is deleted. Postgres enforces cascades natively,
    # so this listener is a no-op there.
    #
    # We attach to the underlying sync engine so the cursor and the
    # `connect_args` (`check_same_thread=False`) line up with the
    # connection that SQLAlchemy actually opens.
    if is_sqlite:

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_sqlite_fks(dbapi_connection, _connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

    return engine


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields an AsyncSession, commits on success, rolls
    back on exception, always closes."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close the engine's connection pool. Call from FastAPI shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def reset_for_tests() -> None:
    """Reset cached engine/factory — used by tests when overriding the URL."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None
