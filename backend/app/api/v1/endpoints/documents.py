"""Document upload endpoints.

Flow:
    1. Client POSTs /upload-intent with metadata (title, filename, mime, size).
    2. Server validates (mime/size/quota), mints a UUID locally, asks
       Supabase for a presigned URL, then inserts a `documents` row in
       PROCESSING state using that same UUID and storage path. Returns 201.
    3. Client PUTs the file directly to that URL (long-running; not awaited
       on the server).
    4. Client POSTs /{id}/confirm once the PUT succeeds.
    5. Server looks up the doc (scoped to owner), marks UPLOADED, returns 200.

Why Supabase-first (and not DB-first)?
    If Supabase fails for any reason (network blip, auth error, SDK
    change), the upload-intent call returns an error WITHOUT having
    written to the DB. Previously the DB row was inserted before the
    Supabase call and then rolled back, wasting a roundtrip and
    risking a race with background workers.

Two additional read endpoints stream the original file back to the owner:
    - GET /{id}/download → Content-Disposition: attachment (forces save)
    - GET /{id}/view     → Content-Disposition: inline     (lets the
                           browser render the file — PDF, image, text;
                           Office formats fall back to a download prompt
                           in the browser, which is the correct
                           cross-platform behaviour)

AI processing (Celery task, moving to LEARNING → READY) is NOT wired up
yet. That's the next step.
"""

# NOTE: this module intentionally OMITS `from __future__ import annotations`.
# slowapi's `@limiter.limit(...)` wraps the route handler, and FastAPI's
# `lenient_issubclass` check (which detects `Request` special parameters)
# requires real class objects, not stringified forward references. With
# `from __future__ import annotations` enabled, FastAPI treats `request:
# Request` as a query param and the route 422s on POST. See the matching
# comment in `app/api/v1/endpoints/auth.py` for the long form.

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUserDep, DbDep
from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.models.document import Document, DocumentStatus
from app.repositories import document as document_repo
from app.repositories import document_log as document_log_repo
from app.repositories import document_stats as document_stats_repo
from app.repositories.document_log import add_log_async
from app.models.document_log import DocumentLog
from app.services.pubsub.ws_publisher import publish_document_log
from app.schemas.document import (
    ConfirmUploadResponse,
    DocumentListItem,
    DocumentListResponse,
    DocumentStatsResponse,
    DocumentUpdate,
    UploadFailedRequest,
    UploadIntentRequest,
    UploadIntentResponse,
)
from app.schemas.document_log import DocumentLogItem, DocumentLogListResponse
from app.schemas.ws_events import document_status_event
from app.services.document.upload_validation import validate_upload_intent
from app.services.storage.supabase_client import get_supabase_storage
from app.tasks.celery import celery_app
from app.websockets.manager import get_connection_manager



logger = logging.getLogger(__name__)

router = APIRouter()


# ----------------------------------------------------------------------
# POST /api/v1/documents/upload-intent
# ----------------------------------------------------------------------

@router.post(
    "/upload-intent",
    status_code=status.HTTP_201_CREATED,
    tags=["documents"],
)
@limiter.limit("10/minute")
async def create_upload_intent(
    request: Request,
    payload: UploadIntentRequest,
    current_user: CurrentUserDep,
    db: DbDep,
) -> UploadIntentResponse:
    """Validate the upload request and return a presigned URL.

    Side effects:
    - Mints a UUID locally and asks Supabase for a presigned upload URL
      for the path `{owner_id}/{uuid}/{filename}`. If Supabase fails,
      no DB row is created (the signed URL just expires after 2h).
    - Only after a valid signed URL is returned, inserts a `documents`
      row in PROCESSING state with that UUID and storage path.

    The client then PUTs the file to `upload_url`. When done, it calls
    POST /documents/{id}/confirm.
    """
    settings = get_settings()

    # 1. Pre-flight: mime type, file size, per-user quotas.
    #    Raises HTTPException (4xx) on failure.
    await validate_upload_intent(
        db,
        owner_id=current_user.id,
        mime_type=payload.mime_type,
        file_size_bytes=payload.file_size_bytes,
    )

    # 2. Build the storage_path up front. We don't need the DB-generated
    #    id for this — the UUID in `document_repo.create_document` is
    #    generated server-side, so we mint one locally too and use it
    #    both as the row id and as part of the storage path.
    #
    #    Doing this BEFORE the Supabase call lets us hand Supabase a
    #    concrete path without having to write to the DB first. That
    #    way, if Supabase fails, no orphan DB row is left behind and we
    #    don't waste a transaction we'll just roll back.
    document_id = uuid4()
    storage_path = f"{current_user.id}/{document_id}"

    # 3. Ask Supabase for a presigned URL the client can PUT to.
    #    Done BEFORE the DB insert so a Supabase failure (network,
    #    auth, SDK change, etc.) costs us nothing on the DB side.
    try:
        print("trying")
        signed = get_supabase_storage().create_signed_upload_url(
            bucket=settings.supabase_storage_bucket,
            storage_path=storage_path,
        )
    except RuntimeError as exc:
        # Supabase is not configured.
        logger.error("Supabase not configured: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage backend is not configured. Contact the administrator.",
        ) from exc
    except Exception as exc:
        logger.exception("Supabase signed-URL call failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not upload to server. Please try again.",
        ) from exc

    # 4. Sanity check the signed URL shape. Refuse to hand back anything
    #    that doesn't look right rather than letting the client fail later.
    upload_url = signed.get("signed_url")
    if not upload_url:
        logger.error("Supabase returned no signed_url: %r", signed)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Storage backend returned an invalid response.",
        )

    # 5. Now that we have a valid signed URL, persist the document row
    #    in PROCESSING state so we have a stable id to return and to
    #    push WebSocket events against later.
    try:
        document = await document_repo.create_document(
            db,
            owner_id=current_user.id,
            title=payload.title,
            filename=payload.filename,
            mime_type=payload.mime_type,
            file_size_bytes=payload.file_size_bytes,
            storage_bucket=settings.supabase_storage_bucket,
            storage_path=storage_path,
            document_id=document_id,
        )
    except Exception as exc:
        # We minted the UUID ourselves; if the DB insert fails here we
        # don't have a row to clean up. Supabase already issued a URL
        # against a path that won't be written to, which is harmless —
        # orphaned signed URLs just expire after 2h.
        logger.exception("Failed to create document row")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to register upload. Please try again.",
        ) from exc

    # No `upload.received` activity-log row is written here. The
    # per-doc feed emits exactly ONE `uploaded` row at confirm time
    # (see the `upload.confirmed` block below, now renamed to
    # `uploaded`). Writing both an "upload started" row at intent
    # and an "upload complete" row at confirm double-counts the
    # event from the user's perspective — the feed should read as
    # `uploaded → parsing → chunking → embedding`, one row per
    # stage. The intent endpoint is observability-only; the row is
    # the user's.

    logger.info(
        "upload-intent created document_id=%s owner_id=%s mime=%s size=%d",
        document.id,
        current_user.id,
        payload.mime_type,
        payload.file_size_bytes,
    )

    return UploadIntentResponse(
        document_id=document.id,
        upload_url=upload_url,
        storage_path=document.storage_path,
        status=document.status,
    )


# ----------------------------------------------------------------------
# POST /api/v1/documents/{id}/confirm
# ----------------------------------------------------------------------

@router.post(
    "/{id}/confirm",
    status_code=status.HTTP_200_OK,
    tags=["documents"],
)
@limiter.limit("30/minute")
async def confirm_upload(
    request: Request,
    id: UUID,
    current_user: CurrentUserDep,
    db: DbDep,
) -> ConfirmUploadResponse:
    """Mark a document as UPLOADED.

    The client calls this after the PUT to Supabase completes. We look
    up the document (scoped to the caller so users can't confirm each
    other's documents), flip status PROCESSING → UPLOADED, stamp
    uploaded_at, and return.

    The actual AI processing (moving to LEARNING → READY) is NOT started
    here yet — that will be a Celery task wired up later.
    """
    # 1. Look up the document, scoped to this owner. None means
    #    "doesn't exist OR belongs to someone else" — both are 404 to
    #    avoid leaking the existence of other users' documents.
    doc = await document_repo.get_document(db, id, owner_id=current_user.id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    # 2. Refuse to confirm something that's not in PROCESSING state.
    #    Idempotency-wise we COULD accept re-confirmations, but a stray
    #    confirm for an already-READY doc usually means the client is
    #    confused; we surface that as 409 to make the mistake visible.
    if doc.status not in (DocumentStatus.processing, DocumentStatus.uploaded):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Document is in status {doc.status.value!r}; "
                f"only processing or uploaded documents can be confirmed."
            ),
        )

    # 3. Flip the status. If it's already UPLOADED, this is a no-op
    #    besides re-stamping uploaded_at — we treat double-confirm as
    #    harmless and idempotent.
    now = datetime.now(timezone.utc)
    updated: Document | None = await document_repo.update_status(
        db,
        id,
        owner_id=current_user.id,
        status=DocumentStatus.uploaded,
        uploaded_at=now,
    )
    if updated is None:
        # Race: deleted between the lookup and the update. Treat as 404.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    # Push a typed status-change event so clients can update their
    # document-list cache (status badge) without a refetch. Best-effort —
    # the REST GET still works if the WS push fails.
    try:
        envelope = document_status_event(
            document_id=updated.id,
            title = doc.title,
            status=updated.status.value,
        )
        await get_connection_manager().send_to_user(
            current_user.id, envelope.model_dump(mode="json")
        )
    except Exception:
        logger.exception("ws status push failed for document_id=%s", id)

    # Write the single `uploaded` activity-feed row in the SAME async
    # session as the status update — using the async session keeps
    # the FK satisfied and the log row commits atomically with the
    # status flip. The WS push is staged as an `after_commit` hook
    # inside the helper. Best-effort: a log-write failure must not
    # fail the confirm response (the upload itself succeeded and
    # the doc is in UPLOADED state). This is the ONLY row this
    # endpoint writes — the per-doc feed reads as
    # `uploaded → parsing → chunking → embedding`, one row per
    # stage.
    try:
        await _write_log_and_publish(
            db,
            document_id=updated.id,
            owner_id=current_user.id,
            event_type="uploaded",
            message=f"Uploaded {updated.filename}",
            metadata={"filename": updated.filename},
        )
    except Exception:
        logger.exception(
            "failed to write uploaded log document_id=%s",
            updated.id,
        )

    logger.info(
        "upload confirmed document_id=%s owner_id=%s", id, current_user.id
    )

    # Hand the document off to Celery for AI processing. Two important
    # details:
    #   1. We pass the document id as a STRING — Celery serializes args
    #      as JSON, and UUID is not natively JSON-serializable.

    #   2. countdown=1 lets the current DB transaction commit before the
    #      worker tries to read the row (the worker uses a fresh session
    #      and won't see uncommitted data).
    # The dispatch itself is fire-and-forget: we don't block the HTTP
    # response on the worker. If the broker is unreachable, we still
    # return 200 to the client — the document is safely UPLOADED in the
    # DB and a future retry / cleanup job can pick it up.
    try:
        async_result = celery_app.send_task(
            "app.tasks.document.process_document",
            args=[str(updated.id)],
            countdown=1, 
        )
        logger.info(
            "celery task enqueued task_id=%s document_id=%s",
            async_result.id,
            updated.id,
        )
    except Exception:
        # Don't fail the confirm — the upload itself succeeded. Just log
        # so the dev knows the handoff didn't happen.
        logger.exception(
            "failed to enqueue celery task for document_id=%s", updated.id
        )

    return ConfirmUploadResponse(
        document_id=updated.id,
        status=updated.status,
        uploaded_at=updated.uploaded_at,
    )


# ----------------------------------------------------------------------
# POST /api/v1/documents/{id}/upload-failed
# ----------------------------------------------------------------------

@router.post(
    "/{id}/upload-failed",
    status_code=status.HTTP_200_OK,
    tags=["documents"],
    response_model=ConfirmUploadResponse,
)
@limiter.limit("30/minute")
async def report_upload_failed(
    request: Request,
    id: UUID,
    payload: UploadFailedRequest,
    current_user: CurrentUserDep,
    db: DbDep,
) -> ConfirmUploadResponse:
    """Delete a document row after the Supabase PUT didn't complete.

    Called by the client when its direct PUT to the presigned URL fails
    (network error, 403 from a stale URL, CORS block, user-cancelled
    upload, etc.). Without something, the document would stay in
    PROCESSING forever — a "ghost" that never gets confirmed.

    Design choice: we DELETE the row instead of flipping it to a
    `failed` status. Two reasons:
      1. Failed uploads never had a user-visible identity — there is
         no file in storage, no AI processing to show, nothing the user
         will ever want to inspect or retry by id. A row in the table
         just looks like noise on the doc list.
      2. Retries get a fresh upload-intent + new document_id, so the
         old id is meaningless anyway.

    Idempotency: if the document is already in a terminal state
    (UPLOADED / READY / FAILED), we accept the call as a no-op and
    return the current status without touching the row. This makes
    client-side retries safe — a flaky mobile network retrying the
    failure report will not delete a successfully-uploaded document.

    Side effects:
    - DELETE the `documents` row (owner-scoped; no orphan storage
      cleanup — the signed URL just expires after 2h).
    - Push a `document.status` event with status="failed" and the
      error_message so the open doc-list refetches and drops the row
      live. The row is already gone server-side; the WS message is
      purely to nudge the UI before the next refetch.

    What this endpoint does NOT do (yet):
    - Sweep the orphan storage object at `{owner_id}/{doc_id}/{filename}`.
      A future cleanup job can do this for processing docs whose
      signed URL has expired without ever reaching /confirm.
    """
    # 1. Look up the doc, scoped to this owner. None = 404 (don't reveal
    #    whether the doc exists for some other user).
    doc = await document_repo.get_document(db, id, owner_id=current_user.id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    # 2. Idempotency: if the document is no longer in PROCESSING, the
    #    upload already resolved (successfully or otherwise). Return its
    #    current status without touching the row — never overwrite /
    #    delete a doc that has moved past the upload phase.
    if doc.status != DocumentStatus.processing:
        logger.info(
            "upload-failed ignored (already resolved) document_id=%s status=%s",
            id,
            doc.status.value,
        )
        return ConfirmUploadResponse(
            document_id=doc.id,
            status=doc.status,
            uploaded_at=doc.uploaded_at,
        )

    # 3. Compose the error message for the WS payload + server log.
    #    We include the HTTP status from Supabase if the client sent
    #    one so 403 vs 500 vs network errors stay distinguishable.
    log_reason = payload.error_message
    if payload.http_status is not None:
        log_reason = f"{log_reason}"

    # 4. Push the typed status-change event BEFORE the delete so any
    #    open doc-list / doc-detail view that listens for
    #    document.status invalidates and refetches; the refetch then
    #    sees the row is gone and drops it from the list. Best-effort —
    #    the REST GET still works if the WS push fails (it just won't
    #    be live).
    try:
        from app.schemas.ws_events import document_status_event
        from app.websockets.manager import get_connection_manager

        envelope = document_status_event(
            document_id=doc.id,
            title=doc.title,
            status=DocumentStatus.failed.value,
            error_message=log_reason,
        )
        await get_connection_manager().send_to_user(
            current_user.id, envelope.model_dump(mode="json")
        )
    except Exception:
        logger.exception("ws status push failed for document_id=%s", id)

    # 5. Delete the row. `delete_document` is owner-scoped and returns
    #    False if the doc vanished between get + delete (race).
    deleted = await document_repo.delete_document(
        db,
        id,
        owner_id=current_user.id,
    )
    if not deleted:
        # Race with another delete (or a hard delete from somewhere
        # else). Treat as 404 — the end state the client cares about
        # ("row is gone") is already true.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    logger.warning(
        "upload failed, row deleted document_id=%s owner_id=%s reason=%s",
        id,
        current_user.id,
        log_reason,
    )

    # 6. Echo back a representation of the now-gone row. The client
    #    only uses status here — uploaded_at is null because nothing
    #    ever uploaded.
    return ConfirmUploadResponse(
        document_id=doc.id,
        status=DocumentStatus.failed,
        uploaded_at=None,
    )


# ----------------------------------------------------------------------
# PATCH /api/v1/documents/{id}
# ----------------------------------------------------------------------

@router.patch(
    "/{id}",
    status_code=status.HTTP_200_OK,
    tags=["documents"],
    response_model=DocumentListItem,
)
@limiter.limit("60/minute")
async def update_document(
    request: Request,
    id: UUID,
    payload: DocumentUpdate,
    current_user: CurrentUserDep,
    db: DbDep,
) -> DocumentListItem:
    """Update editable user-facing attributes on a document.

    Today the only editable attribute is `title` (the row's display
    label). The original `filename` is NOT editable — it's a property
    of the uploaded file, not a label, and renaming it would create a
    mismatch between what's stored in Supabase Storage and what the
    row claims the file is called.

    Body: `DocumentUpdate` (Pydantic). All fields are optional so the
    route is forward-compatible: future editable fields (description,
    tags, etc.) will be added there as optional fields without
    breaking existing callers.

    Side effects:
      - Owner-scoped UPDATE on the documents row (returns 404 on
        missing / cross-owner access — same convention as every other
        endpoint, no info leak).
      - Best-effort WS push of `document.status` so any open doc list
      / detail view invalidates and refetches the new title.
        Best-effort: the REST GET still works if the WS push fails.

    Returns the updated document as a `DocumentListItem`.
    """
    # 1. Apply the partial update at the data layer — this is also
    #    where the owner-scope check happens (the repo returns None
    #    for "doesn't exist OR not owned", which we treat as 404).
    updated = await document_repo.update_document(
        db,
        id,
        owner_id=current_user.id,
        title=payload.title,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    # 2. Best-effort WS push so any open list / detail view refetches
    #    with the new title. We send the SAME status back (not a
    #    lifecycle transition), but the freshly-emitted event causes
    #    subscribers to invalidate their cache and refetch, which is
    #    exactly what we want here.
    try:
        envelope = document_status_event(
            document_id=updated.id,
            title=updated.title,
            status=updated.status.value,
        )
        await get_connection_manager().send_to_user(
            current_user.id, envelope.model_dump(mode="json")
        )
    except Exception:
        logger.exception("ws status push failed for document_id=%s", id)

    logger.info(
        "document updated document_id=%s owner_id=%s new_title=%r",
        id,
        current_user.id,
        updated.title,
    )

    return DocumentListItem.model_validate(updated)


# ----------------------------------------------------------------------
# DELETE /api/v1/documents/{id}
# ----------------------------------------------------------------------

@router.delete(
    "/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["documents"],
    response_class=Response,
)
@limiter.limit("30/minute")
async def delete_document(
    request: Request,
    id: UUID,
    current_user: CurrentUserDep,
    db: DbDep,
) -> Response:
    """Delete a document and all of its derived data.

    Works in any status (`processing`, `uploaded`, `learning`, `ready`,
    `failed`). The user owns the row; cross-owner access is rejected
    as 404 (consistent with every other document endpoint) so we don't
    leak the existence of other users' documents.

    Cleanup order matters:

      1. Look up the document owner-scoped. Capture `storage_bucket`
         and `storage_path` BEFORE the DB row is gone — they're how
         we find the object in Supabase Storage.
      2. Explicit `document_logs` cleanup via
         `document_log_repo.delete_logs_for_document(...)`. The DB
         schema declares `ON DELETE CASCADE` on
         `document_logs.document_id`, so a Postgres backend (or a
         SQLite backend with `PRAGMA foreign_keys=ON`) handles this
         automatically. We do it explicitly too — defence-in-depth so
         the activity feed never shows orphan rows if the cascade ever
         stops firing. Runs in the same AsyncSession/transaction as
         the document delete.
      3. `document_repo.delete_document(...)` runs a single bulk
         `DELETE WHERE id=? AND owner_id=?`. The DB schema cascades:
            documents → document_pages → document_chunks
                      → document_embeddings
            documents → conversation_documents (join rows)
         so all chunks, embeddings, and pages are removed by the DB
         itself. Past `conversation_messages.citations[].document_id`
         references stay as historical data — they're not FK-constrained
         by design (see `models/conversation_document.py`).
      4. Push a WS event so any open doc list refetches and drops the
         row before its next scheduled refetch. Best-effort.
      5. Best-effort Supabase Storage cleanup. The signed-upload flow
         means the object MAY not exist (e.g. user cancelled during
         upload, so `processing` rows never had a successful PUT) —
         the existing `delete_file` swallows that silently, which is the
         correct behaviour here.

    Idempotency: calling DELETE twice is safe. The first call returns
    204; the second call's owner-scoped lookup returns `None` and we
    return 404. Clients that retry on network errors will not
    accidentally surface a 404 to the user — they should treat 204 as
    terminal regardless of how many times they sent the request.

    Returns 204 No Content on success.
    """
    # 1. Owner-scoped lookup. None = "doesn't exist OR belongs to
    #    someone else" — both are 404 to avoid leaking existence.
    doc = await document_repo.get_document(db, id, owner_id=current_user.id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    # 2. Snapshot the storage location BEFORE deleting the row —
    #    once the row is gone, we'd have no way to find the object.
    storage_bucket = doc.storage_bucket
    storage_path = doc.storage_path
    title = doc.title

    # 3. Push the typed status event BEFORE the delete so any open
    #    doc-list / detail view invalidates and refetches; the
    #    refetch then sees the row is gone and drops it. Best-effort.
    try:
        envelope = document_status_event(
            document_id=doc.id,
            title=title,
            status=DocumentStatus.failed.value,
            error_message="Document deleted by user.",
        )
        await get_connection_manager().send_to_user(
            current_user.id, envelope.model_dump(mode="json")
        )
    except Exception:
        logger.exception("ws status push failed for document_id=%s", id)

    # 4. Wipe the document's activity log rows. The DB schema declares
    #    `ON DELETE CASCADE` on `document_logs.document_id`, so a
    #    Postgres backend (or a SQLite backend with FK enforcement on)
    #    cleans these up automatically. We do it explicitly here too:
    #      - defence-in-depth: if the cascade ever stops firing (DB
    #        migration changes, connection without `PRAGMA
    #        foreign_keys=ON`, etc.) the activity log never gets
    #        orphaned rows that would still show up in the global feed.
    #      - same-transaction guarantee: this runs BEFORE the
    #        `delete_document` call and inside the same AsyncSession,
    #        so the activity-feed refetch that the WS push triggers
    #        sees a clean state.
    deleted_logs = await document_log_repo.delete_logs_for_document(
        db,
        document_id=id,
        owner_id=current_user.id,
    )
    if deleted_logs:
        logger.info(
            "document_logs cleaned up document_id=%s owner_id=%s count=%d",
            id, current_user.id, deleted_logs,
        )

    # 5. Delete the row. The DB cascades wipe pages, chunks, embeddings,
    #    and conversation_documents join rows. `delete_document` is
    #    owner-scoped and returns False if the row vanished between
    #    get + delete (race) — treat that as 404.
    deleted = await document_repo.delete_document(
        db,
        id,
        owner_id=current_user.id,
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    logger.info(
        "document deleted document_id=%s owner_id=%s status=%s",
        id,
        current_user.id,
        doc.status.value,
    )

    # 6. Best-effort Supabase Storage cleanup. The signed-upload flow
    #    means the object may legitimately not exist (e.g. processing
    #    rows whose PUT never landed, or failed uploads) — `delete_file`
    #    already swallows errors silently, which is what we want.
    if storage_bucket and storage_path:
        try:
            get_supabase_storage().delete_file(storage_bucket, storage_path)
        except Exception:
            logger.exception(
                "failed to delete storage object bucket=%s path=%s "
                "for document_id=%s",
                storage_bucket,
                storage_path,
                id,
            )

    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------------
# GET /api/v1/documents/{id}/download
# ----------------------------------------------------------------------

@router.get(
    "/{id}/download",
    status_code=status.HTTP_200_OK,
    tags=["documents"],
    # Raw bytes, not a Pydantic model — Response is the right response_class.
    response_class=Response,
)
@limiter.limit("60/minute")
async def download_document(
    request: Request,
    id: UUID,
    current_user: CurrentUserDep,
    db: DbDep,
) -> Response:
    """Stream the original uploaded file to the owner with
    `Content-Disposition: attachment`, triggering a browser save.

    Symmetric with `view_document` — same auth, same status guard, same
    storage call. The only difference is the `Content-Disposition`
    value, which is what tells the browser to save instead of open.

    See `_stream_document` for the auth, status, and storage mechanics
    shared by this route and `view_document`.
    """
    doc = await _get_downloadable_doc(db, id, current_user.id)
    return await _stream_document(doc, disposition="attachment")


# ----------------------------------------------------------------------
# GET /api/v1/documents/{id}/view
# ----------------------------------------------------------------------

@router.get(
    "/{id}/view",
    status_code=status.HTTP_200_OK,
    tags=["documents"],
    response_class=Response,
)
@limiter.limit("60/minute")
async def view_document(
    request: Request,
    id: UUID,
    current_user: CurrentUserDep,
    db: DbDep,
) -> Response:
    """Stream the original uploaded file to the owner with
    `Content-Disposition: inline`, letting the browser render it.

    For MIME types the browser knows how to render (PDF, PNG/JPG, text,
    Markdown, CSV, JSON, SVG, ...) the file opens directly in a new
    tab. For types it doesn't (e.g. `.docx`), the browser falls back to
    a download prompt — which is the correct cross-platform behaviour
    and means this endpoint is the single "open the file" affordance
    for every document kind.

    See `_stream_document` for the auth, status, and storage mechanics
    shared by this route and `download_document`.
    """
    doc = await _get_downloadable_doc(db, id, current_user.id)
    return await _stream_document(doc, disposition="inline")


# ----------------------------------------------------------------------
# GET /api/v1/documents
# ----------------------------------------------------------------------

# Hard cap so a malicious client can't ask for limit=1_000_000 and OOM us.
_MAX_LIST_LIMIT = 200


@router.get(
    "",
    status_code=status.HTTP_200_OK,
    tags=["documents"],
    response_model=DocumentListResponse,
)
@limiter.limit("120/minute")
async def list_my_documents(
    request: Request,
    current_user: CurrentUserDep,
    db: DbDep,
    status_filter: DocumentStatus | None = Query(
        default=None,
        alias="status",
        description=(
            "Optional. Filter to documents in this status only "
            "(e.g. ?status=ready to list only chat-ready documents)."
        ),
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=_MAX_LIST_LIMIT,
        description="Page size (max 100).",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Number of items to skip. Use with limit for pagination.",
    ),
) -> DocumentListResponse:
    """List the caller's documents, newest first.

    Scoped to the authenticated user — other users' documents are
    never returned. The `total` field respects the `status` filter,
    so the client can paginate a filtered list correctly.
    """
    # Two repo calls: rows for this page, then count for the total.
    # Two round-trips, but simpler than a window-function COUNT(*) OVER()
    # and the perf difference is negligible at this scale.
    docs = await document_repo.list_documents(
        db,
        owner_id=current_user.id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    total = await document_repo.count_user_documents(
        db,
        owner_id=current_user.id,
        status=status_filter,
    )

    return DocumentListResponse(
        items=[DocumentListItem.model_validate(d) for d in docs],
        total=total,
        limit=limit,
        offset=offset,
    )


# ----------------------------------------------------------------------
# GET /api/v1/documents/{id}/stats
# ----------------------------------------------------------------------

@router.get(
    "/{id}/stats",
    status_code=status.HTTP_200_OK,
    tags=["documents"],
    response_model=DocumentStatsResponse,
)
@limiter.limit("60/minute")
async def get_document_stats(
    request: Request,
    id: UUID,
    current_user: CurrentUserDep,
    db: DbDep,
) -> DocumentStatsResponse:
    """Per-document aggregate counts (powers the detail dialog).

    Returns the same `DocumentListItem` shape the list endpoint uses
    PLUS aggregated counts over `document_pages`, `document_chunks`,
    and `document_embeddings` for this single owner-scoped document.

    Owner-scope is enforced inside `document_stats_repo`: a None result
    means "doc doesn't exist OR belongs to another user", which we
    translate to 404 so the endpoint doesn't leak existence (same
    convention as every other document endpoint).

    The list endpoint deliberately stays narrow (no embedded counts) so
    listing N docs is O(N) cheap; this route is the dedicated one-call
    surface for the per-document detail view. See
    `DocumentStatsResponse` for the field contract.
    """
    stats = await document_stats_repo.document_stats_for(
        db,
        id,
        owner_id=current_user.id,
    )
    if stats is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    # Re-fetch the document row so we can return the same
    # `DocumentListItem` shape the list endpoint uses. The repo's
    # owner-scope check already proved the row exists and belongs to
    # this user, so this fetch is a guaranteed hit.
    doc = await document_repo.get_document(db, id, owner_id=current_user.id)
    assert doc is not None  # invariant: stats_for returned non-None above

    return DocumentStatsResponse(
        document=DocumentListItem.model_validate(doc),
        page_count=stats.page_count,
        chunks_total=stats.chunks_total,
        chunks_embedded=stats.chunks_embedded,
        chunks_pending=stats.chunks_pending,
        chunks_failed=stats.chunks_failed,
        pages_needing_ocr=stats.pages_needing_ocr,
        pages_ocr_pending=stats.pages_ocr_pending,
        pages_ocr_failed=stats.pages_ocr_failed,
        embedding_models=stats.embedding_models,
        embedding_status_counts=stats.embedding_status_counts,
    )


# ----------------------------------------------------------------------
# GET /api/v1/documents/{id}/logs
# ----------------------------------------------------------------------

# Hard cap so a malicious client can't ask for limit=1_000_000 and OOM us.
# 200 is a generous page size — the dialog's activity feed typically
# only needs the first ~20 rows; the cap is here to bound damage.
_MAX_LOG_LIMIT = 200


@router.get(
    "/{id}/logs",
    status_code=status.HTTP_200_OK,
    tags=["documents"],
    response_model=DocumentLogListResponse,
)
@limiter.limit("60/minute")
async def get_document_logs(
    request: Request,
    id: UUID,
    current_user: CurrentUserDep,
    db: DbDep,
    limit: int = Query(
        default=20,
        ge=1,
        le=_MAX_LOG_LIMIT,
        description="Page size (max 200).",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Number of items to skip. Use with limit for pagination.",
    ),
) -> DocumentLogListResponse:
    """Per-document activity log (powers the dialog's activity feed).

    Returns one page of `document_logs` rows, newest first. The
    dialog fetches this on open to seed the feed; new rows are
    pushed in real time via the `document.log` WS event and spliced
    into the local cache without a refetch.

    Owner-scope is enforced by `get_document` (via the existing
    404-on-cross-owner pattern): a doc that doesn't exist or that
    belongs to another user is 404, never 403, so this endpoint
    never leaks the existence of other users' documents.

    The list and the count are scoped to the same `(document_id,
    owner_id)` predicate inside the repository, so `total` is
    consistent with the page contents.
    """
    # 1. Owner-scope check. The same 404 contract every other document
    #    endpoint uses, so we don't leak existence.
    doc = await document_repo.get_document(db, id, owner_id=current_user.id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    # 2. Page + count, two cheap indexed queries.
    rows = await document_log_repo.list_logs_for_document(
        db,
        document_id=id,
        owner_id=current_user.id,
        limit=limit,
        offset=offset,
    )
    total = await document_log_repo.count_logs_for_document(
        db,
        document_id=id,
        owner_id=current_user.id,
    )

    return DocumentLogListResponse(
        items=[DocumentLogItem.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


# ----------------------------------------------------------------------
# Private helpers (used by /download and /view)
# ----------------------------------------------------------------------

# Statuses in which the file is guaranteed to exist in storage AND
# is something the user might reasonably want to retrieve. `processing`
# is excluded because the file may not exist yet (the signed-URL PUT
# hasn't landed), and `failed` is excluded because there's nothing
# useful to send back — the row is still around for the activity log
# but the file is either missing or partial.
_DOWNLOADABLE_STATUSES: frozenset[DocumentStatus] = frozenset({
    DocumentStatus.uploaded,
    DocumentStatus.learning,
    DocumentStatus.ready,
})

# Conservative cap: a 255-char filename is the max our DB stores; 200
# leaves room for " 1.txt" suffixes some browsers add on collision.
_MAX_FILENAME_LEN = 200

# Anything that could break the Content-Disposition header or the
# filesystem on a future re-host. We replace with `_` rather than
# stripping so the user still sees their name in the downloaded file.
_FILENAME_SANITISE_RE = re.compile(r'[\r\n";\\/]|[\x00-\x1f]')


def _sanitise_filename(name: str) -> str:
    """Make `name` safe to drop into a `Content-Disposition` header.

    Defends against header-injection (CRLF) and path traversal (`/`,
    `\\`) without stripping the user's name entirely. Empty / all-bad
    input falls back to `document` so the header is always well-formed.
    """
    cleaned = _FILENAME_SANITISE_RE.sub("_", name).strip().strip(".")
    if not cleaned:
        cleaned = "document"
    if len(cleaned) > _MAX_FILENAME_LEN:
        cleaned = cleaned[:_MAX_FILENAME_LEN]
    return cleaned


async def _get_downloadable_doc(
    db: AsyncSession,
    doc_id: UUID,
    owner_id: UUID,
) -> Document:
    """Owner-scoped lookup + 409 status guard shared by /download and /view.

    Raises the same `HTTPException`s the routes would raise on their own
    so the helper's callers stay short and the error contract is one
    place.

    The status check is deliberately lenient: we allow `uploaded`,
    `learning`, AND `ready`. `learning` is fine because the file is
    fully present in storage at that point — only the embeddings are
    still being computed, and the user should be able to download the
    original even while we work in the background.
    """
    doc = await document_repo.get_document(db, doc_id, owner_id=owner_id)
    if doc is None:
        # Don't reveal whether the doc exists for some other user.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )
    if doc.status not in _DOWNLOADABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Document is in status {doc.status.value!r}; only "
                f"uploaded, learning, or ready documents can be downloaded."
            ),
        )
    return doc


async def _stream_document(
    doc: Document,
    *,
    disposition: Literal["attachment", "inline"],
) -> Response:
    """Fetch the doc's bytes from Supabase and return a `Response`.

    Shared by the `/download` (`attachment`) and `/view` (`inline`)
    routes — the only thing the browser sees that's different is the
    `Content-Disposition` value.

    `download_file` is a sync SDK call. We hop it to a worker thread
    via `asyncio.to_thread` so it doesn't block the FastAPI event loop
    while the file is in flight from Supabase; the rest of the route
    is still async-native.
    """
    storage = get_supabase_storage()
    try:
        data = await asyncio.to_thread(
            storage.download_file,
            doc.storage_bucket,
            doc.storage_path,
        )
    except Exception:
        # The file is missing in storage, the SDK auth expired, the
        # network went away — all of these are server-side problems
        # the user can't fix by retrying, so a 502 is honest. The
        # detail message is intentionally generic to avoid leaking
        # Supabase internals.
        logger.exception(
            "storage download failed for document_id=%s bucket=%s path=%s",
            doc.id,
            doc.storage_bucket,
            doc.storage_path,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not retrieve the file from storage. Please try again.",
        ) from None

    safe_name = _sanitise_filename(doc.filename)
    media_type = doc.mime_type or "application/octet-stream"
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe_name}"',
            "Content-Length": str(len(data)),
            # Per-user private content — never let an intermediary
            # cache it, and never let the browser use a stale copy
            # after a same-name re-upload.
            "Cache-Control": "private, no-store",
        },
    )


# ----------------------------------------------------------------------
# Log write + WS publish helper (shared by upload-intent and confirm)
# ----------------------------------------------------------------------


async def _write_log_and_publish(
    db: AsyncSession,
    *,
    document_id: UUID,
    owner_id: UUID,
    event_type: str,
    message: str | None,
    metadata: dict | None,
) -> None:
    """Write one log row in the caller's AsyncSession and stage the WS push.

    The log row goes into the SAME transaction as the document row
    the caller is operating on — using `add_log_async` (which writes
    via the caller's `AsyncSession`) rather than `add_log` (which
    opens a fresh sync session on a different engine). Two reasons:

      1. **FK visibility.** `add_log` opens a brand-new sync session.
         The async session's document insert is uncommitted when this
         runs, so the new sync session cannot see the document row.
         The `document_logs.document_id` FK insert then fails — and
         the caller's outer `try/except` swallows it, so the user
         sees "LOG 0" in the detail dialog even after the doc is
         fully ready.

      2. **SQLite write-lock collision.** The async engine and sync
         engine are two distinct connection pools. While the async
         session holds its connection mid-request, a sync session
         either blocks on `busy_timeout` (fine) or errors with
         `database is locked` (silently swallowed). Using the async
         session for the log write avoids both engines.

    The WS push is deferred until AFTER the request's outer
    `await session.commit()` commits both rows — we `flush()` here
    so the row is visible inside the transaction, but the publisher
    is invoked after the helper returns and the caller commits.
    To make that work without a callback dance, this helper actually
    commits nothing and pushes nothing; instead it appends a callback
    to `db.sync__session` (the underlying sync handle) that fires on
    commit and dispatches the WS event. The event is only sent when
    the surrounding transaction commits successfully — subscribers
    never see a log row that doesn't exist yet.

    Args:
        db: The caller's `AsyncSession` (the same one that wrote the
            document row). Must NOT be committed by the caller BEFORE
            the log row is added — the FK only resolves once both
            rows are in the same transaction.
        document_id, owner_id: Log row FKs.
        event_type: Catalog value (e.g. "uploaded",
            "parsing", "chunking", "embedding"). See
            `DocumentLogEventType` for the full list.
        message: Short human-readable narration.
        metadata: Per-event structured detail (JSON-encoded by
            `add_log_async`).
    """
    try:
        log_id = await add_log_async(
            db,
            document_id=document_id,
            owner_id=owner_id,
            event_type=event_type,
            message=message,
            metadata=metadata,
        )
    except Exception:
        logger.exception(
            "_write_log_and_publish: add_log_async failed "
            "document_id=%s event_type=%s",
            document_id, event_type,
        )
        return

    if log_id is None:
        logger.warning(
            "_write_log_and_publish: log_id is None after insert "
            "document_id=%s event_type=%s",
            document_id, event_type,
        )
        return

    # Stage the WS push as an `after_commit` hook on the underlying
    # sync session. This fires only when the surrounding transaction
    # commits successfully — so subscribers never see a log row that
    # doesn't exist yet, and a rollback (e.g. unhandled exception
    # inside the route) silently cancels the push.
    def _push_after_commit(_session) -> None:
        try:
            row = _session.get(DocumentLog, log_id)
            if row is None:
                logger.warning(
                    "_write_log_and_publish: row vanished before push "
                    "log_id=%s event_type=%s",
                    log_id, event_type,
                )
                return
            publish_document_log(
                owner_id,
                document_id=document_id,
                log_row=row,
            )
        except Exception:
            logger.exception(
                "_write_log_and_publish: ws publish failed "
                "log_id=%s event_type=%s",
                log_id, event_type,
            )

    try:
        # `db.sync_session` is the SQLAlchemy AsyncSession's underlying
        # sync handle — registering an `after_commit` hook there is
        # the canonical way to schedule post-commit work in async
        # SQLAlchemy 2.0.
        from sqlalchemy import event as _sa_event  # local import: tiny
        _sa_event.listen(db.sync_session, "after_commit", _push_after_commit)
    except Exception:
        # If the event hook can't be registered, fall back to a
        # best-effort inline push from the now-flushed row. The row
        # is still uncommitted at this point, but the publisher is
        # best-effort anyway and the dialog does an initial REST
        # fetch on open, so a missed push is recoverable.
        logger.exception(
            "_write_log_and_publish: failed to register after_commit hook "
            "log_id=%s event_type=%s",
            log_id, event_type,
        )
        try:
            row = await db.get(DocumentLog, log_id)
            if row is not None:
                publish_document_log(
                    owner_id,
                    document_id=document_id,
                    log_row=row,
                )
        except Exception:
            logger.exception(
                "_write_log_and_publish: fallback publish failed "
                "log_id=%s event_type=%s",
                log_id, event_type,
            )

