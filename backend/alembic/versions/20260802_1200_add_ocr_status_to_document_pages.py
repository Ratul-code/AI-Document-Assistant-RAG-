"""add ocr_status to document_pages

Revision ID: a4f7e8b9c012
Revises: 8a1c2d3e4f50
Create Date: 2026-08-02 12:00:00.000000

Adds the OCR lifecycle column to document_pages. The parser-only round
populates every row as `ocr_status='not_needed'`. The future OCR worker
will flip pages to `pending` -> `processing` -> `succeeded` / `failed` /
`skipped`.

Values:
    not_needed   - parser produced usable text; OCR will not run
    pending      - parser flagged needs_ocr=True; OCR queued
    processing   - OCR worker is currently handling this page
    succeeded    - OCR completed; ocr_text populated, final_text = ocr_text
    failed       - OCR ran and failed; page_metadata.ocr_error has reason
    skipped      - explicitly opted out of OCR (policy decision)

The enum is created with `postgresql.ENUM(create_type=True)` so the
type only exists on Postgres. On SQLite the column stores strings —
SQLite has no native enum.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "a4f7e8b9c012"
down_revision: Union[str, None] = "8a1c2d3e4f50"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OCR_STATUS_VALUES = (
    "not_needed",
    "pending",
    "processing",
    "succeeded",
    "failed",
    "skipped",
)

_ocr_status_enum = postgresql.ENUM(
    *_OCR_STATUS_VALUES,
    name="ocr_status",
    create_type=True,
)


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Create the Postgres enum type if we're on Postgres. SQLite has
    #    no enum; the column is a plain VARCHAR there.
    if bind.dialect.name == "postgresql":
        _ocr_status_enum.create(bind, checkfirst=True)

    column_type = sa.Enum(
        *_OCR_STATUS_VALUES,
        name="ocr_status",
        create_type=False,  # already created above (or skipped for SQLite)
    ).with_variant(sa.String(length=20), "sqlite")

    op.add_column(
        "document_pages",
        sa.Column(
            "ocr_status",
            column_type,
            nullable=False,
            server_default="not_needed",
        ),
    )

    # 2. Partial-style index for the OCR worker's hot query:
    #    "find me pending pages to process."
    op.create_index(
        op.f("ix_document_pages_ocr_pending"),
        "document_pages",
        ["ocr_status"],
        postgresql_where=sa.text("ocr_status IN ('pending', 'processing')"),
        sqlite_where=sa.text("ocr_status IN ('pending', 'processing')"),
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_document_pages_ocr_pending"), table_name="document_pages"
    )
    op.drop_column("document_pages", "ocr_status")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        _ocr_status_enum.drop(bind, checkfirst=True)
