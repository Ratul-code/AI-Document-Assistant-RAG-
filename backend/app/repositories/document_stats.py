"""Per-document aggregate queries.

`document_stats_for(...)` runs a small set of COUNTs over
`document_pages`, `document_chunks`, and `document_embeddings` for one
owner-scoped document, plus one DISTINCT for the embedding model list.
The numbers are derived purely from the existing tables — no new
columns, no migrations, no caching.

Performance:
  - All queries filter by `document_id` (and, for chunk/embedding,
    join through `document_pages`) so they hit the existing indexes
    (`ix_document_pages_document_id`, `ix_document_chunks_page_id`).
  - The five COUNTs are independent and small (per-document), so we
    run them sequentially in a single transaction. We could parallelise
    across connections, but the wall-clock saving is in the
    millisecond range and the extra connection pressure is not worth it.

Why a single helper instead of one query per route field:
  - The route is called from one place (the per-doc detail dialog),
    so the cost of composing the response in Python is one extra
    function call.
  - Future fields (e.g. "chunks_above_500_tokens") can be added here
    without changing the route or the Pydantic schema.
  - The owner-scope check lives in `get_document`; we reuse it so
    every aggregate inherits the same security boundary by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.document_embedding import DocumentEmbedding, EmbeddingStatus
from app.models.document_page import DocumentPage, OcrStatus


# OCR statuses that mean "OCR work is outstanding or done". "not_needed"
# is excluded so parser-only pages don't inflate the count. We treat
# `pending` and `processing` as a single "in flight" bucket because the
# detail dialog only needs to know whether OCR is still owed, not
# exactly which substate.
_OCR_IN_FLIGHT = (OcrStatus.pending, OcrStatus.processing)


@dataclass(frozen=True)
class DocumentStats:
    """Aggregate numbers for a single document, derived from existing tables.

    All counts are zero-initialised so a document with no pages yet
    (e.g. status `uploaded` or `processing`) returns a clean struct
    instead of `None`.
    """

    page_count: int = 0
    chunks_total: int = 0
    chunks_embedded: int = 0
    chunks_pending: int = 0
    chunks_failed: int = 0
    pages_needing_ocr: int = 0
    pages_ocr_pending: int = 0
    pages_ocr_failed: int = 0
    embedding_models: list[str] = field(default_factory=list)
    embedding_status_counts: dict[str, int] = field(default_factory=dict)


async def document_stats_for(
    db: AsyncSession,
    document_id: UUID,
    *,
    owner_id: UUID,
) -> DocumentStats | None:
    """Return aggregate stats for one owner-scoped document.

    Returns `None` if the document doesn't exist or doesn't belong to
    `owner_id` — the route treats that as 404 so the response shape
    matches every other document endpoint (no info leak).

    Owner-scope is enforced by `get_document` first; once the
    document is confirmed to belong to the caller, the per-table
    queries naturally inherit that scope because they filter by
    `document_pages.document_id` (which is unique to the owned doc).
    """
    # 1. Owner-scope check. None = "missing OR cross-owner"; the route
    #    will translate that to 404.
    doc = await db.execute(
        select(Document.id).where(
            Document.id == document_id,
            Document.owner_id == owner_id,
        )
    )
    if doc.scalar_one_or_none() is None:
        return None

    # 2. Per-table COUNTs. Each is a single indexed query; we run them
    #    sequentially because the per-query cost is sub-millisecond and
    #    the per-document candidate set is tiny (dozens of rows, not
    #    millions). Parallelising would add connection churn for no
    #    measurable win at this scale.
    page_count = await _count_pages(db, document_id)
    chunks_total = await _count_chunks(db, document_id)
    pages_needing_ocr = await _count_pages_with_ocr_status(
        db, document_id, exclude=OcrStatus.not_needed
    )
    pages_ocr_pending = await _count_pages_with_ocr_status(
        db, document_id, include=_OCR_IN_FLIGHT
    )
    pages_ocr_failed = await _count_pages_with_ocr_status(
        db, document_id, include=(OcrStatus.failed,)
    )

    # 3. Embedding-derived numbers. Splitting "succeeded" / "failed" /
    #    "pending" into named fields plus a full histogram lets the
    #    dialog render the common "X succeeded, Y failed" headline AND
    #    survive a future embedding-status enum addition without a
    #    schema change.
    chunks_embedded, chunks_failed, embedding_status_counts = (
        await _embedding_breakdown(db, document_id)
    )
    chunks_pending = chunks_total - chunks_embedded - chunks_failed

    # 4. Distinct model names. Alphabetical for a stable, predictable
    #    dialog order. `None` model names are excluded — they should
    #    not exist (the column is NOT NULL) but the filter is cheap
    #    insurance against a corrupt row.
    models = await _distinct_embedding_models(db, document_id)

    return DocumentStats(
        page_count=page_count,
        chunks_total=chunks_total,
        chunks_embedded=chunks_embedded,
        chunks_pending=chunks_pending,
        chunks_failed=chunks_failed,
        pages_needing_ocr=pages_needing_ocr,
        pages_ocr_pending=pages_ocr_pending,
        pages_ocr_failed=pages_ocr_failed,
        embedding_models=models,
        embedding_status_counts=embedding_status_counts,
    )


# ---------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------


async def _count_pages(db: AsyncSession, document_id: UUID) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(DocumentPage)
        .where(DocumentPage.document_id == document_id)
    )
    return int(result.scalar_one())


async def _count_chunks(db: AsyncSession, document_id: UUID) -> int:
    """Count chunks for this document via the page join.

    The chunk table has no direct `document_id` — it joins through
    `document_pages`, which is the standard lookup shape in
    `repositories/document_chunk.py`.
    """
    result = await db.execute(
        select(func.count())
        .select_from(DocumentChunk)
        .join(DocumentPage, DocumentPage.id == DocumentChunk.page_id)
        .where(DocumentPage.document_id == document_id)
    )
    return int(result.scalar_one())


async def _count_pages_with_ocr_status(
    db: AsyncSession,
    document_id: UUID,
    *,
    include: tuple[OcrStatus, ...] = (),
    exclude: OcrStatus | None = None,
) -> int:
    """Count pages whose `ocr_status` matches the given filter.

    Pass `include=tuple(...)` to count pages whose status is in the
    set; pass `exclude=OcrStatus.X` to count pages whose status is
    anything OTHER than X. Exactly one of the two is used per call.
    The route calls this three times — once per filter — to keep the
    SQL straight-line; a single GROUP BY query would also work but
    is harder to read and the perf delta is meaningless at this
    scale.
    """
    stmt = (
        select(func.count())
        .select_from(DocumentPage)
        .where(DocumentPage.document_id == document_id)
    )
    if include:
        stmt = stmt.where(DocumentPage.ocr_status.in_(include))
    if exclude is not None:
        stmt = stmt.where(DocumentPage.ocr_status != exclude)
    result = await db.execute(stmt)
    return int(result.scalar_one())


async def _embedding_breakdown(
    db: AsyncSession,
    document_id: UUID,
) -> tuple[int, int, dict[str, int]]:
    """Return (succeeded, failed, status_histogram) for this doc's embeddings.

    The histogram includes every status the DB actually has, so a
    future enum addition (e.g. `rate_limited`) flows through to the
    dialog without a schema bump.
    """
    stmt = (
        select(DocumentEmbedding.status, func.count())
        .join(DocumentChunk, DocumentChunk.id == DocumentEmbedding.chunk_id)
        .join(DocumentPage, DocumentPage.id == DocumentChunk.page_id)
        .where(DocumentPage.document_id == document_id)
        .group_by(DocumentEmbedding.status)
    )
    rows = (await db.execute(stmt)).all()

    histogram: dict[str, int] = {}
    for status_value, count in rows:
        # `status` is the SQLAlchemy enum wrapper; `.value` is the
        # underlying string (e.g. "succeeded"). Use it so the
        # response keys match the public API contract, not the
        # Python identifier.
        histogram[str(status_value.value)] = int(count)

    succeeded = histogram.get(EmbeddingStatus.succeeded.value, 0)
    failed = histogram.get(EmbeddingStatus.failed.value, 0)
    return succeeded, failed, histogram


async def _distinct_embedding_models(
    db: AsyncSession,
    document_id: UUID,
) -> list[str]:
    stmt = (
        select(distinct(DocumentEmbedding.model_name))
        .join(DocumentChunk, DocumentChunk.id == DocumentEmbedding.chunk_id)
        .join(DocumentPage, DocumentPage.id == DocumentChunk.page_id)
        .where(
            DocumentPage.document_id == document_id,
            DocumentEmbedding.model_name.is_not(None),
        )
        .order_by(DocumentEmbedding.model_name.asc())
    )
    return [row[0] for row in (await db.execute(stmt)).all()]


__all__ = ["DocumentStats", "document_stats_for"]
