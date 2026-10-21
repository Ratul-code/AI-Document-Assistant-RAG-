"""Embedding Celery task.

`embed_chunks_for_document` pulls a worklist of chunks that still need
an embedding and runs them through the configured `EmbeddingModel`.
Each chunk is upserted via `document_embedding.upsert_embedding` and
its `needs_embedding` flag is cleared on success.

This worker is also the ONLY place that flips a document LEARNING ->
READY (or -> FAILED). The contract is:
  * At least one chunk succeeded                  -> LEARNING -> READY
  * Every chunk the worker tried failed            -> LEARNING -> FAILED
  * No chunks needed embedding (already embedded,
    or doc had zero chunks)                        -> leave status alone

`process_document` keeps the doc in LEARNING and enqueues this task;
this task is responsible for the lifecycle-to-queryable transition so
that `READY` actually means "queryable" (i.e. embeddings exist).

The stub model is deterministic so the worker can be exercised in CI
without a real provider. When a real model gets wired up, only
`get_default_embedding_model()` needs to change.
"""

from __future__ import annotations

import logging
from uuid import UUID

from celery import shared_task
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.celery_app import celery_app  # noqa: F401
from app.db.sync_session import run_in_sync_session
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.models.document_embedding import EmbeddingStatus
from app.models.document_log import DocumentLog
from app.repositories.document_embedding import (
    EmbeddingSpec,
    list_pending_chunk_ids,
    upsert_embedding,
)
from app.repositories.document_log import add_log
from app.schemas.ws_events import document_status_event
from app.services.ai.embeddings import get_default_embedding_model
from app.services.pubsub.ws_publisher import (
    publish_embedding_progress,
    publish_event,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------


def _mark_chunks_embedded(db: Session, chunk_ids: list[UUID]) -> int:
    if not chunk_ids:
        return 0
    result = db.execute(
        update(DocumentChunk)
        .where(DocumentChunk.id.in_(chunk_ids))
        .values(needs_embedding=False)
    )
    return result.rowcount or 0


def _flip_status(
    db: Session,
    document: Document,
    new_status: DocumentStatus,
    *,
    error_message: str | None = None,
) -> None:
    """Update a Document's status and (optionally) its error_message.

    Caller owns the transaction (run_in_sync_session commits on exit).
    """
    document.status = new_status
    if error_message is not None:
        document.error_message = error_message
    db.add(document)
    db.flush()


def _push_status_event(
    *,
    owner_id: UUID,
    document_id: UUID,
    title: str,
    status: DocumentStatus,
    error_message: str | None = None,
) -> None:
    """Push a `document.status` WS event to the owner via Redis pub/sub.

    Mirrors the helper in `app.tasks.document`. The worker has no
    access to the FastAPI process's in-memory ConnectionManager, so we
    PUBLISH on the global `ws:events` channel and let each FastAPI
    instance route to its own sockets.
    """
    envelope = document_status_event(
        document_id=document_id,
        title=title,
        status=status.value,
        error_message=error_message,
    )
    publish_event(owner_id, envelope.model_dump(mode="json"))


def _finalize_document_status(
    db: Session,
    document_id: UUID,
    *,
    succeeded: int,
    failed: int,
    error_message: str | None = None,
) -> DocumentStatus:
    """Flip the doc from LEARNING to READY or FAILED at end of embedding.

    Contract:
        succeeded >= 1                            -> READY
        succeeded == 0 and failed >= 1             -> FAILED (nothing retrievable)
        succeeded == 0 and failed == 0 (no work)   -> leave status alone
                                                        (e.g. retried run; already terminal)

    `error_message`, when provided, is recorded on the doc as the
    reason for FAILED. Truncated to 1000 chars to match the column.

    Returns the new status (or the existing one if no flip occurred).
    Pushes the matching WS event when a flip happens.

    Does NOT write an activity-feed row here. The caller
    (`_process_one_document`) has already written the single
    `embedding.completed` / `embedding.failed` row, and writing
    another `ready` / `failed` lifecycle row would double the
    per-doc feed's terminal line.
    """
    document = db.get(Document, document_id)
    if document is None:
        return DocumentStatus.failed  # row vanished; treat as terminal
    if succeeded >= 1:
        _flip_status(db, document, DocumentStatus.ready)
        _push_status_event(
            owner_id=document.owner_id,
            document_id=document_id,
            title=document.title,
            status=DocumentStatus.ready,
        )
        return DocumentStatus.ready
    if failed >= 1:
        # Surface the most recent provider error in the doc-level
        # message so ops can see *why* the doc failed without digging
        # into per-chunk rows. Falls back to a generic message.
        msg = (error_message or "all chunks failed to embed")[:1000]
        _flip_status(
            db,
            document,
            DocumentStatus.failed,
            error_message=msg,
        )
        _push_status_event(
            owner_id=document.owner_id,
            document_id=document_id,
            title=document.title,
            status=DocumentStatus.failed,
            error_message=msg,
        )
        return DocumentStatus.failed
    return document.status


def _process_one_document(
    db: Session,
    document_id: UUID,
    owner_id: UUID,
    *,
    batch_size: int = 50,
) -> dict:
    """Embed all pending chunks for a single document.

    Returns a small dict of counters so the task result is useful for
    observability. Caller owns the transaction.
    """
    model = get_default_embedding_model()

    # Count pending chunks up front so the progress bar knows the
    # denominator. This is the same join `list_pending_chunk_ids`
    # uses, just without the LIMIT. On a real doc this is one indexed
    # COUNT(*) — sub-millisecond.
    chunks_total = _count_pending_chunks(db, document_id, owner_id=owner_id)

    # Publish the initial progress event so the UI bar can show the
    # denominator (`chunks_total`) immediately. No activity-feed row
    # is written here — the terminal `embedding.completed` /
    # `embedding.failed` row is the only log entry this stage emits.
    _publish_progress(
        owner_id=owner_id,
        document_id=document_id,
        phase="embedding",
        chunks_total=chunks_total,
        chunks_done=0,
        failed_total=0,
        model_name=model.model_name,
    )

    succeeded = 0
    failed = 0
    # Most recent batch-failure message. Kept across iterations so the
    # doc-level error_message recorded by `_finalize_document_status`
    # reflects the last failure we saw (useful for ops when the whole
    # batch repeatedly fails for the same reason).
    last_error_message: str | None = None

    while True:
        chunk_ids = list_pending_chunk_ids(
            db, document_id, owner_id=owner_id, limit=batch_size
        )
        if not chunk_ids:
            break

        # We need the actual chunk text to embed. Pull just the rows we
        # claimed this batch. Keep chunk_id and text aligned by index.
        rows = db.execute(
            select(DocumentChunk.id, DocumentChunk.text)
            .where(DocumentChunk.id.in_(chunk_ids))
        ).all()
        texts = [text for _cid, text in rows]

        # One batched forward pass per 50 chunks. On a real model
        # (Qwen3, OpenAI, etc.) this is 5-10x faster than calling
        # embed() in a loop because the model stays warm in memory.
        try:
            results = model.embed_batch(texts)
        except Exception as exc:
            logger.warning(
                "embed_chunks: batch failed size=%d err=%s", len(rows), exc
            )
            # Whole-batch failure: record the same error against each
            # chunk so none are stuck in `pending` forever. Chunks
            # remain visible for future retries.
            error_message = str(exc)[:1000]
            last_error_message = error_message
            for chunk_id, _text in rows:
                upsert_embedding(
                    db,
                    EmbeddingSpec(
                        chunk_id=chunk_id,
                        model_name=model.model_name,
                        dim=0,
                        vector=[],
                    ),
                    status=EmbeddingStatus.failed,
                    error_message=error_message,
                )
                failed += 1
        else:
            for (chunk_id, _text), result in zip(rows, results):
                upsert_embedding(
                    db,
                    EmbeddingSpec(
                        chunk_id=chunk_id,
                        model_name=result.model_name,
                        dim=result.dim,
                        vector=result.vector,
                        distance_metric=result.distance_metric,
                    ),
                    status=EmbeddingStatus.succeeded,
                )
                succeeded += 1

        _mark_chunks_embedded(db, [cid for cid, _ in rows])
        # Flush so the next batch's `list_pending_chunk_ids` sees the
        # updated `needs_embedding` flags.
        db.flush()

        # Progress event drives the UI's progress bar. The bar's
        # denominator is `chunks_total` (set once at start), and the
        # numerator is `succeeded + failed` so failed chunks also
        # count as "attempted" — a batch that fully crashes still
        # advances the bar. The per-batch activity-feed log row was
        # intentionally removed so the feed stays at one row per
        # stage (uploaded, parsed, chunked, embedded); live counters
        # above the log panel carry the in-flight detail.
        _publish_progress(
            owner_id=owner_id,
            document_id=document_id,
            phase="embedding",
            chunks_total=chunks_total,
            chunks_done=succeeded + failed,
            failed_total=failed,
            model_name=model.model_name,
        )

    # One terminal activity-feed row for the embedding stage. We
    # decide success vs. failure the same way `_finalize_document_status`
    # does so the row tells the same story as the doc-level status
    # flip a moment later. The per-doc feed gets exactly one row per
    # stage — `uploaded → parsing → chunking → embedding` — so this
    # is the only embedding row.
    if chunks_total == 0:
        embedding_event_type = "embedding"
        embedding_message = "Embedding complete (no chunks)"
    elif succeeded == 0 and failed == 0:
        embedding_event_type = "embedding"
        embedding_message = "Embedding complete (already up to date)"
    elif succeeded >= 1:
        embedding_event_type = "embedding"
        embedding_message = (
            f"Embedding complete "
            f"({succeeded} embedded"
            + (f", {failed} failed" if failed else "")
            + ")"
        )
    else:
        embedding_event_type = "embedding_failed"
        embedding_message = f"Embedding failed ({failed} chunks)"

    _log_and_publish(
        db,
        document_id=document_id,
        owner_id=owner_id,
        event_type=embedding_event_type,
        message=embedding_message,
        metadata={
            "chunks_total": chunks_total,
            "succeeded": succeeded,
            "failed": failed,
        },
    )

    # All batches done. Now that we know whether the doc is actually
    # queryable, perform the only LEARNING -> READY transition in the
    # whole pipeline. See `_finalize_document_status` for the rules.
    final_status = _finalize_document_status(
        db,
        document_id,
        succeeded=succeeded,
        failed=failed,
        error_message=last_error_message,
    )
    logger.info(
        "embed_chunks_for_document: finalize document_id=%s status=%s succeeded=%d failed=%d",
        document_id, final_status.value, succeeded, failed,
    )

    return {
        "document_id": str(document_id),
        "succeeded": succeeded,
        "failed": failed,
        "final_status": final_status.value,
    }


# ---------------------------------------------------------------------
# Per-event log + publish helpers
# ---------------------------------------------------------------------


def _count_pending_chunks(
    db: Session, document_id: UUID, *, owner_id: UUID
) -> int:
    """Count chunks that still need an embedding, owner-scoped.

    Mirrors the WHERE clause in `list_pending_chunk_ids` minus the
    LIMIT, so the denominator matches what the worker actually
    processes. Used to publish `chunks_total` on the embedding.started
    event so the progress bar has the right max.
    """
    from app.models.document_page import DocumentPage
    from app.models.document_embedding import DocumentEmbedding

    stmt = (
        select(func.count())
        .select_from(DocumentChunk)
        .join(DocumentPage, DocumentPage.id == DocumentChunk.page_id)
        .join(Document, Document.id == DocumentPage.document_id)
        .outerjoin(
            DocumentEmbedding,
            DocumentEmbedding.chunk_id == DocumentChunk.id,
        )
        .where(
            DocumentPage.document_id == document_id,
            Document.owner_id == owner_id,
            DocumentChunk.needs_embedding.is_(True),
            (DocumentEmbedding.id.is_(None))
            | (DocumentEmbedding.status != EmbeddingStatus.succeeded),
        )
    )
    return int(db.execute(stmt).scalar_one())


def _log_and_publish(
    db: Session,
    *,
    document_id: UUID,
    owner_id: UUID,
    event_type: str,
    message: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Write one log row in the caller's session + push the WS event.

    Same contract as the equivalent helper in `app.tasks.document`:
    log insert + WS push are both best-effort, and the WS push fires
    AFTER `add_log` has flushed so subscribers can't see a row that
    doesn't exist yet.
    """
    try:
        log_id = add_log(
            db,
            document_id=document_id,
            owner_id=owner_id,
            event_type=event_type,
            message=message,
            metadata=metadata,
        )
    except Exception:
        logger.warning(
            "embed_chunks: add_log failed event_type=%s document_id=%s",
            event_type, document_id, exc_info=True,
        )
        return

    if log_id is None:
        return

    try:
        row = db.get(DocumentLog, log_id)
    except Exception:
        logger.warning(
            "embed_chunks: failed to refetch log row id=%s", log_id,
            exc_info=True,
        )
        return
    if row is None:
        return

    try:
        from app.services.pubsub.ws_publisher import publish_document_log
        publish_document_log(
            owner_id,
            document_id=document_id,
            log_row=row,
        )
    except Exception:
        logger.warning(
            "embed_chunks: publish_document_log failed id=%s",
            log_row.id, exc_info=True,
        )


def _publish_progress(
    *,
    owner_id: UUID,
    document_id: UUID,
    phase: str,
    pages_total: int | None = None,
    pages_done: int | None = None,
    chunks_total: int | None = None,
    chunks_done: int | None = None,
    failed_total: int = 0,
    model_name: str | None = None,
) -> None:
    """Best-effort publish of a `document.embedding_progress` event."""
    try:
        publish_embedding_progress(
            owner_id,
            document_id=document_id,
            phase=phase,
            pages_total=pages_total,
            pages_done=pages_done,
            chunks_total=chunks_total,
            chunks_done=chunks_done,
            failed_total=failed_total,
            model_name=model_name,
        )
    except Exception:
        logger.warning(
            "embed_chunks: progress publish failed phase=%s document_id=%s",
            phase, document_id, exc_info=True,
        )


# ---------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------


@shared_task(
    name="app.tasks.embedding.embed_chunks_for_document",
    bind=True,
    max_retries=3,
    default_retry_delay=15,
)
def embed_chunks_for_document(self, document_id: str, owner_id: str) -> dict:
    """Embed every pending chunk for `document_id`.

    Args:
        document_id: UUID string of the document to process.
        owner_id: UUID string of the document owner — used for
            owner-scoped chunk reads.
    """
    task_id = self.request.id
    log_extra = {"task_id": task_id, "document_id": document_id}
    logger.info("embed_chunks_for_document start %s", log_extra)

    try:
        doc_uuid = UUID(document_id)
        owner_uuid = UUID(owner_id)
    except ValueError:
        logger.exception("embed_chunks_for_document: invalid uuid %s", log_extra)
        return {"ok": False, "error": "invalid uuid"}

    try:
        result = run_in_sync_session(
            _process_one_document, doc_uuid, owner_uuid
        )
    except Exception as exc:
        logger.exception(
            "embed_chunks_for_document failed document_id=%s", document_id
        )
        raise

    logger.info(
        "embed_chunks_for_document done %s succeeded=%s failed=%s",
        log_extra, result["succeeded"], result["failed"],
    )
    return {"ok": True, "succeeded": result["succeeded"], "failed": result["failed"]}


__all__ = ["embed_chunks_for_document"]
