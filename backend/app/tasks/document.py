"""Document-related Celery tasks.

`process_document` runs after a user confirms their upload. It:
    1. Loads the Document row (sync DB session, workers aren't async).
    2. Flips status UPLOADED -> LEARNING and commits.
    3. Downloads the file from Supabase storage.
    4. Parses the file into pages and persists them.
    5. Chunks each page and persists the chunks.
    6. Re-publishes LEARNING (parsing+chunking finished; the doc stays
       in LEARNING because embeddings are not yet stored).
    7. Enqueues `embed_chunks_for_document` for that owner.
       Empty / unsupported documents are deleted and a `failed` event
       is pushed instead.

The doc is NEVER flipped to READY from here. READY means "queryable" —
it is reached only after every chunk has at least one embedding row,
which is the embedding worker's contract. See app.tasks.embedding.

OCR is not implemented in this round — pages with no usable text are
flagged `text_source='ocr'` and `ocr_status='not_needed'`; the future
OCR worker will back-fill them.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from uuid import UUID

from celery import shared_task
from sqlalchemy.orm import Session

from app.core.celery_app import celery_app  # noqa: F401 -- keeps shared_task bound to our app
from app.db.sync_session import run_in_sync_session
from app.models.document import Document, DocumentStatus
from app.models.document_log import DocumentLog
from app.models.document_page import DocumentPage
from app.repositories.document import delete_document_any_owner
from app.repositories.document_chunk import replace_chunks_for_page
from app.repositories.document_log import add_log
from app.repositories.document_page import replace_pages_for_document
from app.schemas.ws_events import document_status_event
from app.services.document.chunker import build_chunks_for_page
from app.services.document.parser import (
    EmptyDocumentError,
    ParsedPage,
    parse_document,
)
from app.services.pubsub.ws_publisher import (
    publish_embedding_progress,
    publish_event,
)
from app.services.storage.supabase_client import get_supabase_storage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------


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


def _claim_for_processing(db: Session, document_id: UUID) -> Document | None:
    """Atomically load+claim a Document that's in UPLOADED state.

    Returns the Document (now LEARNING, flushed but uncommitted) or
    None if the row doesn't exist or isn't in UPLOADED status. Logging
    of the "missing" case is left to the caller so it can use the
    caller's log context.
    """
    document = db.get(Document, document_id)
    if document is None:
        return None
    if document.status != DocumentStatus.uploaded:
        return None
    _flip_status(db, document, DocumentStatus.learning)
    return document


def _mark_stage_failed(
    db: Session,
    document_id: UUID,
    owner_id: UUID,
    *,
    event_type: str,
    error_message: str,
) -> None:
    """Best-effort: flip the doc to FAILED, push the WS event, and
    write one stage-specific terminal log row (e.g. `processing.failed`).

    This is the catch-all terminal handler for unexpected exceptions
    in the worker. The activity-feed row uses the supplied
    `event_type` so the per-doc feed gets a single, well-labelled
    `*.failed` line instead of an orphan `failed` row. The status
    flip and WS event are the same as `_mark_failed`; the log row
    is the only new thing.
    """
    document = db.get(Document, document_id)
    if document is None:
        return
    _flip_status(db, document, DocumentStatus.failed, error_message=error_message)
    _push_status_event(
        owner_id=document.owner_id,
        document_id=document_id,
        title=document.title,
        status=DocumentStatus.failed,
        error_message=error_message,
    )
    # Activity-feed terminal row. Best-effort: a failed log insert
    # must not mask the original exception the caller is about to
    # re-raise.
    try:
        add_log(
            db,
            document_id=document_id,
            owner_id=owner_id,
            event_type=event_type,
            message=error_message[:500],
            metadata={"error": error_message[:500]},
        )
    except Exception:
        logger.warning(
            "process_document: failed to write stage-failed-log event_type=%s "
            "document_id=%s",
            event_type, document_id, exc_info=True,
        )


def _mark_failed(db: Session, document_id: UUID, error_message: str) -> None:
    """Best-effort: flip the doc to FAILED and push the WS event.

    No-op if the row is gone. Caller wraps this in a try/except so any
    secondary failure can't mask the original exception. Does NOT
    write a `failed` activity-feed row — the caller is responsible
    for that, so it can use a stage-specific event_type
    (`processing.failed`, `embedding.failed`, …) and the message it
    actually wants to surface. This keeps the per-doc feed at one
    terminal `*.failed` row per failed stage instead of a generic
    `failed` row that just repeats the error message.
    """
    document = db.get(Document, document_id)
    if document is None:
        return
    _flip_status(db, document, DocumentStatus.failed, error_message=error_message)
    _push_status_event(
        owner_id=document.owner_id,
        document_id=document_id,
        title=document.title,
        status=DocumentStatus.failed,
        error_message=error_message,
    )


# ---------------------------------------------------------------------
# Parse + persist helpers
# ---------------------------------------------------------------------


def _persist_pages(
    db: Session, document_id: UUID, pages: list[ParsedPage]
) -> int:
    """Bulk-replace all pages for the document. Returns row count."""
    return replace_pages_for_document(db, document_id, pages)


def _log_and_publish(
    db: Session,
    *,
    document_id: UUID,
    owner_id: UUID,
    event_type: str,
    message: str | None = None,
    metadata: dict | None = None,
) -> DocumentLog | None:
    """Write a log row in the caller's session and push the WS event.

    Used inside `_chunk_pages_for_document`, which already owns a
    session — the log row commits with the chunk rows for the same
    page, so the WS push (which fires AFTER the per-page commit) can
    only reference a row that's already in the DB.

    The publish path is best-effort and fully swallowed; a Redis
    blip must not abort the chunking loop.
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
            "process_document: add_log failed event_type=%s document_id=%s",
            event_type, document_id, exc_info=True,
        )
        return None

    if log_id is None:
        return None

    try:
        row = db.get(DocumentLog, log_id)
    except Exception:
        logger.warning(
            "process_document: failed to refetch log row id=%s", log_id,
            exc_info=True,
        )
        return None
    if row is None:
        return None

    try:
        from app.services.pubsub.ws_publisher import publish_document_log
        publish_document_log(
            owner_id,
            document_id=document_id,
            log_row=row,
        )
    except Exception:
        logger.warning(
            "process_document: ws publish failed event_type=%s document_id=%s",
            event_type, document_id, exc_info=True,
        )

    return row


def _log_one(
    document_id: UUID,
    owner_id: UUID,
    *,
    event_type: str,
    message: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Write one log row + WS push in its own short transaction.

    Used for top-level events that don't share a session with other
    work (download, parse, chunking terminal). Opens a fresh session
    via `run_in_sync_session`, writes + commits, then publishes.
    """
    def _do(db: Session) -> DocumentLog | None:
        return _log_and_publish(
            db,
            document_id=document_id,
            owner_id=owner_id,
            event_type=event_type,
            message=message,
            metadata=metadata,
        )

    try:
        run_in_sync_session(_do)
    except Exception:
        logger.warning(
            "process_document: _log_one failed event_type=%s document_id=%s",
            event_type, document_id, exc_info=True,
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
    """Best-effort publish of a `document.embedding_progress` event.

    Wrapped in try/except so a Redis blip never breaks the worker.
    """
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
            "process_document: progress publish failed phase=%s document_id=%s",
            phase, document_id, exc_info=True,
        )


def _chunk_pages_for_document(db: Session, document_id: UUID, owner_id: UUID) -> int:
    """Build and persist chunks for every page of `document_id`.

    Reads the pages back (so we have the persisted ids), runs the
    chunker on each, and replaces the chunk rows for that page. Returns
    the total number of chunks persisted.

    No per-page activity-feed rows are written here — the per-stage
    log is emitted by the caller (`process_document`) as a single
    `chunking.completed` row. The progress bar above the log panel
    is driven by the `document.embedding_progress` WS event, which we
    still publish on every page so the bar advances in step with the
    actual work. Keeping the activity feed at one row per stage means
    the user sees a clean "Chunked into N chunks" line instead of a
    wall of per-page breadcrumbs.

    `owner_id` is needed for the progress-event push (to route to the
    right subscriber) and is also passed to `_log_and_publish` calls
    if any are re-introduced later.
    """
    pages = (
        db.query(DocumentPage)
        .filter(DocumentPage.document_id == document_id)
        .order_by(DocumentPage.page_number.asc())
        .all()
    )
    total = 0
    for page_idx, page in enumerate(pages, start=1):
        chunks = build_chunks_for_page(page)
        if chunks:
            replace_chunks_for_page(db, page.id, chunks)
            total += len(chunks)
        # Progress event only — no activity-feed log row. The
        # `chunks_total` here is the running best estimate; the
        # caller publishes the final, accurate value with
        # `chunks_total=chunks_total, chunks_done=chunks_total`
        # after the loop, so the bar can settle cleanly.
        _publish_progress(
            owner_id=owner_id,
            document_id=document_id,
            phase="chunking",
            pages_total=len(pages),
            pages_done=page_idx,
            chunks_total=total,
            chunks_done=total,
        )
    return total


def _delete_document(
    db: Session, document_id: UUID
) -> tuple[str, str, UUID, str] | None:
    """Delete the row and return data needed after the commit.

    The CASCADE on `document_pages.document_id` removes child pages.
    The caller publishes the event and cleans storage after the DB
    transaction commits, so a client refetch cannot see a stale row.
    """
    document = db.get(Document, document_id)
    if document is None:
        return None
    result = (
        document.storage_bucket,
        document.storage_path,
        document.owner_id,
        document.title,
    )
    delete_document_any_owner(db, document_id)
    return result


def _publish_failed_event(
    document_id: UUID, owner_id: UUID, title: str, error_message: str
) -> None:
    """Publish the failed event after the delete transaction commits."""
    _push_status_event(
        owner_id=owner_id,
        document_id=document_id,
        title=title,
        status=DocumentStatus.failed,
        error_message=error_message[:1000],
    )


def _cleanup_storage(bucket: str, storage_path: str) -> None:
    """Best-effort Supabase cleanup. Logs but never raises."""
    try:
        get_supabase_storage().delete_file(bucket, storage_path)
    except Exception:
        logger.exception(
            "process_document: failed to delete storage %s/%s", bucket, storage_path
        )


# ---------------------------------------------------------------------
# WS / storage helpers
# ---------------------------------------------------------------------


def _push_status_event(
    *,
    owner_id: UUID,
    document_id: UUID,
    title: str,
    status: DocumentStatus,
    error_message: str | None = None,
) -> None:
    """Push a `document.status` WS event to the owner via Redis pub/sub.

    The worker has no access to the FastAPI process's in-memory
    ConnectionManager, so we PUBLISH on the global `ws:events` channel
    and let each FastAPI instance route to its own sockets.
    """
    envelope = document_status_event(
        document_id=document_id,
        title=title,
        status=status.value,
        error_message=error_message,
    )
    publish_event(owner_id, envelope.model_dump(mode="json"))


def _download_to_tempfile(bucket: str, storage_path: str) -> tuple[Path, int]:
    """Download the file from Supabase into a fresh tempfile.

    Returns (path, byte_count). Caller is responsible for unlinking
    the path. Using NamedTemporaryFile so we don't leave bytes lying
    around if something crashes mid-processing.
    """
    file_bytes = get_supabase_storage().download_file(bucket, storage_path)
    byte_count = len(file_bytes)
    logger.info(
        "process_document: downloaded %d bytes bucket=%s path=%s",
        byte_count, bucket, storage_path,
    )
    suffix = Path(storage_path).suffix or ".bin"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(file_bytes)
        return Path(tmp.name), byte_count


# ---------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------


@shared_task(name="app.tasks.document.ping")
def ping() -> str:
    """Smoke-test task. Returns 'pong' so we can verify the worker is wired up."""
    return "pong"


@shared_task(
    name="app.tasks.document.process_document",
    bind=True,
    max_retries=3,
    default_retry_delay=10,
)
def process_document(self, document_id: str) -> dict:
    """Process an uploaded document.

    State transitions driven here:
        UPLOADED  -> LEARNING  (this function, on entry)
        LEARNING  -> LEARNING  (after pages + chunks; still not queryable)
        any       -> FAILED    (on exception)

    LEARNING -> READY is NOT performed here. Only the embedding worker
    flips a doc to READY, because READY means "all chunks have stored
    embeddings and the doc is queryable". This function enqueues
    `embed_chunks_for_document` and returns; the embedding worker
    finishes the lifecycle.

    Args:
        document_id: UUID string from the API (JSON-serialized).
    """
    task_id = self.request.id
    log_extra = {"task_id": task_id, "document_id": document_id}
    logger.info("process_document start %s", log_extra)

    try:
        doc_uuid = UUID(document_id)
    except ValueError:
        logger.exception("process_document: invalid document_id %r", document_id)
        return {"ok": False, "error": "invalid document_id"}

    try:
        document = run_in_sync_session(_claim_for_processing, doc_uuid)
        if document is None:
            logger.warning(
                "process_document: document missing or not in UPLOADED document_id=%s",
                document_id,
            )
            return {"ok": False, "error": "document not found or not in UPLOADED"}

        # Push the LEARNING event AFTER the commit (which happens when
        # run_in_sync_session exits) so any client refetch after the WS
        # message sees the new state.
        _push_status_event(
            owner_id=document.owner_id,
            document_id=doc_uuid,
            title=document.title,
            status=DocumentStatus.learning,
        )

        # Announce download started so the activity feed has a marker
        # for "we're working on this doc now". The download itself
        # is local I/O; the log row is a breadcrumb, not per-byte.
        tmp_path, bytes_downloaded = _download_to_tempfile(
            document.storage_bucket, document.storage_path
        )
        try:
            try:
                parsed = parse_document(
                    tmp_path, document.mime_type, document.filename
                )
            except EmptyDocumentError as exc:
                logger.warning(
                    "process_document: empty/unsupported document_id=%s reason=%s",
                    document_id, exc,
                )
                error_message = str(exc)[:1000]
                deleted = run_in_sync_session(_delete_document, doc_uuid)
                if deleted is not None:
                    bucket, storage_path, owner_id, title = deleted
                    _publish_failed_event(
                        doc_uuid, owner_id, title, error_message
                    )
                    _cleanup_storage(bucket, storage_path)
                return {
                    "ok": False,
                    "error": "empty_or_unsupported_document",
                    "document_id": document_id,
                    "task_id": task_id,
                }

            # Persist pages first, then log + push the parse progress.
            run_in_sync_session(_persist_pages, doc_uuid, parsed)
            _log_one(
                doc_uuid, document.owner_id,
                event_type="parsing",
                message=f"Parsed {len(parsed)} pages",
                metadata={"pages": len(parsed)},
            )
            _publish_progress(
                owner_id=document.owner_id,
                document_id=doc_uuid,
                phase="parsing",
                pages_total=len(parsed),
                pages_done=len(parsed),
            )

            chunks_total = run_in_sync_session(
                _chunk_pages_for_document, doc_uuid, document.owner_id
            )
            # Emit the terminal chunking event so the progress bar can
            # settle to 100% before embedding starts.
            _log_one(
                doc_uuid, document.owner_id,
                event_type="chunking",
                message=f"Chunked into {chunks_total} chunks",
                metadata={"chunks_total": chunks_total},
            )
            _publish_progress(
                owner_id=document.owner_id,
                document_id=doc_uuid,
                phase="chunking",
                pages_total=len(parsed),
                pages_done=len(parsed),
                chunks_total=chunks_total,
                chunks_done=chunks_total,
            )
            # No status push here — the doc is still in LEARNING (set on
            # entry by `_claim_for_processing`) and pushing the same
            # status again would emit a duplicate WS event with no new
            # information. The embedding worker will push READY (or
            # FAILED) when it actually transitions the lifecycle.
            logger.info(
                "process_document: stored %d pages / %d chunks document_id=%s",
                len(parsed), chunks_total, document_id,
            )

            # Hand off to the embedding worker. The doc is still in
            # LEARNING; chunks are committed with `needs_embedding=True`
            # and the embedding worker picks them up via
            # `list_pending_chunk_ids`. The worker is the only thing
            # that flips LEARNING -> READY (or -> FAILED).
            #
            # Fire-and-forget, same shape as the `process_document`
            # enqueue in documents.py: if the broker is unreachable we
            # log and move on — a future sweep job can back-fill by
            # scanning for LEARNING docs with chunks still needing
            # embeddings.
            #
            # countdown=1 lets the worker's fresh DB session see the
            # committed chunks before it starts reading.
            try:
                embed_result = celery_app.send_task(
                    "app.tasks.embedding.embed_chunks_for_document",
                    args=[document_id, str(document.owner_id)],
                    countdown=1,
                )
                logger.info(
                    "embedding task enqueued task_id=%s document_id=%s",
                    embed_result.id,
                    document_id,
                )
            except Exception:
                logger.exception(
                    "failed to enqueue embedding task document_id=%s",
                    document_id,
                )
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                logger.warning(
                    "process_document: failed to remove temp file %s", tmp_path
                )

        return {
            "document_id": document_id,
            "task_id": task_id,
            "ok": True,
            "bytes_downloaded": bytes_downloaded,
            "pages_stored": len(parsed),
            "chunks_stored": chunks_total,
        }

    except Exception as exc:
        logger.exception("process_document failed document_id=%s", document_id)
        # Best-effort: flip the doc to FAILED and write one terminal
        # `processing.failed` activity-feed row so the per-doc feed
        # has a clean terminal line. We use `processing.failed` (not
        # `failed`) so the row's icon and label stay consistent with
        # the parsing/chunking/embedding family — the activity feed
        # reads as one `*.failed` line per failed stage. The
        # underlying status flip still goes to FAILED so the doc-list
        # pill and the detail dialog's stats stay accurate.
        #
        # `document` may be undefined if the exception happened
        # before `_claim_for_processing` returned. In that case we
        # fall back to a status-only flip (no log row) — the doc is
        # almost certainly in a degenerate state anyway.
        try:
            error_message = str(exc)[:1000]
            if "document" in locals() and document is not None:
                run_in_sync_session(
                    _mark_stage_failed,
                    doc_uuid, document.owner_id,
                    event_type="processing_failed",
                    error_message=error_message,
                )
            else:
                run_in_sync_session(
                    _mark_failed, doc_uuid, error_message
                )
        except Exception:
            logger.exception(
                "process_document: failed to mark document FAILED document_id=%s",
                document_id,
            )
        raise
