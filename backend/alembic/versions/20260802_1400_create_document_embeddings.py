"""create document_embeddings

Revision ID: c6d9e0f1a234
Revises: b5c8d9e0f123
Create Date: 2026-08-02 14:00:00.000000

Stores one row per chunk embedding. The vector itself is JSON for
portability — the next round will likely swap to pgvector. Lifecycle
status (pending / processing / succeeded / failed / skipped) lives
here; chunks only carry the `needs_embedding` boolean.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "c6d9e0f1a234"
down_revision: Union[str, None] = "b5c8d9e0f123"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_EMBEDDING_STATUS_VALUES = (
    "pending",
    "processing",
    "succeeded",
    "failed",
    "skipped",
)


_embedding_status_enum = postgresql.ENUM(
    *_EMBEDDING_STATUS_VALUES,
    name="embedding_status",
    create_type=True,
)


def upgrade() -> None:
    bind = op.get_bind()

    # Postgres gets a real ENUM. SQLite stores the values as plain
    # VARCHAR — the column type still works, just without type safety.
    if bind.dialect.name == "postgresql":
        _embedding_status_enum.create(bind, checkfirst=True)
        status_column_type = postgresql.ENUM(
            *_EMBEDDING_STATUS_VALUES,
            name="embedding_status",
            create_type=False,
        )
    else:
        status_column_type = sa.Enum(
            *_EMBEDDING_STATUS_VALUES,
            name="embedding_status",
            create_type=False,
            native_enum=False,
            length=20,
        )

    op.create_table(
        "document_embeddings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("model_name", sa.String(length=100), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        # JSON-as-TEXT on both dialects for portability.
        sa.Column("vector", sa.JSON(), nullable=False),
        sa.Column("distance_metric", sa.String(length=32), nullable=True),
        sa.Column(
            "status", status_column_type, nullable=False, server_default="pending"
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"], ["document_chunks.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        # One embedding per chunk. Re-running the worker on the same
        # chunk updates this row instead of creating a duplicate.
        sa.UniqueConstraint("chunk_id", name="uq_document_embeddings_chunk"),
    )
    op.create_index(
        op.f("ix_document_embeddings_status"),
        "document_embeddings",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_document_embeddings_status"), table_name="document_embeddings"
    )
    op.drop_table("document_embeddings")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        _embedding_status_enum.drop(bind, checkfirst=True)
