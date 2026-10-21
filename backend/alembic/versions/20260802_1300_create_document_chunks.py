"""create document_chunks

Revision ID: b5c8d9e0f123
Revises: a4f7e8b9c012
Create Date: 2026-08-02 13:00:00.000000

One row per chunk produced by the chunker from a parsed page. Stores
the chunk text, its token/char counts, and the section breadcrumb
captured at chunking time. `needs_embedding` is the fast boolean the
embedding worker scans to find work; the lifecycle status itself
lives on `document_embeddings`.

`section_path` is TEXT[] on Postgres (queryable, indexable) and a
JSON-encoded list on SQLite (no native array type there).
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "b5c8d9e0f123"
down_revision: Union[str, None] = "a4f7e8b9c012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # Pick the section_path column type per dialect. Postgres gets a
    # native TEXT[]; SQLite falls back to JSON-as-TEXT.
    if bind.dialect.name == "postgresql":
        section_path_type = postgresql.ARRAY(sa.Text())
    else:
        section_path_type = sa.JSON()

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("section_path", section_path_type, nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "needs_embedding",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("prev_chunk_id", sa.Uuid(), nullable=True),
        sa.Column("next_chunk_id", sa.Uuid(), nullable=True),
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
            ["page_id"], ["document_pages.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["prev_chunk_id"], ["document_chunks.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["next_chunk_id"], ["document_chunks.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        # (page_id, chunk_index) is unique — a chunk's index in its
        # page is stable across re-runs.
        sa.UniqueConstraint(
            "page_id", "chunk_index", name="uq_document_chunks_page_index"
        ),
    )
    op.create_index(
        op.f("ix_document_chunks_page_id"), "document_chunks", ["page_id"], unique=False
    )
    op.create_index(
        op.f("ix_document_chunks_needs_embedding"),
        "document_chunks",
        ["needs_embedding"],
        unique=False,
    )

    # Self-referential FK on prev/next needs the table to exist first.
    # Postgres enforces a check on the deferred constraint; SQLite
    # doesn't, so this is a no-op there.
    if bind.dialect.name == "postgresql":
        op.create_check_constraint(
            "ck_document_chunks_no_self_link",
            "document_chunks",
            "prev_chunk_id IS NULL OR prev_chunk_id <> id",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_constraint(
            "ck_document_chunks_no_self_link", "document_chunks", type_="check"
        )
    op.drop_index(
        op.f("ix_document_chunks_needs_embedding"), table_name="document_chunks"
    )
    op.drop_index(op.f("ix_document_chunks_page_id"), table_name="document_chunks")
    op.drop_table("document_chunks")
