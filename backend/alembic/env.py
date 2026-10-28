"""Alembic env.py — connects our async SQLAlchemy setup to Alembic migrations."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.db.base import Base

# Importing the models module ensures all model classes are registered with
# Base.metadata before autogenerate walks it.
import app.models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override sqlalchemy.url with the URL from our settings.
#
# WHICH URL THIS USES
# -------------------
# `effective_database_url` resolves to `DATABASE_URL` (the async URL —
# Supabase's transaction pooler on port 6543), falling back to
# `DATABASE_URL_FALLBACK` (SQLite) when that is unset.
#
# Note that `DATABASE_URL_SYNC` (the session pooler on port 5432) is
# NOT used here, even though its field description in
# `app/core/config.py` says "Sync URL for Alembic". That comment is
# misleading: this project's DDL — including the native Postgres ENUM
# types created via `postgresql.ENUM(create_type=True)` in the
# `create_document_embeddings`, `add_ocr_status_to_document_pages`
# and `create_conversations` migrations — runs correctly through the
# transaction pooler. This was verified end-to-end by running the full
# migration chain from an empty database against Supabase through
# port 6543: all four ENUM types were created and all 9 revisions
# applied.
#
# `DATABASE_URL_SYNC` remains in use for the Celery workers, which
# need a genuinely synchronous SQLAlchemy URL (their `psycopg` driver
# is sync, while the app's is async).
#
# If a future Supabase pooler release does restrict DDL on the
# transaction pooler, the fix is to point this at a session-mode URL
# AND switch this module to a synchronous engine (`create_engine`
# instead of `async_engine_from_config`) — the `psycopg` dialect in
# `DATABASE_URL_SYNC` is the sync driver, so it cannot be handed to
# the async engine used below as-is.
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.effective_database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a live connection)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode using an async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
