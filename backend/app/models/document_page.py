"""SQLAlchemy model for extracted document pages.

One row per logical page of a document. Text comes from a parser first
(`raw_text`), and — if the page is scanned / image-only — from OCR
later (`ocr_text`). The single field downstream code should read is
`final_text`, which the worker populates as:

    final_text = raw_text or ocr_text

Storing both raw and ocr (rather than just one) lets us:
  - know which path produced the text (`text_source` / `ocr_engine`)
  - re-run OCR later if quality improves
  - debug low-quality extractions without re-parsing the whole doc
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

if TYPE_CHECKING:
    pass


class TextSource(str, enum.Enum):
    """How `final_text` was produced for a page.

    For this milestone only `raw` and `merged` will appear; `ocr` is
    reserved for when Google Vision integration lands.
    """

    raw = "raw"  # parsed directly by pypdf / python-docx / read()
    ocr = "ocr"  # produced by an OCR engine (no parser output)
    merged = "merged"  # raw + ocr stitched together (mixed-content pages)


class OcrStatus(str, enum.Enum):
    """Lifecycle of the OCR pipeline for a single page.

    Dormant in the parser-only round: every page is `not_needed`. The
    future OCR worker transitions pages through
    `pending` -> `processing` -> `succeeded` | `failed` | `skipped`.

    `skipped` exists for an explicit policy decision (e.g. user disabled
    OCR for this doc, or we hit a retry budget and gave up).
    """

    not_needed = "not_needed"
    pending = "pending"
    processing = "processing"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"


class DocumentPage(Base):
    """A single page of an uploaded document.

    Pages are inserted by the Celery `process_document` task. `page_number`
    is 1-indexed and unique within a document (enforced by a composite
    unique index). For markdown / plain-text without form feeds, the
    whole file is stored as a single row with `page_number=1`.
    """

    __tablename__ = "document_pages"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    # Ownership — cascades so deleting a document wipes its pages.
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )

    # 1-indexed page number. Combined with document_id this is unique
    # so we never accidentally insert the same page twice.
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # ---- Extracted text ----
    # raw_text:  text directly from the parser (pypdf/python-docx/read()).
    # ocr_text:  text from OCR (Google Vision). Nullable — only set when
    #            the parser returned nothing usable or the page is mixed.
    # final_text: the field downstream code (chunking, embeddings, chat)
    #             actually reads. Populated by the worker as
    #             `raw_text or ocr_text`.
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Which field ended up in final_text.
    text_source: Mapped[TextSource] = mapped_column(
        Enum(TextSource, name="text_source"),
        nullable=False,
        default=TextSource.raw,
    )

    # OCR lifecycle. See OcrStatus docstring. Defaults to not_needed so
    # the parser-only round needs no per-row logic to maintain it.
    ocr_status: Mapped[OcrStatus] = mapped_column(
        Enum(OcrStatus, name="ocr_status"),
        nullable=False,
        default=OcrStatus.not_needed,
        index=True,
    )

    # ---- Parser provenance ----
    # parser_name:    e.g. "pypdf", "python-docx", "builtin-read"
    # parser_version: e.g. "5.1.0", "1.1.2"
    # Always set when raw_text is populated. NULL when the page is
    # OCR-only (no parser succeeded).
    parser_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # ---- OCR provenance ----
    # Reserved for when Google Vision is wired up. All three are NULL
    # for pages where OCR wasn't needed.
    ocr_engine: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ocr_confidence: Mapped[float | None] = mapped_column(nullable=True)

    # Cached `len(final_text)` so we can filter / paginate without
    # recomputing on every query. Workers keep this in sync on insert.
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Free-form metadata. On Postgres this is JSONB (queryable); on
    # SQLite (dev / tests) it is plain JSON serialized as TEXT so we
    # can pass dicts without bespoke encoding. Use for things like page
    # dimensions, rotation angle, image-extraction warnings, etc.
    page_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
