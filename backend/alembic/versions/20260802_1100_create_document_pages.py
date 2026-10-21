"""create document_pages

Revision ID: 8a1c2d3e4f50
Revises: 65cfc10044dd
Create Date: 2026-08-02 11:00:00.000000

Stores one row per parsed page. Text comes from a parser first
(`raw_text`); OCR fills `ocr_text` for scanned pages. `final_text`
is the merged field downstream code reads.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "8a1c2d3e4f50"
down_revision: Union[str, None] = "65cfc10044dd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_pages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("ocr_text", sa.Text(), nullable=True),
        sa.Column("final_text", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "text_source",
            sa.Enum("raw", "ocr", "merged", name="text_source"),
            nullable=False,
            server_default="raw",
        ),
        sa.Column("parser_name", sa.String(length=64), nullable=True),
        sa.Column("parser_version", sa.String(length=32), nullable=True),
        sa.Column("ocr_engine", sa.String(length=64), nullable=True),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
        # JSONB on Postgres, plain TEXT on SQLite (via with_variant in the model).
        sa.Column(
            "page_metadata",
            sa.JSON().with_variant(sa.Text(), "sqlite"),
            nullable=True,
        ),
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
            ["document_id"], ["documents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        # Composite uniqueness: page_number is unique within a document.
        sa.UniqueConstraint(
            "document_id", "page_number", name="uq_document_pages_doc_page"
        ),
    )
    # Foreign-key lookups: "all pages of this document".
    op.create_index(
        op.f("ix_document_pages_document_id"),
        "document_pages",
        ["document_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_document_pages_document_id"), table_name="document_pages")
    op.drop_table("document_pages")
    # Drop the enum type so a future upgrade can recreate it cleanly.
    sa.Enum(name="text_source").drop(op.get_bind(), checkfirst=True)
